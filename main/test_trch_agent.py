import json
import sys
import tempfile
import unittest
from pathlib import Path

import trch_agent


def tool_block(name, arguments):
    return "```tool\n" + json.dumps({"name": name, "arguments": arguments}) + "\n```"


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ctx = trch_agent.ToolContext(root=self.root, auto_approve=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_write_read_and_edit_file(self):
        result = trch_agent.execute_tool(
            self.ctx, "write_file", {"path": "notes.txt", "content": "hello\nworld\n"}
        )
        self.assertIn("Created notes.txt", result)

        content = trch_agent.execute_tool(self.ctx, "read_file", {"path": "notes.txt"})
        self.assertIn("1 | hello", content)
        self.assertIn("2 | world", content)

        edited = trch_agent.execute_tool(
            self.ctx, "edit_file",
            {"path": "notes.txt", "old_text": "world", "new_text": "trench"},
        )
        self.assertIn("trench", (self.root / "notes.txt").read_text())
        self.assertIn("Replaced", edited)

    def test_edit_requires_unique_match(self):
        (self.root / "a.txt").write_text("x\nx\n", encoding="utf-8")
        with self.assertRaisesRegex(trch_agent.ToolError, "appears 2 times"):
            trch_agent.execute_tool(
                self.ctx, "edit_file", {"path": "a.txt", "old_text": "x", "new_text": "y"}
            )
        trch_agent.execute_tool(
            self.ctx, "edit_file",
            {"path": "a.txt", "old_text": "x", "new_text": "y", "replace_all": True},
        )
        self.assertEqual((self.root / "a.txt").read_text(), "y\ny\n")

    def test_path_escape_is_rejected(self):
        with self.assertRaisesRegex(trch_agent.ToolError, "escapes the workspace"):
            trch_agent.execute_tool(self.ctx, "read_file", {"path": "../outside.txt"})
        with self.assertRaisesRegex(trch_agent.ToolError, "escapes the workspace"):
            trch_agent.execute_tool(self.ctx, "read_file", {"path": "/etc/passwd"})

    def test_allow_outside_opt_in(self):
        outside = self.root.parent / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        try:
            ctx = trch_agent.ToolContext(root=self.root, auto_approve=True, allow_outside=True)
            content = trch_agent.execute_tool(ctx, "read_file", {"path": str(outside)})
            self.assertIn("outside", content)
        finally:
            outside.unlink()

    def test_search_and_list_files(self):
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("def need_me():\n    pass\n", encoding="utf-8")
        (self.root / "README.md").write_text("docs\n", encoding="utf-8")

        listing = trch_agent.execute_tool(self.ctx, "list_files", {"path": ".", "depth": 2})
        self.assertIn("src/app.py", listing)
        self.assertIn("README.md", listing)

        found = trch_agent.execute_tool(
            self.ctx, "search", {"pattern": "need_me", "glob": "*.py"}
        )
        self.assertIn("src/app.py:1", found)
        self.assertIn("need_me", found)

    def test_denied_write_does_not_touch_disk(self):
        ctx = trch_agent.ToolContext(root=self.root, approver=lambda action, detail: False)
        with self.assertRaises(trch_agent.ToolDenied):
            trch_agent.execute_tool(ctx, "write_file", {"path": "no.txt", "content": "x"})
        self.assertFalse((self.root / "no.txt").exists())

    def test_sensitive_file_requires_approval(self):
        (self.root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        denied = trch_agent.ToolContext(root=self.root, approver=lambda action, detail: False)
        with self.assertRaises(trch_agent.ToolDenied):
            trch_agent.execute_tool(denied, "read_file", {"path": ".env"})

        allowed = trch_agent.ToolContext(root=self.root, auto_approve=True)
        content = trch_agent.execute_tool(allowed, "read_file", {"path": ".env"})
        self.assertIn("TOKEN=secret", content)

    def test_search_skips_sensitive_files(self):
        (self.root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        (self.root / ".env.example").write_text("TOKEN=example\n", encoding="utf-8")
        output = trch_agent.execute_tool(self.ctx, "search", {"pattern": "TOKEN"})
        self.assertIn(".env.example", output)
        self.assertNotIn("TOKEN=secret", output)

    def test_run_command_captures_output(self):
        command = f'"{sys.executable}" -c "print(42)"'
        output = trch_agent.execute_tool(self.ctx, "run_command", {"command": command})
        self.assertIn("exit_code: 0", output)
        self.assertIn("42", output)

    def test_run_command_reports_failure(self):
        command = f'"{sys.executable}" -c "import sys; sys.exit(3)"'
        output = trch_agent.execute_tool(self.ctx, "run_command", {"command": command})
        self.assertIn("exit_code: 3", output)

    def test_parse_tool_call_variants(self):
        fenced = tool_block("read_file", {"path": "README.md"})
        call = trch_agent.parse_tool_call(f"thinking\n{fenced}\nmore")
        self.assertEqual(call.name, "read_file")
        self.assertEqual(call.arguments["path"], "README.md")

        bare = json.dumps({"tool": "list_files", "args": {"path": "."}})
        call = trch_agent.parse_tool_call(bare)
        self.assertEqual(call.name, "list_files")

        self.assertIsNone(trch_agent.parse_tool_call("just a normal answer"))


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def make_agent(self, responses, max_turns=5, auto_approve=True):
        calls = []

        def chat(messages):
            calls.append(list(messages))
            return responses.pop(0)

        ctx = trch_agent.ToolContext(root=self.root, auto_approve=auto_approve)
        engine = trch_agent.Agent(chat, ctx, max_turns=max_turns)
        return engine, calls

    def test_agent_executes_tool_then_answers(self):
        responses = [
            tool_block("write_file", {"path": "hello.txt", "content": "hi\n"}),
            "Created hello.txt.",
        ]
        engine, calls = self.make_agent(responses)
        result = engine.run("create a file", system_prompt="test system")

        self.assertEqual(result.stopped, "final")
        self.assertEqual(result.final, "Created hello.txt.")
        self.assertEqual(result.turns, 2)
        self.assertEqual((self.root / "hello.txt").read_text(), "hi\n")
        self.assertEqual(calls[0][0]["role"], "system")
        self.assertIn("Tool result for write_file", calls[1][-1]["content"])

    def test_agent_finish_tool_stops(self):
        engine, _ = self.make_agent([tool_block("finish", {"summary": "all done"})])
        result = engine.run("do work")
        self.assertEqual(result.stopped, "finished")
        self.assertEqual(result.final, "all done")

    def test_agent_reports_tool_errors_and_continues(self):
        responses = [
            tool_block("read_file", {"path": "missing.txt"}),
            "The file does not exist.",
        ]
        engine, calls = self.make_agent(responses)
        result = engine.run("read it")
        self.assertEqual(result.stopped, "final")
        self.assertIn("ERROR:", calls[1][-1]["content"])

    def test_agent_max_turns(self):
        responses = [tool_block("list_files", {"path": "."}) for _ in range(3)]
        engine, _ = self.make_agent(responses, max_turns=2)
        result = engine.run("loop")
        self.assertEqual(result.stopped, "max_turns")
        self.assertEqual(result.turns, 2)
        self.assertEqual(len(result.tool_calls), 2)

    def test_agent_denied_tool_reports_denial(self):
        responses = [
            tool_block("write_file", {"path": "x.txt", "content": "x"}),
            "I need approval to write the file.",
        ]
        engine, calls = self.make_agent(responses, auto_approve=False)
        result = engine.run("write it")
        self.assertFalse((self.root / "x.txt").exists())
        self.assertIn("DENIED", calls[1][-1]["content"])
        self.assertEqual(result.stopped, "final")


if __name__ == "__main__":
    unittest.main()
