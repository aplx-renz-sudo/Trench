#!/usr/bin/env python3
"""Trench CLI: a terminal AI agent with provider switching and real tools."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.styles import Style
    HAS_PROMPT_TOOLKIT = True
except ImportError:
    HAS_PROMPT_TOOLKIT = False

# Allow running this file directly from any working directory.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import trch_config
import trch_session
from trch_agent import Agent, ToolContext, build_system_prompt, tools_prompt
from trch_config import ConfigError
from trch_providers import (
    MODEL_CHOICES,
    PROVIDERS,
    ProviderError,
    chat,
    chat_stream,
    configured_providers,
    list_ollama_models,
    normalize_provider,
    ollama_reachable,
    provider_env_var,
)
from trch_session import SessionError

VERSION = "1.0.0"
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INTERRUPT = 130

ASK_SYSTEM_PROMPT = (
    "You are Trench, a precise and practical terminal assistant. "
    "Answer concisely, use Markdown and fenced code blocks where useful, and "
    "never invent command output or citations."
)

HELP_TEXT = """\
Commands:
  trch                       Start an interactive agent session
  trch ask [prompt]          One-shot answer (no tools)
  trch run <task>            One-shot agent with file and shell tools
  trch chat                  Interactive chat without tools
  trch resume [session-id]   Resume an interactive session
  trch models [provider]     List providers and models
  trch config ...            Show or edit configuration
  trch sessions ...          List, show, or delete saved sessions
  trch doctor                Diagnose providers and environment

Examples:
  trch ask "explain this traceback" < error.log
  trch run "add retry with backoff to the upload client" --yes
  echo "summarize README.md" | trch ask
  trch -m gpt-4.1-mini ask "write a regex for ISO dates"
  trch --provider ollama --model qwen2.5-coder:3b run "fix the failing test"

Global options:
  -C, --cwd DIR              Work in DIR (default: current directory)
  -m, --model NAME           Model ID for this run
      --provider NAME        Provider: openai, anthropic, gemini, deepseek,
                             mistral, kimi, ollama, openai_compatible
      --stream / --no-stream Stream model output (default: config)
      --json                 Machine-readable output
      --no-color             Disable ANSI color
  -y, --yes                  Approve file writes and shell commands
      --config PATH          Use an alternate config file
  -v, --verbose              Show tool output and diagnostics
  -q, --quiet                Suppress status messages
  -h, --help                 Show this help
      --version              Show version
"""

SLASH_COMMANDS = [
    ("/init", "Create a TRENCH.md file with workspace instructions"),
    ("/add-dir", "Add or change the current working directory"),
    ("/agents", "Manage agent configurations (tools, turns, approvals)"),
    ("/bashes", "List recent executed shell commands and tasks"),
    ("/clear (reset, new)", "Clear conversation history and free up context"),
    ("/compact", "Clear conversation history but keep a summary in context"),
    ("/config", "Show or edit configuration"),
    ("/cost", "Show token usage estimates and session metrics"),
    ("/doctor", "Diagnose providers, keys, and environment"),
    ("/help", "Show all available commands"),
    ("/model", "Show or change the model"),
    ("/provider", "Show or change the provider"),
    ("/sessions", "List saved sessions"),
    ("/tools", "Toggle agent workspace tools (on/off)"),
    ("/exit (quit)", "Exit Trench"),
]


class SlashCompleter(Completer if HAS_PROMPT_TOOLKIT else object):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith("/"):
            query = text.lower()
            for cmd, desc in SLASH_COMMANDS:
                main_cmd = cmd.split()[0]
                if main_cmd.lower().startswith(query) or query == "/":
                    yield Completion(
                        main_cmd,
                        start_position=-len(text),
                        display=main_cmd,
                        display_meta=desc,
                    )


def print_slash_commands(ui):
    col_cmd = 32
    print()
    for cmd, desc in SLASH_COMMANDS:
        cmd_styled = ui.style(cmd, "36")
        pad = " " * max(2, col_cmd - len(cmd))
        print(f"  {cmd_styled}{pad}{desc}")
    print()


class CLIError(RuntimeError):
    """A user-facing usage error."""


class SetupError(RuntimeError):
    """The environment or provider configuration is incomplete."""


class UI:
    def __init__(self, no_color=False, quiet=False, verbose=0):
        self.quiet = quiet
        self.verbose = verbose
        self.color = (
            not no_color
            and os.environ.get("NO_COLOR") is None
            and os.environ.get("TERM") != "dumb"
        )

    def _tty(self, stream):
        return self.color and getattr(stream, "isatty", lambda: False)()

    def style(self, text, code, stream=None):
        stream = stream or sys.stdout
        if not self._tty(stream):
            return text
        return f"\033[{code}m{text}\033[0m"

    def dim(self, text):
        if not self.quiet:
            print(self.style(str(text), "2", sys.stderr), file=sys.stderr)

    def info(self, text):
        if not self.quiet:
            print(str(text), file=sys.stderr)

    def success(self, text):
        if not self.quiet:
            print(self.style(str(text), "32", sys.stderr), file=sys.stderr)

    def warn(self, text):
        print(self.style(f"warning: {text}", "33", sys.stderr), file=sys.stderr)

    def error(self, text):
        print(self.style(f"error: {text}", "31", sys.stderr), file=sys.stderr)


def _add_common(parser, suppress=False):
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument("--provider", default=default, help="Model provider")
    parser.add_argument("-m", "--model", default=default, help="Model ID")
    stream = parser.add_mutually_exclusive_group()
    stream.add_argument("--stream", dest="stream", action="store_true", default=argparse.SUPPRESS if suppress else None)
    stream.add_argument("--no-stream", dest="stream", action="store_false", default=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS if suppress else False)
    parser.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS if suppress else False)
    parser.add_argument("-y", "--yes", action="store_true", default=argparse.SUPPRESS if suppress else False)
    parser.add_argument("--config", default=default, help="Alternate config file")
    parser.add_argument("-C", "--cwd", default=default, help="Working directory")
    parser.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS if suppress else 0)
    parser.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS if suppress else False)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="trch",
        description="Trench: a terminal AI coding agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HELP_TEXT,
    )
    parser.add_argument("--version", action="version", version=f"trch {VERSION}")
    _add_common(parser)
    parser.add_argument("-c", "--continue", dest="continue_session", action="store_true",
                        help="Resume the latest session in this directory")

    sub = parser.add_subparsers(dest="command", metavar="command")

    ask = sub.add_parser("ask", help="One-shot answer without tools",
                         description="Ask a single question and print the answer.")
    _add_common(ask, suppress=True)
    ask.add_argument("prompt", nargs="*", help="Prompt (reads stdin when omitted)")
    ask.add_argument("-s", "--system", help="System prompt for this request")
    ask.add_argument("-f", "--file", help="Read the prompt from a file")
    ask.add_argument("-c", "--continue", dest="continue_session", action="store_true",
                     default=argparse.SUPPRESS, help="Continue the latest session in this directory")

    run = sub.add_parser("run", aliases=["agent"], help="Run a tool-using agent task",
                         description="Run one agent task with workspace tools.")
    _add_common(run, suppress=True)
    run.add_argument("prompt", nargs="*", help="Task (reads stdin when omitted)")
    run.add_argument("-s", "--system", help="Extra system instructions")
    run.add_argument("-f", "--file", help="Read the task from a file")
    run.add_argument("--max-turns", type=int, default=None, help="Maximum model turns")
    run.add_argument("--allow-outside", action="store_true", help="Allow tools outside the workspace")
    run.add_argument("--no-save", action="store_true", help="Do not save a session")

    chat_parser = sub.add_parser("chat", help="Interactive chat without tools")
    _add_common(chat_parser, suppress=True)
    chat_parser.add_argument("-s", "--system", help="System prompt")
    chat_parser.add_argument("-c", "--continue", dest="continue_session", action="store_true",
                             default=argparse.SUPPRESS, help="Continue the latest session in this directory")

    resume = sub.add_parser("resume", help="Resume an interactive session")
    _add_common(resume, suppress=True)
    resume.add_argument("session_id", nargs="?", help="Session ID (default: latest)")

    models = sub.add_parser("models", help="List providers and models")
    _add_common(models, suppress=True)
    models.add_argument("provider", nargs="?", help="Only show this provider")

    doctor = sub.add_parser("doctor", help="Check providers and environment")
    _add_common(doctor, suppress=True)
    doctor.add_argument("--network", action="store_true", help="Also test network access")

    config = sub.add_parser("config", help="Inspect or edit configuration")
    _add_common(config, suppress=True)
    config.add_argument("action", nargs="?", default="list",
                        choices=["list", "get", "set", "unset", "path", "edit"])
    config.add_argument("key", nargs="?")
    config.add_argument("value", nargs="*")

    sessions = sub.add_parser("sessions", help="Manage saved sessions")
    _add_common(sessions, suppress=True)
    sessions.add_argument("action", nargs="?", default="list",
                          choices=["list", "show", "delete", "path"])
    sessions.add_argument("session_id", nargs="?")

    return parser


def _get(args, name, default=None):
    return getattr(args, name, default)


def apply_cwd(args):
    target = _get(args, "cwd")
    if target:
        path = Path(target).expanduser()
        if not path.is_dir():
            raise CLIError(f"Not a directory: {target}")
        os.chdir(path)
    return Path.cwd()


def load_settings(args):
    try:
        return trch_config.merged(_get(args, "config"))
    except ConfigError as error:
        raise CLIError(str(error)) from error


def _detect_provider(environ=None):
    env = os.environ if environ is None else environ
    for name in ("openai", "anthropic", "gemini", "deepseek", "mistral", "kimi", "openai_compatible"):
        variable = provider_env_var(name)
        if variable and env.get(variable):
            if name != "openai_compatible" or env.get("TRCH_OPENAI_COMPAT_BASE_URL"):
                return name
    if ollama_reachable(environ=env):
        return "ollama"
    return None


def resolve_target(args, settings, ui):
    explicit = _get(args, "provider") or settings.get("provider")
    configured_provider = normalize_provider(settings.get("provider")) if settings.get("provider") else None

    if explicit:
        provider = normalize_provider(explicit)
        if provider not in PROVIDERS:
            raise SetupError(
                f"Unknown provider '{explicit}'. Choose from: {', '.join(PROVIDERS)}."
            )
    else:
        provider = _detect_provider()
        if not provider:
            raise SetupError(
                "No provider configured. Set a provider API key (for example OPENAI_API_KEY), "
                "start Ollama, or run 'trch config set provider ollama'."
            )

    explicit_model = _get(args, "model")
    if explicit_model:
        model = explicit_model
    elif settings.get("model") and (configured_provider in (None, provider)):
        model = settings["model"]
    else:
        model = PROVIDERS[provider].get("model", "")

    if provider == "ollama" and not ollama_reachable():
        raise SetupError(
            "Cannot reach Ollama. Start it with 'ollama serve' or set OLLAMA_HOST."
        )
    if not model:
        raise SetupError(
            f"Provider '{provider}' needs a model ID. Pass --model NAME or run "
            f"'trch config set model NAME'."
        )
    variable = provider_env_var(provider)
    if provider != "ollama" and variable and not os.environ.get(variable):
        raise SetupError(f"{provider} needs {variable} set in the environment.")
    if provider == "openai_compatible" and not os.environ.get("TRCH_OPENAI_COMPAT_BASE_URL"):
        raise SetupError("openai_compatible needs TRCH_OPENAI_COMPAT_BASE_URL set in the environment.")
    return provider, model


def read_prompt(args, what="prompt"):
    text = " ".join(getattr(args, "prompt", []) or []).strip()
    source = getattr(args, "file", None)
    if not text and source:
        try:
            text = Path(source).expanduser().read_text(encoding="utf-8").strip()
        except OSError as error:
            raise CLIError(f"Cannot read {source}: {error}") from error
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if not text:
        raise CLIError(f"No {what} provided. Pass it as an argument or pipe it on stdin.")
    return text


def emit_json(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def make_chat(provider, model, timeout):
    def call(messages):
        return chat(provider, model, messages, timeout=timeout)
    return call


def stream_to_stdout(provider, model, messages, timeout):
    chunks = []
    for chunk in chat_stream(provider, model, messages, timeout=timeout):
        sys.stdout.write(chunk)
        sys.stdout.flush()
        chunks.append(chunk)
    if chunks:
        sys.stdout.write("\n")
        sys.stdout.flush()
    return "".join(chunks)


def cmd_ask(args, settings, ui):
    prompt = read_prompt(args, "prompt")
    session = _load_session(args)
    if session:
        if not _get(args, "provider") and session.get("provider"):
            setattr(args, "provider", session["provider"])
        if not _get(args, "model") and session.get("model"):
            setattr(args, "model", session["model"])
    provider, model = resolve_target(args, settings, ui)
    timeout = int(settings["timeout"])
    system = _get(args, "system") or settings.get("system_prompt") or ASK_SYSTEM_PROMPT
    history = list(session.get("messages", [])) if session else []
    messages = [{"role": "system", "content": system}] + history + [{"role": "user", "content": prompt}]
    stream = settings["stream"] if _get(args, "stream") is None else bool(_get(args, "stream"))
    if _get(args, "json"):
        stream = False

    if stream:
        response = stream_to_stdout(provider, model, messages, timeout)
    else:
        response = chat(provider, model, messages, timeout=timeout)
        if not _get(args, "json"):
            print(response)

    if not response.strip():
        ui.warn("The provider returned an empty response.")
        if _get(args, "json"):
            emit_json({"provider": provider, "model": model, "response": response, "error": "empty response"})
        return EXIT_ERROR

    if session:
        session["messages"] = history + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": response},
        ]
        session["provider"] = provider
        session["model"] = model
        try:
            trch_session.save(session)
        except SessionError as error:
            ui.warn(str(error))

    if _get(args, "json"):
        emit_json(
            {
                "provider": provider,
                "model": model,
                "response": response,
                "session": session["id"] if session else None,
            }
        )
    return EXIT_OK


def _tool_summary(name, arguments):
    for key in ("path", "command", "pattern", "summary"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().splitlines()[0][:120]
    return ""


def make_approver(runtime):
    def approve(action, detail):
        if runtime.auto_approve:
            return True
        if not sys.stdin.isatty():
            return False
        # Keep approvals off stdout so --json and shell redirection stay clean.
        print(file=sys.stderr)
        print(runtime.ui.style(f"Allow {action}?", "36", sys.stderr), file=sys.stderr)
        print(detail, file=sys.stderr)
        print("  [y]es / [a]lways / [N]o: ", end="", file=sys.stderr, flush=True)
        try:
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return False
        if answer in ("a", "always"):
            runtime.auto_approve = True
            return True
        return answer in ("y", "yes")
    return approve


def make_event_handler(ui, runtime=None):
    def on_event(event):
        if event["type"] == "tool_start":
            summary = _tool_summary(event["name"], event["arguments"])
            suffix = f" {ui.style(summary, '36')}" if summary else ""
            ui.dim(f"● {event['name']}{suffix}...")
        elif event["type"] == "tool_end":
            status = ui.style("ok", "32") if event["ok"] else ui.style("failed", "31")
            ui.dim(f"↳ {event['name']} ({status})")
            if runtime is not None and event.get("name") == "run_command":
                runtime.command_history.append({
                    "command": event.get("arguments", {}).get("command", ""),
                    "code": 0 if event.get("ok") else 1,
                    "time": time.strftime("%H:%M:%S"),
                })
            if ui.verbose:
                for line in str(event["output"]).splitlines()[:40]:
                    ui.dim(f"   {line}")
    return on_event


@dataclass
class Runtime:
    ui: UI
    provider: str
    model: str
    root: Path
    settings: dict
    system_prompt: str
    agent_enabled: bool = True
    auto_approve: bool = False
    custom_system: bool = False
    config_path: Optional[str] = None
    session: Optional[dict] = None
    command_history: list = field(default_factory=list)

    @property
    def timeout(self):
        return int(self.settings["timeout"])

    @property
    def max_turns(self):
        return int(self.settings["max_turns"])

    def chat(self, messages):
        return chat(self.provider, self.model, messages, timeout=self.timeout)

    def context(self):
        return ToolContext(
            root=self.root,
            approver=make_approver(self),
            auto_approve=self.auto_approve,
            allow_outside=bool(self.settings.get("allow_outside")),
            max_output=int(self.settings["max_tool_output"]),
            timeout=self.timeout,
        )


def cmd_run(args, settings, ui):
    task = read_prompt(args, "task")
    provider, model = resolve_target(args, settings, ui)
    root = Path.cwd()
    settings = dict(settings)
    settings["allow_outside"] = bool(_get(args, "allow_outside") or settings.get("allow_outside"))
    max_turns = _get(args, "max_turns") or int(settings["max_turns"])
    auto_approve = bool(_get(args, "yes") or settings.get("approval") == "auto")
    extra_system = _get(args, "system") or settings.get("system_prompt")
    system_prompt = build_system_prompt(root, extra_system, tools=True)

    if not auto_approve and not sys.stdin.isatty():
        ui.warn("non-interactive run: file writes and commands are denied without --yes.")

    runtime = Runtime(
        ui=ui, provider=provider, model=model, root=root, settings=settings,
        system_prompt=system_prompt, agent_enabled=True, auto_approve=auto_approve,
        config_path=_get(args, "config"),
    )
    on_event = make_event_handler(ui) if not _get(args, "json") else (lambda event: None)
    agent = Agent(runtime.chat, runtime.context(), max_turns=max_turns, on_event=on_event)
    result = agent.run(task, system_prompt=system_prompt)

    session = trch_session.new_session(root, provider, model, system_prompt)
    session["messages"] = [m for m in result.messages if m.get("role") != "system"]
    if not _get(args, "no_save"):
        try:
            trch_session.save(session, env=os.environ)
        except SessionError as error:
            ui.warn(str(error))

    if result.stopped == "max_turns":
        ui.warn(f"agent stopped after {result.turns} turns without finishing.")
    elif not result.final.strip():
        ui.warn("The agent returned an empty final response.")
    if _get(args, "json"):
        emit_json(
            {
                "provider": provider,
                "model": model,
                "response": result.final,
                "stopped": result.stopped,
                "turns": result.turns,
                "session": None if _get(args, "no_save") else session["id"],
                "tool_calls": [{"name": call.name, "arguments": call.arguments} for call in result.tool_calls],
            }
        )
    else:
        if result.final:
            print(result.final)
        elif result.stopped == "max_turns":
            ui.error("The agent reached the turn limit without a final answer.")
    success = result.stopped in ("final", "finished") and bool(result.final.strip())
    return EXIT_OK if success else EXIT_ERROR


def cmd_models(args, settings, ui):
    requested = _get(args, "provider")
    names = [normalize_provider(requested)] if requested else list(PROVIDERS)
    for name in names:
        if name not in PROVIDERS:
            raise CLIError(f"Unknown provider: {name}")
    installed = list_ollama_models() if "ollama" in names else []
    configured = set(configured_providers())

    if _get(args, "json"):
        emit_json(
            {
                "providers": [
                    {
                        "name": name,
                        "configured": name in configured,
                        "default_model": PROVIDERS[name].get("model", ""),
                        "env": provider_env_var(name),
                        "models": installed if name == "ollama" and installed else MODEL_CHOICES.get(name, []),
                    }
                    for name in names
                ]
            }
        )
        return EXIT_OK

    for name in names:
        marker = "configured" if name in configured else "no key"
        if name == "ollama":
            marker = "running" if ollama_reachable() else "not running"
        print(f"{name}  ({marker})")
        print(f"  default: {PROVIDERS[name].get('model') or '(set --model)'}")
        variable = provider_env_var(name)
        if variable:
            print(f"  env:     {variable}")
        models = installed if name == "ollama" and installed else MODEL_CHOICES.get(name, [])
        if models:
            print("  models:  " + ", ".join(models[:12]))
    return EXIT_OK


def cmd_config(args, settings, ui):
    path = Path(_get(args, "config")).expanduser() if _get(args, "config") else trch_config.default_config_path()
    action = args.action
    if action == "path":
        print(path)
        return EXIT_OK
    if action == "list":
        print(f"# {path}" + ("" if path.exists() else " (file does not exist; showing defaults)"))
        for key, value in settings.items():
            print(f"{key} = {trch_config.describe(value)}")
        return EXIT_OK
    if action == "get":
        if not args.key:
            raise CLIError("Usage: trch config get KEY")
        if args.key not in trch_config.DEFAULTS:
            raise CLIError(f"Unknown setting: {args.key}")
        print(trch_config.describe(settings.get(args.key)))
        return EXIT_OK
    if action == "set":
        if not args.key:
            raise CLIError("Usage: trch config set KEY VALUE")
        value_text = " ".join(args.value)
        if args.key == "provider":
            name = normalize_provider(value_text)
            if name not in PROVIDERS:
                raise CLIError(f"Unknown provider: {value_text}")
            value_text = name
        value, saved = trch_config.set_value(args.key, value_text, _get(args, "config"))
        ui.success(f"{args.key} = {trch_config.describe(value)} ({saved})")
        return EXIT_OK
    if action == "unset":
        if not args.key:
            raise CLIError("Usage: trch config unset KEY")
        existed, saved = trch_config.unset_value(args.key, _get(args, "config"))
        if existed:
            ui.success(f"Removed {args.key} from {saved}")
        else:
            ui.info(f"{args.key} was not set in {saved}")
        return EXIT_OK
    if action == "edit":
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if not editor:
            raise CLIError("Set $VISUAL or $EDITOR to edit the config file.")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            trch_config.save({}, path)
        return subprocess.call(shlex.split(editor) + [str(path)])
    raise CLIError(f"Unknown config action: {action}")


def _provider_url(provider):
    return {
        "openai": "https://api.openai.com/v1",
        "anthropic": "https://api.anthropic.com",
        "gemini": "https://generativelanguage.googleapis.com",
        "deepseek": "https://api.deepseek.com",
        "mistral": "https://api.mistral.ai/v1",
        "kimi": "https://api.moonshot.ai/v1",
        "openai_compatible": os.environ.get("TRCH_OPENAI_COMPAT_BASE_URL", ""),
    }.get(provider, "")


def _check(name, ok, detail, hint=""):
    return {"name": name, "ok": ok, "detail": detail, "hint": hint}


def collect_doctor(settings, ui, network=False, config_path=None):
    checks = []
    checks.append(_check("python", sys.version_info >= (3, 9), sys.version.split()[0],
                         "Python 3.9 or newer is required."))
    config_path = Path(config_path).expanduser() if config_path else trch_config.default_config_path()
    try:
        trch_config.load(config_path)
        checks.append(_check("config", True, str(config_path), ""))
    except ConfigError as error:
        checks.append(_check("config", False, str(error), f"Fix or delete {config_path}."))

    root = Path.cwd()
    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix=".trch-doctor-", delete=True):
            pass
        checks.append(_check("workspace", os.access(root, os.W_OK), f"{root} (writable)", ""))
    except OSError as error:
        checks.append(_check("workspace", False, f"{root}: {error}", "Run trch from a writable directory."))

    configured = normalize_provider(settings.get("provider")) if settings.get("provider") else None
    selected = configured or _detect_provider()
    if selected and selected not in PROVIDERS:
        checks.append(_check("provider", False, f"unknown provider '{selected}'",
                             "Run 'trch config set provider NAME' with a supported provider."))
    elif selected == "ollama":
        reachable = ollama_reachable()
        checks.append(_check("provider", reachable, "ollama (selected)" if reachable else "ollama is not reachable",
                             "Start it with: ollama serve"))
    elif selected:
        variable = provider_env_var(selected)
        has_key = bool(os.environ.get(variable)) if variable else False
        if selected == "openai_compatible":
            has_key = has_key and bool(os.environ.get("TRCH_OPENAI_COMPAT_BASE_URL"))
        detail = f"{selected} (selected)" if configured else f"{selected} (auto-detected)"
        checks.append(_check("provider", has_key, detail,
                             f"Set {variable} in the environment." if variable and not has_key else ""))
    else:
        checks.append(
            _check("provider", False, "none configured",
                   "Set an API key, start Ollama, or run 'trch config set provider NAME'.")
        )

    for name in ("openai", "anthropic", "gemini", "deepseek", "mistral", "kimi"):
        variable = provider_env_var(name)
        if os.environ.get(variable):
            checks.append(_check(f"key:{name}", True, f"{variable} is set", ""))
        else:
            checks.append(_check(f"key:{name}", None, f"{variable} is not set", ""))

    compat_base = os.environ.get("TRCH_OPENAI_COMPAT_BASE_URL")
    compat_key = os.environ.get("TRCH_OPENAI_COMPAT_API_KEY")
    if compat_base and compat_key:
        checks.append(_check("key:openai_compatible", True, f"{compat_base} is configured", ""))
    elif compat_base or compat_key:
        checks.append(_check("key:openai_compatible", None, "base URL or key is missing",
                             "Set both TRCH_OPENAI_COMPAT_BASE_URL and TRCH_OPENAI_COMPAT_API_KEY."))

    if ollama_reachable():
        models = list_ollama_models()
        checks.append(_check("ollama", True, "server reachable, " + (", ".join(models[:5]) or "no models installed"), ""))
    else:
        checks.append(_check("ollama", None, "server not reachable", "Start it with: ollama serve"))

    git = shutil.which("git")
    checks.append(_check("git", bool(git), git or "not found", "Install git for diff-aware workflows."))

    if network and selected and selected != "ollama":
        url = _provider_url(selected)
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                checks.append(_check("network", True, f"{url} -> HTTP {response.status}", ""))
        except Exception as error:
            checks.append(_check("network", False, f"{url}: {error}", "Check connectivity or proxy settings."))
    return checks


def cmd_doctor(args, settings, ui):
    checks = collect_doctor(settings, ui, network=bool(_get(args, "network")), config_path=_get(args, "config"))
    if _get(args, "json"):
        emit_json({"checks": checks, "ok": all(item["ok"] is not False for item in checks)})
        return EXIT_OK if all(item["ok"] is not False for item in checks) else EXIT_ERROR
    labels = {True: "ok  ", False: "fail", None: "info"}
    for item in checks:
        if item["ok"] is None and ui.quiet:
            continue
        print(f"[{labels[item['ok']]}] {item['name']}: {item['detail']}")
        if item["ok"] is False and item["hint"]:
            print(f"       {item['hint']}")
    failed = [item for item in checks if item["ok"] is False]
    if failed:
        return EXIT_ERROR
    print("Everything essential looks good.")
    return EXIT_OK


def cmd_sessions(args, settings, ui):
    directory = trch_session.sessions_dir()
    if args.action == "path":
        if _get(args, "json"):
            emit_json({"path": str(directory)})
        else:
            print(directory)
        return EXIT_OK
    if args.action == "list":
        sessions = trch_session.all_sessions()
        if _get(args, "json"):
            emit_json(
                {
                    "sessions": [
                        {
                            "id": item["id"],
                            "updated": item.get("updated", ""),
                            "title": item.get("title", ""),
                            "cwd": item.get("cwd", ""),
                            "provider": item.get("provider", ""),
                            "model": item.get("model", ""),
                            "messages": len(item.get("messages", [])),
                        }
                        for item in sessions
                    ]
                }
            )
            return EXIT_OK
        if not sessions:
            ui.info("No saved sessions.")
            return EXIT_OK
        for item in sessions:
            print(f"{item['id']}  {item.get('updated', '')}  {item.get('title', '')}")
        return EXIT_OK
    if not args.session_id:
        raise CLIError(f"Usage: trch sessions {args.action} SESSION_ID")
    if args.action == "show":
        session = trch_session.load(args.session_id)
        if _get(args, "json"):
            emit_json(session)
            return EXIT_OK
        print(f"# {session['id']}  ({session.get('cwd', '')})  {session.get('provider', '')}/{session.get('model', '')}")
        for message in session.get("messages", []):
            role = message.get("role", "?")
            print(f"\n[{role}]\n{message.get('content', '')}")
        return EXIT_OK
    if args.action == "delete":
        path = trch_session.delete(args.session_id)
        if _get(args, "json"):
            emit_json({"deleted": args.session_id, "path": str(path)})
        else:
            ui.success(f"Deleted {path}")
        return EXIT_OK
    raise CLIError(f"Unknown sessions action: {args.action}")


def _load_session(args):
    store = trch_session
    if _get(args, "command") == "resume" or _get(args, "session_id"):
        session_id = _get(args, "session_id")
        if session_id:
            return store.load(session_id)
        latest = store.latest(cwd=Path.cwd())
        if not latest:
            raise CLIError("No saved session found for this directory.")
        return latest
    if _get(args, "continue_session"):
        latest = store.latest(cwd=Path.cwd())
        if not latest:
            raise CLIError("No saved session found for this directory.")
        return latest
    return None


def _print_banner(runtime):
    ui = runtime.ui
    tools = "tools on" if runtime.agent_enabled else "tools off"
    recent_info = "No recent activity"
    try:
        sessions = trch_session.all_sessions()
        if sessions:
            latest = sessions[0]
            title = latest.get("title") or latest.get("id", "")
            updated = latest.get("updated", "")[:16]
            recent_info = f"{title} ({updated})"
    except Exception:
        pass

    print()
    print(ui.style(f"Trench CLI v{VERSION}", "1;36"))
    print(f"  Model:     {ui.style(f'{runtime.provider}/{runtime.model}', '1')}  ·  {tools}")
    print(f"  Workspace: {runtime.root}")
    print(f"  Recent:    {ui.style(recent_info, '2')}")
    print(f"  Tips:      Run {ui.style('/init', '36')} to create TRENCH.md, {ui.style('/help', '36')} for commands, {ui.style('/exit', '36')} to quit.")
    print()


def _save_interactive_session(runtime):
    if not runtime.session:
        return
    runtime.session["provider"] = runtime.provider
    runtime.session["model"] = runtime.model
    runtime.session["cwd"] = str(runtime.root)
    runtime.session["system_prompt"] = runtime.system_prompt
    try:
        trch_session.save(runtime.session)
    except SessionError as error:
        runtime.ui.warn(str(error))


def _setup_readline():
    try:
        import readline  # noqa: F401
    except ImportError:
        return None
    history = trch_config.history_path()
    try:
        history.parent.mkdir(parents=True, exist_ok=True)
        if history.exists():
            readline.read_history_file(history)
    except (OSError, ValueError):
        pass

    def rl_completer(text, state):
        matches = [cmd.split()[0] for cmd, _ in SLASH_COMMANDS if cmd.startswith(text)]
        return matches[state] if state < len(matches) else None

    try:
        readline.set_completer(rl_completer)
        readline.parse_and_bind("tab: complete")
    except Exception:
        pass
    return readline


def _finish_readline(readline_module):
    if readline_module is None:
        return
    try:
        path = trch_config.history_path()
        readline_module.write_history_file(path)
        os.chmod(path, 0o600)
    except (OSError, ValueError):
        pass


def _slash_command(text, runtime):
    """Handle a slash command. Returns 'exit' when the REPL should stop."""
    ui = runtime.ui
    parts = text.split(maxsplit=1)
    command = parts[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""

    if command in ("/exit", "/quit", "/q"):
        return "exit"
    if command in ("/help", "/?", "/h", "/"):
        print_slash_commands(ui)
        return None
    if command in ("/clear", "/reset", "/new"):
        runtime.session = trch_session.new_session(runtime.root, runtime.provider, runtime.model, runtime.system_prompt)
        if sys.stdout.isatty():
            os.system("cls" if os.name == "nt" else "clear")
        _print_banner(runtime)
        ui.success("Conversation history cleared and context freed.")
        return None
    if command == "/compact":
        messages = list(runtime.session.get("messages", [])) if runtime.session else []
        if len(messages) < 2:
            ui.info("Nothing to compact; session history is short.")
            return None
        ui.info("Compacting conversation history...")
        instruction = argument if argument else "Summarize the key progress, files modified, current state, and next steps."
        compact_prompt = (
            f"{instruction}\n\n"
            "Provide a concise, dense summary of our prior work and current context so we can continue cleanly."
        )
        try:
            summary = runtime.chat(messages + [{"role": "user", "content": compact_prompt}])
            if summary and summary.strip():
                runtime.session["messages"] = [
                    {"role": "user", "content": f"[Context Summary of Prior Turns]:\n{summary.strip()}"},
                    {"role": "assistant", "content": "Context preserved. Ready to continue."},
                ]
                _save_interactive_session(runtime)
                ui.success("Conversation history compacted. Context window freed up.")
                print()
                print(summary.strip())
                print()
            else:
                ui.warn("Could not summarize session.")
        except Exception as error:
            ui.error(f"Compaction failed: {error}")
        return None
    if command == "/init":
        trench_file = runtime.root / "TRENCH.md"
        if trench_file.exists():
            ui.info(f"TRENCH.md already exists in {runtime.root}")
            print(f"# {trench_file}")
            content = trench_file.read_text(encoding="utf-8", errors="replace")
            print(content[:300] + ("..." if len(content) > 300 else ""))
            return None
        langs = []
        commands = []
        detect_files = [f.name for f in runtime.root.iterdir() if f.is_file()][:100]
        if any(f.endswith(".py") for f in detect_files) or (runtime.root / "pyproject.toml").exists() or (runtime.root / "requirements.txt").exists():
            langs.append("Python")
            commands.append("- Test: `python -m unittest discover` or `pytest`")
            commands.append("- Run: `python <entrypoint>.py`")
        if (runtime.root / "package.json").exists():
            langs.append("Node.js / TypeScript")
            commands.append("- Install: `npm install`")
            commands.append("- Test: `npm test`")
            commands.append("- Build: `npm run build`")
        if (runtime.root / "Cargo.toml").exists():
            langs.append("Rust")
            commands.append("- Build: `cargo build`")
            commands.append("- Test: `cargo test`")
        if (runtime.root / "go.mod").exists():
            langs.append("Go")
            commands.append("- Test: `go test ./...`")
        if (runtime.root / "Makefile").exists():
            commands.append("- Make: `make`")

        proj_name = runtime.root.name
        content = f"""# Trench Workspace Instructions: {proj_name}

## Project Overview
{proj_name} is a {' / '.join(langs) if langs else 'software'} project.

## Development Commands
{chr(10).join(commands) if commands else '- Add your test and build commands here'}

## Guidelines for Trench
- Inspect files before modifying them.
- Keep edits minimal, precise, and targeted.
- Run tests and verify changes before reporting completion.
- Respect existing project patterns, formatting, and conventions.
"""
        try:
            trench_file.write_text(content, encoding="utf-8")
            if not runtime.custom_system:
                runtime.system_prompt = build_system_prompt(runtime.root, None, runtime.agent_enabled)
            ui.success(f"Created {trench_file.name} with workspace instructions.")
        except OSError as e:
            ui.error(f"Could not create TRENCH.md: {e}")
        return None
    if command in ("/add-dir", "/cwd"):
        if argument:
            path = Path(argument).expanduser()
            if not path.is_dir():
                ui.error(f"Not a directory: {argument}")
            else:
                runtime.root = path.resolve()
                os.chdir(runtime.root)
                if runtime.session:
                    runtime.session["cwd"] = str(runtime.root)
                if not runtime.custom_system:
                    runtime.system_prompt = build_system_prompt(runtime.root, None, runtime.agent_enabled)
                ui.success(f"cwd = {runtime.root}")
        else:
            print(f"Current workspace: {runtime.root}")
        return None
    if command == "/agents":
        if argument:
            parts_arg = argument.split(maxsplit=1)
            sub = parts_arg[0].lower()
            val = parts_arg[1].strip() if len(parts_arg) > 1 else ""
            if sub in ("tools", "tool"):
                if val in ("on", "off"):
                    runtime.agent_enabled = val == "on"
                    if not runtime.custom_system:
                        runtime.system_prompt = build_system_prompt(runtime.root, None, runtime.agent_enabled)
                    ui.success(f"Agent tools {'on' if runtime.agent_enabled else 'off'}")
                else:
                    print(f"Agent tools are {'on' if runtime.agent_enabled else 'off'}")
            elif sub in ("approval", "approve"):
                if val in ("auto", "ask"):
                    runtime.auto_approve = val == "auto"
                    ui.success(f"Agent approval set to {val}")
                else:
                    print(f"Agent approval is currently: {'auto' if runtime.auto_approve else 'ask'}")
            elif sub in ("turns", "max-turns"):
                if val.isdigit():
                    runtime.settings["max_turns"] = int(val)
                    ui.success(f"Agent max turns set to {val}")
                else:
                    print(f"Max turns: {runtime.max_turns}")
            else:
                ui.error("Usage: /agents [tools on|off | approval ask|auto | turns N]")
            return None

        print(ui.style("Agent Configuration:", "1"))
        print(f"  Mode:        {'Autonomous coding agent (tools enabled)' if runtime.agent_enabled else 'Interactive chat (tools disabled)'}")
        print(f"  Provider:    {runtime.provider}")
        print(f"  Model:       {runtime.model}")
        print(f"  Approval:    {'auto (unattended)' if runtime.auto_approve else 'ask (prompts for approval)'}")
        print(f"  Max turns:   {runtime.max_turns}")
        trench_md = (runtime.root / "TRENCH.md").exists()
        print(f"  TRENCH.md:   {'loaded' if trench_md else 'not found (run /init to create)'}")
        print(f"  Workspace:   {runtime.root}")
        return None
    if command in ("/bashes", "/tasks"):
        if not runtime.command_history:
            ui.info("No shell commands executed in this session yet.")
            return None
        print(ui.style("Recent Shell Commands:", "1"))
        for item in runtime.command_history[-15:]:
            status = ui.style("ok", "32") if item["code"] == 0 else ui.style(f"exit {item['code']}", "31")
            dur = f" ({item['duration']})" if "duration" in item else ""
            print(f"  [{status}] {item['time']}  {item['command']}{dur}")
        return None
    if command in ("/cost", "/tokens"):
        messages = runtime.session.get("messages", []) if runtime.session else []
        total_chars = sum(len(m.get("content", "")) for m in messages)
        est_tokens = max(1, total_chars // 4)
        print(ui.style("Session Metrics:", "1"))
        print(f"  Session ID:       {runtime.session['id'] if runtime.session else 'none'}")
        print(f"  Messages:         {len(messages)}")
        print(f"  Estimated tokens: ~{est_tokens:,} tokens (~{total_chars:,} chars)")
        print(f"  Commands run:     {len(runtime.command_history)}")
        return None
    if command == "/model":
        if argument:
            runtime.model = argument
            ui.success(f"model = {runtime.model} (this session)")
        else:
            print(f"current: {runtime.provider}/{runtime.model}")
            choices = MODEL_CHOICES.get(runtime.provider, [])
            if choices:
                print("suggested: " + ", ".join(choices))
        return None
    if command == "/provider":
        if argument:
            name = normalize_provider(argument.split()[0])
            if name not in PROVIDERS:
                ui.error(f"Unknown provider: {name}")
            else:
                runtime.provider = name
                runtime.model = PROVIDERS[name].get("model", "")
                if len(argument.split()) > 1:
                    runtime.model = argument.split(maxsplit=1)[1]
                if not runtime.model:
                    ui.warn("set a model with /model NAME")
                ui.success(f"provider = {runtime.provider}/{runtime.model} (this session)")
        else:
            print(f"current: {runtime.provider}/{runtime.model}")
            print("available: " + ", ".join(PROVIDERS))
        return None
    if command == "/tools":
        if argument.lower() in ("on", "off"):
            runtime.agent_enabled = argument.lower() == "on"
            if not runtime.custom_system:
                runtime.system_prompt = build_system_prompt(runtime.root, None, runtime.agent_enabled)
            ui.success(f"tools {'on' if runtime.agent_enabled else 'off'}")
        else:
            print(f"tools are {'on' if runtime.agent_enabled else 'off'}")
            print(tools_prompt())
        return None
    if command == "/system":
        if argument.lower() == "reset":
            runtime.custom_system = False
            runtime.system_prompt = build_system_prompt(runtime.root, None, runtime.agent_enabled)
            ui.success("system prompt reset")
        elif argument:
            runtime.custom_system = True
            runtime.system_prompt = argument
            ui.success("system prompt updated for this session")
        else:
            print(runtime.system_prompt)
        return None
    if command == "/save":
        _save_interactive_session(runtime)
        ui.success("session saved")
        return None
    if command == "/sessions":
        sessions = trch_session.all_sessions()
        if not sessions:
            ui.info("No saved sessions.")
        for item in sessions[:20]:
            print(f"{item['id']}  {item.get('updated', '')}  {item.get('title', '')}")
        return None
    if command == "/config":
        path = Path(runtime.config_path).expanduser() if runtime.config_path else trch_config.default_config_path()
        print(f"# {path}")
        for key, value in runtime.settings.items():
            print(f"{key} = {trch_config.describe(value)}")
        return None
    if command == "/doctor":
        cmd_doctor(argparse.Namespace(json=False, network=False, quiet=False), runtime.settings, ui)
        return None
    ui.error(f"Unknown command: {command}. Type /help.")
    return None


def _run_shell_direct(command, runtime):
    start = time.time()
    try:
        result = subprocess.run(
            command, shell=True, cwd=str(runtime.root), text=True,
            capture_output=True, errors="replace", stdin=subprocess.DEVNULL,
        )
    except OSError as error:
        runtime.ui.error(str(error))
        return
    runtime.command_history.append({
        "command": command,
        "code": result.returncode,
        "time": time.strftime("%H:%M:%S"),
        "duration": f"{time.time() - start:.2f}s",
    })
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    if result.returncode:
        runtime.ui.error(f"command exited with {result.returncode}")


def _chat_turn(runtime, message):
    history = list(runtime.session["messages"]) if runtime.session else []
    if runtime.agent_enabled:
        agent = Agent(runtime.chat, runtime.context(), max_turns=runtime.max_turns,
                      on_event=make_event_handler(runtime.ui, runtime=runtime))
        result = agent.run(message, messages=history, system_prompt=runtime.system_prompt)
        if runtime.session:
            runtime.session["messages"] = [m for m in result.messages if m.get("role") != "system"]
        if result.final.strip():
            print(result.final)
        elif result.stopped == "max_turns":
            runtime.ui.error("agent reached the turn limit without a final answer.")
        else:
            runtime.ui.warn("The agent returned an empty response.")
        return result.stopped != "max_turns" and bool(result.final.strip())

    messages = [{"role": "system", "content": runtime.system_prompt}] + history
    messages.append({"role": "user", "content": message})
    stream = bool(runtime.settings.get("stream"))
    if stream:
        response = stream_to_stdout(runtime.provider, runtime.model, messages, runtime.timeout)
        if not response:
            runtime.ui.warn("The provider returned an empty response.")
    else:
        response = runtime.chat(messages)
        print(response)
    if runtime.session:
        runtime.session["messages"].append({"role": "user", "content": message})
        runtime.session["messages"].append({"role": "assistant", "content": response})
    return True


def start_repl(args, settings, ui, agent_enabled=True):
    root = Path.cwd()
    session = _load_session(args)
    if session:
        if not _get(args, "provider") and session.get("provider"):
            setattr(args, "provider", session["provider"])
        if not _get(args, "model") and session.get("model"):
            setattr(args, "model", session["model"])
    provider, model = resolve_target(args, settings, ui)
    if session:
        ui.info(f"Resuming session {session['id']} ({len(session.get('messages', []))} messages)")
    else:
        session = trch_session.new_session(root, provider, model)

    custom_system = bool(_get(args, "system") or settings.get("system_prompt") or session.get("system_prompt"))
    system_prompt = session.get("system_prompt") or _get(args, "system") or settings.get("system_prompt")
    if not system_prompt:
        system_prompt = build_system_prompt(root, None, tools=agent_enabled)
    runtime = Runtime(
        ui=ui, provider=provider, model=model, root=root, settings=settings,
        system_prompt=system_prompt, agent_enabled=agent_enabled, custom_system=custom_system,
        auto_approve=bool(_get(args, "yes") or settings.get("approval") == "auto"),
        config_path=_get(args, "config"), session=session,
    )
    _print_banner(runtime)
    readline_module = _setup_readline()

    pt_session = None
    if HAS_PROMPT_TOOLKIT and sys.stdin.isatty():
        try:
            pt_history = FileHistory(str(trch_config.history_path()))
            pt_session = PromptSession(
                completer=SlashCompleter(),
                complete_while_typing=True,
                style=Style.from_dict({
                    "prompt": "bold #00d7d7",
                    "completion-menu": "bg:#222222 #ffffff",
                    "completion-menu.completion": "bold #00d7d7",
                    "completion-menu.completion.current": "bold #ffffff bg:#d97706",
                    "completion-menu.meta": "#aaaaaa",
                    "completion-menu.meta.current": "#ffffff bg:#d97706",
                }),
                history=pt_history,
            )
        except Exception:
            pt_session = None

    try:
        while True:
            try:
                if pt_session is not None:
                    message = pt_session.prompt("> ")
                else:
                    message = input(f"{ui.style('>', '36')} ")
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print()
                ui.dim("(interrupt: press Ctrl+D or type /exit to quit)")
                continue

            message = message.strip()
            if not message:
                continue
            if message == "/":
                print_slash_commands(ui)
                continue
            if message.startswith("/"):
                if _slash_command(message, runtime) == "exit":
                    break
                continue
            if message.startswith("!"):
                _run_shell_direct(message[1:].strip(), runtime)
                continue

            try:
                completed = _chat_turn(runtime, message)
            except ProviderError as error:
                ui.error(str(error))
                continue
            except KeyboardInterrupt:
                print()
                ui.dim("(request interrupted)")
                continue
            if not completed:
                ui.warn("continuing after an incomplete agent turn; refine the task or raise --max-turns.")
            _save_interactive_session(runtime)
    finally:
        _finish_readline(readline_module)
        _save_interactive_session(runtime)
    return EXIT_OK


def cmd_repl(args, settings, ui, agent_enabled=True):
    return start_repl(args, settings, ui, agent_enabled=agent_enabled)


def cmd_chat(args, settings, ui):
    return start_repl(args, settings, ui, agent_enabled=False)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    ui = UI(no_color=_get(args, "no_color", False), quiet=_get(args, "quiet", False),
            verbose=_get(args, "verbose", 0))

    try:
        apply_cwd(args)
        trch_config.load_dotenv()
        settings = load_settings(args)

        command = _get(args, "command")
        if command in (None, "repl"):
            if not sys.stdin.isatty():
                return cmd_ask(args, settings, ui)
            return cmd_repl(args, settings, ui)
        if command == "ask":
            return cmd_ask(args, settings, ui)
        if command in ("run", "agent"):
            return cmd_run(args, settings, ui)
        if command == "chat":
            return cmd_chat(args, settings, ui)
        if command == "resume":
            if not sys.stdin.isatty():
                raise CLIError("Interactive mode needs a terminal.")
            args.session_id = _get(args, "session_id")
            return start_repl(args, settings, ui)
        if command == "models":
            return cmd_models(args, settings, ui)
        if command == "doctor":
            return cmd_doctor(args, settings, ui)
        if command == "config":
            return cmd_config(args, settings, ui)
        if command == "sessions":
            return cmd_sessions(args, settings, ui)
        raise CLIError(f"Unknown command: {command}")
    except CLIError as error:
        ui.error(str(error))
        return EXIT_USAGE
    except (SetupError, ProviderError, ConfigError, SessionError) as error:
        ui.error(str(error))
        return EXIT_ERROR
    except KeyboardInterrupt:
        print(file=sys.stderr)
        ui.error("interrupted")
        return EXIT_INTERRUPT
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
