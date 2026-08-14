from __future__ import annotations

import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from multiagent_cli.bridge_models import (
    AgentCommandSettings,
    AgentRunResult,
    AgentTimeoutError,
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


def _git_repository(root: Path) -> Path:
    """Initialise a committed repository so change capture has a real baseline."""

    repository = root / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repository, check=True
    )
    (repository / "tracked.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)
    return repository


def settings(workspace: Path) -> BridgeSettings:
    return BridgeSettings(
        workspace=workspace,
        claude=AgentCommandSettings(("claude",)),
        codex=AgentCommandSettings(("codex",)),
    )


class GroupChatTests(unittest.TestCase):
    def test_timeout_is_persisted_as_the_agents_failed_reply(self) -> None:
        class TimeoutAdapter(FakeAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                raise AgentTimeoutError("Claude CLI 超过 1 秒未完成，已终止")

        with tempfile.TemporaryDirectory() as directory:
            claude = TimeoutAdapter("Claude Code", [])
            codex = FakeAdapter(
                "Codex",
                [AgentRunResult("Codex", "Codex 正常回复", session_id="cb")],
            )
            engine = GroupChatEngine(
                settings(Path(directory)),
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            )

            turn = engine.ask("@all 分别检查这个问题")
            state = engine.to_dict()

            self.assertEqual([message["sender"] for message in state["messages"]], [
                "user",
                "claude",
                "codex",
            ])
            failed = state["messages"][1]
            self.assertEqual(failed["role"], "assistant")
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["failure_reason"], "timeout")
            self.assertIn("响应超时", failed["content"])
            self.assertIn("Claude Code", failed["content"])
            self.assertEqual(failed["reply_to"], state["messages"][0]["id"])
            self.assertEqual(turn.responses[0].final_text, "Codex 正常回复")
            self.assertIn("claude", turn.errors)

            restored = GroupChatEngine(
                settings(Path(directory)),
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
                state,
            ).to_dict()
            self.assertEqual(restored["messages"][1]["failure_reason"], "timeout")
            self.assertIn("响应超时", restored["messages"][1]["content"])

    def test_single_agent_questions_do_not_block_another_agent(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class SlowAdapter(FakeAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                started.set()
                release.wait(2)
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            claude = SlowAdapter(
                "Claude",
                [AgentRunResult("Claude", "慢回答", session_id="ca")],
            )
            codex = FakeAdapter(
                "Codex",
                [AgentRunResult("Codex", "即时回答", session_id="cb")],
            )
            engine = GroupChatEngine(
                settings(Path(directory)),
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            )
            first = engine.reserve("@Claude 慢慢分析")
            first_thread = threading.Thread(
                target=lambda: engine.ask("@Claude 慢慢分析", reservation=first),
                daemon=True,
            )
            first_thread.start()
            self.assertTrue(started.wait(1))
            second = engine.ask("@Codex 先回答一个问题")
            release.set()
            first_thread.join(2)

            self.assertEqual(second.responses[0].final_text, "即时回答")
        self.assertEqual(len(codex.calls), 1)

    def test_only_one_worktree_is_created_and_merged_after_main_writer(self) -> None:
        repository = None
        started = threading.Event()
        release = threading.Event()

        class WritingAdapter(FakeAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                workspace = Path(kwargs["workspace"])
                (workspace / f"{self.display_name.lower()}.txt").write_text(
                    self.display_name,
                    encoding="utf-8",
                )
                if self.display_name == "Claude":
                    started.set()
                    release.wait(2)
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            repository = _git_repository(Path(directory))
            claude = WritingAdapter(
                "Claude",
                [AgentRunResult("Claude", "主工作区完成", session_id="ca")],
            )
            codex = WritingAdapter(
                "Codex",
                [AgentRunResult("Codex", "Worktree 完成", session_id="cb")],
            )
            engine = GroupChatEngine(
                settings(repository),
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            )
            first = engine.reserve("@Claude 执行：写入主工作区")
            first_thread = threading.Thread(
                target=lambda: engine.ask("@Claude 执行：写入主工作区", reservation=first),
                daemon=True,
            )
            first_thread.start()
            self.assertTrue(started.wait(1))
            second = engine.reserve("@Codex 执行：写入隔离区")
            second_thread = threading.Thread(
                target=lambda: engine.ask("@Codex 执行：写入隔离区", reservation=second),
                daemon=True,
            )
            second_thread.start()
            time.sleep(0.1)
            self.assertTrue(second_thread.is_alive())
            deadline = time.monotonic() + 1
            while not codex.calls and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(codex.calls)
            self.assertNotEqual(Path(codex.calls[0]["workspace"]), repository)
            self.assertTrue(Path(codex.calls[0]["workspace"]).is_dir())
            release.set()
            first_thread.join(2)
            second_thread.join(2)
            self.assertFalse(second_thread.is_alive())
            self.assertTrue((repository / "claude.txt").is_file())
            self.assertTrue((repository / "codex.txt").is_file())
            status = subprocess.run(
                ["git", "status", "--porcelain=v1"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn("?? claude.txt", status)
            self.assertIn("?? codex.txt", status)
            self.assertNotIn("A  codex.txt", status)

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

    def test_group_chat_uses_custom_agent_identities(self) -> None:
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
                group_chat_agent_a_identity="Claude 的自然群聊身份",
                group_chat_agent_b_identity="Codex 的工程群聊身份",
            )
            GroupChatEngine(
                resolved,
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            ).ask("分别回答")

        self.assertIn("Claude 的自然群聊身份", claude.calls[0]["prompt"])
        self.assertIn("Codex 的工程群聊身份", codex.calls[0]["prompt"])

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

    def test_single_mentioned_agent_gets_write_access_without_an_exec_prefix(
        self,
    ) -> None:
        """Write access is no longer gated on the /exec prefix: a solo recipient
        holds the workspace in write mode and decides from the message whether
        editing is warranted."""

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            repository.mkdir()
            claude = FakeAdapter(
                "Claude",
                [AgentRunResult("Claude", "已修改", session_id="ca")],
            )
            engine = GroupChatEngine(
                settings(repository),
                {
                    "claude": claude,
                    "codex": FakeAdapter("Codex", []),
                },  # type: ignore[arg-type]
            )
            turn = engine.ask("@Claude 把这个函数改名")
            prompt = claude.calls[0]["prompt"]

            self.assertEqual(turn.action, "discuss")
            self.assertEqual(claude.calls[0]["mode"], "write")
            self.assertEqual(Path(turn.workspaces["claude"]), repository)
            self.assertIn("自行判断是否需要修改文件", prompt)
            self.assertIn("不要提交 Git", prompt)

    def test_broadcast_turns_stay_read_only_so_only_one_agent_can_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            repository.mkdir()
            claude = FakeAdapter(
                "Claude", [AgentRunResult("Claude", "只读回答", session_id="ca")]
            )
            codex = FakeAdapter(
                "Codex", [AgentRunResult("Codex", "只读回答", session_id="cb")]
            )
            engine = GroupChatEngine(
                settings(repository),
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            )
            turn = engine.ask("@all 帮我改一下这个模块")

            self.assertEqual(turn.workspaces, {})
            self.assertEqual(claude.calls[0]["mode"], "read")
            self.assertEqual(codex.calls[0]["mode"], "read")
            self.assertIn("本轮是只读讨论", claude.calls[0]["prompt"])

    def test_disabling_execution_keeps_a_solo_agent_read_only(self) -> None:
        """The existing toggle still governs write access; with it off even a
        single recipient must not receive the workspace in write mode."""

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            repository.mkdir()
            claude = FakeAdapter(
                "Claude", [AgentRunResult("Claude", "只读回答", session_id="ca")]
            )
            resolved = replace(settings(repository), group_chat_execution=False)
            engine = GroupChatEngine(
                resolved,
                {
                    "claude": claude,
                    "codex": FakeAdapter("Codex", []),
                },  # type: ignore[arg-type]
            )
            turn = engine.ask("@Claude 改一下这个文件")

            self.assertEqual(turn.workspaces, {})
            self.assertEqual(claude.calls[0]["mode"], "read")
            self.assertIn("本轮是只读讨论", claude.calls[0]["prompt"])

    def test_autonomous_writes_are_captured_on_a_discussion_turn(self) -> None:
        """Baseline capture must run on every turn, not just /exec ones -
        otherwise an autonomously-writing agent's edits never surface in the UI."""

        class WritingAdapter(FakeAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                Path(kwargs["workspace"], "created.txt").write_text(
                    "written autonomously",
                    encoding="utf-8",
                )
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            repository = _git_repository(Path(directory))
            claude = WritingAdapter(
                "Claude",
                [AgentRunResult("Claude", "顺手改好了", session_id="ca")],
            )
            engine = GroupChatEngine(
                settings(repository),
                {
                    "claude": claude,
                    "codex": FakeAdapter("Codex", []),
                },  # type: ignore[arg-type]
            )
            turn = engine.ask("@Claude 这个拼写错了")
            message = engine.to_dict()["messages"][-1]

            self.assertEqual(turn.action, "discuss")
            self.assertTrue((repository / "created.txt").is_file())
            self.assertIsNotNone(turn.changes)
            self.assertTrue(turn.changes["available"])
            self.assertEqual(
                [item["path"] for item in turn.changes["files"]],
                ["created.txt"],
            )
            self.assertIsNotNone(message.get("changes"))
            self.assertEqual(Path(message["workspace"]), repository)

    def test_a_discussion_turn_without_writes_carries_no_change_block(self) -> None:
        """Because the baseline is now captured on every turn the summary is
        always produced; attaching it unconditionally would put an empty change
        block under every ordinary answer."""

        with tempfile.TemporaryDirectory() as directory:
            repository = _git_repository(Path(directory))
            claude = FakeAdapter(
                "Claude", [AgentRunResult("Claude", "只是回答问题", session_id="ca")]
            )
            engine = GroupChatEngine(
                settings(repository),
                {
                    "claude": claude,
                    "codex": FakeAdapter("Codex", []),
                },  # type: ignore[arg-type]
            )
            turn = engine.ask("@Claude 这个函数是做什么的")
            message = engine.to_dict()["messages"][-1]

            self.assertEqual(claude.calls[0]["mode"], "write")
            self.assertIsNone(turn.changes)
            self.assertNotIn("changes", message)

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

    def test_edit_and_retry_append_auditable_messages_without_mutating_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = GroupChatEngine(
                settings(Path(directory)),
                {
                    "claude": FakeAdapter("Claude", [AgentRunResult("Claude", "原答", session_id="c1"), AgentRunResult("Claude", "新答", session_id="c2")]),
                    "codex": FakeAdapter("Codex", []),
                },  # type: ignore[arg-type]
            )
            first = engine.ask("@Claude 原问题")
            original = engine.find_message(first.user_message_id)
            self.assertIsNotNone(original)
            reservation = engine.reserve("@Claude 修订问题")
            edited = engine.ask(
                "@Claude 修订问题",
                reservation=reservation,
                edited_from=first.user_message_id,
            )
            messages = engine.to_dict()["messages"]
            self.assertEqual(messages[0]["content"], "@Claude 原问题")
            self.assertEqual(messages[2]["edited_from"], first.user_message_id)
            self.assertEqual(messages[3]["content"], "新答")
            self.assertEqual(edited.responses[0].final_text, "新答")


if __name__ == "__main__":
    unittest.main()
