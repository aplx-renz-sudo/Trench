"""Tool-using agent engine for the Trench CLI.

The model requests tools with a fenced JSON block:

    ```tool
    {"name": "read_file", "arguments": {"path": "src/app.py"}}
    ```

Only one tool is executed per model turn.  File tools are confined to the
workspace unless ``allow_outside`` is enabled.  Mutating tools and shell
commands require explicit approval (or ``auto_approve`` / ``--yes``).
"""

from __future__ import annotations

import difflib
import fnmatch
import json
import os
import re
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

MAX_FILE_BYTES = 2_000_000
MAX_READ_LINES = 2000
MAX_LIST_ENTRIES = 500
MAX_SEARCH_RESULTS = 100
BINARY_SUFFIXES = {
    ".7z", ".avi", ".bmp", ".class", ".db", ".dll", ".dylib", ".exe", ".gif", ".gz",
    ".ico", ".jar", ".jpeg", ".jpg", ".lock", ".mov", ".mp3", ".mp4", ".o", ".obj",
    ".pdf", ".png", ".pyc", ".pyd", ".so", ".sqlite", ".tar", ".ttf", ".wav", ".webp",
    ".woff", ".woff2", ".xz", ".zip",
}
IGNORED_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist", "build",
    "target", ".next", ".nuxt", "coverage", ".cache",
}
SECRET_FILENAMES = {
    ".env", ".netrc", ".npmrc", ".pypirc", "credentials", "credentials.json",
    "id_dsa", "id_ed25519", "id_rsa", "secrets.json",
}
SECRET_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template"}

TOOL_SPECS = {
    "read_file": {
        "description": "Read a text file with line numbers.",
        "arguments": {"path": "string (required)", "start_line": "integer (optional, default 1)",
                      "end_line": "integer (optional)"},
    },
    "write_file": {
        "description": "Create or replace a file. Directories are created as needed.",
        "arguments": {"path": "string (required)", "content": "string (required)"},
    },
    "edit_file": {
        "description": "Replace an exact text fragment in a file.",
        "arguments": {"path": "string (required)", "old_text": "string (required)",
                      "new_text": "string (required)", "replace_all": "boolean (optional)"},
    },
    "list_files": {
        "description": "List files and directories under a path.",
        "arguments": {"path": "string (optional, default '.')", "depth": "integer (optional, default 2)"},
    },
    "search": {
        "description": "Search file contents with a regular expression.",
        "arguments": {"pattern": "string (required)", "path": "string (optional, default '.')",
                      "glob": "string (optional, e.g. '*.py')", "max_results": "integer (optional)"},
    },
    "run_command": {
        "description": "Run a shell command in the workspace and return its output.",
        "arguments": {"command": "string (required)", "timeout": "integer seconds (optional)"},
    },
    "finish": {
        "description": "Finish the task and return a final summary to the user.",
        "arguments": {"summary": "string (required)"},
    },
}

TOOL_BLOCK_RE = re.compile(r"```(?:tool|tool-call)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


class ToolError(RuntimeError):
    """A tool could not complete its work."""


class ToolDenied(ToolError):
    """The user or policy denied a tool call."""


@dataclass
class ToolContext:
    root: Path
    approver: Optional[Callable[[str, str], bool]] = None
    auto_approve: bool = False
    allow_outside: bool = False
    max_output: int = 20000
    timeout: int = 120

    def __post_init__(self):
        self.root = Path(self.root).expanduser().resolve()

    def resolve(self, raw: Any) -> Path:
        if not isinstance(raw, str) or not raw.strip():
            raise ToolError("A non-empty path is required.")
        path = Path(os.path.expanduser(raw.strip()))
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve(strict=False)
        if not self.allow_outside:
            try:
                path.relative_to(self.root)
            except ValueError:
                raise ToolError(
                    f"Path escapes the workspace: {raw}. Use a path inside {self.root}."
                ) from None
        return path

    def approve(self, action: str, detail: str) -> bool:
        if self.auto_approve:
            return True
        if self.approver is None:
            return False
        return bool(self.approver(action, detail))


@dataclass
class ToolCall:
    name: str
    arguments: dict
    span: tuple = (0, 0)


@dataclass
class AgentResult:
    final: str
    messages: list
    turns: int
    stopped: str
    tool_calls: list = field(default_factory=list)


def truncate(text: str, limit: int, note: str = "\n… [output truncated]") -> str:
    text = "" if text is None else str(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(note))] + note


def _relative(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root)
        value = str(rel)
        return "." if value in ("", ".") else value
    except ValueError:
        return str(path)


def _is_secret_path(path: Path) -> bool:
    name = path.name.lower()
    if name in ENV_TEMPLATES:
        return False
    if name in SECRET_FILENAMES:
        return True
    if name.startswith(".env."):
        return True
    return path.suffix.lower() in SECRET_SUFFIXES


def _is_binary(path: Path) -> bool:
    if path.suffix.lower() in BINARY_SUFFIXES:
        return True
    try:
        with open(path, "rb") as handle:
            chunk = handle.read(4096)
    except OSError:
        return True
    return b"\x00" in chunk


def _walk_files(root: Path, start: Path):
    if start.is_file():
        yield start
        return
    for directory, dirnames, filenames in os.walk(start):
        dirnames[:] = sorted(name for name in dirnames if name not in IGNORED_DIRS)
        for filename in sorted(filenames):
            path = Path(directory) / filename
            if path.suffix.lower() in BINARY_SUFFIXES:
                continue
            yield path


def _read_text(path: Path) -> str:
    if not path.exists():
        raise ToolError(f"File not found: {path}")
    if not path.is_file():
        raise ToolError(f"Not a file: {path}")
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ToolError(f"File is too large ({size} bytes; limit {MAX_FILE_BYTES}). Use search instead.")
    if _is_binary(path):
        raise ToolError(f"Refusing to read binary file: {path.name}")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            raise ToolError(f"Cannot read {path}: {error}") from error
    except OSError as error:
        raise ToolError(f"Cannot read {path}: {error}") from error


def _diff(old: str, new: str, path: str, context: int = 3) -> str:
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=context,
    )
    return "".join(diff)


def _write_text(path: Path, content: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as error:
        raise ToolError(f"Cannot write {path}: {error}") from error


def _string(args, key, required=True, default=""):
    value = args.get(key, default)
    if value is None and not required:
        return default
    if not isinstance(value, str):
        raise ToolError(f"'{key}' must be a string.")
    if required and not value.strip():
        raise ToolError(f"'{key}' is required.")
    return value


def _integer(args, key, default, minimum=1, maximum=3600):
    value = args.get(key, default)
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"'{key}' must be an integer.") from None
    return max(minimum, min(maximum, value))


def tool_read_file(ctx: ToolContext, args: dict) -> str:
    path = ctx.resolve(_string(args, "path"))
    if _is_secret_path(path):
        relative = _relative(path, ctx.root)
        if not ctx.approve("read_file", f"Read sensitive file {relative}? Its contents will be sent to the model."):
            raise ToolDenied("reading a sensitive file was not approved.")
    text = _read_text(path)
    lines = text.splitlines()
    start = _integer(args, "start_line", 1, 1, max(1, len(lines) or 1))
    end = _integer(args, "end_line", len(lines) or 1, start, max(start, len(lines) or 1))
    selected = lines[start - 1:end]
    if len(selected) > MAX_READ_LINES:
        selected = selected[:MAX_READ_LINES]
        note = f"\n… [showing first {MAX_READ_LINES} of {end - start + 1} requested lines]"
    else:
        note = ""
    width = len(str(end))
    body = "\n".join(f"{number:>{width}} | {line}" for number, line in enumerate(selected, start))
    header = f"{_relative(path, ctx.root)} ({len(lines)} lines)"
    if note:
        body += note
    return f"{header}\n{body}" if body else f"{header}\n(empty file)"


def tool_write_file(ctx: ToolContext, args: dict) -> str:
    path = ctx.resolve(_string(args, "path"))
    content = _string(args, "content", required=False)
    if path.exists() and path.is_dir():
        raise ToolError(f"Path is a directory: {path}")
    existed = path.exists()
    old = path.read_text(encoding="utf-8", errors="replace") if existed else ""
    preview = _diff(old, content, _relative(path, ctx.root))
    detail = truncate(preview, 4000) if preview else f"Create {_relative(path, ctx.root)} ({len(content)} bytes)"
    if not ctx.approve("write_file", detail):
        raise ToolDenied("write_file was not approved.")
    _write_text(path, content)
    action = "Updated" if existed else "Created"
    return f"{action} {_relative(path, ctx.root)} ({len(content)} bytes, {content.count(chr(10)) + 1} lines)"


def tool_edit_file(ctx: ToolContext, args: dict) -> str:
    path = ctx.resolve(_string(args, "path"))
    old_text = _string(args, "old_text")
    new_text = _string(args, "new_text", required=False)
    replace_all = bool(args.get("replace_all", False))
    text = _read_text(path)
    count = text.count(old_text)
    if count == 0:
        raise ToolError("old_text was not found in the file. Read the file and retry with exact text.")
    if count > 1 and not replace_all:
        raise ToolError(
            f"old_text appears {count} times. Add more context to make it unique, or set replace_all=true."
        )
    updated = text.replace(old_text, new_text, -1 if replace_all else 1)
    preview = _diff(text, updated, _relative(path, ctx.root))
    if not ctx.approve("edit_file", truncate(preview, 4000)):
        raise ToolDenied("edit_file was not approved.")
    _write_text(path, updated)
    return f"{'Replaced' if count == 1 else f'Replaced {count} occurrences in'} {_relative(path, ctx.root)}\n{truncate(preview, 4000)}"


def tool_list_files(ctx: ToolContext, args: dict) -> str:
    start = ctx.resolve(_string(args, "path", required=False, default="."))
    if not start.exists():
        raise ToolError(f"Path not found: {start}")
    depth = _integer(args, "depth", 2, 1, 10)
    if start.is_file():
        return _relative(start, ctx.root)
    entries = []
    base_depth = len(start.parts)
    for directory, dirnames, filenames in os.walk(start):
        current = Path(directory)
        level = len(current.parts) - base_depth
        if level >= depth:
            dirnames[:] = []
        else:
            dirnames[:] = sorted(name for name in dirnames if name not in IGNORED_DIRS)
        for name in dirnames:
            entries.append(_relative(current / name, ctx.root) + "/")
        for name in sorted(filenames):
            entries.append(_relative(current / name, ctx.root))
        if len(entries) >= MAX_LIST_ENTRIES:
            break
    entries = sorted(dict.fromkeys(entries))[:MAX_LIST_ENTRIES]
    header = f"{_relative(start, ctx.root)} ({len(entries)} entries)"
    return header + "\n" + "\n".join(entries)


def tool_search(ctx: ToolContext, args: dict) -> str:
    pattern = _string(args, "pattern")
    start = ctx.resolve(_string(args, "path", required=False, default="."))
    glob = _string(args, "glob", required=False, default="") or None
    limit = _integer(args, "max_results", 50, 1, MAX_SEARCH_RESULTS)
    try:
        regex = re.compile(pattern)
    except re.error as error:
        raise ToolError(f"Invalid regular expression: {error}") from error
    if not start.exists():
        raise ToolError(f"Path not found: {start}")

    matches = []
    for path in _walk_files(ctx.root, start):
        if glob and not fnmatch.fnmatch(_relative(path, ctx.root), glob) and not fnmatch.fnmatch(path.name, glob):
            continue
        if _is_secret_path(path):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append(f"{_relative(path, ctx.root)}:{number}: {line.strip()[:300]}")
                if len(matches) >= limit:
                    break
        if len(matches) >= limit:
            break
    if not matches:
        return f"No matches for /{pattern}/ in {_relative(start, ctx.root)}"
    header = f"{len(matches)} match(es) for /{pattern}/"
    if len(matches) >= limit:
        header += f" (limit {limit})"
    return header + "\n" + "\n".join(matches)


def tool_run_command(ctx: ToolContext, args: dict) -> str:
    command = _string(args, "command")
    timeout = _integer(args, "timeout", min(ctx.timeout, 120), 1, 3600)
    if not ctx.approve("run_command", command):
        raise ToolDenied("run_command was not approved. Ask the user to rerun with --yes if appropriate.")
    env = os.environ.copy()
    env.update({"PAGER": "cat", "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "TRCH_AGENT": "1"})
    launcher = {}
    if os.name == "posix":
        # Put the command in its own process group so a timeout can kill the
        # whole tree instead of orphaning children.
        launcher["start_new_session"] = True
    try:
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=str(ctx.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            env=env,
            **launcher,
        )
    except OSError as error:
        raise ToolError(f"Command failed to start: {error}") from error
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
        else:
            process.kill()
        process.communicate()
        raise ToolError(f"Command timed out after {timeout}s: {command}") from None
    parts = [f"exit_code: {process.returncode}"]
    if stdout.strip():
        parts.append("stdout:\n" + stdout.rstrip())
    if stderr.strip():
        parts.append("stderr:\n" + stderr.rstrip())
    if len(parts) == 1:
        parts.append("(no output)")
    return truncate("\n".join(parts), ctx.max_output)


def tool_finish(ctx: ToolContext, args: dict) -> str:
    return _string(args, "summary", required=False, default="Task finished.")


TOOLS = {
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "list_files": tool_list_files,
    "search": tool_search,
    "run_command": tool_run_command,
    "finish": tool_finish,
}


def execute_tool(ctx: ToolContext, name: str, arguments: dict) -> str:
    if name not in TOOLS:
        raise ToolError(f"Unknown tool '{name}'. Available tools: {', '.join(TOOLS)}")
    if not isinstance(arguments, dict):
        raise ToolError("Tool arguments must be a JSON object.")
    return TOOLS[name](ctx, arguments)


def parse_tool_call(text: str) -> Optional[ToolCall]:
    """Return the first valid tool call in a model response, if any."""
    if not isinstance(text, str):
        return None
    candidates = []
    for match in TOOL_BLOCK_RE.finditer(text):
        candidates.append((match.group(1).strip(), match.span()))
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        candidates.append((stripped, (0, len(text))))
    for raw, span in candidates:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        name = data.get("name") or data.get("tool")
        arguments = data.get("arguments", data.get("args", data.get("parameters", {})))
        if isinstance(name, str) and isinstance(arguments, dict):
            return ToolCall(name=name.strip(), arguments=arguments, span=span)
    return None


def tools_prompt() -> str:
    lines = ["Available tools (JSON arguments):"]
    for name, spec in TOOL_SPECS.items():
        lines.append(f"- {name}: {spec['description']}")
        lines.append(f"  arguments: {json.dumps(spec['arguments'])}")
    return "\n".join(lines)


def build_system_prompt(root, extra=None, tools=True) -> str:
    root = Path(root).resolve()
    sections = [
        "You are trch, an autonomous coding agent running in a terminal.",
        f"Workspace root: {root}",
        "Use relative paths. Inspect files before changing them. Keep edits minimal and verify your work.",
        "Never claim a command or edit succeeded unless you have seen its result.",
    ]
    trench_md = root / "TRENCH.md"
    if trench_md.is_file():
        try:
            content = trench_md.read_text(encoding="utf-8", errors="replace").strip()
            if content:
                sections.append(f"Project instructions (from TRENCH.md):\n{content}")
        except OSError:
            pass
    if extra:
        sections.append("Additional user instructions:\n" + extra.strip())
    if tools:
        sections.append(tools_prompt())
        sections.append(
            "Tool protocol:\n"
            "When you need a tool, reply with exactly one fenced block and no other text:\n"
            '```tool\n{"name": "read_file", "arguments": {"path": "README.md"}}\n```\n'
            "After the tool result arrives, continue with the next step. "
            "When the task is complete, call finish with a concise summary, or reply with the final answer. "
            "If a tool is denied, explain what you need or choose a safer approach."
        )
    else:
        sections.append("Answer directly. You have no tools available in this mode.")
    return "\n\n".join(sections)


class Agent:
    """A small, provider-agnostic ReAct-style tool loop."""

    def __init__(self, chat: Callable[[list], str], ctx: ToolContext, max_turns=20, on_event=None):
        self.chat = chat
        self.ctx = ctx
        self.max_turns = max(1, int(max_turns))
        self.on_event = on_event or (lambda event: None)

    def run(self, task, messages=None, system_prompt=None) -> AgentResult:
        history = list(messages or [])
        if system_prompt:
            history.insert(0, {"role": "system", "content": system_prompt})
        history.append({"role": "user", "content": str(task)})

        final = ""
        stopped = "max_turns"
        calls = []
        turn = 0
        for turn in range(1, self.max_turns + 1):
            reply = self.chat(history)
            reply = "" if reply is None else str(reply)
            history.append({"role": "assistant", "content": reply})
            call = parse_tool_call(reply)
            if call is None:
                final = reply
                stopped = "final"
                self.on_event({"type": "final", "text": final})
                break
            calls.append(call)
            self.on_event({"type": "tool_start", "name": call.name, "arguments": call.arguments})
            try:
                output = execute_tool(self.ctx, call.name, call.arguments)
                ok = True
            except ToolDenied as error:
                output = f"DENIED: {error}"
                ok = False
            except ToolError as error:
                output = f"ERROR: {error}"
                ok = False
            self.on_event({"type": "tool_end", "name": call.name, "ok": ok, "output": output})
            if call.name == "finish" and ok:
                history.append({"role": "user", "content": f"Tool result for finish:\n{output}"})
                final = output
                stopped = "finished"
                self.on_event({"type": "final", "text": final})
                break
            history.append(
                {
                    "role": "user",
                    "content": f"Tool result for {call.name}:\n{truncate(output, self.ctx.max_output)}",
                }
            )
        else:
            final = reply if "reply" in locals() else ""

        return AgentResult(final=final, messages=history, turns=turn, stopped=stopped, tool_calls=calls)


def run_agent(chat, task, root, messages=None, system_prompt=None, max_turns=20,
              approver=None, auto_approve=False, allow_outside=False,
              max_output=20000, timeout=120, on_event=None) -> AgentResult:
    ctx = ToolContext(
        root=Path(root),
        approver=approver,
        auto_approve=auto_approve,
        allow_outside=allow_outside,
        max_output=max_output,
        timeout=timeout,
    )
    return Agent(chat, ctx, max_turns=max_turns, on_event=on_event).run(
        task, messages=messages, system_prompt=system_prompt
    )
