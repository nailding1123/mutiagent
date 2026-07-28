from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from multiagent_cli.adapters import (
    ClaudeAdapter,
    ClaudeEventParser,
    CodexAdapter,
    CodexEventParser,
)
from multiagent_cli.bridge_models import AgentCommandSettings, BridgeError


class EventParserTests(unittest.TestCase):
    def test_codex_parser_extracts_thread_text_and_tool(self) -> None:
        parser = CodexEventParser()
        parser.feed(json.dumps({"type": "thread.started", "thread_id": "thread-1"}))
        tool_events = parser.feed(
            json.dumps(
                {
                    "type": "item.started",
                    "item": {"type": "command_execution", "command": "pytest"},
                }
            )
        )
        text_events = parser.feed(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "修复完成"},
                }
            )
        )
        parser.feed(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 120, "output_tokens": 30},
                }
            )
        )

        self.assertEqual(parser.session_id, "thread-1")
        self.assertEqual(tool_events[0].kind, "tool")
        self.assertEqual(tool_events[0].text, "pytest")
        self.assertEqual(text_events[0].kind, "progress")
        self.assertEqual(text_events[0].text, "修复完成")
        self.assertEqual(parser.final_text, "修复完成")
        self.assertEqual(parser.input_tokens, 120)
        self.assertEqual(parser.output_tokens, 30)

    def test_claude_parser_extracts_session_tools_and_result(self) -> None:
        parser = ClaudeEventParser()
        parser.feed(
            json.dumps({"type": "system", "subtype": "init", "session_id": "session-1"})
        )
        events = parser.feed(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Read", "input": {"file": "a.py"}},
                            {"type": "text", "text": "正在处理"},
                        ]
                    },
                },
                ensure_ascii=False,
            )
        )
        parser.feed(
            json.dumps(
                {
                    "type": "result",
                    "session_id": "session-1",
                    "result": "最终完成",
                    "is_error": False,
                    "usage": {"input_tokens": 80, "output_tokens": 20},
                },
                ensure_ascii=False,
            )
        )

        self.assertEqual(parser.session_id, "session-1")
        self.assertEqual([event.kind for event in events], ["tool", "progress"])
        self.assertEqual(parser.final_text, "最终完成")
        self.assertEqual(parser.input_tokens, 80)
        self.assertEqual(parser.output_tokens, 20)


class CommandBuilderTests(unittest.TestCase):
    def test_claude_writer_and_reviewer_permissions(self) -> None:
        adapter = ClaudeAdapter(AgentCommandSettings(("claude",), model="opus"))
        workspace = Path("/tmp")

        writer = adapter.build_command(workspace=workspace, mode="write", session_id=None)
        reviewer = adapter.build_command(
            workspace=workspace, mode="read", session_id="session-1"
        )

        self.assertIn("acceptEdits", writer)
        self.assertIn("plan", reviewer)
        self.assertIn("session-1", reviewer)
        self.assertIn("opus", writer)

    def test_codex_uses_workspace_sandbox_and_resume(self) -> None:
        adapter = CodexAdapter(AgentCommandSettings(("codex",)))
        with tempfile.TemporaryDirectory() as directory:
            fresh = adapter.build_command(
                workspace=Path(directory), mode="read", session_id=None
            )
            resumed = adapter.build_command(
                workspace=Path(directory), mode="write", session_id="thread-1"
            )

        self.assertIn("read-only", fresh)
        self.assertIn("never", fresh)
        self.assertEqual(resumed[0], "codex")
        self.assertIn("resume", resumed)
        self.assertLess(resumed.index("never"), resumed.index("exec"))
        self.assertIn("thread-1", resumed)


class AdapterProcessTests(unittest.TestCase):
    def test_adapters_stream_events_from_native_processes(self) -> None:
        script_text = """#!/usr/bin/env python3
import json
import sys

sys.stdin.read()
if "exec" in sys.argv:
    print(json.dumps({"type": "thread.started", "thread_id": "codex-thread"}))
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "codex done"}}))
else:
    print(json.dumps({"type": "system", "subtype": "init", "session_id": "claude-session"}))
    print(json.dumps({"type": "result", "session_id": "claude-session", "result": "claude done", "is_error": False}))
"""
        with tempfile.TemporaryDirectory() as directory:
            fake_cli = Path(directory) / "fake-agent"
            fake_cli.write_text(script_text, encoding="utf-8")
            os.chmod(fake_cli, 0o755)
            workspace = Path(directory)

            claude = ClaudeAdapter(AgentCommandSettings((str(fake_cli),)))
            codex = CodexAdapter(AgentCommandSettings((str(fake_cli),)))
            events = []
            claude_result = claude.run(
                "task", workspace=workspace, mode="write", on_event=events.append
            )
            codex_result = codex.run(
                "review", workspace=workspace, mode="read", on_event=events.append
            )

        self.assertEqual(claude_result.final_text, "claude done")
        self.assertEqual(claude_result.session_id, "claude-session")
        self.assertEqual(codex_result.final_text, "codex done")
        self.assertEqual(codex_result.session_id, "codex-thread")
        final_events = [event for event in events if event.kind == "text"]
        self.assertEqual(
            [(event.source, event.text) for event in final_events],
            [("Claude", "claude done"), ("Codex", "codex done")],
        )

    def test_adapter_kills_a_native_cli_after_timeout(self) -> None:
        script_text = """#!/usr/bin/env python3
import sys
import time
sys.stdin.read()
time.sleep(10)
"""
        with tempfile.TemporaryDirectory() as directory:
            fake_cli = Path(directory) / "slow-agent"
            fake_cli.write_text(script_text, encoding="utf-8")
            os.chmod(fake_cli, 0o755)
            adapter = ClaudeAdapter(
                AgentCommandSettings((str(fake_cli),), timeout=0.05)
            )

            with self.assertRaisesRegex(BridgeError, "超过 0.05 秒"):
                adapter.run("task", workspace=Path(directory), mode="read")


if __name__ == "__main__":
    unittest.main()
