import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

MAIN = Path(__file__).resolve().parent
CLI = MAIN / "trch_cli.py"


def tool_block(name, arguments):
    return "```tool\n" + json.dumps({"name": name, "arguments": arguments}) + "\n```"


class FakeProvider(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        server = self.server
        server.requests.append(payload)
        response = server.responses.pop(0) if server.responses else server.default

        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for index in range(0, len(response), 4):
                chunk = response[index:index + 4]
                event = {"choices": [{"delta": {"content": chunk}}]}
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            body = json.dumps({"choices": [{"message": {"content": response}}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):
        pass


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.work = self.base / "work"
        self.work.mkdir()
        (self.base / "home").mkdir()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
        self.server.requests = []
        self.server.responses = []
        self.server.default = "hello from fake"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def env(self, **overrides):
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("OPENAI_", "ANTHROPIC_", "GEMINI_", "DEEPSEEK_", "MISTRAL_", "MOONSHOT_", "TRCH_"))
        }
        env.update(
            {
                "PYTHONPATH": str(MAIN),
                "HOME": str(self.base / "home"),
                "XDG_CONFIG_HOME": str(self.base / "config"),
                "XDG_STATE_HOME": str(self.base / "state"),
                "NO_COLOR": "1",
                "TERM": "dumb",
                "TRCH_PROVIDER": "openai_compatible",
                "TRCH_MODEL": "fake-model",
                "TRCH_OPENAI_COMPAT_BASE_URL": self.url,
                "TRCH_OPENAI_COMPAT_API_KEY": "test-key",
            }
        )
        env.update(overrides)
        return env

    def run_cli(self, args, env=None, stdin=None, cwd=None):
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=str(cwd or self.work),
            env=env or self.env(),
            input=stdin,
            stdin=subprocess.DEVNULL if stdin is None else None,
            capture_output=True,
            text=True,
            timeout=60,
        )


    def test_ask_non_stream(self):
        result = self.run_cli(["ask", "hello", "--no-stream"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "hello from fake")
        self.assertEqual(self.server.requests[0]["model"], "fake-model")
        self.assertEqual(self.server.requests[0]["messages"][-1]["content"], "hello")

    def test_local_env_file_supplies_provider_credentials(self):
        (self.work / ".env").write_text(
            f"TRCH_PROVIDER=openai_compatible\nTRCH_MODEL=fake-model\n"
            f"TRCH_OPENAI_COMPAT_BASE_URL={self.url}\n"
            "TRCH_OPENAI_COMPAT_API_KEY=local-test-key\n",
            encoding="utf-8",
        )
        env = self.env()
        for name in (
            "TRCH_PROVIDER",
            "TRCH_MODEL",
            "TRCH_OPENAI_COMPAT_BASE_URL",
            "TRCH_OPENAI_COMPAT_API_KEY",
        ):
            env.pop(name, None)

        result = self.run_cli(["ask", "hello", "--no-stream"], env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "hello from fake")

    def test_ask_streams_by_default(self):
        result = self.run_cli(["ask", "hello"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "hello from fake\n")
        self.assertTrue(self.server.requests[0]["stream"])

    def test_ask_json(self):
        result = self.run_cli(["ask", "hello", "--json"])
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["response"], "hello from fake")
        self.assertEqual(data["provider"], "openai_compatible")
        self.assertEqual(data["model"], "fake-model")

    def test_ask_reads_stdin(self):
        result = self.run_cli(["ask", "--no-stream"], stdin="from stdin\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("from stdin", self.server.requests[0]["messages"][-1]["content"])

    def test_default_command_reads_stdin(self):
        result = self.run_cli([], stdin="hello default mode\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "hello from fake")
        self.assertIn("hello default mode", self.server.requests[0]["messages"][-1]["content"])

    def test_doctor_json(self):
        result = self.run_cli(["doctor", "--json"])
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data["ok"])
        names = {item["name"] for item in data["checks"]}
        self.assertIn("key:openai_compatible", names)
        self.assertIn("workspace", names)

    def test_agent_writes_file_with_yes(self):
        self.server.responses = [
            tool_block("write_file", {"path": "agent.txt", "content": "written by agent\n"}),
            "Done.",
        ]
        result = self.run_cli(["run", "--yes", "create agent.txt"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "Done.")
        self.assertEqual((self.work / "agent.txt").read_text(), "written by agent\n")
        self.assertEqual(len(self.server.requests), 2)
        self.assertIn("Tool result for write_file", self.server.requests[1]["messages"][-1]["content"])
        self.assertIn("Available tools", self.server.requests[0]["messages"][0]["content"])

    def test_agent_denies_writes_without_yes(self):
        self.server.responses = [
            tool_block("write_file", {"path": "denied.txt", "content": "nope"}),
            "I could not write the file.",
        ]
        result = self.run_cli(["run", "create denied.txt"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.work / "denied.txt").exists())
        self.assertIn("non-interactive run", result.stderr)
        self.assertIn("DENIED", self.server.requests[1]["messages"][-1]["content"])

    def test_run_allow_outside_flag(self):
        outside = self.base / "outside.txt"
        outside.write_text("outside content\n", encoding="utf-8")
        self.server.responses = [
            tool_block("read_file", {"path": str(outside)}),
            "Read it.",
        ]
        result = self.run_cli(["run", "--yes", "--allow-outside", "read the outside file"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("outside content", self.server.requests[1]["messages"][-1]["content"])

    def test_agent_json_output(self):
        self.server.responses = ["Simple answer."]
        result = self.run_cli(["run", "--json", "answer directly"])
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["response"], "Simple answer.")
        self.assertEqual(data["stopped"], "final")
        self.assertEqual(data["turns"], 1)
        self.assertIsNotNone(data["session"])

    def test_missing_provider_is_setup_error(self):
        env = self.env(
            TRCH_PROVIDER="",
            TRCH_MODEL="",
            TRCH_OPENAI_COMPAT_BASE_URL="",
            TRCH_OPENAI_COMPAT_API_KEY="",
            OLLAMA_HOST="http://127.0.0.1:1",
        )
        result = self.run_cli(["ask", "hello"], env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("No provider configured", result.stderr)

    def test_missing_key_is_reported(self):
        env = self.env()
        env.pop("OPENAI_API_KEY", None)
        result = self.run_cli(
            ["ask", "hello", "--provider", "openai", "--model", "gpt-4o-mini", "--no-stream"],
            env=env,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("OPENAI_API_KEY", result.stderr)

    def test_config_set_and_get(self):
        env = self.env(TRCH_PROVIDER="")
        result = self.run_cli(["config", "set", "provider", "ollama"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli(["config", "get", "provider"], env=env)
        self.assertEqual(result.stdout.strip(), "ollama")
        result = self.run_cli(["config", "set", "timeout", "45"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        config_file = self.base / "config" / "trch" / "config.json"
        self.assertEqual(json.loads(config_file.read_text())["timeout"], 45)

    def test_ask_continue_uses_session_history(self):
        self.server.responses = ["first answer"]
        result = self.run_cli(["run", "--yes", "first question"])
        self.assertEqual(result.returncode, 0, result.stderr)

        self.server.responses = ["second answer"]
        result = self.run_cli(["ask", "-c", "--no-stream", "follow up"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "second answer")
        contents = [message["content"] for message in self.server.requests[-1]["messages"]]
        self.assertIn("first question", contents)
        self.assertIn("first answer", contents)
        self.assertIn("follow up", contents)

    def test_sessions_list_after_run(self):
        self.server.responses = ["Done."]
        result = self.run_cli(["run", "--yes", "say done"])
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli(["sessions", "list"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("say done", result.stdout)
        result = self.run_cli(["sessions", "list", "--json"])
        data = json.loads(result.stdout)
        self.assertEqual(len(data["sessions"]), 1)
        self.assertEqual(data["sessions"][0]["title"], "say done")

    def test_doctor_network_check_uses_selected_provider(self):
        import trch_cli

        class FakeResponse:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        with patch.object(trch_cli.urllib.request, "urlopen", return_value=FakeResponse()):
            checks = trch_cli.collect_doctor(
                {"provider": "openai_compatible"}, trch_cli.UI(no_color=True), network=True
            )
        network = next(item for item in checks if item["name"] == "network")
        self.assertTrue(network["ok"])

    def test_help_and_version(self):
        result = self.run_cli(["--help"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("trch run", result.stdout)
        result = self.run_cli(["--version"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("trch 1.0.0", result.stdout)

    def test_slash_init_and_system_prompt(self):
        import trch_cli
        import trch_agent

        runtime = trch_cli.Runtime(
            ui=trch_cli.UI(no_color=True),
            provider="openai_compatible",
            model="fake-model",
            root=self.work,
            settings={"timeout": 10, "max_turns": 5, "max_tool_output": 1000},
            system_prompt="",
        )
        self.assertFalse((self.work / "TRENCH.md").exists())
        trch_cli._slash_command("/init", runtime)
        self.assertTrue((self.work / "TRENCH.md").exists())
        content = (self.work / "TRENCH.md").read_text()
        self.assertIn("Trench Workspace Instructions", content)

        prompt = trch_agent.build_system_prompt(self.work)
        self.assertIn("Project instructions (from TRENCH.md):", prompt)

    def test_slash_compact_and_cost(self):
        import trch_cli

        self.server.responses = ["This is a summary of our progress."]
        session = {"id": "test-session", "messages": [
            {"role": "user", "content": "hello turn 1"},
            {"role": "assistant", "content": "answer 1"},
            {"role": "user", "content": "hello turn 2"},
            {"role": "assistant", "content": "answer 2"},
        ]}
        with patch.dict(os.environ, self.env()):
            runtime = trch_cli.Runtime(
                ui=trch_cli.UI(no_color=True),
                provider="openai_compatible",
                model="fake-model",
                root=self.work,
                settings={"timeout": 10, "max_turns": 5, "max_tool_output": 1000},
                system_prompt="",
                session=session,
            )
            trch_cli._slash_command("/compact", runtime)
            self.assertEqual(len(session["messages"]), 2)
            self.assertIn("Context Summary", session["messages"][0]["content"])
            self.assertIn("This is a summary of our progress.", session["messages"][0]["content"])

            trch_cli._slash_command("/cost", runtime)
            trch_cli._slash_command("/agents", runtime)


if __name__ == "__main__":
    unittest.main()
