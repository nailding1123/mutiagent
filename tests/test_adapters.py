from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from multiagent_cli.adapters import (
    ClaudeAdapter,
    ClaudeEventParser,
    CodexAdapter,
    CodexEventParser,
    _claude_interaction_request,
    _claude_interaction_response,
    _codex_interaction_request,
    _codex_interaction_response,
)
from multiagent_cli.bridge_models import (
    AgentCommandSettings,
    BridgeError,
    NativeInteractionResponse,
)
from multiagent_cli.bridge_config import resolve_bridge_settings
from multiagent_cli.runtime import make_adapters
from multiagent_cli.token_api import TokenAPICredentials


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
        self.assertEqual(tool_events[0].metadata["activity_type"], "command")
        self.assertEqual(tool_events[0].metadata["command"], "pytest")
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
        self.assertEqual(events[0].metadata["activity_type"], "read")
        self.assertEqual(events[0].metadata["path"], "a.py")
        self.assertEqual(parser.final_text, "最终完成")
        self.assertEqual(parser.input_tokens, 80)
        self.assertEqual(parser.output_tokens, 20)


class CommandBuilderTests(unittest.TestCase):
    def test_claude_write_and_read_permissions(self) -> None:
        adapter = ClaudeAdapter(AgentCommandSettings(("claude",), model="opus"))
        workspace = Path("/tmp")

        writer = adapter.build_command(workspace=workspace, mode="write", session_id=None)
        validator = adapter.build_command(
            workspace=workspace, mode="read", session_id="session-1"
        )

        self.assertIn("acceptEdits", writer)
        self.assertIn("plan", validator)
        self.assertIn("session-1", validator)
        self.assertIn("opus", writer)

    def test_codex_uses_workspace_sandbox_and_resume(self) -> None:
        adapter = CodexAdapter(AgentCommandSettings(("codex",)))
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            fresh = adapter.build_command(
                workspace=workspace, mode="read", session_id=None
            )
            resumed_write = adapter.build_command(
                workspace=workspace, mode="write", session_id="thread-1"
            )
            resumed_read = adapter.build_command(
                workspace=workspace, mode="read", session_id="thread-1"
            )

        self.assertIn("read-only", fresh)
        self.assertIn("never", fresh)
        self.assertEqual(resumed_write[0], "codex")
        self.assertIn("resume", resumed_write)
        self.assertLess(resumed_write.index("never"), resumed_write.index("exec"))
        self.assertLess(resumed_write.index("workspace-write"), resumed_write.index("exec"))
        self.assertEqual(resumed_write[resumed_write.index("-C") + 1], str(workspace))
        self.assertIn("thread-1", resumed_write)
        self.assertIn("read-only", resumed_read)
        self.assertNotEqual(resumed_write, resumed_read)

    def test_token_api_provider_is_passed_without_putting_key_in_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            state_root = workspace / "state"
            TokenAPICredentials(state_root, environ={}).save("secret-company-key")
            settings = resolve_bridge_settings(
                {
                    "token_api": {"enabled": True, "base_url": "https://tokencheap.io"},
                    "claude": {"command": "/bin/echo", "models": ["claude-opus-5"]},
                    "codex": {"command": "/bin/echo", "models": ["gpt-5.6-sol"]},
                },
                workspace=workspace,
            )

            adapters = make_adapters(settings, state_root=state_root)
            claude_command = adapters["claude"].build_command(
                workspace=workspace,
                mode="read",
                session_id=None,
            )
            codex_command = adapters["codex"].build_command(
                workspace=workspace,
                mode="read",
                session_id=None,
            )

        self.assertEqual(
            adapters["claude"].environment["ANTHROPIC_BASE_URL"],
            "https://tokencheap.io",
        )
        self.assertEqual(
            adapters["codex"].environment["OPENAI_API_KEY"],
            "secret-company-key",
        )
        self.assertNotIn("secret-company-key", " ".join(claude_command))
        self.assertNotIn("secret-company-key", " ".join(codex_command))
        self.assertIn('model_providers.OpenAI.wire_api="responses"', codex_command)
        self.assertIn(
            'model_providers.OpenAI.base_url="https://tokencheap.io/v1"',
            codex_command,
        )

    def test_native_request_adapters_normalize_provider_protocols(self) -> None:
        claude_request = _claude_interaction_request(
            {
                "type": "control_request",
                "request_id": "claude-1",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Bash",
                    "input": {"command": "pytest -q"},
                },
            },
            "Claude",
        )
        self.assertIsNotNone(claude_request)
        assert claude_request is not None
        self.assertEqual(claude_request.kind, "command_approval")
        claude_response = _claude_interaction_response(
            {"request_id": "claude-1"},
            NativeInteractionResponse("approve"),
        )
        self.assertEqual(claude_response["response"]["request_id"], "claude-1")
        self.assertEqual(claude_response["response"]["response"]["behavior"], "allow")

        codex_request = _codex_interaction_request(
            {
                "id": 7,
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "pytest -q", "cwd": "/tmp"},
            },
            "Codex",
        )
        self.assertIsNotNone(codex_request)
        assert codex_request is not None
        self.assertEqual(codex_request.id, "7")
        self.assertEqual(
            _codex_interaction_response(
                {"method": "item/commandExecution/requestApproval"},
                NativeInteractionResponse("cancel"),
            ),
            {"decision": "cancel"},
        )


class AdapterProcessTests(unittest.TestCase):
    def test_timeout_switches_to_next_model_in_order(self) -> None:
        script_text = """#!/usr/bin/env python3
import json
import sys
import time

sys.stdin.read()
model = sys.argv[sys.argv.index("--model") + 1]
if model == "slow-model":
    time.sleep(10)
print(json.dumps({"type": "result", "session_id": model, "result": model + " done", "is_error": False}))
"""
        with tempfile.TemporaryDirectory() as directory:
            fake_cli = Path(directory) / "fallback-agent.py"
            fake_cli.write_text(script_text, encoding="utf-8")
            adapter = ClaudeAdapter(
                AgentCommandSettings(
                    (sys.executable, str(fake_cli)),
                    model="slow-model",
                    models=("slow-model", "fast-model"),
                    timeout=0.08,
                )
            )
            self.assertFalse(adapter.session_resume_enabled)
            events = []

            result = adapter.run(
                "task",
                workspace=Path(directory),
                mode="read",
                session_id="old-session-must-not-resume",
                on_event=events.append,
            )

        self.assertEqual(result.final_text, "fast-model done")
        self.assertIsNone(result.session_id)
        warnings = [event for event in events if event.kind == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("slow-model", warnings[0].text)
        self.assertIn("fast-model", warnings[0].text)

    def test_adapter_reads_stdout_while_writing_a_large_prompt(self) -> None:
        script_text = """#!/usr/bin/env python3
import json
import sys

print(json.dumps({"type": "system", "padding": "x" * 300000}), flush=True)
received = sys.stdin.read()
print(json.dumps({"type": "result", "session_id": "large-io", "result": str(len(received)), "is_error": False}))
"""
        with tempfile.TemporaryDirectory() as directory:
            fake_cli = Path(directory) / "large-io-agent.py"
            fake_cli.write_text(script_text, encoding="utf-8")
            adapter = ClaudeAdapter(
                AgentCommandSettings(
                    (sys.executable, str(fake_cli)),
                    timeout=5,
                )
            )
            prompt = "p" * 300_000

            result = adapter.run(prompt, workspace=Path(directory), mode="read")

        self.assertEqual(result.final_text, str(len(prompt)))

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
            fake_cli = Path(directory) / "fake-agent.py"
            fake_cli.write_text(script_text, encoding="utf-8")
            workspace = Path(directory)

            command = (sys.executable, str(fake_cli))
            claude = ClaudeAdapter(AgentCommandSettings(command))
            codex = CodexAdapter(AgentCommandSettings(command))
            events = []
            claude_result = claude.run(
                "task",
                workspace=workspace,
                mode="write",
                on_event=events.append,
                step_id="implementation",
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
        lifecycle = [event for event in events if event.kind == "lifecycle"]
        self.assertEqual(lifecycle[0].status, "starting")
        self.assertEqual(lifecycle[0].step_id, "implementation")
        self.assertTrue(any(event.status == "waiting_model" for event in lifecycle))
        self.assertTrue(any(event.status == "completed" for event in lifecycle))
        self.assertTrue(all(event.timestamp for event in lifecycle))

    def test_claude_native_control_request_round_trip(self) -> None:
        script_text = """#!/usr/bin/env python3
import json
import sys

initial = json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "control_request",
    "request_id": "approval-1",
    "request": {
        "subtype": "can_use_tool",
        "tool_name": "Bash",
        "input": {"command": "pytest -q"}
    }
}), flush=True)
response = json.loads(sys.stdin.readline())
allowed = response["response"]["response"]["behavior"] == "allow"
print(json.dumps({
    "type": "result",
    "session_id": "claude-native",
    "result": "approved" if allowed and initial["type"] == "user" else "failed",
    "is_error": False
}), flush=True)
"""
        with tempfile.TemporaryDirectory() as directory:
            fake_cli = Path(directory) / "claude-bridge.py"
            fake_cli.write_text(script_text, encoding="utf-8")
            adapter = ClaudeAdapter(
                AgentCommandSettings((sys.executable, str(fake_cli)), timeout=2)
            )
            requests = []
            result = adapter.run(
                "task",
                workspace=Path(directory),
                mode="write",
                on_interaction=lambda request: (
                    requests.append(request) or NativeInteractionResponse("approve")
                ),
            )

        self.assertEqual(result.final_text, "approved")
        self.assertEqual(requests[0].command, "pytest -q")

    def test_adapter_kills_a_native_cli_after_timeout(self) -> None:
        script_text = """#!/usr/bin/env python3
import sys
import time
sys.stdin.read()
time.sleep(10)
"""
        with tempfile.TemporaryDirectory() as directory:
            fake_cli = Path(directory) / "slow-agent.py"
            fake_cli.write_text(script_text, encoding="utf-8")
            adapter = ClaudeAdapter(
                AgentCommandSettings(
                    (sys.executable, str(fake_cli)),
                    timeout=0.05,
                )
            )

            with self.assertRaisesRegex(BridgeError, "超过 0.05 秒"):
                adapter.run("task", workspace=Path(directory), mode="read")

    def test_adapter_stop_interrupts_the_native_process_group(self) -> None:
        script_text = """#!/usr/bin/env python3
import sys
import time
sys.stdin.read()
time.sleep(30)
"""
        with tempfile.TemporaryDirectory() as directory:
            fake_cli = Path(directory) / "stoppable-agent.py"
            fake_cli.write_text(script_text, encoding="utf-8")
            adapter = ClaudeAdapter(
                AgentCommandSettings((sys.executable, str(fake_cli)))
            )
            waiting = threading.Event()
            failures: list[BaseException] = []
            events = []

            def on_event(event) -> None:
                events.append(event)
                if event.status == "waiting_model":
                    waiting.set()

            def run_agent() -> None:
                try:
                    adapter.run(
                        "task",
                        workspace=Path(directory),
                        mode="read",
                        on_event=on_event,
                    )
                except BaseException as exc:
                    failures.append(exc)

            worker = threading.Thread(target=run_agent)
            worker.start()
            self.assertTrue(waiting.wait(timeout=2))
            adapter.request_stop()
            worker.join(timeout=3)

        self.assertFalse(worker.is_alive())
        self.assertIsInstance(failures[0], KeyboardInterrupt)
        self.assertTrue(adapter.stop_requested)
        self.assertTrue(any(event.status == "interrupted" for event in events))


if __name__ == "__main__":
    unittest.main()
