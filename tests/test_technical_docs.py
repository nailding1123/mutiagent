from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from multiagent_cli.bridge_models import AgentRunResult, WorkspaceSnapshot
from multiagent_cli.checkpoints import WorkflowCheckpoint
from multiagent_cli.collaboration import CollaborationState
from multiagent_cli.consensus import parse_consensus_decision
from multiagent_cli.technical_docs import (
    export_technical_document,
    render_technical_document,
)


def unresolved_review() -> str:
    return json.dumps(
        {
            "protocol": "multiagent.consensus.v2",
            "proposal_version": 2,
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
                    "id": "REQ-001",
                    "text": "失败后恢复原状态",
                    "covered": False,
                    "evidence": ["统一方案：失败路径"],
                }
            ],
            "issues": [
                {
                    "id": "ISSUE-001",
                    "severity": "P1",
                    "requirement": "REQ-001",
                    "problem": "缺少回滚失败后的恢复策略",
                    "status": "open",
                    "resolution": "仍需补充补偿事务",
                    "evidence": ["统一方案：异常处理"],
                }
            ],
            "agreements": ["保留原接口"],
            "remaining_disagreements": ["是否需要补偿事务"],
            "required_revisions": ["补充回滚失败测试"],
        },
        ensure_ascii=False,
    )


def checkpoint(directory: str) -> WorkflowCheckpoint:
    collaboration = CollaborationState.create(
        agent_a="claude",
        agent_b="codex",
        planning_collaboration=True,
        executor="claude",
    )
    collaboration.set_canonical_proposal(
        "统一方案 v2",
        author="claude",
        version=2,
    )
    collaboration.apply_consensus(
        parse_consensus_decision(unresolved_review()),
        2,
    )
    state = WorkflowCheckpoint(
        task="实现可靠回滚",
        workspace=directory,
        executor="claude",
        phase="consensus_review_v2_complete",
        baseline=WorkspaceSnapshot(False),
        collaboration=collaboration,
    )
    state.set_artifact("proposal_a", AgentRunResult("Claude", "Agent A 方案"))
    state.set_artifact("proposal_b", AgentRunResult("Codex", "Agent B 方案"))
    state.set_artifact(
        "cross_review_a", AgentRunResult("Claude", "Agent A 交叉审核")
    )
    state.set_artifact(
        "cross_review_b", AgentRunResult("Codex", "Agent B 交叉审核")
    )
    state.set_artifact(
        "unified_proposal", AgentRunResult("Claude", "统一方案 v2")
    )
    state.set_artifact(
        "consensus_review_v2", AgentRunResult("Codex", unresolved_review())
    )
    return state


class TechnicalDocumentTests(unittest.TestCase):
    def test_consensus_limit_document_explains_every_unresolved_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rendered = render_technical_document(
                checkpoint(directory),
                max_consensus_rounds=2,
                consensus_limit_reached=True,
            )

        self.assertIn("共识状态：**未达成共识**", rendered)
        self.assertIn("已达到最大共识审核轮次 2", rendered)
        self.assertIn("失败与边界路径", rendered)
        self.assertIn("测试与验收", rendered)
        self.assertIn("失败后恢复原状态。原因：尚未覆盖", rendered)
        self.assertIn("缺少回滚失败后的恢复策略", rendered)
        self.assertIn("原因：仍需补充补偿事务", rendered)
        self.assertIn("是否需要补偿事务", rendered)
        self.assertIn("补充回滚失败测试", rendered)
        self.assertIn("尚未批准当前方案摘要的 Agent：codex", rendered)
        self.assertNotIn('"protocol": "multiagent.consensus.v2"', rendered)

    def test_export_uses_markdown_extension_and_stable_run_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = export_technical_document(
                workspace=Path(directory),
                run_id="run/unsafe id",
                checkpoint=checkpoint(directory),
                max_consensus_rounds=3,
            )

            content = path.read_text(encoding="utf-8")

        self.assertEqual(path.name, "run-unsafe-id-technical-plan.md")
        self.assertIn("# MultiAgent 最终技术方案", content)
        self.assertIn("## 统一技术方案", content)

    def test_document_records_user_targeted_requirements(self) -> None:
        state = checkpoint("/tmp/workspace")
        state.collaboration.post(
            "user",
            "codex",
            "instruction",
            "请单独复核数据库迁移的回滚路径",
        )

        rendered = render_technical_document(
            state,
            max_consensus_rounds=3,
        )

        self.assertIn("## 用户定向要求", rendered)
        self.assertIn("Agent B / Codex", rendered)
        self.assertIn("请单独复核数据库迁移的回滚路径", rendered)


if __name__ == "__main__":
    unittest.main()
