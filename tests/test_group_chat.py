from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from multiagent_cli.bridge_models import (
    AgentCommandSettings,
    AgentRunResult,
    BridgeError,
    BridgeSettings,
)
from multiagent_cli.group_chat import (
    GroupChatEngine,
    resolve_directive,
    resolve_mentions,
)


class FakeAdapter:
    def __init__(
        self,
        name: str,
        results: list[AgentRunResult],
        *,
        session_resume_enabled: bool = True,
    ) -> None:
        self.display_name = name
        self.results = iter(results)
        self.calls: list[dict[str, Any]] = []
        self.session_resume_enabled = session_resume_enabled

    def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
        self.calls.append({"prompt": prompt, **kwargs})
        return next(self.results)


def settings(workspace: Path) -> BridgeSettings:
    return BridgeSettings(
        workspace=workspace,
        executor="claude",
        review_rounds=1,
        planning_collaboration=True,
        consensus=False,
        max_consensus_rounds=3,
        plan_approval=True,
        max_plan_revisions=2,
        final_review=True,
        verification_commands=(),
        claude=AgentCommandSettings(("claude",)),
        codex=AgentCommandSettings(("codex",)),
    )


class GroupChatTests(unittest.TestCase):
    def test_stateless_multi_model_turns_receive_full_history_and_drop_old_session(self) -> None:
        first_adapter = FakeAdapter(
            "Claude",
            [AgentRunResult("Claude", "第一轮回答", session_id="old-session")],
        )
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            first_engine = GroupChatEngine(
                settings(workspace),
                {"claude": first_adapter},  # type: ignore[arg-type]
            )
            first_engine.ask("@Claude 第一轮用户问题")

            failed_adapter = FakeAdapter(
                "Claude",
                [],
                session_resume_enabled=False,
            )
            failed_engine = GroupChatEngine(
                settings(workspace),
                {"claude": failed_adapter},  # type: ignore[arg-type]
                first_engine.to_dict(),
            )
            failed_turn = failed_engine.ask("@Claude 这轮模拟失败")
            failed_state = failed_engine.to_dict()

            stateless_adapter = FakeAdapter(
                "Claude",
                [AgentRunResult("Claude", "第二轮回答", session_id=None)],
                session_resume_enabled=False,
            )
            second_engine = GroupChatEngine(
                settings(workspace),
                {"claude": stateless_adapter},  # type: ignore[arg-type]
                first_engine.to_dict(),
            )
            second_engine.ask("@Claude 第二轮用户问题")
            second_state = second_engine.to_dict()

            resumed_adapter = FakeAdapter(
                "Claude",
                [AgentRunResult("Claude", "第三轮回答", session_id="new-session")],
            )
            third_engine = GroupChatEngine(
                settings(workspace),
                {"claude": resumed_adapter},  # type: ignore[arg-type]
                second_state,
            )
            third_engine.ask("@Claude 第三轮用户问题")

        self.assertIsNone(stateless_adapter.calls[0]["session_id"])
        self.assertIn("claude", failed_turn.errors)
        self.assertIsNone(failed_state["sessions"]["claude"])
        self.assertEqual(failed_state["cursors"]["claude"], 0)
        self.assertIn("第一轮用户问题", stateless_adapter.calls[0]["prompt"])
        self.assertIn("第一轮回答", stateless_adapter.calls[0]["prompt"])
        self.assertIsNone(second_state["sessions"]["claude"])
        self.assertEqual(second_state["cursors"]["claude"], 0)
        self.assertIsNone(resumed_adapter.calls[0]["session_id"])
        self.assertIn("第一轮用户问题", resumed_adapter.calls[0]["prompt"])
        self.assertIn("第二轮用户问题", resumed_adapter.calls[0]["prompt"])

    def test_group_chat_uses_its_own_identities_without_workflow_identity(self) -> None:
        claude = FakeAdapter(
            "Claude",
            [AgentRunResult("Claude", "A1", session_id="claude-1")],
        )
        codex = FakeAdapter(
            "Codex",
            [AgentRunResult("Codex", "B1", session_id="codex-1")],
        )
        with tempfile.TemporaryDirectory() as directory:
            resolved = replace(
                settings(Path(directory)),
                agent_a_identity="仅限共识实施的 Claude 身份",
                agent_b_identity="仅限共识实施的 Codex 身份",
                group_chat_agent_a_identity="Claude 的自然群聊身份",
                group_chat_agent_b_identity="Codex 的工程群聊身份",
            )
            GroupChatEngine(
                resolved,
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            ).ask("分别回答")

        self.assertIn("Claude 的自然群聊身份", claude.calls[0]["prompt"])
        self.assertNotIn("仅限共识实施", claude.calls[0]["prompt"])
        self.assertIn("Codex 的工程群聊身份", codex.calls[0]["prompt"])
        self.assertNotIn("仅限共识实施", codex.calls[0]["prompt"])

    def test_mentions_route_to_one_or_all_agents(self) -> None:
        self.assertEqual(resolve_mentions("请都回答"), ("claude", "codex"))
        self.assertEqual(resolve_mentions("@Claude 请审核"), ("claude",))
        self.assertEqual(resolve_mentions("@codex @claude 比较"), ("claude", "codex"))
        self.assertEqual(resolve_mentions("@all 汇总"), ("claude", "codex"))
        with self.assertRaisesRegex(BridgeError, "未识别的群聊成员"):
            resolve_mentions("@Gemini 请回答")

    def test_execution_requires_an_explicit_command_prefix(self) -> None:
        self.assertEqual(
            resolve_directive("@Claude 执行：修复测试"),
            (("claude",), "execute"),
        )
        self.assertEqual(
            resolve_directive("/exec @all 修复测试"),
            (("claude", "codex"), "execute"),
        )
        self.assertEqual(
            resolve_directive("@Claude 如何执行这个方案"),
            (("claude",), "discuss"),
        )
        self.assertEqual(
            resolve_directive("请 @Codex，执行：修复测试"),
            (("codex",), "execute"),
        )
        self.assertEqual(
            resolve_directive("让 @all 执行任务：分别验证"),
            (("claude", "codex"), "execute"),
        )

    def test_unmentioned_agent_receives_missed_messages_next_time(self) -> None:
        claude = FakeAdapter(
            "Claude",
            [AgentRunResult("Claude", "Claude 的意见", session_id="claude-1")],
        )
        codex = FakeAdapter(
            "Codex",
            [AgentRunResult("Codex", "Codex 的复核", session_id="codex-1")],
        )
        with tempfile.TemporaryDirectory() as directory:
            engine = GroupChatEngine(
                settings(Path(directory)),
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            )
            first = engine.ask("@Claude 给出你的方案")
            second = engine.ask("@Codex 审核 Claude 刚才的回答")

        self.assertEqual(first.recipients, ("claude",))
        self.assertEqual(second.recipients, ("codex",))
        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(len(codex.calls), 1)
        self.assertIn("@Claude 给出你的方案", codex.calls[0]["prompt"])
        self.assertIn("Claude 的意见", codex.calls[0]["prompt"])
        self.assertIn("@Codex 审核 Claude 刚才的回答", codex.calls[0]["prompt"])

    def test_broadcast_answers_in_parallel_sessions_and_persists_context(self) -> None:
        claude = FakeAdapter(
            "Claude",
            [
                AgentRunResult("Claude", "A1", session_id="claude-1"),
                AgentRunResult("Claude", "A2", session_id="claude-1"),
            ],
        )
        codex = FakeAdapter(
            "Codex",
            [
                AgentRunResult("Codex", "B1", session_id="codex-1"),
                AgentRunResult("Codex", "B2", session_id="codex-1"),
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            resolved = settings(Path(directory))
            engine = GroupChatEngine(
                resolved,
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            )
            engine.ask("分别给出方案")
            restored = GroupChatEngine(
                resolved,
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
                engine.to_dict(),
            )
            restored.ask("继续比较")
            state = restored.to_dict()

        self.assertEqual(claude.calls[1]["session_id"], "claude-1")
        self.assertEqual(codex.calls[1]["session_id"], "codex-1")
        self.assertIn("B1", claude.calls[1]["prompt"])
        self.assertIn("A1", codex.calls[1]["prompt"])
        self.assertEqual([item["sender"] for item in state["messages"]], [
            "user", "claude", "codex", "user", "claude", "codex"
        ])
        self.assertEqual(state["turn"], 2)

    def test_unmentioned_messages_use_configured_default_agent(self) -> None:
        claude = FakeAdapter(
            "Claude",
            [AgentRunResult("Claude", "A1", session_id="claude-1")],
        )
        codex = FakeAdapter("Codex", [])
        with tempfile.TemporaryDirectory() as directory:
            resolved = replace(
                settings(Path(directory)),
                group_chat_default_agent="claude",
            )
            turn = GroupChatEngine(
                resolved,
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            ).ask("给出初步判断")

        self.assertEqual(turn.recipients, ("claude",))
        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(codex.calls, [])

    def test_execution_can_be_disabled_for_group_chat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            resolved = replace(
                settings(Path(directory)),
                group_chat_execution=False,
            )
            engine = GroupChatEngine(
                resolved,
                {
                    "claude": FakeAdapter("Claude", []),
                    "codex": FakeAdapter("Codex", []),
                },  # type: ignore[arg-type]
            )
            with self.assertRaisesRegex(BridgeError, "群聊执行已在设置中关闭"):
                engine.ask("@Claude 执行：修改文件")

    def test_single_agent_executes_directly_in_target_workspace(self) -> None:
        class WritingAdapter(FakeAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                workspace = Path(kwargs["workspace"])
                (workspace / f"{self.display_name.lower()}.txt").write_text(
                    self.display_name,
                    encoding="utf-8",
                )
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            repository.mkdir()
            claude = WritingAdapter(
                "Claude",
                [
                    AgentRunResult("Claude", "Claude 已执行", session_id="claude-write"),
                    AgentRunResult("Claude", "Claude 继续执行", session_id="claude-write"),
                ],
            )
            codex = WritingAdapter(
                "Codex",
                [],
            )
            resolved = settings(repository)
            engine = GroupChatEngine(
                resolved,
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            )
            first = engine.ask("@Claude 执行：添加结果文件")
            state = engine.to_dict()
            restored = GroupChatEngine(
                resolved,
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
                state,
            )
            second = restored.ask("@Claude 执行：继续检查结果")

            self.assertEqual(first.action, "execute")
            self.assertEqual(second.action, "execute")
            self.assertEqual(Path(first.workspaces["claude"]), repository)
            self.assertTrue((repository / "claude.txt").is_file())
            self.assertFalse((repository / "codex.txt").exists())
            self.assertEqual(claude.calls[0]["mode"], "write")
            self.assertEqual(claude.calls[1]["session_id"], "claude-write")
            self.assertEqual(
                Path(claude.calls[1]["workspace"]),
                repository,
            )
            self.assertEqual(
                state["messages"][-1]["action"],
                "execute",
            )

    def test_all_agent_execution_is_rejected_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = GroupChatEngine(
                settings(Path(directory)),
                {
                    "claude": FakeAdapter("Claude", []),
                    "codex": FakeAdapter("Codex", []),
                },  # type: ignore[arg-type]
            )
            with self.assertRaisesRegex(BridgeError, "一次只能由一个 Agent 执行"):
                engine.ask("@all 执行：分别修改代码")

        self.assertEqual(engine.to_dict()["messages"], [])


if __name__ == "__main__":
    unittest.main()
