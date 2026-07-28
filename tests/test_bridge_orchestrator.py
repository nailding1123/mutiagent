from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from multiagent_cli.bridge_models import (
    AgentCommandSettings,
    AgentRunResult,
    BridgeError,
    BridgeSettings,
    PlanDecision,
    VerificationCommand,
)
from multiagent_cli.bridge_orchestrator import BridgeOrchestrator
from multiagent_cli.checkpoints import WorkflowCheckpoint


def consensus_json(*, accepted: bool) -> str:
    return json.dumps(
        {
            "protocol": "mutiagent.consensus.v1",
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
        },
        ensure_ascii=False,
    )


def evidence_consensus_json() -> str:
    return json.dumps(
        {
            "protocol": "mutiagent.consensus.v2",
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


class BridgeOrchestratorTests(unittest.TestCase):
    def test_evidence_consensus_populates_shared_ledger(self) -> None:
        claude = FakeAdapter(
            "Claude",
            [
                AgentRunResult("Claude", "主方案", session_id="plan"),
                AgentRunResult("Claude", "实施完成", session_id="implementation"),
            ],
        )
        codex = FakeAdapter(
            "Codex",
            [
                AgentRunResult("Codex", "独立需求", session_id="review-plan"),
                AgentRunResult("Codex", evidence_consensus_json(), session_id="review-plan"),
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            settings = BridgeSettings(
                workspace=Path(directory),
                lead="claude",
                review_rounds=0,
                requirement_review=True,
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
        first_lead = FakeAdapter(
            "Claude",
            [AgentRunResult("Claude", "已保存方案", session_id="lead-plan")],
        )
        first_reviewer = FakeAdapter("Codex", [])
        saved: WorkflowCheckpoint | None = None

        class StopAfterProposal(RuntimeError):
            pass

        def stop_at_proposal(checkpoint: WorkflowCheckpoint) -> None:
            nonlocal saved
            if checkpoint.phase == "proposal_complete":
                saved = WorkflowCheckpoint.from_dict(checkpoint.to_dict())
                raise StopAfterProposal()

        with tempfile.TemporaryDirectory() as directory:
            settings = BridgeSettings(
                workspace=Path(directory),
                lead="claude",
                review_rounds=0,
                requirement_review=True,
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
                    {"claude": first_lead, "codex": first_reviewer},  # type: ignore[arg-type]
                ).run("实现功能", on_checkpoint=stop_at_proposal)

            resumed_lead = FakeAdapter(
                "Claude",
                [AgentRunResult("Claude", "实施完成", session_id="implementation")],
            )
            resumed_reviewer = FakeAdapter(
                "Codex",
                [
                    AgentRunResult("Codex", "独立需求", session_id="review-plan"),
                    AgentRunResult("Codex", "SOLUTION_VERDICT: ACCEPT", session_id="review-plan"),
                ],
            )
            outcome = BridgeOrchestrator(
                settings,
                {"claude": resumed_lead, "codex": resumed_reviewer},  # type: ignore[arg-type]
            ).run("实现功能", checkpoint=saved)

        self.assertEqual(first_lead.calls[0]["mode"], "read")
        self.assertEqual(len(resumed_lead.calls), 1)
        self.assertEqual(resumed_lead.calls[0]["mode"], "write")
        self.assertEqual(outcome.proposal.final_text, "已保存方案")

    def test_resume_rejects_workspace_changed_after_checkpoint(self) -> None:
        lead = FakeAdapter(
            "Claude",
            [AgentRunResult("Claude", "已保存方案", session_id="lead-plan")],
        )
        reviewer = FakeAdapter("Codex", [])
        saved: WorkflowCheckpoint | None = None

        class StopAfterProposal(RuntimeError):
            pass

        def stop_at_proposal(checkpoint: WorkflowCheckpoint) -> None:
            nonlocal saved
            if checkpoint.phase == "proposal_complete":
                saved = WorkflowCheckpoint.from_dict(checkpoint.to_dict())
                raise StopAfterProposal()

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            settings = BridgeSettings(
                workspace=workspace,
                lead="claude",
                review_rounds=0,
                requirement_review=True,
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
                    {"claude": lead, "codex": reviewer},  # type: ignore[arg-type]
                ).run("实现功能", on_checkpoint=stop_at_proposal)
            (workspace / "external.txt").write_text("changed", encoding="utf-8")

            with self.assertRaisesRegex(BridgeError, "工作区.*发生变化"):
                BridgeOrchestrator(
                    settings,
                    {"claude": FakeAdapter("Claude", []), "codex": FakeAdapter("Codex", [])},  # type: ignore[arg-type]
                ).run("实现功能", checkpoint=saved)

    def test_review_feedback_resumes_lead_then_gets_final_approval(self) -> None:
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
                lead="claude",
                review_rounds=1,
                requirement_review=False,
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
        self.assertEqual(outcome.lead_result.final_text, "修订完成")
        self.assertEqual(len(outcome.reviews), 2)
        self.assertEqual(claude.calls[1]["session_id"], "claude-session")
        self.assertEqual(codex.calls[0]["mode"], "read")

    def test_zero_rounds_runs_only_lead(self) -> None:
        codex = FakeAdapter(
            "Codex", [AgentRunResult("Codex", "实现完成", session_id="thread")]
        )
        claude = FakeAdapter("Claude", [])
        with tempfile.TemporaryDirectory() as directory:
            settings = BridgeSettings(
                workspace=Path(directory),
                lead="codex",
                review_rounds=0,
                requirement_review=False,
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

    def test_secondary_agent_analyzes_requirement_and_reviews_lead_proposal(self) -> None:
        claude = FakeAdapter(
            "Claude",
            [
                AgentRunResult("Claude", "主方案：修改服务层并增加测试"),
                AgentRunResult("Claude", "实现完成", session_id="implementation-session"),
            ],
        )
        codex = FakeAdapter(
            "Codex",
            [
                AgentRunResult(
                    "Codex",
                    "独立需求分析：需要覆盖失败路径和回滚测试",
                ),
                AgentRunResult(
                    "Codex",
                    "SOLUTION_VERDICT: REVISE\nREQUIRED_REVISIONS: 增加回滚测试",
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
                lead="claude",
                review_rounds=1,
                requirement_review=True,
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
        self.assertEqual(outcome.proposal.final_text, "主方案：修改服务层并增加测试")
        self.assertNotIn("主方案：修改服务层并增加测试", codex.calls[0]["prompt"])
        self.assertIn("主方案：修改服务层并增加测试", codex.calls[1]["prompt"])
        self.assertIn("增加回滚测试", claude.calls[1]["prompt"])
        self.assertIn("增加回滚测试", codex.calls[2]["prompt"])
        self.assertEqual(claude.calls[0]["mode"], "read")
        self.assertEqual(claude.calls[1]["mode"], "write")
        self.assertIn("<mutiagent_identity>", claude.calls[0]["prompt"])
        self.assertIn(settings.lead_identity, claude.calls[0]["prompt"])
        self.assertIn(settings.reviewer_identity, codex.calls[0]["prompt"])

    def test_user_can_request_plan_revision_before_implementation(self) -> None:
        claude = FakeAdapter(
            "Claude",
            [
                AgentRunResult("Claude", "方案一", session_id="plan-session"),
                AgentRunResult("Claude", "方案二", session_id="plan-session"),
                AgentRunResult("Claude", "实施完成", session_id="impl-session"),
            ],
        )
        codex = FakeAdapter(
            "Codex",
            [
                AgentRunResult("Codex", "独立需求", session_id="review-session"),
                AgentRunResult("Codex", "SOLUTION_VERDICT: REVISE"),
                AgentRunResult("Codex", "SOLUTION_VERDICT: ACCEPT"),
            ],
        )
        decisions = iter(
            [PlanDecision("revise", "减少改动范围"), PlanDecision("approve")]
        )

        with tempfile.TemporaryDirectory() as directory:
            settings = BridgeSettings(
                workspace=Path(directory),
                lead="claude",
                review_rounds=0,
                requirement_review=True,
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

        self.assertEqual(outcome.proposal.final_text, "方案二")
        self.assertIn("减少改动范围", claude.calls[1]["prompt"])
        self.assertEqual(claude.calls[1]["session_id"], "plan-session")
        self.assertEqual(claude.calls[2]["mode"], "write")

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
                lead="claude",
                review_rounds=1,
                requirement_review=False,
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

    def test_consensus_revises_plan_until_reviewer_accepts(self) -> None:
        claude = FakeAdapter(
            "Claude",
            [
                AgentRunResult("Claude", "主方案一", session_id="lead-plan"),
                AgentRunResult("Claude", "共识方案二", session_id="lead-plan"),
                AgentRunResult("Claude", "实施完成", session_id="implementation"),
            ],
        )
        codex = FakeAdapter(
            "Codex",
            [
                AgentRunResult(
                    "Codex", "独立方案：增加失败路径测试", session_id="review-plan"
                ),
                AgentRunResult(
                    "Codex",
                    consensus_json(accepted=False),
                    session_id="review-plan",
                ),
                AgentRunResult(
                    "Codex",
                    consensus_json(accepted=True),
                    session_id="review-plan",
                ),
            ],
        )
        events = []

        with tempfile.TemporaryDirectory() as directory:
            settings = BridgeSettings(
                workspace=Path(directory),
                lead="claude",
                review_rounds=0,
                requirement_review=True,
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

        self.assertEqual(outcome.proposal.final_text, "共识方案二")
        self.assertEqual(claude.calls[1]["session_id"], "lead-plan")
        self.assertEqual(claude.calls[1]["mode"], "read")
        self.assertIn("独立方案：增加失败路径测试", claude.calls[1]["prompt"])
        self.assertIn("共识方案二", codex.calls[2]["prompt"])
        self.assertEqual(codex.calls[2]["session_id"], "review-plan")
        self.assertIn("共识方案二", claude.calls[2]["prompt"])
        self.assertEqual(claude.calls[2]["mode"], "write")
        self.assertTrue(any("已达成方案共识" in event.text for event in events))

    def test_consensus_limit_stops_before_implementation(self) -> None:
        claude = FakeAdapter(
            "Claude",
            [
                AgentRunResult("Claude", "方案一", session_id="lead-plan"),
                AgentRunResult("Claude", "方案二", session_id="lead-plan"),
            ],
        )
        codex = FakeAdapter(
            "Codex",
            [
                AgentRunResult("Codex", "独立方案", session_id="review-plan"),
                AgentRunResult("Codex", "SOLUTION_VERDICT: REVISE"),
                AgentRunResult("Codex", "SOLUTION_VERDICT: REVISE"),
            ],
        )

        with tempfile.TemporaryDirectory() as directory:
            settings = BridgeSettings(
                workspace=Path(directory),
                lead="claude",
                review_rounds=0,
                requirement_review=True,
                consensus=True,
                max_consensus_rounds=1,
                plan_approval=False,
                max_plan_revisions=2,
                final_review=True,
                verification_commands=(),
                claude=AgentCommandSettings(("claude",)),
                codex=AgentCommandSettings(("codex",)),
            )
            with self.assertRaisesRegex(BridgeError, "未达成共识"):
                BridgeOrchestrator(
                    settings,
                    {"claude": claude, "codex": codex},  # type: ignore[arg-type]
                ).run("实现功能")

        self.assertEqual([call["mode"] for call in claude.calls], ["read", "read"])


if __name__ == "__main__":
    unittest.main()
