"""Configuration and XDG paths for the Trench CLI.

Configuration lives in ``~/.config/trch/config.json`` (or
``$XDG_CONFIG_HOME/trch/config.json``).  Only non-secret preferences are stored
there; API keys always come from environment variables.
"""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from pathlib import Path

DEFAULTS = {
    "provider": None,
    "model": None,
    "stream": True,
    "timeout": 120,
    "max_turns": 20,
    "approval": "ask",
    "agent": True,
    "system_prompt": None,
    "max_tool_output": 20000,
    "allow_outside": False,
}

VALIDATORS = {
    "provider": lambda value: isinstance(value, str) and bool(value.strip()),
    "model": lambda value: isinstance(value, str) and bool(value.strip()),
    "stream": lambda value: isinstance(value, bool),
    "timeout": lambda value: isinstance(value, int) and 1 <= value <= 3600,
    "max_turns": lambda value: isinstance(value, int) and 1 <= value <= 200,
    "approval": lambda value: value in ("ask", "auto"),
    "agent": lambda value: isinstance(value, bool),
    "system_prompt": lambda value: value is None or isinstance(value, str),
    "max_tool_output": lambda value: isinstance(value, int) and 500 <= value <= 200000,
    "allow_outside": lambda value: isinstance(value, bool),
}

ENV_OVERRIDES = {
    "provider": "TRCH_PROVIDER",
    "model": "TRCH_MODEL",
    "stream": "TRCH_STREAM",
    "timeout": "TRCH_TIMEOUT",
    "max_turns": "TRCH_MAX_TURNS",
    "approval": "TRCH_APPROVAL",
    "agent": "TRCH_AGENT",
    "system_prompt": "TRCH_SYSTEM_PROMPT",
    "max_tool_output": "TRCH_MAX_TOOL_OUTPUT",
    "allow_outside": "TRCH_ALLOW_OUTSIDE",
}


class ConfigError(RuntimeError):
    """Raised for unreadable or invalid configuration."""


def _xdg(env, variable, fallback):
    value = env.get(variable)
    return Path(value).expanduser() if value else Path(fallback).expanduser()


def config_dir(env=None):
    env = os.environ if env is None else env
    return _xdg(env, "XDG_CONFIG_HOME", "~/.config") / "trch"


def default_config_path(env=None):
    env = os.environ if env is None else env
    return config_dir(env) / "config.json"


def state_dir(env=None):
    env = os.environ if env is None else env
    return _xdg(env, "XDG_STATE_HOME", "~/.local/state") / "trch"


def sessions_dir(env=None):
    return state_dir(env) / "sessions"


def history_path(env=None):
    return state_dir(env) / "history"


def load_dotenv(path=None, environ=None):
    """Load KEY=VALUE entries from a local .env without overriding the shell."""
    env = os.environ if environ is None else environ
    path = Path(path).expanduser() if path else Path.cwd() / ".env"
    if not path.exists():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ConfigError(f"Cannot read {path}: {error}") from error

    for line_number, line in enumerate(lines, 1):
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith("export "):
            entry = entry[7:].lstrip()
        if "=" not in entry:
            continue
        name, raw_value = entry.split("=", 1)
        name = name.strip()
        if (not name or not name.isascii() or not name.replace("_", "a").isalnum()
                or name[0].isdigit()):
            raise ConfigError(f"Invalid variable name in {path}:{line_number}.")
        if name in env:
            continue
        try:
            parts = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as error:
            raise ConfigError(f"Invalid value in {path}:{line_number}: {error}") from error
        env[name] = " ".join(parts)
    return True


def coerce(key, raw):
    """Convert a command-line string into the type expected for ``key``."""
    if key not in DEFAULTS:
        raise ConfigError(f"Unknown setting: {key}. Known settings: {', '.join(DEFAULTS)}")
    if key in ("stream", "agent", "allow_outside"):
        lowered = str(raw).strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ConfigError(f"{key} expects true or false.")
    if key in ("timeout", "max_turns", "max_tool_output"):
        try:
            return int(raw)
        except (TypeError, ValueError) as error:
            raise ConfigError(f"{key} expects an integer.") from error
    if raw is None:
        return None
    return str(raw)


def validate(key, value):
    if key not in VALIDATORS:
        raise ConfigError(f"Unknown setting: {key}")
    if not VALIDATORS[key](value):
        raise ConfigError(f"Invalid value for {key}: {value!r}")
    return value


def load(path=None, env=None):
    """Load the raw config file.  Missing files return an empty dict."""
    env = os.environ if env is None else env
    path = Path(path).expanduser() if path else default_config_path(env)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as error:
        raise ConfigError(f"Cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ConfigError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object.")
    unknown = sorted(set(data) - set(DEFAULTS))
    if unknown:
        raise ConfigError(f"Unknown setting(s) in {path}: {', '.join(unknown)}")
    for key, value in data.items():
        validate(key, value)
    return data


def merged(path=None, env=None):
    """Return defaults + config file + environment overrides."""
    env = os.environ if env is None else env
    result = dict(DEFAULTS)
    result.update(load(path, env))
    for key, variable in ENV_OVERRIDES.items():
        if variable in env and env[variable] != "":
            result[key] = coerce(key, env[variable])
            validate(key, result[key])
    return result


def save(config, path=None, env=None):
    """Atomically write ``config`` and return the path."""
    env = os.environ if env is None else env
    path = Path(path).expanduser() if path else default_config_path(env)
    for key, value in config.items():
        validate(key, value)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=".config-", suffix=".tmp", delete=False
    )
    try:
        with handle:
            json.dump(config, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(handle.name, path)
    except OSError as error:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise ConfigError(f"Cannot write {path}: {error}") from error
    return path


def set_value(key, raw, path=None, env=None):
    env = os.environ if env is None else env
    path = Path(path).expanduser() if path else default_config_path(env)
    value = coerce(key, raw)
    validate(key, value)
    config = load(path, env)
    config[key] = value
    save(config, path, env)
    return value, path


def unset_value(key, path=None, env=None):
    env = os.environ if env is None else env
    path = Path(path).expanduser() if path else default_config_path(env)
    if key not in DEFAULTS:
        raise ConfigError(f"Unknown setting: {key}")
    config = load(path, env)
    existed = key in config
    config.pop(key, None)
    save(config, path, env)
    return existed, path


def describe(value):
    if value is None:
        return "(not set)"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
