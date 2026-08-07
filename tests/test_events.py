from __future__ import annotations

import unittest

from multiagent_cli.bridge_models import AgentEvent, EVENT_PROTOCOL


class AgentEventProtocolTests(unittest.TestCase):
    def test_v2_event_adds_status_workflow_and_time_context(self) -> None:
        event = AgentEvent(
            "Claude",
            "lifecycle",
            "waiting_model",
            status="waiting_model",
            step_id="proposal",
            elapsed_seconds=1.25,
            safe_summary="Claude · 等待模型响应",
            metadata={"attempt": 1},
        )

        payload = event.to_dict()

        self.assertEqual(payload["protocol"], EVENT_PROTOCOL)
        self.assertEqual(payload["status"], "waiting_model")
        self.assertEqual(payload["step_id"], "proposal")
        self.assertEqual(payload["elapsed_seconds"], 1.25)
        self.assertTrue(payload["timestamp"].endswith("+00:00"))
        self.assertEqual(payload["metadata"], {"attempt": 1})

    def test_safe_serialization_hides_commands_and_final_text(self) -> None:
        tool = AgentEvent(
            "Codex",
            "tool",
            "cat /private/secret.txt",
            safe_summary="Codex · 正在检查文件",
        )
        final = AgentEvent(
            "Claude",
            "text",
            "包含内部细节的完整方案",
            safe_summary="Claude · 已生成本轮最终输出",
        )

        self.assertEqual(tool.to_dict(safe=True)["text"], "Codex · 正在检查文件")
        self.assertEqual(
            final.to_dict(safe=True)["text"],
            "Claude · 已生成本轮最终输出",
        )
        self.assertEqual(tool.to_dict()["text"], "cat /private/secret.txt")

    def test_safe_serialization_hides_native_errors_and_unapproved_metadata(self) -> None:
        event = AgentEvent(
            "Codex",
            "error",
            "command failed in /private/project with token sk-example-secret",
            safe_summary="Codex · 本轮发生错误",
            metadata={
                "exit_code": 1,
                "command": "cat /private/project/.env",
                "api_key": "sk-example-secret",
            },
        )

        safe = event.to_dict(safe=True)

        self.assertEqual(safe["text"], "Codex · 本轮发生错误")
        self.assertEqual(safe["metadata"], {"exit_code": 1})
        self.assertIn("sk-example-secret", event.to_dict()["text"])

    def test_safe_serialization_preserves_bridge_warning_text(self) -> None:
        event = AgentEvent(
            "Bridge",
            "warning",
            "工作区已有未提交改动",
            safe_summary="工作区已有未提交改动",
        )

        self.assertEqual(
            event.to_dict(safe=True)["text"],
            "工作区已有未提交改动",
        )


if __name__ == "__main__":
    unittest.main()
