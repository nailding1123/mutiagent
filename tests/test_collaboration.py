from __future__ import annotations

import json
import unittest

from multiagent_cli.collaboration import CollaborationState
from multiagent_cli.consensus import parse_consensus_decision


def evidence_decision(*, resolved: bool) -> str:
    return json.dumps(
        {
            "protocol": "multiagent.consensus.v2",
            "proposal_version": 2,
            "verdict": "accept" if resolved else "revise",
            "criteria": {
                "requirements": True,
                "architecture": True,
                "failure_paths": resolved,
                "compatibility": True,
                "testing": resolved,
            },
            "requirements": [
                {
                    "id": "REQ-001",
                    "text": "异常路径释放锁",
                    "covered": True,
                    "evidence": ["方案：异常处理", "tests/test_lock.py"],
                }
            ],
            "issues": [
                {
                    "id": "ISSUE-001",
                    "severity": "P1",
                    "requirement": "REQ-001",
                    "problem": "缺少失败路径测试",
                    "status": "resolved" if resolved else "open",
                    "resolution": "增加测试" if resolved else "",
                    "evidence": ["tests/test_lock.py"] if resolved else [],
                }
            ],
            "agreements": ["保持兼容"],
            "remaining_disagreements": [] if resolved else ["测试不足"],
            "required_revisions": [] if resolved else ["增加测试"],
        },
        ensure_ascii=False,
    )


class CollaborationTests(unittest.TestCase):
    def test_evidence_protocol_blocks_unresolved_p1(self) -> None:
        unresolved = parse_consensus_decision(evidence_decision(resolved=False))
        resolved = parse_consensus_decision(evidence_decision(resolved=True))

        self.assertTrue(unresolved.valid)
        self.assertFalse(unresolved.accepted)
        self.assertTrue(resolved.accepted)

    def test_shared_state_round_trips_tasks_messages_and_ledger(self) -> None:
        state = CollaborationState.create(
            agent_a="claude",
            agent_b="codex",
            planning_collaboration=True,
            executor="claude",
        )
        state.set_task("plan", "done", evidence="session=1")
        state.post("claude", "codex", "proposal", "方案正文")
        state.apply_consensus(
            parse_consensus_decision(evidence_decision(resolved=False)), 1
        )

        restored = CollaborationState.from_dict(state.to_dict())

        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.tasks["plan"].status, "done")
        self.assertEqual(restored.messages[0].sender, "claude")
        self.assertEqual(restored.blocking_issues[0].id, "ISSUE-001")

    def test_consensus_requires_both_agents_to_approve_same_digest(self) -> None:
        state = CollaborationState.create(
            agent_a="claude",
            agent_b="codex",
            planning_collaboration=True,
            executor="claude",
        )
        state.set_canonical_proposal("统一方案 v1", author="claude", version=1)

        self.assertFalse(state.has_unanimous_approval({"claude", "codex"}))
        self.assertFalse(state.approve_canonical("codex", "另一个方案"))
        self.assertTrue(state.approve_canonical("codex", "统一方案 v1"))
        self.assertTrue(state.has_unanimous_approval({"claude", "codex"}))

        restored = CollaborationState.from_dict(state.to_dict())
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.proposal_digest, state.proposal_digest)
        self.assertEqual(set(restored.approvals), {"claude", "codex"})


if __name__ == "__main__":
    unittest.main()
