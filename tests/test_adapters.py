from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from multiagent_cli.adapters import (
    ClaudeAdapter,
    ClaudeEventParser,
    CodexAdapter,
    CodexEventParser,
    _ClaudePermissionBroker,
    _claude_interaction_request,
    _claude_interaction_response,
    _claude_permission_prompt_request,
    _claude_permission_prompt_response,
    _codex_interaction_request,
    _codex_interaction_response,
    _codex_turn_params,
    _codex_thread_params,
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
        completed_tool = parser.feed(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "pytest",
                        "status": "completed",
                    },
                }
            )
        )
        self.assertEqual(completed_tool[0].status, "completed")
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

    def test_claude_parser_accumulates_partial_message_deltas(self) -> None:
        parser = ClaudeEventParser()
        parser.feed(
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {"type": "message_start", "message": {"id": "msg-1"}},
                }
            )
        )
        parser.feed(
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                }
            )
        )

        first = parser.feed(
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "正在"},
                    },
                },
                ensure_ascii=False,
            )
        )
        second = parser.feed(
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "处理"},
                    },
                },
                ensure_ascii=False,
            )
        )
        completed = parser.feed(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "正在处理"}]},
                },
                ensure_ascii=False,
            )
        )

        self.assertEqual(first[0].text, "正在")
        self.assertEqual(second[0].text, "正在处理")
        self.assertEqual(completed[0].text, "正在处理")
        self.assertEqual(parser.final_text, "正在处理")


class CommandBuilderTests(unittest.TestCase):
    def test_claude_write_and_read_permissions(self) -> None:
        adapter = ClaudeAdapter(AgentCommandSettings(("claude",), model="opus"))
        workspace = Path("/tmp")

        writer = adapter.build_command(workspace=workspace, mode="write", session_id=None)
        validator = adapter.build_command(
            workspace=workspace, mode="read", session_id="session-1"
        )
        interactive_reader = adapter.build_command(
            workspace=workspace,
            mode="read",
            session_id=None,
            interactive=True,
        )
        interactive_writer = adapter.build_command(
            workspace=workspace,
            mode="write",
            session_id=None,
            interactive=True,
        )

        self.assertIn("acceptEdits", writer)
        self.assertIn("plan", validator)
        self.assertIn("plan", interactive_reader)
        self.assertIn("acceptEdits", interactive_writer)
        self.assertNotIn("manual", interactive_writer)
        self.assertIn("--include-partial-messages", writer)
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
        self.assertIn("--ephemeral", fresh)
        self.assertFalse(adapter.session_resume_enabled)
        self.assertEqual(resumed_write[0], "codex")
        self.assertIn("resume", resumed_write)
        self.assertLess(resumed_write.index("never"), resumed_write.index("exec"))
        self.assertLess(resumed_write.index("workspace-write"), resumed_write.index("exec"))
        self.assertEqual(resumed_write[resumed_write.index("-C") + 1], str(workspace))
        self.assertIn("thread-1", resumed_write)
        self.assertIn("read-only", resumed_read)
        self.assertNotEqual(resumed_write, resumed_read)

    def test_codex_routes_read_and_write_approvals_to_user(self) -> None:
        workspace = Path("/tmp")

        for mode in ("read", "write"):
            with self.subTest(mode=mode):
                params = _codex_thread_params(workspace, mode, None)
                self.assertEqual(params["approvalPolicy"], "on-request")
                self.assertEqual(params["approvalsReviewer"], "user")
                self.assertTrue(params["ephemeral"])

    def test_codex_reasoning_effort_is_forwarded_to_cli_and_app_server(self) -> None:
        adapter = CodexAdapter(
            AgentCommandSettings(("codex",), reasoning_effort="high")
        )
        command = adapter.build_command(
            workspace=Path("/tmp"), mode="write", session_id=None
        )
        params = _codex_turn_params(
            thread_id="thread-1",
            prompt="task",
            workspace=Path("/tmp"),
            mode="write",
            model="gpt-test",
            reasoning_effort="high",
        )

        self.assertIn('model_reasoning_effort="high"', command)
        self.assertEqual(params["effort"], "high")
        self.assertEqual(params["model"], "gpt-test")

    def test_codex_app_server_sends_reasoning_effort_on_turn_start(self) -> None:
        script_text = """#!/usr/bin/env python3
import json
import sys

def receive():
    return json.loads(sys.stdin.readline())

def send(payload):
    print(json.dumps(payload), flush=True)

initialize = receive()
send({"jsonrpc": "2.0", "id": initialize["id"], "result": {}})
receive()
thread_start = receive()
send({"jsonrpc": "2.0", "id": thread_start["id"], "result": {"thread": {"id": "thread-1"}}})
turn_start = receive()
if turn_start.get("params", {}).get("effort") != "xhigh":
    raise SystemExit("reasoning effort was not forwarded")
send({"jsonrpc": "2.0", "id": turn_start["id"], "result": {"turn": {"id": "turn-1"}}})
item = {"type": "agentMessage", "id": "final", "text": "完成", "phase": "final_answer"}
send({"jsonrpc": "2.0", "method": "item/started", "params": {"item": item}})
send({"jsonrpc": "2.0", "method": "item/completed", "params": {"item": item}})
send({"jsonrpc": "2.0", "method": "turn/completed", "params": {"turn": {"status": "completed"}}})
sys.stdin.read()
"""
        with tempfile.TemporaryDirectory() as directory:
            fake_cli = Path(directory) / "codex-effort-app-server.py"
            fake_cli.write_text(script_text, encoding="utf-8")
            adapter = CodexAdapter(
                AgentCommandSettings(
                    (sys.executable, str(fake_cli)),
                    reasoning_effort="xhigh",
                    timeout=2,
                )
            )
            result = adapter.run(
                "task",
                workspace=Path(directory),
                mode="read",
                on_interaction=lambda _request: NativeInteractionResponse("approve"),
            )

        self.assertEqual(result.final_text, "完成")

    def test_codex_app_server_forwards_supported_extra_args(self) -> None:
        adapter = CodexAdapter(
            AgentCommandSettings(
                ("codex",),
                extra_args=("-c", "features.test=true", "--enable=search", "--strict-config"),
            )
        )

        command = adapter.build_app_server_command()

        self.assertEqual(command[:2], ["codex", "app-server"])
        self.assertIn("features.test=true", command)
        self.assertIn("--enable=search", command)
        self.assertIn("--strict-config", command)
        self.assertEqual(command[-1], "--stdio")

    def test_codex_app_server_rejects_exec_only_extra_args(self) -> None:
        adapter = CodexAdapter(
            AgentCommandSettings(
                ("codex",),
                extra_args=("--skip-git-repo-check",),
            )
        )

        with self.assertRaisesRegex(BridgeError, "app-server"):
            adapter.build_app_server_command()

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

        prompt_request = _claude_permission_prompt_request(
            {
                "tool_name": "Write",
                "tool_use_id": "tool-1",
                "input": {"file_path": "/tmp/result.txt", "content": "ok"},
            },
            "Claude",
        )
        self.assertEqual(prompt_request.kind, "file_approval")
        self.assertEqual(prompt_request.cwd, "/tmp")
        self.assertEqual(
            [option.value for option in prompt_request.options],
            ["approve", "deny", "cancel"],
        )
        self.assertEqual(
            _claude_permission_prompt_response(
                {"input": {"file_path": "/tmp/result.txt", "content": "ok"}},
                NativeInteractionResponse("approve"),
            ),
            {
                "behavior": "allow",
                "updatedInput": {"file_path": "/tmp/result.txt", "content": "ok"},
            },
        )

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
    def test_claude_permission_prompt_mcp_round_trip(self) -> None:
        requests = []

        def approve(request):
            requests.append(request)
            return NativeInteractionResponse("approve")

        with _ClaudePermissionBroker(approve) as broker:
            bridge_args = broker.command_args()
            self.assertEqual(bridge_args[-2], "--permission-prompt-tool")
            self.assertEqual(bridge_args[-1], "mcp__multiagent_permission__approve")
            config = json.loads(bridge_args[1])["mcpServers"]["multiagent_permission"]
            environment = os.environ.copy()
            environment.update(config["env"])
            process = subprocess.Popen(
                [config["command"], *config["args"]],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                env=environment,
            )
            assert process.stdin is not None
            assert process.stdout is not None

            def exchange(message: dict) -> dict:
                process.stdin.write(json.dumps(message) + "\n")
                process.stdin.flush()
                return json.loads(process.stdout.readline())

            exchange({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            process.stdin.write(
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
                + "\n"
            )
            process.stdin.flush()
            result = exchange(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "approve",
                        "arguments": {
                            "tool_name": "Bash",
                            "tool_use_id": "tool-2",
                            "input": {"command": "pytest -q"},
                        },
                    },
                }
            )
            process.stdin.close()
            process.wait(timeout=5)
            process.stdout.close()

        permission_result = json.loads(result["result"]["content"][0]["text"])
        self.assertEqual(permission_result["behavior"], "allow")
        self.assertEqual(permission_result["updatedInput"], {"command": "pytest -q"})
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].command, "pytest -q")

    def test_codex_app_server_keeps_only_final_answer_message(self) -> None:
        script_text = """#!/usr/bin/env python3
import json
import sys

def receive():
    return json.loads(sys.stdin.readline())

def send(payload):
    print(json.dumps(payload, ensure_ascii=False), flush=True)

initialize = receive()
send({"jsonrpc": "2.0", "id": initialize["id"], "result": {}})
receive()
thread_start = receive()
send({"jsonrpc": "2.0", "id": thread_start["id"], "result": {"thread": {"id": "thread-1"}}})
turn_start = receive()
send({"jsonrpc": "2.0", "id": turn_start["id"], "result": {"turn": {"id": "turn-1"}}})

for index, text in enumerate(("我先定位代码。", "已经确认调用链。", "现在做最后检查。"), start=1):
    item_id = f"commentary-{index}"
    item = {"type": "agentMessage", "id": item_id, "text": "", "phase": "commentary"}
    send({"jsonrpc": "2.0", "method": "item/started", "params": {"item": item}})
    send({"jsonrpc": "2.0", "method": "item/agentMessage/delta", "params": {"itemId": item_id, "delta": text}})
    item["text"] = text
    send({"jsonrpc": "2.0", "method": "item/completed", "params": {"item": item}})

final_item = {"type": "agentMessage", "id": "final-1", "text": "", "phase": "final_answer"}
send({"jsonrpc": "2.0", "method": "item/started", "params": {"item": final_item}})
send({"jsonrpc": "2.0", "method": "item/agentMessage/delta", "params": {"itemId": "final-1", "delta": "修复完成，"}})
send({"jsonrpc": "2.0", "method": "item/agentMessage/delta", "params": {"itemId": "final-1", "delta": "测试通过。"}})
final_item["text"] = "修复完成，测试通过。"
send({"jsonrpc": "2.0", "method": "item/completed", "params": {"item": final_item}})
send({"jsonrpc": "2.0", "method": "turn/completed", "params": {"turn": {"status": "completed"}}})
sys.stdin.read()
"""
        with tempfile.TemporaryDirectory() as directory:
            fake_cli = Path(directory) / "codex-app-server.py"
            fake_cli.write_text(script_text, encoding="utf-8")
            adapter = CodexAdapter(
                AgentCommandSettings((sys.executable, str(fake_cli)), timeout=2)
            )
            events = []

            result = adapter.run(
                "task",
                workspace=Path(directory),
                mode="write",
                on_event=events.append,
                on_interaction=lambda _request: NativeInteractionResponse("approve"),
            )

        progress = [event.text for event in events if event.kind == "progress"]
        self.assertEqual(result.final_text, "修复完成，测试通过。")
        self.assertEqual(
            progress,
            ["修复完成，", "修复完成，测试通过。", "修复完成，测试通过。"],
        )
        self.assertFalse(any("定位" in text or "调用链" in text for text in progress))

    def test_codex_app_server_does_not_confuse_reused_request_id_with_thread_response(self) -> None:
        script_text = """#!/usr/bin/env python3
import json
import sys

def receive():
    return json.loads(sys.stdin.readline())

def send(payload):
    print(json.dumps(payload, ensure_ascii=False), flush=True)

initialize = receive()
send({"jsonrpc": "2.0", "id": initialize["id"], "result": {}})
receive()  # initialized notification
thread_start = receive()
send({"jsonrpc": "2.0", "id": thread_start["id"], "result": {"thread": {"id": "thread-1"}}})
turn_start = receive()
send({"jsonrpc": "2.0", "id": turn_start["id"], "result": {"turn": {"id": "turn-1"}}})

# The provider is allowed to choose an id that happens to equal an old
# client-request id for a server-initiated approval request.  The adapter must
# route this through the native interaction handler, not the stale thread
# response branch.
send({
    "jsonrpc": "2.0",
    "id": thread_start["id"],
    "method": "item/commandExecution/requestApproval",
    "params": {"command": "echo approved", "cwd": "."},
})
approval = receive()
if approval.get("id") != thread_start["id"]:
    raise SystemExit("approval response used the wrong id")

item = {"type": "agentMessage", "id": "final", "text": "完成", "phase": "final_answer"}
send({"jsonrpc": "2.0", "method": "item/started", "params": {"item": item}})
send({"jsonrpc": "2.0", "method": "item/completed", "params": {"item": item}})
send({"jsonrpc": "2.0", "method": "turn/completed", "params": {"turn": {"status": "completed"}}})
sys.stdin.read()
"""
        with tempfile.TemporaryDirectory() as directory:
            fake_cli = Path(directory) / "codex-request-id-reuse.py"
            fake_cli.write_text(script_text, encoding="utf-8")
            adapter = CodexAdapter(
                AgentCommandSettings((sys.executable, str(fake_cli)), timeout=2)
            )

            result = adapter.run(
                "task",
                workspace=Path(directory),
                mode="write",
                on_interaction=lambda _request: NativeInteractionResponse("approve"),
            )

        self.assertEqual(result.final_text, "完成")

    def test_codex_app_server_surfaces_thread_start_error_details(self) -> None:
        script_text = """#!/usr/bin/env python3
import json
import sys

def receive():
    return json.loads(sys.stdin.readline())

def send(payload):
    print(json.dumps(payload), flush=True)

initialize = receive()
send({"jsonrpc": "2.0", "id": initialize["id"], "result": {}})
receive()
thread_start = receive()
send({
    "jsonrpc": "2.0",
    "id": thread_start["id"],
    "error": {"code": -32000, "message": "workspace is not trusted"},
})
"""
        with tempfile.TemporaryDirectory() as directory:
            fake_cli = Path(directory) / "codex-thread-error.py"
            fake_cli.write_text(script_text, encoding="utf-8")
            adapter = CodexAdapter(
                AgentCommandSettings((sys.executable, str(fake_cli)), timeout=2)
            )

            with self.assertRaisesRegex(
                BridgeError,
                r"thread 启动失败：workspace is not trusted（code=-32000）",
            ):
                adapter.run(
                    "task",
                    workspace=Path(directory),
                    mode="write",
                    on_interaction=lambda _request: NativeInteractionResponse("approve"),
                )

    def test_codex_app_server_activity_resets_inactivity_timeout(self) -> None:
        script_text = """#!/usr/bin/env python3
import json
import sys
import time

def receive():
    return json.loads(sys.stdin.readline())

def send(payload):
    print(json.dumps(payload), flush=True)

initialize = receive()
send({"jsonrpc": "2.0", "id": initialize["id"], "result": {}})
receive()
thread_start = receive()
send({"jsonrpc": "2.0", "id": thread_start["id"], "result": {"thread": {"id": "active-thread"}}})
turn_start = receive()
send({"jsonrpc": "2.0", "id": turn_start["id"], "result": {"turn": {"id": "active-turn"}}})

for index in range(5):
    send({"jsonrpc": "2.0", "method": "heartbeat", "params": {"index": index}})
    time.sleep(0.07)

item = {"type": "agentMessage", "id": "final", "text": "done", "phase": "final_answer"}
send({"jsonrpc": "2.0", "method": "item/started", "params": {"item": item}})
send({"jsonrpc": "2.0", "method": "item/completed", "params": {"item": item}})
send({"jsonrpc": "2.0", "method": "turn/completed", "params": {"turn": {"status": "completed"}}})
sys.stdin.read()
"""
        with tempfile.TemporaryDirectory() as directory:
            fake_cli = Path(directory) / "active-codex-app-server.py"
            fake_cli.write_text(script_text, encoding="utf-8")
            adapter = CodexAdapter(
                AgentCommandSettings((sys.executable, str(fake_cli)), timeout=0.12)
            )

            result = adapter.run(
                "task",
                workspace=Path(directory),
                mode="write",
                on_interaction=lambda _request: NativeInteractionResponse("approve"),
            )

        self.assertEqual(result.final_text, "done")
        self.assertGreater(result.duration_seconds, 0.28)

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

    def test_active_claude_output_resets_inactivity_timeout(self) -> None:
        script_text = """#!/usr/bin/env python3
import json
import sys
import time

sys.stdin.read()
for index in range(4):
    print(json.dumps({"type": "system", "tick": index}), flush=True)
    time.sleep(0.07)
print(json.dumps({"type": "result", "session_id": "active", "result": "done", "is_error": False}), flush=True)
"""
        with tempfile.TemporaryDirectory() as directory:
            fake_cli = Path(directory) / "active-agent.py"
            fake_cli.write_text(script_text, encoding="utf-8")
            adapter = ClaudeAdapter(
                AgentCommandSettings(
                    (sys.executable, str(fake_cli)),
                    timeout=0.12,
                )
            )

            result = adapter.run("task", workspace=Path(directory), mode="read")

        self.assertEqual(result.final_text, "done")
        self.assertGreater(result.duration_seconds, 0.24)

    def test_claude_mcp_permission_wait_pauses_inactivity_timeout(self) -> None:
        script_text = """#!/usr/bin/env python3
import json
import sys
import time
from pathlib import Path

json.loads(sys.stdin.readline())
config = json.loads(sys.argv[sys.argv.index("--mcp-config") + 1])
directory = Path(config["mcpServers"]["multiagent_permission"]["env"]["MULTIAGENT_CLAUDE_PERMISSION_DIR"])
request = directory / "request-test.json"
pending = directory / ".request-test.tmp"
pending.write_text(json.dumps({"tool_name": "Bash", "tool_use_id": "tool-1", "input": {"command": "pytest -q"}}), encoding="utf-8")
pending.replace(request)
response = directory / "response-test.json"
while not response.exists():
    time.sleep(0.01)
print(json.dumps({"type": "result", "session_id": "approved", "result": "done", "is_error": False}), flush=True)
"""
        with tempfile.TemporaryDirectory() as directory:
            fake_cli = Path(directory) / "permission-wait.py"
            fake_cli.write_text(script_text, encoding="utf-8")
            adapter = ClaudeAdapter(
                AgentCommandSettings(
                    (sys.executable, str(fake_cli)),
                    timeout=0.15,
                )
            )

            approval_started = threading.Event()
            approval_duration = []

            def delayed_approval(_request):
                approval_started.set()
                started = time.monotonic()
                time.sleep(0.25)
                approval_duration.append(time.monotonic() - started)
                return NativeInteractionResponse("approve")

            result = adapter.run(
                "task",
                workspace=Path(directory),
                mode="write",
                on_interaction=delayed_approval,
            )

        self.assertEqual(result.final_text, "done")
        self.assertTrue(approval_started.is_set())
        self.assertGreater(approval_duration[0], 0.2)
        self.assertGreater(result.duration_seconds, 0.2)

    def test_claude_skips_and_quarantines_protocol_incompatible_model(self) -> None:
        script_text = """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

sys.stdin.read()
model = sys.argv[sys.argv.index("--model") + 1]
calls = Path(sys.argv[1]).with_name("calls.txt")
with calls.open("a", encoding="utf-8") as handle:
    handle.write(model + "\\n")
if model == "bad-model":
    print(json.dumps({
        "type": "result",
        "result": "API Error: 400 request validation error: cache_control Extra inputs are not permitted",
        "is_error": True,
    }), flush=True)
    # Claude Code reports provider validation failures in a result event but
    # may still exit 0; the adapter must honor is_error, not only exit status.
    raise SystemExit(0)
print(json.dumps({"type": "result", "session_id": model, "result": "done", "is_error": False}), flush=True)
"""
        with tempfile.TemporaryDirectory() as directory:
            fake_cli = Path(directory) / "incompatible-agent.py"
            fake_cli.write_text(script_text, encoding="utf-8")
            adapter = ClaudeAdapter(
                AgentCommandSettings(
                    (sys.executable, str(fake_cli)),
                    models=("bad-model", "good-model"),
                    timeout=2,
                )
            )
            events = []

            first = adapter.run(
                "task",
                workspace=Path(directory),
                mode="read",
                on_event=events.append,
            )
            second = adapter.run(
                "task again",
                workspace=Path(directory),
                mode="read",
            )
            calls = (Path(directory) / "calls.txt").read_text(encoding="utf-8").splitlines()

        self.assertEqual(first.final_text, "done")
        self.assertEqual(second.final_text, "done")
        self.assertEqual(calls, ["bad-model", "good-model", "good-model"])
        self.assertTrue(
            any(
                event.kind == "warning" and "接口不兼容" in event.text
                for event in events
            )
        )

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
        self.assertIsNone(codex_result.session_id)
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

    def test_claude_native_result_closes_interactive_stdin(self) -> None:
        script_text = """#!/usr/bin/env python3
import json
import sys

json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "result",
    "session_id": "claude-native",
    "result": "finished",
    "is_error": False
}), flush=True)
sys.stdin.read()
"""
        with tempfile.TemporaryDirectory() as directory:
            fake_cli = Path(directory) / "claude-waits-for-eof.py"
            fake_cli.write_text(script_text, encoding="utf-8")
            adapter = ClaudeAdapter(
                AgentCommandSettings((sys.executable, str(fake_cli)), timeout=1)
            )

            result = adapter.run(
                "task",
                workspace=Path(directory),
                mode="read",
                on_interaction=lambda _request: NativeInteractionResponse("approve"),
            )

        self.assertEqual(result.final_text, "finished")

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
