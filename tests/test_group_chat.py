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
    AgentModelCompatibilityError,
    AgentRunResult,
    AgentTimeoutError,
    BridgeError,
    BridgeSettings,
    ContextCompactionSettings,
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
        ["git", "config", "core.autocrlf", "false"],
        cwd=repository,
        check=True,
    )
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

    def test_model_protocol_failure_is_explained_in_failed_reply(self) -> None:
        class IncompatibleAdapter(FakeAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                raise AgentModelCompatibilityError(
                    "Claude Code",
                    "kimi-k3",
                    "模型接口拒绝了 cache_control",
                )

        with tempfile.TemporaryDirectory() as directory:
            claude = IncompatibleAdapter("Claude Code", [])
            codex = FakeAdapter("Codex", [])
            engine = GroupChatEngine(
                settings(Path(directory)),
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            )

            engine.ask("@Claude 检查一下")
            failed = engine.to_dict()["messages"][1]

        self.assertEqual(failed["failure_reason"], "model_incompatible")
        self.assertIn("模型不兼容", failed["content"])
        self.assertIn("kimi-k3", failed["content"])

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

    def test_fast_agent_reply_is_persisted_before_slow_peer_finishes(self) -> None:
        claude_started = threading.Event()
        release_claude = threading.Event()
        partial_states: list[dict[str, Any]] = []

        class SlowClaude(FakeAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                claude_started.set()
                release_claude.wait(2)
                return next(self.results)

        class FastCodex(FakeAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            engine = GroupChatEngine(
                settings(Path(directory)),
                {
                    "claude": SlowClaude(
                        "Claude", [AgentRunResult("Claude", "慢回复")]
                    ),
                    "codex": FastCodex(
                        "Codex", [AgentRunResult("Codex", "快回复")]
                    ),
                },  # type: ignore[arg-type]
            )
            worker = threading.Thread(
                target=lambda: engine.ask(
                    "@all 一起回答",
                    on_state=lambda state: partial_states.append(state),
                ),
                daemon=True,
            )
            worker.start()
            self.assertTrue(claude_started.wait(1))
            deadline = time.monotonic() + 1
            while not any(
                any(
                    item.get("sender") == "codex"
                    and item.get("content") == "快回复"
                    for item in state.get("messages", [])
                )
                and not any(
                    item.get("sender") == "claude"
                    and item.get("content") == "慢回复"
                    for item in state.get("messages", [])
                )
                for state in partial_states
            ) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(
                any(
                    any(
                        item.get("sender") == "codex"
                        and item.get("content") == "快回复"
                        for item in state.get("messages", [])
                    )
                    and not any(
                        item.get("sender") == "claude"
                        and item.get("content") == "慢回复"
                        for item in state.get("messages", [])
                    )
                    for state in partial_states
                )
            )
            release_claude.set()
            worker.join(2)
            messages = engine.to_dict()["messages"]
            self.assertEqual(
                [item["content"] for item in messages if item["role"] == "assistant"],
                ["慢回复", "快回复"],
            )

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
                replace(settings(repository), worktree=True),
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

    def test_dual_agent_execution_creates_reviewable_candidates_without_touching_main(self) -> None:
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
            repository = _git_repository(Path(directory))
            claude = WritingAdapter(
                "Claude", [AgentRunResult("Claude", "方案 A", session_id="ca")]
            )
            codex = WritingAdapter(
                "Codex", [AgentRunResult("Codex", "方案 B", session_id="cb")]
            )
            engine = GroupChatEngine(
                settings(repository),
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            )

            turn = engine.ask("@all 执行：分别实现同一个功能")
            comparison = engine.to_dict()["comparison"]

            self.assertEqual(comparison["status"], "review")
            self.assertNotEqual(
                claude.calls[0]["workspace"],
                codex.calls[0]["workspace"],
            )
            self.assertNotEqual(Path(claude.calls[0]["workspace"]), repository)
            self.assertFalse((repository / "claude.txt").exists())
            self.assertFalse((repository / "codex.txt").exists())
            self.assertEqual(comparison["candidates"]["claude"]["status"], "ready")
            self.assertEqual(comparison["candidates"]["codex"]["status"], "ready")
            self.assertEqual(len(turn.responses), 2)

            applied = engine.apply_comparison("claude")
            applied_state = engine.to_dict()
            claude_reply = next(
                message
                for message in applied_state["messages"]
                if message.get("sender") == "claude"
            )

            self.assertEqual(applied["status"], "applied")
            self.assertEqual(applied["selected_agent"], "claude")
            self.assertEqual(
                claude_reply["changes"]["rollback"]["status"],
                "available",
            )
            self.assertTrue((repository / "claude.txt").is_file())
            self.assertFalse((repository / "codex.txt").exists())
            self.assertFalse(Path(claude.calls[0]["workspace"]).exists())
            self.assertFalse(Path(codex.calls[0]["workspace"]).exists())

    def test_comparison_can_preview_each_candidate_in_main_and_restore_on_discard(self) -> None:
        class WritingAdapter(FakeAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                (Path(kwargs["workspace"]) / f"{self.display_name.lower()}.txt").write_text(
                    self.display_name,
                    encoding="utf-8",
                )
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            repository = _git_repository(Path(directory))
            engine = GroupChatEngine(
                settings(repository),
                {
                    "claude": WritingAdapter("Claude", [AgentRunResult("Claude", "A")]),
                    "codex": WritingAdapter("Codex", [AgentRunResult("Codex", "B")]),
                },  # type: ignore[arg-type]
            )
            engine.ask("@all 执行：分别实现")

            preview = engine.preview_comparison("claude")
            self.assertEqual(preview["status"], "previewing")
            self.assertEqual(preview["preview"]["active_agent"], "claude")
            self.assertTrue((repository / "claude.txt").is_file())
            self.assertFalse((repository / "codex.txt").exists())

            preview = engine.preview_comparison("codex")
            self.assertEqual(preview["preview"]["active_agent"], "codex")
            self.assertFalse((repository / "claude.txt").exists())
            self.assertTrue((repository / "codex.txt").is_file())

            discarded = engine.discard_comparison()
            self.assertEqual(discarded["status"], "discarded")
            self.assertFalse((repository / "claude.txt").exists())
            self.assertFalse((repository / "codex.txt").exists())

    def test_interrupting_comparison_cleans_preview_and_marks_pending_candidates(self) -> None:
        class WritingAdapter(FakeAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                (Path(kwargs["workspace"]) / f"{self.display_name.lower()}.txt").write_text(
                    self.display_name,
                    encoding="utf-8",
                )
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            repository = _git_repository(Path(directory))
            engine = GroupChatEngine(
                settings(repository),
                {
                    "claude": WritingAdapter("Claude", [AgentRunResult("Claude", "A")]),
                    "codex": WritingAdapter("Codex", [AgentRunResult("Codex", "B")]),
                },  # type: ignore[arg-type]
            )
            engine.ask("@all 执行：分别实现")
            engine.preview_comparison("codex")
            engine._state["comparison"]["candidates"]["claude"]["status"] = "running"
            comparison = engine.interrupt_comparison()

            self.assertEqual(comparison["status"], "interrupted")
            self.assertEqual(comparison["candidates"]["claude"]["apply_status"], "interrupted")
            self.assertEqual(comparison["candidates"]["codex"]["apply_status"], "discarded")
            self.assertFalse((repository / "claude.txt").exists())
            self.assertFalse((repository / "codex.txt").exists())
            for candidate in comparison["candidates"].values():
                self.assertFalse(Path(candidate["workspace"]).exists())

    def test_recovering_comparison_marks_stale_conflict_operation_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = _git_repository(Path(directory))
            state = {
                "protocol": "multiagent.group_chat.v2",
                "turn": 0,
                "messages": [],
                "sessions": {"claude": None, "codex": None},
                "cursors": {"claude": 0, "codex": 0},
                "execution_sessions": {"claude": None, "codex": None},
                "execution_cursors": {"claude": 0, "codex": 0},
                "comparison": {
                    "id": "comparison-stale",
                    "status": "conflict",
                    "operation": {"kind": "resolve", "agent": "claude", "status": "running"},
                    "candidates": {},
                    "base": {"repository": str(repository), "pathspec": ".", "tree": ""},
                },
            }
            engine = GroupChatEngine(
                settings(repository),
                {"claude": FakeAdapter("Claude", []), "codex": FakeAdapter("Codex", [])},
                state,
            )
            operation = (engine.comparison() or {}).get("operation") or {}
            self.assertEqual(operation.get("status"), "failed")

    def test_completed_candidate_can_preview_while_peer_is_still_running(self) -> None:
        class WritingAdapter(FakeAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                (Path(kwargs["workspace"]) / f"{self.display_name.lower()}.txt").write_text(
                    self.display_name,
                    encoding="utf-8",
                )
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            repository = _git_repository(Path(directory))
            engine = GroupChatEngine(
                settings(repository),
                {
                    "claude": WritingAdapter("Claude", [AgentRunResult("Claude", "A")]),
                    "codex": WritingAdapter("Codex", [AgentRunResult("Codex", "B")]),
                },  # type: ignore[arg-type]
            )
            engine.ask("@all 执行：分别实现")
            # Reproduce the live snapshot between the first candidate finishing
            # and the peer finishing. The candidate worktrees and diffs already
            # exist; only the comparison state is still running.
            engine._state["comparison"]["status"] = "running"
            engine._state["comparison"]["candidates"]["codex"]["status"] = "running"

            preview = engine.preview_comparison("claude")
            self.assertEqual(preview["status"], "previewing")
            self.assertEqual(preview["preview"]["active_agent"], "claude")
            self.assertTrue((repository / "claude.txt").is_file())
            self.assertFalse((repository / "codex.txt").exists())

            with self.assertRaisesRegex(BridgeError, "另一个 Agent 仍在执行"):
                engine.apply_comparison("claude")

            engine._state["comparison"]["candidates"]["codex"]["status"] = "ready"
            applied = engine.apply_comparison("claude")
            self.assertEqual(applied["status"], "applied")

    def test_comparison_apply_stops_when_main_workspace_changed(self) -> None:
        class WritingAdapter(FakeAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                (Path(kwargs["workspace"]) / f"{self.display_name}.txt").write_text(
                    "candidate",
                    encoding="utf-8",
                )
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            repository = _git_repository(Path(directory))
            engine = GroupChatEngine(
                settings(repository),
                {
                    "claude": WritingAdapter(
                        "Claude", [AgentRunResult("Claude", "A")]
                    ),
                    "codex": WritingAdapter(
                        "Codex", [AgentRunResult("Codex", "B")]
                    ),
                },  # type: ignore[arg-type]
            )
            engine.ask("@all 执行：分别实现")
            (repository / "tracked.txt").write_text("user change", encoding="utf-8")

            comparison = engine.apply_comparison("claude")

            self.assertEqual(comparison["status"], "conflict")
            self.assertIn("主工作区在对比期间发生了变化", comparison["error"])
            self.assertTrue(comparison["recovery_patch"])
            self.assertTrue(Path(comparison["recovery_patch"]).is_file())
            self.assertIn(
                "tracked.txt",
                [item["path"] for item in comparison["changed_files"]],
            )

            (repository / "tracked.txt").write_text("base", encoding="utf-8")
            rechecked = engine.recheck_comparison()
            self.assertEqual(rechecked["status"], "review")
            self.assertEqual(rechecked["error"], "")

            discarded = engine.discard_comparison()
            self.assertEqual(discarded["status"], "discarded")

    def test_subsequent_prompt_includes_selected_and_discarded_comparison_outcome(self) -> None:
        class WritingAdapter(FakeAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                if kwargs.get("mode") == "write":
                    (Path(kwargs["workspace"]) / f"{self.display_name}.txt").write_text(
                        "candidate",
                        encoding="utf-8",
                    )
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            repository = _git_repository(Path(directory))
            claude = WritingAdapter(
                "Claude",
                [AgentRunResult("Claude", "A"), AgentRunResult("Claude", "后续")],
            )
            codex = WritingAdapter("Codex", [AgentRunResult("Codex", "B")])
            engine = GroupChatEngine(
                settings(repository),
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            )
            engine.ask("@all 执行：分别实现")
            applied = engine.apply_comparison("claude")
            self.assertEqual(applied["status"], "applied")

            engine.ask("@Claude 检查采用后的实现")

        follow_up_prompt = claude.calls[-1]["prompt"]
        self.assertIn("上一轮 A/B 对比已结束", follow_up_prompt)
        self.assertIn("采用了 Claude 方案", follow_up_prompt)
        self.assertIn("Codex 方案未被采用", follow_up_prompt)

    def test_subsequent_prompt_includes_discarded_comparison_outcome(self) -> None:
        claude = FakeAdapter(
            "Claude",
            [AgentRunResult("Claude", "后续")],
        )
        codex = FakeAdapter("Codex", [])
        with tempfile.TemporaryDirectory() as directory:
            engine = GroupChatEngine(
                settings(Path(directory)),
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            )
            engine._state["comparison"] = {"id": "comparison-old", "status": "discarded"}
            engine.ask("@Claude 继续讨论")

        self.assertIn("上一轮 A/B 对比已放弃", claude.calls[0]["prompt"])
        self.assertIn("主工作区未因该对比任务发生修改", claude.calls[0]["prompt"])

    def test_agent_can_assess_conflict_without_applying_or_mutating_main(self) -> None:
        class WritingAdapter(FakeAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                if kwargs.get("mode") == "write":
                    (Path(kwargs["workspace"]) / f"{self.display_name}.txt").write_text(
                        "candidate",
                        encoding="utf-8",
                    )
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            repository = _git_repository(Path(directory))
            claude = WritingAdapter(
                "Claude",
                [
                    AgentRunResult("Claude", "A"),
                    AgentRunResult(
                        "Claude",
                        '{"decision":"safe","confidence":"high","reason":"改动文件不重叠","files":["Claude.txt"],"checks":["git apply --check"]}',
                    ),
                ],
            )
            codex = WritingAdapter("Codex", [AgentRunResult("Codex", "B")])
            engine = GroupChatEngine(
                settings(repository),
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            )
            engine.ask("@all 执行：分别实现")
            (repository / "tracked.txt").write_text("user change", encoding="utf-8")

            conflict = engine.apply_comparison("claude")
            self.assertEqual(conflict["status"], "conflict")
            assessed = engine.assess_comparison_conflict("claude")
            assessment = assessed["candidates"]["claude"]["conflict_assessment"]

            self.assertEqual(assessment["status"], "completed")
            self.assertEqual(assessment["decision"], "safe")
            self.assertEqual(assessment["confidence"], "high")
            self.assertEqual((repository / "tracked.txt").read_text(encoding="utf-8"), "user change")
            self.assertEqual(claude.calls[-1]["mode"], "read")
            self.assertTrue(Path(conflict["candidates"]["claude"]["workspace"]).is_dir())

            engine.discard_comparison()

    def test_agent_can_reimplement_candidate_on_current_main_after_conflict(self) -> None:
        class WritingAdapter(FakeAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                workspace = Path(kwargs["workspace"])
                if kwargs.get("mode") == "write":
                    if "conflict_resolution" in str(kwargs.get("step_id") or ""):
                        (workspace / "resolved.txt").write_text(
                            "adapted",
                            encoding="utf-8",
                        )
                    else:
                        (workspace / f"{self.display_name}.txt").write_text(
                            "candidate",
                            encoding="utf-8",
                        )
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            repository = _git_repository(Path(directory))
            claude = WritingAdapter(
                "Claude",
                [AgentRunResult("Claude", "A"), AgentRunResult("Claude", "重做完成")],
            )
            codex = WritingAdapter("Codex", [AgentRunResult("Codex", "B")])
            engine = GroupChatEngine(
                settings(repository),
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            )
            engine.ask("@all 执行：分别实现")
            (repository / "tracked.txt").write_text("user change", encoding="utf-8")
            conflict = engine.apply_comparison("claude")
            self.assertEqual(conflict["status"], "conflict")

            resolved = engine.resolve_comparison_conflict("claude")
            self.assertEqual(resolved["status"], "review")
            self.assertEqual(claude.calls[-1]["mode"], "write")
            self.assertEqual(
                (repository / "tracked.txt").read_text(encoding="utf-8"),
                "user change",
            )
            applied = engine.apply_comparison("claude")
            self.assertEqual(applied["status"], "applied")
            self.assertEqual(
                (repository / "resolved.txt").read_text(encoding="utf-8").rstrip("\r\n"),
                "adapted",
            )
            self.assertEqual(
                (repository / "tracked.txt").read_text(encoding="utf-8"),
                "user change",
            )

    def test_comparison_tracks_no_changes_and_keeps_successful_peer_when_one_fails(self) -> None:
        class WritingAdapter(FakeAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                (Path(kwargs["workspace"]) / "codex.txt").write_text(
                    "candidate",
                    encoding="utf-8",
                )
                return next(self.results)

        class FailingAdapter(FakeAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                raise BridgeError("Claude failed")

        with tempfile.TemporaryDirectory() as directory:
            repository = _git_repository(Path(directory))
            no_change_engine = GroupChatEngine(
                settings(repository),
                {
                    "claude": FakeAdapter(
                        "Claude", [AgentRunResult("Claude", "只检查")]
                    ),
                    "codex": WritingAdapter(
                        "Codex", [AgentRunResult("Codex", "有修改")]
                    ),
                },  # type: ignore[arg-type]
            )
            no_change_engine.ask("@all 执行：分别实现")
            first = no_change_engine.to_dict()["comparison"]
            self.assertEqual(first["candidates"]["claude"]["status"], "no_changes")
            self.assertEqual(first["candidates"]["codex"]["status"], "ready")
            no_change_engine.discard_comparison()

            failing_engine = GroupChatEngine(
                settings(repository),
                {
                    "claude": FailingAdapter("Claude", []),
                    "codex": WritingAdapter(
                        "Codex", [AgentRunResult("Codex", "可采用")]
                    ),
                },  # type: ignore[arg-type]
            )
            failing_engine.ask("@all 执行：分别实现")
            second = failing_engine.to_dict()["comparison"]
            self.assertEqual(second["candidates"]["claude"]["status"], "failed")
            self.assertTrue(second["candidates"]["claude"]["response_message_id"])
            self.assertEqual(second["candidates"]["codex"]["status"], "ready")

            applied = failing_engine.apply_comparison("codex")

            self.assertEqual(applied["status"], "applied")
            self.assertTrue((repository / "codex.txt").is_file())

    def test_running_comparison_recovers_existing_candidate_worktrees(self) -> None:
        class WritingAdapter(FakeAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                (Path(kwargs["workspace"]) / f"{self.display_name}.txt").write_text(
                    "candidate",
                    encoding="utf-8",
                )
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            repository = _git_repository(Path(directory))
            adapters = {
                "claude": WritingAdapter(
                    "Claude", [AgentRunResult("Claude", "A")]
                ),
                "codex": WritingAdapter(
                    "Codex", [AgentRunResult("Codex", "B")]
                ),
            }
            engine = GroupChatEngine(
                settings(repository),
                adapters,  # type: ignore[arg-type]
            )
            engine.ask("@all 执行：分别实现")
            persisted = engine.to_dict()
            persisted["comparison"]["status"] = "running"
            for candidate in persisted["comparison"]["candidates"].values():
                candidate["status"] = "running"
                candidate["changes"] = None

            recovered = GroupChatEngine(
                settings(repository),
                adapters,  # type: ignore[arg-type]
                persisted,
            )
            comparison = recovered.to_dict()["comparison"]

            self.assertEqual(comparison["status"], "review")
            self.assertEqual(comparison["candidates"]["claude"]["status"], "ready")
            self.assertEqual(comparison["candidates"]["codex"]["status"], "ready")
            recovered.discard_comparison()
            self.assertFalse(Path(comparison["candidates"]["claude"]["workspace"]).exists())
            self.assertFalse(Path(comparison["candidates"]["codex"]["workspace"]).exists())

    def test_dual_agent_execution_requires_git_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = GroupChatEngine(
                settings(Path(directory)),
                {
                    "claude": FakeAdapter("Claude", []),
                    "codex": FakeAdapter("Codex", []),
                },  # type: ignore[arg-type]
            )

            with self.assertRaisesRegex(BridgeError, "需要 Git 工作区"):
                engine.ask("@all 执行：分别实现")

    def test_disabled_worktree_serializes_git_writers_in_main_checkout(self) -> None:
        claude_started = threading.Event()
        release_claude = threading.Event()
        codex_started = threading.Event()
        codex_events: list[Any] = []

        class BlockingAdapter(FakeAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                if self.display_name == "Claude":
                    claude_started.set()
                    release_claude.wait(2)
                else:
                    codex_started.set()
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            repository = _git_repository(Path(directory))
            claude = BlockingAdapter(
                "Claude", [AgentRunResult("Claude", "first", session_id="ca")]
            )
            codex = BlockingAdapter(
                "Codex", [AgentRunResult("Codex", "second", session_id="cb")]
            )
            engine = GroupChatEngine(
                replace(settings(repository), worktree=False),
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            )
            first = engine.reserve("@Claude first")
            first_thread = threading.Thread(
                target=lambda: engine.ask("@Claude first", reservation=first),
                daemon=True,
            )
            first_thread.start()
            self.assertTrue(claude_started.wait(1))

            second = engine.reserve("@Codex second")
            second_thread = threading.Thread(
                target=lambda: engine.ask(
                    "@Codex second",
                    reservation=second,
                    on_event=codex_events.append,
                ),
                daemon=True,
            )
            second_thread.start()
            time.sleep(0.1)
            self.assertFalse(codex_started.is_set())
            self.assertTrue(
                any(event.status == "waiting_workspace" for event in codex_events)
            )
            release_claude.set()
            first_thread.join(2)
            second_thread.join(2)

        self.assertTrue(codex_started.is_set())
        self.assertEqual(Path(claude.calls[0]["workspace"]), repository)
        self.assertEqual(Path(codex.calls[0]["workspace"]), repository)

    def test_worktree_merge_conflict_preserves_successful_agent_reply(self) -> None:
        class WritingAdapter(FakeAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                workspace = Path(kwargs["workspace"])
                (workspace / "changed.txt").write_text("agent change", encoding="utf-8")
                return next(self.results)

        class ConflictLease:
            def __init__(self, workspace: Path) -> None:
                self.workspace = workspace

            def release(self) -> dict[str, Any]:
                return {
                    "merged": False,
                    "error": "修改未合并。完整修改已保存为补丁：recovery.patch",
                }

        class ConflictCoordinator:
            def __init__(self, workspace: Path) -> None:
                self.workspace = workspace

            def acquire(self, *args: Any, **kwargs: Any) -> ConflictLease:
                return ConflictLease(self.workspace)

        with tempfile.TemporaryDirectory() as directory:
            workspace = _git_repository(Path(directory))
            claude = WritingAdapter(
                "Claude", [AgentRunResult("Claude", "Agent 已正常完成", session_id="ca")]
            )
            engine = GroupChatEngine(
                settings(workspace),
                {"claude": claude},  # type: ignore[arg-type]
            )
            engine.workspace_coordinator = ConflictCoordinator(workspace)  # type: ignore[assignment]

            turn = engine.ask("@Claude 修改文件")
            reply = engine.to_dict()["messages"][-1]

        self.assertFalse(turn.errors)
        self.assertEqual(reply["content"], "Agent 已正常完成")
        self.assertEqual(reply["changes"]["merge_status"], "conflict")
        self.assertEqual(reply["changes"]["file_count"], 1)
        self.assertIn("recovery.patch", reply["changes"]["merge_error"])

    def test_completed_write_turn_records_safe_rollback_patch(self) -> None:
        class WritingAdapter(FakeAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                workspace = Path(kwargs["workspace"])
                (workspace / "changed.txt").write_text("agent change", encoding="utf-8")
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            workspace = _git_repository(Path(directory))
            adapter = WritingAdapter(
                "Claude",
                [AgentRunResult("Claude", "已完成修改", session_id="ca")],
            )
            engine = GroupChatEngine(
                settings(workspace),
                {"claude": adapter},  # type: ignore[arg-type]
            )
            turn = engine.ask("@Claude 执行：修改文件")
            reply = engine.to_dict()["messages"][-1]
            rollback = reply["changes"]["rollback"]
            result = engine.workspace_coordinator.rollback_patch(rollback)

            self.assertTrue(turn.changes)
            self.assertEqual(rollback["status"], "available")
            self.assertEqual(result["status"], "rolled_back")
            self.assertFalse((workspace / "changed.txt").exists())

            (workspace / "after-agent.txt").write_text("user change", encoding="utf-8")
            conflict = engine.workspace_coordinator.rollback_patch(rollback)
            self.assertEqual(conflict["status"], "conflict")
            self.assertTrue((workspace / "after-agent.txt").exists())

    def test_merge_failure_without_file_changes_does_not_show_conflict(self) -> None:
        class ConflictLease:
            def __init__(self, workspace: Path) -> None:
                self.workspace = workspace

            def release(self) -> dict[str, Any]:
                return {
                    "merged": False,
                    "error": "协调状态不存在",
                }

        class ConflictCoordinator:
            def __init__(self, workspace: Path) -> None:
                self.workspace = workspace

            def acquire(self, *args: Any, **kwargs: Any) -> ConflictLease:
                return ConflictLease(self.workspace)

        with tempfile.TemporaryDirectory() as directory:
            workspace = _git_repository(Path(directory))
            claude = FakeAdapter(
                "Claude", [AgentRunResult("Claude", "本轮只检查，没有修改代码", session_id="ca")]
            )
            engine = GroupChatEngine(
                settings(workspace),
                {"claude": claude},  # type: ignore[arg-type]
            )
            engine.workspace_coordinator = ConflictCoordinator(workspace)  # type: ignore[assignment]

            turn = engine.ask("@Claude 检查代码")
            reply = engine.to_dict()["messages"][-1]

        self.assertFalse(turn.errors)
        self.assertIsNone(turn.changes)
        self.assertNotIn("changes", reply)

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

    def test_stateless_agent_receives_compacted_projection_without_mutating_history(
        self,
    ) -> None:
        adapter = FakeAdapter(
            "Codex",
            [
                AgentRunResult(
                    "Codex",
                    f"第 {index} 轮回答 " + "实现细节 " * 120,
                    session_id=None,
                )
                for index in range(1, 5)
            ],
            session_resume_enabled=False,
        )
        with tempfile.TemporaryDirectory() as directory:
            resolved = replace(
                settings(Path(directory)),
                context_compaction=ContextCompactionSettings(
                    threshold_tokens=300,
                    target_tokens=160,
                    recent_messages=2,
                ),
            )
            engine = GroupChatEngine(
                resolved,
                {"codex": adapter},  # type: ignore[arg-type]
            )
            engine.ask(
                "@Codex 必须保留早期约束：不要删除 src/state.py。"
                + "背景资料 " * 160
            )
            engine.ask("@Codex 第二轮问题")
            engine.ask("@Codex 第三轮问题")
            engine.ask("@Codex CURRENT_MESSAGE 请给出结论")
            state = engine.to_dict()

            projection = state["context_projections"]["discuss"]["codex"]
            self.assertIsNotNone(projection)
            self.assertIn("group_chat_history_summary", adapter.calls[-1]["prompt"])
            self.assertIn("不要删除 src/state.py", adapter.calls[-1]["prompt"])
            self.assertIn("CURRENT_MESSAGE 请给出结论", adapter.calls[-1]["prompt"])
            self.assertEqual(len(state["messages"]), 8)
            self.assertIn("背景资料", state["messages"][0]["content"])

            restored = GroupChatEngine(
                resolved,
                {"codex": adapter},  # type: ignore[arg-type]
                state,
            ).to_dict()
            self.assertEqual(
                restored["context_projections"]["discuss"]["codex"]["source_hash"],
                projection["source_hash"],
            )

            engine.set_message_context(state["messages"][1]["id"], False)
            invalidated = engine.to_dict()["context_projections"]

        self.assertTrue(
            all(
                value is None
                for channel in invalidated.values()
                for value in channel.values()
            )
        )

    def test_resumable_claude_rolls_over_to_a_compacted_native_session(self) -> None:
        adapter = FakeAdapter(
            "Claude",
            [
                AgentRunResult("Claude", "第一轮回答", session_id="claude-1"),
                AgentRunResult("Claude", "第二轮回答", session_id="claude-1"),
                AgentRunResult("Claude", "第三轮回答", session_id="claude-2"),
                AgentRunResult("Claude", "第四轮回答", session_id="claude-2"),
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            resolved = replace(
                settings(Path(directory)),
                context_compaction=ContextCompactionSettings(
                    threshold_tokens=1_800,
                    target_tokens=800,
                    recent_messages=1,
                ),
            )
            engine = GroupChatEngine(
                resolved,
                {"claude": adapter},  # type: ignore[arg-type]
            )

            engine.ask("@Claude 第一轮用户问题")
            engine.ask(
                "@Claude 必须保留早期约束：不要删除 src/state.py。"
                + "背景资料 " * 400
            )
            engine.ask("@Claude CURRENT_MESSAGE 请给出结论")
            state_after_rollover = engine.to_dict()
            engine.ask("@Claude 第四轮继续")

        self.assertIsNone(adapter.calls[0]["session_id"])
        self.assertEqual(adapter.calls[1]["session_id"], "claude-1")
        self.assertIsNone(adapter.calls[2]["session_id"])
        self.assertIn(
            "group_chat_history_summary",
            adapter.calls[2]["prompt"],
        )
        self.assertIn("不要删除 src/state.py", adapter.calls[2]["prompt"])
        self.assertIn("CURRENT_MESSAGE 请给出结论", adapter.calls[2]["prompt"])
        self.assertEqual(adapter.calls[3]["session_id"], "claude-2")
        self.assertNotIn(
            "group_chat_history_summary",
            adapter.calls[3]["prompt"],
        )
        self.assertIsNotNone(
            state_after_rollover["context_projections"]["discuss"]["claude"]
        )
        self.assertGreater(
            state_after_rollover["native_context_tokens"]["discuss"]["claude"],
            0,
        )

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
        self.assertEqual(resolve_mentions("请检查这个页面@Claude"), ("claude",))
        self.assertEqual(resolve_mentions("请检查这个页面@Claude后再回复"), ("claude",))
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

    def test_agent_reply_can_be_excluded_from_and_restored_to_shared_context(
        self,
    ) -> None:
        claude = FakeAdapter(
            "Claude",
            [AgentRunResult("Claude", "ONLY_FROM_CLAUDE", session_id="ca")],
        )
        codex = FakeAdapter(
            "Codex",
            [
                AgentRunResult("Codex", "第一次回答", session_id="cb-1"),
                AgentRunResult("Codex", "第二次回答", session_id="cb-2"),
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            engine = GroupChatEngine(
                settings(Path(directory)),
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            )
            engine.ask("@Claude 给出候选回答")
            claude_reply = engine.to_dict()["messages"][-1]

            self.assertNotIn("include_in_context", claude_reply)
            excluded = engine.set_message_context(claude_reply["id"], False)
            excluded_state = engine.to_dict()
            restored_engine = GroupChatEngine(
                settings(Path(directory)),
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
                excluded_state,
            )
            restored_engine.ask("@Codex 第一次检查")

            self.assertFalse(excluded["include_in_context"])
            self.assertTrue(
                all(value is None for value in excluded_state["sessions"].values())
            )
            self.assertTrue(
                all(value == 0 for value in excluded_state["cursors"].values())
            )
            self.assertTrue(
                all(
                    value == 0
                    for channel in excluded_state["native_context_tokens"].values()
                    for value in channel.values()
                )
            )
            self.assertNotIn("ONLY_FROM_CLAUDE", codex.calls[0]["prompt"])

            restored = restored_engine.set_message_context(
                claude_reply["id"],
                True,
            )
            restored_engine.ask("@Codex 第二次检查")

        self.assertNotIn("include_in_context", restored)
        self.assertIn("ONLY_FROM_CLAUDE", codex.calls[1]["prompt"])

    def test_user_message_cannot_be_removed_from_shared_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = GroupChatEngine(
                settings(Path(directory)),
                {
                    "claude": FakeAdapter(
                        "Claude",
                        [AgentRunResult("Claude", "回答", session_id="ca")],
                    ),
                    "codex": FakeAdapter("Codex", []),
                },  # type: ignore[arg-type]
            )
            turn = engine.ask("@Claude 用户问题")

            with self.assertRaisesRegex(BridgeError, "只有 Agent 回复"):
                engine.set_message_context(turn.user_message_id, False)

    def test_deleting_an_agent_reply_resets_native_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = GroupChatEngine(
                settings(Path(directory)),
                {
                    "claude": FakeAdapter(
                        "Claude",
                        [AgentRunResult("Claude", "旧回复", session_id="ca")],
                    ),
                    "codex": FakeAdapter("Codex", []),
                },  # type: ignore[arg-type]
            )
            engine.ask("@Claude 用户问题")
            old_reply = engine.to_dict()["messages"][-1]

            removed = engine.delete_assistant_message(old_reply["id"])
            state = engine.to_dict()

        self.assertEqual(removed["content"], "旧回复")
        self.assertEqual(
            [message["role"] for message in state["messages"]],
            ["user"],
        )
        self.assertTrue(all(value is None for value in state["sessions"].values()))
        self.assertTrue(all(value == 0 for value in state["cursors"].values()))

    def test_recalling_user_message_hides_replies_and_excludes_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claude = FakeAdapter(
                "Claude",
                [AgentRunResult("Claude", "旧的 Agent 回复", session_id="ca")],
            )
            codex = FakeAdapter("Codex", [])
            engine = GroupChatEngine(
                settings(Path(directory)),
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            )
            turn = engine.ask("@Claude 这条消息稍后撤回")
            before = engine.to_dict()
            user_id = turn.user_message_id
            reply = before["messages"][1]

            recalled = engine.recall_user_message(user_id)
            state = engine.to_dict()

            self.assertTrue(recalled["recalled"])
            self.assertEqual(state["messages"][0]["content"], "消息已撤回")
            self.assertTrue(state["messages"][0]["recalled"])
            self.assertTrue(state["messages"][1]["hidden"])
            self.assertTrue(state["messages"][1]["recalled"])
            self.assertTrue(all(value is None for value in state["sessions"].values()))
            self.assertTrue(all(value == 0 for value in state["cursors"].values()))

            restored = GroupChatEngine(
                settings(Path(directory)),
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
                state,
            )
            self.assertNotIn("旧的 Agent 回复", restored._prompt_for(  # noqa: SLF001
                "codex",
                ("codex",),
                "discuss",
                session_id=None,
            )[0])
            self.assertEqual(reply["content"], "旧的 Agent 回复")

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

    def test_execution_prefix_can_route_to_all_native_agents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = _git_repository(Path(directory))
            claude = FakeAdapter(
                "Claude", [AgentRunResult("Claude", "Claude 已执行", session_id="ca")]
            )
            codex = FakeAdapter(
                "Codex", [AgentRunResult("Codex", "Codex 已执行", session_id="cb")]
            )
            engine = GroupChatEngine(
                settings(repository),
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            )
            turn = engine.ask("@all 执行：分别检查并修改代码")

        self.assertEqual(turn.action, "execute")
        self.assertEqual(turn.recipients, ("claude", "codex"))
        self.assertEqual(claude.calls[0]["mode"], "write")
        self.assertEqual(codex.calls[0]["mode"], "write")
        self.assertIn("明确要求 Claude 执行", claude.calls[0]["prompt"])
        self.assertIn("明确要求 Codex 执行", codex.calls[0]["prompt"])

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

    def test_single_mentioned_agent_delegates_read_write_decision_to_native_agent(
        self,
    ) -> None:
        """Every native Agent receives its normal workspace capabilities."""

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
            self.assertIn("MultiAgent 不替你判断本轮应只读还是写入", prompt)
            self.assertIn("已经具备本轮工作区的读取和写入能力", prompt)
            self.assertIn("不得以只读模式或没有工作区写权限为由拒绝", prompt)
            self.assertIn("不要提交 Git", prompt)

    def test_broadcast_turns_delegate_read_write_decision_to_each_native_agent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = _git_repository(Path(directory))
            claude = FakeAdapter(
                "Claude", [AgentRunResult("Claude", "Claude 回答", session_id="ca")]
            )
            codex = FakeAdapter(
                "Codex", [AgentRunResult("Codex", "Codex 回答", session_id="cb")]
            )
            engine = GroupChatEngine(
                settings(repository),
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            )
            turn = engine.ask("@all 帮我改一下这个模块")

            self.assertEqual(set(turn.workspaces), {"claude", "codex"})
            self.assertEqual(claude.calls[0]["mode"], "write")
            self.assertEqual(codex.calls[0]["mode"], "write")
            self.assertIn(
                "MultiAgent 不替你判断本轮应只读还是写入",
                claude.calls[0]["prompt"],
            )

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

    def test_non_git_concurrent_native_agents_remain_non_blocking(self) -> None:
        claude_started = threading.Event()
        release_claude = threading.Event()
        codex_started = threading.Event()

        class BlockingAdapter(FakeAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                if self.display_name == "Claude":
                    claude_started.set()
                    release_claude.wait(2)
                else:
                    codex_started.set()
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            claude = BlockingAdapter(
                "Claude", [AgentRunResult("Claude", "完成", session_id="ca")]
            )
            codex = BlockingAdapter(
                "Codex", [AgentRunResult("Codex", "完成", session_id="cb")]
            )
            engine = GroupChatEngine(
                settings(workspace),
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            )
            first = engine.reserve("@Claude 先处理")
            first_thread = threading.Thread(
                target=lambda: engine.ask("@Claude 先处理", reservation=first),
                daemon=True,
            )
            first_thread.start()
            self.assertTrue(claude_started.wait(1))

            second = engine.reserve("@Codex 同时处理")
            second_thread = threading.Thread(
                target=lambda: engine.ask("@Codex 同时处理", reservation=second),
                daemon=True,
            )
            second_thread.start()
            self.assertTrue(codex_started.wait(1))

            release_claude.set()
            first_thread.join(2)
            second_thread.join(2)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(claude.calls[0]["mode"], "write")
        self.assertEqual(codex.calls[0]["mode"], "write")

    def test_edit_appends_auditable_messages_without_mutating_history(self) -> None:
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
