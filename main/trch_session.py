"""Conversation sessions for the Trench CLI.

Sessions are plain JSON files under ``$XDG_STATE_HOME/trch/sessions``.  They
store only conversation messages and metadata, never API keys.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from trch_config import sessions_dir


class SessionError(RuntimeError):
    """Raised when a session cannot be read or written."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_session(cwd, provider="", model="", system_prompt=None):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return {
        "id": f"{stamp}-{secrets.token_hex(3)}",
        "created": _now(),
        "updated": _now(),
        "cwd": str(Path(cwd).resolve()),
        "provider": provider,
        "model": model,
        "system_prompt": system_prompt,
        "title": "",
        "messages": [],
    }


def title_for(messages):
    for message in messages:
        if message.get("role") == "user":
            text = " ".join(str(message.get("content", "")).split())
            if text:
                return text[:70] + ("…" if len(text) > 70 else "")
    return "(empty session)"


def save(session, directory=None, env=None):
    directory = Path(directory) if directory else sessions_dir(env)
    directory.mkdir(parents=True, exist_ok=True)
    session = dict(session)
    session["updated"] = _now()
    session["title"] = session.get("title") or title_for(session.get("messages", []))
    path = directory / f"{session['id']}.json"
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, prefix=".session-", suffix=".tmp", delete=False
    )
    try:
        with handle:
            json.dump(session, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(handle.name, path)
    except OSError as error:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise SessionError(f"Cannot write session {path}: {error}") from error
    return path


def load(session_id, directory=None, env=None):
    directory = Path(directory) if directory else sessions_dir(env)
    path = directory / f"{session_id}.json"
    if not path.exists():
        raise SessionError(f"Session not found: {session_id}")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            session = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise SessionError(f"Cannot read session {session_id}: {error}") from error
    if not isinstance(session, dict) or not isinstance(session.get("messages"), list):
        raise SessionError(f"Session {session_id} is malformed.")
    return session


def all_sessions(directory=None, env=None):
    directory = Path(directory) if directory else sessions_dir(env)
    if not directory.exists():
        return []
    sessions = []
    for path in directory.glob("*.json"):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("messages"), list):
            sessions.append(data)
    sessions.sort(key=lambda item: item.get("updated", ""), reverse=True)
    return sessions


def latest(cwd=None, directory=None, env=None):
    sessions = all_sessions(directory, env)
    if cwd:
        resolved = str(Path(cwd).resolve())
        sessions = [item for item in sessions if item.get("cwd") == resolved]
    return sessions[0] if sessions else None


def delete(session_id, directory=None, env=None):
    directory = Path(directory) if directory else sessions_dir(env)
    path = directory / f"{session_id}.json"
    if not path.exists():
        raise SessionError(f"Session not found: {session_id}")
    path.unlink()
    return path
