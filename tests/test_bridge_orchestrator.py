from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import tempfile
import unittest
from pathlib import Path
from typing import Any

from multiagent_cli.bridge_models import (
    AgentCommandSettings,
    AgentRunResult,
    BridgeError,
    BridgeSettings,
    ConsensusLimitReached,
    PlanDecision,
    VerificationCommand,
)
from multiagent_cli.bridge_orchestrator import BridgeOrchestrator
from multiagent_cli.checkpoints import WorkflowCheckpoint


def consensus_json(*, accepted: bool, version: int = 1) -> str:
    return json.dumps(
        {
            "protocol": "multiagent.consensus.v2",
            "proposal_version": version,
            "verdict": "accept" if accepted else "revise",
            "criteria": {
                "requirements": True,
                "architecture": True,
                "failure_paths": accepted,
                "compatibility": True,
                "testing": accepted,
            },
            "agreements": ["保持接口兼容"],
            "remaining_disagreements": [] if accepted else ["失败路径"],
            "required_revisions": [] if accepted else ["增加失败路径测试"],
            "requirements": [
                {
                    "id": "REQ-001",
                    "text": "覆盖失败路径",
                    "covered": True,
                    "evidence": ["方案：失败路径章节"],
                }
            ],
            "issues": [
                {
                    "id": "ISSUE-001",
                    "severity": "P1",
                    "requirement": "REQ-001",
                    "problem": "失败路径测试不足",
                    "status": "resolved" if accepted else "open",
                    "resolution": "已增加测试" if accepted else "待增加测试",
                    "evidence": ["方案：测试计划"],
                }
            ],
        },
        ensure_ascii=False,
    )


def evidence_consensus_json() -> str:
    return json.dumps(
        {
            "protocol": "multiagent.consensus.v2",
            "proposal_version": 1,
            "verdict": "accept",
            "criteria": {
                "requirements": True,
                "architecture": True,
                "failure_paths": True,
                "compatibility": True,
                "testing": True,
            },
            "requirements": [
                {
                    "id": "REQ-001",
                    "text": "完成实现",
                    "covered": True,
                    "evidence": ["方案：实施步骤", "tests/test_feature.py"],
                }
            ],
            "issues": [],
            "agreements": ["实施和测试"],
            "remaining_disagreements": [],
            "required_revisions": [],
        },
        ensure_ascii=False,
    )


class FakeAdapter:
    def __init__(self, name: str, results: list[AgentRunResult]) -> None:
        self.display_name = name
        self.results = iter(results)
        self.calls: list[dict[str, Any]] = []

    def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
        self.calls.append({"prompt": prompt, **kwargs})
        return next(self.results)


class CoordinatedAdapter(FakeAdapter):
    def __init__(
        self,
        name: str,
        results: list[AgentRunResult],
        *,
        own_started: threading.Event,
        other_started: threading.Event,
    ) -> None:
        super().__init__(name, results)
        self.own_started = own_started
        self.other_started = other_started
        self.overlapped = False

    def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
        self.own_started.set()
        self.overlapped = self.other_started.wait(timeout=1)
        time.sleep(0.02)
        return super().run(prompt, **kwargs)


class PhasedCoordinatedAdapter(FakeAdapter):
    def __init__(
        self,
        name: str,
        results: list[AgentRunResult],
        *,
        own_events: tuple[threading.Event, ...],
        other_events: tuple[threading.Event, ...],
    ) -> None:
        super().__init__(name, results)
        self.own_events = own_events
        self.other_events = other_events
        self.overlaps: list[bool] = []

    def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
        index = len(self.calls)
        if index < len(self.own_events):
            self.own_events[index].set()
            self.overlaps.append(self.other_events[index].wait(timeout=1))
            time.sleep(0.02)
        return super().run(prompt, **kwargs)


class BridgeOrchestratorTests(unittest.TestCase):
    def test_workflow_checkpoint_contains_task_scoped_change_preview(self) -> None:
        class WritingAdapter(FakeAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                if kwargs.get("mode") == "write":
                    (Path(kwargs["workspace"]) / "result.py").write_text(
                        "answer = 42\n",
                        encoding="utf-8",
                    )
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=workspace,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"],
                cwd=workspace,
                check=True,
            )
            (workspace / "README.md").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=workspace, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "initial"], cwd=workspace, check=True
            )
            claude = WritingAdapter(
                "Claude",
                [AgentRunResult("Claude", "实施完成", session_id="write")],
            )
            codex = FakeAdapter("Codex", [])
            settings = BridgeSettings(
                workspace=workspace,
                executor="claude",
                review_rounds=0,
                planning_collaboration=False,
                consensus=False,
                max_consensus_rounds=1,
                plan_approval=False,
                max_plan_revisions=0,
                final_review=False,
                verification_commands=(),
                claude=AgentCommandSettings(("claude",)),
                codex=AgentCommandSettings(("codex",)),
            )
            saved: list[dict[str, Any]] = []
            BridgeOrchestrator(
                settings,
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            ).run("生成结果", on_checkpoint=lambda state: saved.append(state.to_dict()))

        summary = saved[-1]["change_summary"]
        self.assertTrue(summary["available"])
        self.assertEqual(summary["file_count"], 1)
        self.assertEqual(summary["additions"], 1)
        self.assertEqual(summary["files"][0]["path"], "result.py")
        self.assertIn("+answer = 42", summary["files"][0]["patch"])

    def test_evidence_consensus_populates_shared_ledger(self) -> None:
        claude = FakeAdapter(
            "Claude",
            [
                AgentRunResult("Claude", "Agent A 方案", session_id="plan-a"),
                AgentRunResult("Claude", consensus_json(accepted=True), session_id="plan-a"),
                AgentRunResult("Claude", "双方统一方案", session_id="plan-a"),
                AgentRunResult("Claude", "实施完成", session_id="implementation"),
            ],
        )
        codex = FakeAdapter(
            "Codex",
            [
                AgentRunResult("Codex", "Agent B 方案", session_id="plan-b"),
                AgentRunResult("Codex", consensus_json(accepted=True), session_id="plan-b"),
                AgentRunResult("Codex", evidence_consensus_json(), session_id="review-plan"),
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            settings = BridgeSettings(
                workspace=Path(directory),
                executor="claude",
                review_rounds=0,
                planning_collaboration=True,
                consensus=True,
                max_consensus_rounds=3,
                plan_approval=False,
                max_plan_revisions=2,
                final_review=True,
                verification_commands=(),
                claude=AgentCommandSettings(("claude",)),
                codex=AgentCommandSettings(("codex",)),
            )
            outcome = BridgeOrchestrator(
                settings,
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            ).run("完成实现")

        self.assertTrue(outcome.collaboration.accepted)
        self.assertEqual(
            outcome.collaboration.requirements["REQ-001"].evidence[0],
            "方案：实施步骤",
        )

    def test_resume_skips_agent_turns_already_saved_in_checkpoint(self) -> None:
        first_agent_a = FakeAdapter(
            "Claude",
            [
                AgentRunResult("Claude", "已保存方案 A", session_id="executor-plan"),
                AgentRunResult("Claude", consensus_json(accepted=True), session_id="executor-plan"),
                AgentRunResult("Claude", "已保存统一方案", session_id="executor-plan"),
            ],
        )
        first_agent_b = FakeAdapter(
            "Codex",
            [
                AgentRunResult("Codex", "已保存方案 B", session_id="peer-plan"),
                AgentRunResult("Codex", consensus_json(accepted=True), session_id="peer-plan"),
            ],
        )
        saved: WorkflowCheckpoint | None = None

        class StopAfterProposal(RuntimeError):
            pass

        def stop_at_proposal(checkpoint: WorkflowCheckpoint) -> None:
            nonlocal saved
            if checkpoint.phase == "unified_proposal_complete":
                saved = WorkflowCheckpoint.from_dict(checkpoint.to_dict())
                raise StopAfterProposal()

        with tempfile.TemporaryDirectory() as directory:
            settings = BridgeSettings(
                workspace=Path(directory),
                executor="claude",
                review_rounds=0,
                planning_collaboration=True,
                consensus=False,
                max_consensus_rounds=3,
                plan_approval=False,
                max_plan_revisions=2,
                final_review=True,
                verification_commands=(),
                claude=AgentCommandSettings(("claude",)),
                codex=AgentCommandSettings(("codex",)),
            )
            with self.assertRaises(StopAfterProposal):
                BridgeOrchestrator(
                    settings,
                    {"claude": first_agent_a, "codex": first_agent_b},  # type: ignore[arg-type]
                ).run("实现功能", on_checkpoint=stop_at_proposal)

            resumed_executor = FakeAdapter(
                "Claude",
                [AgentRunResult("Claude", "实施完成", session_id="implementation")],
            )
            resumed_validator = FakeAdapter(
                "Codex", []
            )
            outcome = BridgeOrchestrator(
                settings,
                {"claude": resumed_executor, "codex": resumed_validator},  # type: ignore[arg-type]
            ).run("实现功能", checkpoint=saved)

        self.assertEqual(first_agent_a.calls[0]["mode"], "read")
        self.assertEqual(len(resumed_executor.calls), 1)
        self.assertEqual(resumed_executor.calls[0]["mode"], "write")
        self.assertEqual(outcome.unified_proposal.final_text, "已保存统一方案")

    def test_resume_rejects_workspace_changed_after_checkpoint(self) -> None:
        executor = FakeAdapter(
            "Claude",
            [
                AgentRunResult("Claude", "已保存方案 A", session_id="executor-plan"),
                AgentRunResult("Claude", consensus_json(accepted=True), session_id="executor-plan"),
                AgentRunResult("Claude", "已保存统一方案", session_id="executor-plan"),
            ],
        )
        validator = FakeAdapter(
            "Codex",
            [
                AgentRunResult("Codex", "已保存方案 B", session_id="peer-plan"),
                AgentRunResult("Codex", consensus_json(accepted=True), session_id="peer-plan"),
            ],
        )
        saved: WorkflowCheckpoint | None = None

        class StopAfterProposal(RuntimeError):
            pass

        def stop_at_proposal(checkpoint: WorkflowCheckpoint) -> None:
            nonlocal saved
            if checkpoint.phase == "unified_proposal_complete":
                saved = WorkflowCheckpoint.from_dict(checkpoint.to_dict())
                raise StopAfterProposal()

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            settings = BridgeSettings(
                workspace=workspace,
                executor="claude",
                review_rounds=0,
                planning_collaboration=True,
                consensus=False,
                max_consensus_rounds=3,
                plan_approval=False,
                max_plan_revisions=2,
                final_review=True,
                verification_commands=(),
                claude=AgentCommandSettings(("claude",)),
                codex=AgentCommandSettings(("codex",)),
            )
            with self.assertRaises(StopAfterProposal):
                BridgeOrchestrator(
                    settings,
                    {"claude": executor, "codex": validator},  # type: ignore[arg-type]
                ).run("实现功能", on_checkpoint=stop_at_proposal)
            (workspace / "external.txt").write_text("changed", encoding="utf-8")

            with self.assertRaisesRegex(BridgeError, "工作区.*发生变化"):
                BridgeOrchestrator(
                    settings,
                    {
                        "claude": FakeAdapter("Claude", []),
                        "codex": FakeAdapter("Codex", []),
                    },  # type: ignore[arg-type]
                ).run("实现功能", checkpoint=saved)

    def test_review_feedback_resumes_executor_then_gets_final_approval(self) -> None:
        claude = FakeAdapter(
            "Claude",
            [
                AgentRunResult("Claude", "初次实现", session_id="claude-session"),
                AgentRunResult("Claude", "修订完成", session_id="claude-session"),
            ],
        )
        codex = FakeAdapter(
            "Codex",
            [
                AgentRunResult(
                    "Codex",
                    '{"verdict":"request_changes","requirements_covered":[],"findings":'
                    '[{"severity":"P1","file":"a.py","line":1,"requirement":"边界",'
                    '"problem":"未处理","evidence":"缺少分支","suggestion":"补充"}]}'
                ),
                AgentRunResult(
                    "Codex",
                    '{"verdict":"approve","requirements_covered":["边界"],"findings":[]}'
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as directory:
            settings = BridgeSettings(
                workspace=Path(directory),
                executor="claude",
                review_rounds=1,
                planning_collaboration=False,
                consensus=False,
                max_consensus_rounds=3,
                plan_approval=False,
                max_plan_revisions=2,
                final_review=True,
                verification_commands=(),
                claude=AgentCommandSettings(("claude",)),
                codex=AgentCommandSettings(("codex",)),
            )
            outcome = BridgeOrchestrator(
                settings,
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            ).run("修复问题")

        self.assertTrue(outcome.approved)
        self.assertEqual(outcome.execution_result.final_text, "修订完成")
        self.assertEqual(len(outcome.reviews), 2)
        self.assertEqual(claude.calls[1]["session_id"], "claude-session")
        self.assertEqual(codex.calls[0]["mode"], "read")

    def test_zero_rounds_runs_only_executor(self) -> None:
        codex = FakeAdapter(
            "Codex", [AgentRunResult("Codex", "实现完成", session_id="thread")]
        )
        claude = FakeAdapter("Claude", [])
        with tempfile.TemporaryDirectory() as directory:
            settings = BridgeSettings(
                workspace=Path(directory),
                executor="codex",
                review_rounds=0,
                planning_collaboration=False,
                consensus=False,
                max_consensus_rounds=3,
                plan_approval=False,
                max_plan_revisions=2,
                final_review=True,
                verification_commands=(),
                claude=AgentCommandSettings(("claude",)),
                codex=AgentCommandSettings(("codex",)),
            )
            outcome = BridgeOrchestrator(
                settings,
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            ).run("实现功能")

        self.assertIsNone(outcome.approved)
        self.assertEqual(len(claude.calls), 0)
        self.assertEqual(len(codex.calls), 1)

    def test_active_task_statuses_are_persisted_before_agent_stages_run(self) -> None:
        claude = FakeAdapter(
            "Claude",
            [AgentRunResult("Claude", "实现完成", session_id="implementation")],
        )
        codex = FakeAdapter(
            "Codex",
            [
                AgentRunResult(
                    "Codex",
                    '{"verdict":"approve","requirements_covered":["实现"],'
                    '"findings":[]}',
                )
            ],
        )
        snapshots: list[dict[str, Any]] = []

        with tempfile.TemporaryDirectory() as directory:
            settings = BridgeSettings(
                workspace=Path(directory),
                executor="claude",
                review_rounds=1,
                planning_collaboration=False,
                consensus=False,
                max_consensus_rounds=3,
                plan_approval=False,
                max_plan_revisions=2,
                final_review=True,
                verification_commands=(),
                claude=AgentCommandSettings(("claude",)),
                codex=AgentCommandSettings(("codex",)),
            )
            BridgeOrchestrator(
                settings,
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            ).run(
                "实现并审核",
                on_checkpoint=lambda checkpoint: snapshots.append(
                    checkpoint.to_dict()
                ),
            )

        task_snapshots = [
            {
                task["id"]: task["status"]
                for task in snapshot["collaboration"]["tasks"]
            }
            for snapshot in snapshots
        ]
        self.assertTrue(
            any(tasks.get("implementation") == "in_progress" for tasks in task_snapshots)
        )
        self.assertTrue(
            any(tasks.get("verification") == "in_progress" for tasks in task_snapshots)
        )
        self.assertTrue(
            any(tasks.get("code-review") == "in_progress" for tasks in task_snapshots)
        )

    def test_agents_independently_propose_then_cross_review_both_directions(self) -> None:
        claude = FakeAdapter(
            "Claude",
            [
                AgentRunResult("Claude", "Agent A 方案：修改服务层并增加测试"),
                AgentRunResult(
                    "Claude",
                    consensus_json(accepted=False),
                ),
                AgentRunResult("Claude", "统一方案：服务层修改、失败回滚和双向测试"),
                AgentRunResult("Claude", "实现完成", session_id="implementation-session"),
            ],
        )
        codex = FakeAdapter(
            "Codex",
            [
                AgentRunResult(
                    "Codex",
                    "Agent B 方案：需要覆盖失败路径和回滚测试",
                ),
                AgentRunResult(
                    "Codex",
                    consensus_json(accepted=False),
                ),
                AgentRunResult(
                    "Codex",
                    '{"verdict":"approve","requirements_covered":["回滚"],"findings":[]}',
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as directory:
            settings = BridgeSettings(
                workspace=Path(directory),
                executor="claude",
                review_rounds=1,
                planning_collaboration=True,
                consensus=False,
                max_consensus_rounds=3,
                plan_approval=False,
                max_plan_revisions=2,
                final_review=True,
                verification_commands=(),
                claude=AgentCommandSettings(("claude",)),
                codex=AgentCommandSettings(("codex",)),
            )
            outcome = BridgeOrchestrator(
                settings,
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            ).run("实现失败时自动回滚")

        self.assertTrue(outcome.approved)
        self.assertEqual(
            outcome.unified_proposal.final_text,
            "统一方案：服务层修改、失败回滚和双向测试",
        )
        self.assertNotIn("Agent A 方案", codex.calls[0]["prompt"])
        self.assertNotIn("Agent B 方案", claude.calls[0]["prompt"])
        self.assertIn("Agent A 方案", codex.calls[1]["prompt"])
        self.assertIn("Agent B 方案", claude.calls[1]["prompt"])
        self.assertIn("增加失败路径测试", claude.calls[2]["prompt"])
        self.assertIn("增加失败路径测试", codex.calls[2]["prompt"])
        self.assertEqual(claude.calls[0]["mode"], "read")
        self.assertEqual(claude.calls[3]["mode"], "write")
        self.assertIn("<multiagent_identity>", claude.calls[0]["prompt"])
        self.assertIn(settings.agent_a_identity, claude.calls[0]["prompt"])
        self.assertIn(settings.agent_b_identity, codex.calls[0]["prompt"])

    def test_initial_proposals_run_in_parallel(self) -> None:
        claude_started = threading.Event()
        codex_started = threading.Event()
        claude = CoordinatedAdapter(
            "Claude",
            [
                AgentRunResult("Claude", "Agent A 方案", session_id="executor-plan"),
                AgentRunResult("Claude", consensus_json(accepted=True), session_id="executor-plan"),
                AgentRunResult("Claude", "统一方案", session_id="executor-plan"),
                AgentRunResult("Claude", "实施完成", session_id="implementation"),
            ],
            own_started=claude_started,
            other_started=codex_started,
        )
        codex = CoordinatedAdapter(
            "Codex",
            [
                AgentRunResult("Codex", "Agent B 方案", session_id="review-plan"),
                AgentRunResult(
                    "Codex",
                    consensus_json(accepted=True),
                    session_id="review-plan",
                ),
            ],
            own_started=codex_started,
            other_started=claude_started,
        )

        with tempfile.TemporaryDirectory() as directory:
            settings = BridgeSettings(
                workspace=Path(directory),
                executor="claude",
                review_rounds=0,
                planning_collaboration=True,
                consensus=False,
                max_consensus_rounds=3,
                plan_approval=False,
                max_plan_revisions=2,
                final_review=True,
                verification_commands=(),
                claude=AgentCommandSettings(("claude",)),
                codex=AgentCommandSettings(("codex",)),
            )
            started = time.monotonic()
            outcome = BridgeOrchestrator(
                settings,
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            ).run("实现功能")

        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(claude.overlapped)
        self.assertTrue(codex.overlapped)
        self.assertEqual(len(outcome.agent_proposals), 2)
        self.assertEqual(outcome.unified_proposal.final_text, "统一方案")

    def test_independent_proposals_and_cross_reviews_each_run_in_parallel(self) -> None:
        claude_events = (threading.Event(), threading.Event())
        codex_events = (threading.Event(), threading.Event())
        claude = PhasedCoordinatedAdapter(
            "Claude",
            [
                AgentRunResult("Claude", "Agent A 方案"),
                AgentRunResult("Claude", consensus_json(accepted=True)),
                AgentRunResult("Claude", "统一方案"),
                AgentRunResult("Claude", "实施完成"),
            ],
            own_events=claude_events,
            other_events=codex_events,
        )
        codex = PhasedCoordinatedAdapter(
            "Codex",
            [
                AgentRunResult("Codex", "Agent B 方案"),
                AgentRunResult("Codex", consensus_json(accepted=True)),
            ],
            own_events=codex_events,
            other_events=claude_events,
        )
        with tempfile.TemporaryDirectory() as directory:
            settings = BridgeSettings(
                workspace=Path(directory),
                executor="claude",
                review_rounds=0,
                planning_collaboration=True,
                consensus=False,
                max_consensus_rounds=3,
                plan_approval=False,
                max_plan_revisions=2,
                final_review=True,
                verification_commands=(),
                claude=AgentCommandSettings(("claude",)),
                codex=AgentCommandSettings(("codex",)),
            )
            outcome = BridgeOrchestrator(
                settings,
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            ).run("实现功能")

        self.assertEqual(claude.overlaps, [True, True])
        self.assertEqual(codex.overlaps, [True, True])
        self.assertEqual(len(outcome.cross_reviews), 2)

    def test_user_can_request_plan_revision_before_implementation(self) -> None:
        claude = FakeAdapter(
            "Claude",
            [
                AgentRunResult("Claude", "Agent A 方案", session_id="plan-session"),
                AgentRunResult("Claude", consensus_json(accepted=True), session_id="plan-session"),
                AgentRunResult("Claude", "统一方案一", session_id="plan-session"),
                AgentRunResult("Claude", "统一方案二", session_id="plan-session"),
                AgentRunResult("Claude", "实施完成", session_id="impl-session"),
            ],
        )
        codex = FakeAdapter(
            "Codex",
            [
                AgentRunResult("Codex", "Agent B 方案", session_id="review-session"),
                AgentRunResult("Codex", consensus_json(accepted=False)),
            ],
        )
        decisions = iter(
            [PlanDecision("revise", "减少改动范围"), PlanDecision("approve")]
        )

        with tempfile.TemporaryDirectory() as directory:
            settings = BridgeSettings(
                workspace=Path(directory),
                executor="claude",
                review_rounds=0,
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
            outcome = BridgeOrchestrator(
                settings,
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            ).run(
                "实现功能",
                confirm_plan=lambda *_args: next(decisions),
            )

        self.assertEqual(outcome.unified_proposal.final_text, "统一方案二")
        self.assertIn("减少改动范围", claude.calls[3]["prompt"])
        self.assertEqual(claude.calls[3]["session_id"], "plan-session")
        self.assertEqual(claude.calls[4]["mode"], "write")

    def test_user_can_target_non_executor_and_trigger_peer_consensus_review(self) -> None:
        claude = FakeAdapter(
            "Claude",
            [
                AgentRunResult("Claude", "Agent A 方案", session_id="claude-plan"),
                AgentRunResult("Claude", consensus_json(accepted=True)),
                AgentRunResult("Claude", "统一方案一", session_id="claude-plan"),
                AgentRunResult(
                    "Claude",
                    consensus_json(accepted=True, version=2),
                    session_id="claude-plan",
                ),
                AgentRunResult("Claude", "实施完成", session_id="impl-session"),
            ],
        )
        codex = FakeAdapter(
            "Codex",
            [
                AgentRunResult("Codex", "Agent B 方案", session_id="codex-plan"),
                AgentRunResult("Codex", consensus_json(accepted=True)),
                AgentRunResult(
                    "Codex",
                    consensus_json(accepted=True, version=1),
                    session_id="codex-plan",
                ),
                AgentRunResult("Codex", "Agent B 定向修订后的完整统一方案", session_id="codex-plan"),
            ],
        )
        decisions = iter(
            [
                PlanDecision(
                    "targeted_revision",
                    "只补充数据库迁移回滚策略",
                    "codex",
                ),
                PlanDecision("approve"),
            ]
        )

        with tempfile.TemporaryDirectory() as directory:
            settings = BridgeSettings(
                workspace=Path(directory),
                executor="claude",
                review_rounds=0,
                planning_collaboration=True,
                consensus=True,
                max_consensus_rounds=3,
                plan_approval=True,
                max_plan_revisions=2,
                final_review=True,
                verification_commands=(),
                claude=AgentCommandSettings(("claude",)),
                codex=AgentCommandSettings(("codex",)),
            )
            outcome = BridgeOrchestrator(
                settings,
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            ).run(
                "实现功能",
                confirm_plan=lambda *_args: next(decisions),
            )

        self.assertEqual(
            outcome.unified_proposal.final_text,
            "Agent B 定向修订后的完整统一方案",
        )
        self.assertIn("只补充数据库迁移回滚策略", codex.calls[3]["prompt"])
        self.assertIn("Agent B", codex.calls[3]["prompt"])
        self.assertEqual(codex.calls[3]["session_id"], "codex-plan")
        self.assertEqual(codex.calls[3]["mode"], "read")
        self.assertIn(
            "Agent B 定向修订后的完整统一方案",
            claude.calls[3]["prompt"],
        )
        self.assertEqual(claude.calls[3]["mode"], "read")
        self.assertEqual(claude.calls[4]["mode"], "write")
        self.assertTrue(outcome.collaboration.accepted)
        targeted = [
            message
            for message in outcome.collaboration.messages
            if message.sender == "user"
        ]
        self.assertEqual(len(targeted), 1)
        self.assertEqual(targeted[0].recipient, "codex")
        self.assertEqual(targeted[0].kind, "instruction")

    def test_failed_deterministic_verification_blocks_approval(self) -> None:
        claude = FakeAdapter(
            "Claude",
            [
                AgentRunResult("Claude", "首次实现", session_id="impl"),
                AgentRunResult("Claude", "尝试修订", session_id="impl"),
            ],
        )
        approving_review = AgentRunResult(
            "Codex",
            '{"verdict":"approve","requirements_covered":[],"findings":[]}',
        )
        codex = FakeAdapter("Codex", [approving_review, approving_review])

        with tempfile.TemporaryDirectory() as directory:
            settings = BridgeSettings(
                workspace=Path(directory),
                executor="claude",
                review_rounds=1,
                planning_collaboration=False,
                consensus=False,
                max_consensus_rounds=3,
                plan_approval=False,
                max_plan_revisions=2,
                final_review=True,
                verification_commands=(
                    VerificationCommand(
                        "always-fail",
                        (sys.executable, "-c", "import sys; sys.exit(1)"),
                        timeout=5,
                    ),
                ),
                claude=AgentCommandSettings(("claude",)),
                codex=AgentCommandSettings(("codex",)),
            )
            outcome = BridgeOrchestrator(
                settings,
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            ).run("实现功能")

        self.assertFalse(outcome.approved)
        self.assertEqual(len(outcome.verifications), 2)
        self.assertTrue(all(not result.passed for result in outcome.verifications))

    def test_consensus_alternates_integrator_until_both_accept_same_version(self) -> None:
        claude = FakeAdapter(
            "Claude",
            [
                AgentRunResult("Claude", "Agent A 方案", session_id="executor-plan"),
                AgentRunResult("Claude", consensus_json(accepted=True), session_id="executor-plan"),
                AgentRunResult("Claude", "统一方案 v1", session_id="executor-plan"),
                AgentRunResult(
                    "Claude",
                    consensus_json(accepted=True, version=2),
                    session_id="executor-plan",
                ),
                AgentRunResult("Claude", "实施完成", session_id="implementation"),
            ],
        )
        codex = FakeAdapter(
            "Codex",
            [
                AgentRunResult(
                    "Codex", "Agent B 方案：增加失败路径测试", session_id="review-plan"
                ),
                AgentRunResult(
                    "Codex",
                    consensus_json(accepted=True),
                    session_id="review-plan",
                ),
                AgentRunResult(
                    "Codex",
                    consensus_json(accepted=False, version=1),
                    session_id="review-plan",
                ),
                AgentRunResult("Codex", "统一方案 v2", session_id="review-plan"),
            ],
        )
        events = []

        with tempfile.TemporaryDirectory() as directory:
            settings = BridgeSettings(
                workspace=Path(directory),
                executor="claude",
                review_rounds=0,
                planning_collaboration=True,
                consensus=True,
                max_consensus_rounds=3,
                plan_approval=False,
                max_plan_revisions=2,
                final_review=True,
                verification_commands=(),
                claude=AgentCommandSettings(("claude",)),
                codex=AgentCommandSettings(("codex",)),
            )
            outcome = BridgeOrchestrator(
                settings,
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            ).run("实现失败回滚", on_event=events.append)

        self.assertEqual(outcome.unified_proposal.final_text, "统一方案 v2")
        self.assertEqual(codex.calls[3]["mode"], "read")
        self.assertIn("统一方案 v1", codex.calls[3]["prompt"])
        self.assertIn("统一方案 v2", claude.calls[3]["prompt"])
        self.assertEqual(claude.calls[3]["session_id"], "executor-plan")
        self.assertEqual(claude.calls[4]["mode"], "write")
        self.assertTrue(outcome.collaboration.has_unanimous_approval({"claude", "codex"}))
        self.assertTrue(any("共同批准统一方案" in event.text for event in events))

    def test_consensus_limit_stops_before_implementation(self) -> None:
        claude = FakeAdapter(
            "Claude",
            [
                AgentRunResult("Claude", "Agent A 方案", session_id="executor-plan"),
                AgentRunResult("Claude", consensus_json(accepted=True), session_id="executor-plan"),
                AgentRunResult("Claude", "统一方案 v1", session_id="executor-plan"),
            ],
        )
        codex = FakeAdapter(
            "Codex",
            [
                AgentRunResult("Codex", "Agent B 方案", session_id="review-plan"),
                AgentRunResult("Codex", consensus_json(accepted=True)),
                AgentRunResult("Codex", consensus_json(accepted=False, version=1)),
            ],
        )

        with tempfile.TemporaryDirectory() as directory:
            settings = BridgeSettings(
                workspace=Path(directory),
                executor="claude",
                review_rounds=0,
                planning_collaboration=True,
                consensus=True,
                max_consensus_rounds=1,
                plan_approval=False,
                max_plan_revisions=2,
                final_review=True,
                verification_commands=(),
                claude=AgentCommandSettings(("claude",)),
                codex=AgentCommandSettings(("codex",)),
            )
            with self.assertRaisesRegex(
                ConsensusLimitReached, "仍未批准同一方案版本"
            ):
                BridgeOrchestrator(
                    settings,
                    {"claude": claude, "codex": codex},  # type: ignore[arg-type]
                ).run("实现功能")

        self.assertEqual([call["mode"] for call in claude.calls], ["read", "read", "read"])

    def test_unstructured_cross_reviews_retry_once_then_stop_before_synthesis(self) -> None:
        claude = FakeAdapter(
            "Claude",
            [
                AgentRunResult("Claude", "Agent A 方案"),
                AgentRunResult("Claude", "同意，但没有按协议返回 JSON"),
                AgentRunResult("Claude", "仍然没有按协议返回 JSON"),
            ],
        )
        codex = FakeAdapter(
            "Codex",
            [
                AgentRunResult("Codex", "Agent B 方案"),
                AgentRunResult("Codex", "需要修改，但没有按协议返回 JSON"),
                AgentRunResult("Codex", "仍然没有按协议返回 JSON"),
            ],
        )
        saved: WorkflowCheckpoint | None = None

        def remember(checkpoint: WorkflowCheckpoint) -> None:
            nonlocal saved
            saved = checkpoint

        with tempfile.TemporaryDirectory() as directory:
            settings = BridgeSettings(
                workspace=Path(directory),
                executor="claude",
                review_rounds=0,
                planning_collaboration=True,
                consensus=False,
                max_consensus_rounds=3,
                plan_approval=False,
                max_plan_revisions=2,
                final_review=True,
                verification_commands=(),
                claude=AgentCommandSettings(("claude",)),
                codex=AgentCommandSettings(("codex",)),
            )
            with self.assertRaisesRegex(BridgeError, "交叉审核不符合结构化证据协议"):
                BridgeOrchestrator(
                    settings,
                    {"claude": claude, "codex": codex},  # type: ignore[arg-type]
                ).run("实现功能", on_checkpoint=remember)
            self.assertEqual(len(claude.calls), 3)
            self.assertEqual(len(codex.calls), 3)
            self.assertEqual(claude.calls[2]["step_id"], "cross_review_a_repair")
            self.assertEqual(codex.calls[2]["step_id"], "cross_review_b_repair")
            self.assertIn("这一步只修复格式", claude.calls[2]["prompt"])
            self.assertIn("同意，但没有按协议返回 JSON", claude.calls[2]["prompt"])

            assert saved is not None
            resumed_claude = FakeAdapter(
                "Claude",
                [AgentRunResult("Claude", "恢复后仍然没有按协议返回 JSON")],
            )
            resumed_codex = FakeAdapter(
                "Codex",
                [AgentRunResult("Codex", "恢复后仍然没有按协议返回 JSON")],
            )
            with self.assertRaisesRegex(BridgeError, "交叉审核不符合结构化证据协议"):
                BridgeOrchestrator(
                    settings,
                    {
                        "claude": resumed_claude,
                        "codex": resumed_codex,
                    },  # type: ignore[arg-type]
                ).run("实现功能", checkpoint=saved)
            self.assertEqual(len(resumed_claude.calls), 1)
            self.assertEqual(len(resumed_codex.calls), 1)
            self.assertEqual(
                resumed_claude.calls[0]["step_id"], "cross_review_a_repair"
            )
            self.assertEqual(
                resumed_codex.calls[0]["step_id"], "cross_review_b_repair"
            )

        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertIsNotNone(saved.artifact("cross_review_a"))
        self.assertIsNotNone(saved.artifact("cross_review_b"))
        self.assertEqual(saved.collaboration.tasks["cross-review-a"].status, "blocked")
        self.assertEqual(saved.collaboration.tasks["cross-review-b"].status, "blocked")
        self.assertIsNone(saved.artifact("unified_proposal"))

    def test_invalid_cross_review_is_repaired_without_changing_its_issues(self) -> None:
        invalid_review = (
            "- A-ISSUE-003 [P1/待解决] 候选方案缺少失败路径测试；"
            "证据：tests/test_feature.py 尚无对应场景"
        )
        repaired_review = json.dumps(
            {
                "protocol": "multiagent.consensus.v2",
                "proposal_version": 1,
                "verdict": "revise",
                "criteria": {
                    "requirements": True,
                    "architecture": True,
                    "failure_paths": False,
                    "compatibility": True,
                    "testing": False,
                },
                "requirements": [
                    {
                        "id": "A-REQ-001",
                        "text": "失败路径必须有测试",
                        "covered": False,
                        "evidence": ["tests/test_feature.py"],
                    }
                ],
                "issues": [
                    {
                        "id": "A-ISSUE-003",
                        "severity": "P1",
                        "requirement": "A-REQ-001",
                        "problem": "候选方案缺少失败路径测试",
                        "status": "open",
                        "resolution": "待补充测试",
                        "evidence": ["tests/test_feature.py 尚无对应场景"],
                    }
                ],
                "agreements": [],
                "remaining_disagreements": ["失败路径测试尚未覆盖"],
                "required_revisions": ["增加失败路径测试"],
            },
            ensure_ascii=False,
        )
        claude = FakeAdapter(
            "Claude",
            [
                AgentRunResult("Claude", "Agent A 方案", session_id="claude-plan"),
                AgentRunResult("Claude", invalid_review, session_id="claude-plan"),
                AgentRunResult("Claude", repaired_review, session_id="claude-plan"),
                AgentRunResult("Claude", "统一方案", session_id="claude-plan"),
                AgentRunResult("Claude", "实施完成", session_id="implementation"),
            ],
        )
        codex = FakeAdapter(
            "Codex",
            [
                AgentRunResult("Codex", "Agent B 方案", session_id="codex-plan"),
                AgentRunResult(
                    "Codex", evidence_consensus_json(), session_id="codex-plan"
                ),
            ],
        )
        events = []

        with tempfile.TemporaryDirectory() as directory:
            settings = BridgeSettings(
                workspace=Path(directory),
                executor="claude",
                review_rounds=0,
                planning_collaboration=True,
                consensus=False,
                max_consensus_rounds=3,
                plan_approval=False,
                max_plan_revisions=2,
                final_review=True,
                verification_commands=(),
                claude=AgentCommandSettings(("claude",)),
                codex=AgentCommandSettings(("codex",)),
            )
            outcome = BridgeOrchestrator(
                settings,
                {"claude": claude, "codex": codex},  # type: ignore[arg-type]
            ).run("实现功能", on_event=events.append)

        self.assertEqual(outcome.cross_reviews[0].final_text, repaired_review)
        self.assertEqual(claude.calls[2]["step_id"], "cross_review_a_repair")
        self.assertEqual(claude.calls[2]["session_id"], "claude-plan")
        self.assertIn(invalid_review, claude.calls[2]["prompt"])
        self.assertEqual(
            outcome.collaboration.tasks["cross-review-a"].status,
            "done",
        )
        self.assertTrue(
            any("正在自动修复结构化证据" in event.text for event in events)
        )

    def test_cross_review_repair_cannot_drop_existing_issue_ids(self) -> None:
        claude = FakeAdapter(
            "Claude",
            [
                AgentRunResult("Claude", "Agent A 方案"),
                AgentRunResult(
                    "Claude",
                    "A-ISSUE-003 [P1/待解决] 候选方案缺少失败路径测试",
                ),
                AgentRunResult("Claude", evidence_consensus_json()),
            ],
        )
        codex = FakeAdapter(
            "Codex",
            [
                AgentRunResult("Codex", "Agent B 方案"),
                AgentRunResult("Codex", evidence_consensus_json()),
            ],
        )

        with tempfile.TemporaryDirectory() as directory:
            settings = BridgeSettings(
                workspace=Path(directory),
                executor="claude",
                review_rounds=0,
                planning_collaboration=True,
                consensus=False,
                max_consensus_rounds=3,
                plan_approval=False,
                max_plan_revisions=2,
                final_review=True,
                verification_commands=(),
                claude=AgentCommandSettings(("claude",)),
                codex=AgentCommandSettings(("codex",)),
            )
            with self.assertRaisesRegex(
                BridgeError,
                "交叉审核不符合结构化证据协议",
            ):
                BridgeOrchestrator(
                    settings,
                    {"claude": claude, "codex": codex},  # type: ignore[arg-type]
                ).run("实现功能")

        self.assertEqual(len(claude.calls), 3)
        self.assertEqual(claude.calls[2]["step_id"], "cross_review_a_repair")


if __name__ == "__main__":
    unittest.main()
