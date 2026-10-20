# Trench CLI

Trench is a terminal AI coding agent. It connects directly to hosted model APIs
or a local Ollama server, works inside your current directory, and can read,
search, edit, and run code with your approval.

No third-party Python packages are required. Python 3.9+.

## Install

From this folder:

```sh
python -m pip install -e .
trch --version
```

Or run it without installing:

```sh
./trch --help
python trch_cli.py --help
```

## Quick start

```sh
# Interactive agent in the current project
trch

# One-shot answer (no tools)
trch ask "explain the difference between a process and a thread"

# One-shot agent task (tools enabled)
trch run "add a --json flag to the status command and run the tests"

# Pipe a prompt or an error log
git diff | trch ask "review this diff for bugs"
trch ask < traceback.log

# Pick a provider/model for one run
trch --provider anthropic --model claude-sonnet-4-20250514 ask "summarize README.md"
```

Model output goes to stdout. Status, approvals, and diagnostics go to stderr, so
`trch ask ... > answer.md` stays clean.

## Providers

| Provider | Environment variable | Default model |
| --- | --- | --- |
| OpenAI / ChatGPT | `OPENAI_API_KEY` | `gpt-4o-mini` |
| Claude | `ANTHROPIC_API_KEY` | `claude-3-5-haiku-latest` |
| Gemini | `GEMINI_API_KEY` | `gemini-2.0-flash` |
| DeepSeek | `DEEPSEEK_API_KEY` | `deepseek-chat` |
| Mistral | `MISTRAL_API_KEY` | `mistral-small-latest` |
| Kimi / Moonshot | `MOONSHOT_API_KEY` | `moonshot-v1-8k` |
| Ollama | `OLLAMA_HOST` (optional) | `llama3.2:3b` |
| OpenAI-compatible | `TRCH_OPENAI_COMPAT_BASE_URL`, `TRCH_OPENAI_COMPAT_API_KEY` | pass `--model` |

API keys are read from the environment and are never written to config or
sessions. For a local setup, copy `.env.example` to `.env` in the working
directory and add the key for your provider. `.env` is ignored by Git. Values
already exported in your shell take precedence. You can also use a shell profile,
a secret manager, or `direnv`.

If no provider is configured, Trench auto-detects the first available API key,
then falls back to a reachable Ollama server.

## Commands

```
trch                       Interactive agent session
trch ask [prompt]          One-shot answer (no tools)
trch run <task>            One-shot agent (alias: agent)
trch chat                  Interactive chat without tools
trch resume [session-id]   Resume a saved session
trch models [provider]     Providers, keys, and model suggestions
trch config ...            list | get | set | unset | path | edit
trch sessions ...          list | show | delete | path
trch doctor                Diagnose providers, keys, Ollama, and git
```

Useful global flags:

```
-C, --cwd DIR       Work in another directory
-m, --model NAME    Model ID
    --provider NAME Provider name
    --stream / --no-stream
    --json          Machine-readable output
    --no-color      Plain text output
-y, --yes           Approve file writes and commands (agent)
-v, --verbose       Show tool output
-q, --quiet         Suppress status messages
-c, --continue      Resume the latest session in this directory
```

`--json` is supported by `ask`, `run`, `models`, `doctor`, and `sessions`.

## Agent tools

In `trch` and `trch run`, the model can call:

| Tool | Purpose |
| --- | --- |
| `read_file` | Read a text file with line numbers |
| `write_file` | Create or replace a file |
| `edit_file` | Exact-fragment replacement |
| `list_files` | List a directory tree |
| `search` | Regex search across files |
| `run_command` | Run a shell command in the workspace |
| `finish` | Finish the task with a summary |

File tools are jailed to the workspace: paths that escape it are rejected unless
`--allow-outside` is set. `write_file`, `edit_file`, and `run_command` ask for
approval in interactive mode. In scripts, pass `--yes` to allow mutations;
without it those tools are denied and the agent is told why.

`run_command` runs with stdin closed, `PAGER=cat`, a timeout, and truncated
output, so it cannot hang waiting for input. It is still a real shell — review
the command before approving it.

Inside the interactive session:

```
/help                 Show all commands
/model [name]         Show or change the model
/provider [name]      Show or change the provider
/tools on|off         Enable or disable tools
/cwd [path]           Show or change the workspace
/system [text|reset]  Customize the system prompt
/new                  Start a new session
/save                 Save now
/sessions             List saved sessions
/config               Show effective configuration
/doctor               Run diagnostics
!<command>            Run a shell command directly
/exit                 Quit
```

## Configuration

Config lives at `~/.config/trch/config.json` (or
`$XDG_CONFIG_HOME/trch/config.json`):

```sh
trch config list
trch config set provider ollama
trch config set model qwen2.5-coder:3b
trch config set stream false
trch config set timeout 180
trch config set approval ask
trch config path
trch config edit
```

Supported keys: `provider`, `model`, `stream`, `timeout`, `max_turns`,
`approval` (`ask` or `auto`), `agent`, `system_prompt`, `max_tool_output`,
`allow_outside`.

Environment overrides: `TRCH_PROVIDER`, `TRCH_MODEL`, `TRCH_STREAM`,
`TRCH_TIMEOUT`, `TRCH_MAX_TURNS`, `TRCH_APPROVAL`, `TRCH_AGENT`,
`TRCH_SYSTEM_PROMPT`, `TRCH_MAX_TOOL_OUTPUT`, `TRCH_ALLOW_OUTSIDE`.

Sessions and readline history live under
`$XDG_STATE_HOME/trch` (default `~/.local/state/trch`). Sessions store
conversation messages only — no secrets.

## Streaming

`ask` and `chat` stream by default for every provider (SSE for OpenAI-compatible,
Anthropic, and Gemini; NDJSON for Ollama). Use `--no-stream` for a single
response, or `--json` to force a complete JSON result.

## Architecture

| File | Role |
| --- | --- |
| `trch_cli.py` | Argument parsing, commands, REPL, rendering, exit codes |
| `trch_agent.py` | Tool registry, sandboxing, approvals, agent loop |
| `trch_providers.py` | Provider adapters, streaming, Ollama helpers |
| `trch_config.py` | XDG paths, config validation, env overrides |
| `trch_session.py` | Session persistence |
| `trch_filter.py` | Optional prompt compression layer |
| `trch.py` | Legacy interactive interface (kept for compatibility) |

Exit codes: `0` success, `1` provider/agent failure, `2` usage or setup error,
`130` interrupted.

## Tests

```sh
python -m unittest discover -v
```

The suite is offline by default and includes an in-process fake
OpenAI-compatible server that exercises streaming, JSON output, and a complete
agent tool round-trip.

## Legacy interface

`python trch.py` delegates to the new CLI, so it behaves like `trch`. The
original interface is still available explicitly:

```sh
python trch.py --legacy
python trch.py --query "hello" --mode chat
```

The old model-switcher, token filter, and file bridge remain in `trch.py` for
compatibility, but `trch` is the supported entry point.
