from __future__ import annotations

import json
import unittest

from multiagent_cli.consensus import CONSENSUS_PROTOCOL, parse_consensus_decision


class ConsensusDecisionTests(unittest.TestCase):
    def test_accept_requires_every_criterion_and_no_disagreement(self) -> None:
        accepted = parse_consensus_decision(
            json.dumps(
                {
                    "protocol": CONSENSUS_PROTOCOL,
                    "verdict": "accept",
                    "criteria": {
                        "requirements": True,
                        "architecture": True,
                        "failure_paths": True,
                        "compatibility": True,
                        "testing": True,
                    },
                    "agreements": ["接口保持兼容"],
                    "remaining_disagreements": [],
                    "required_revisions": [],
                }
            )
        )

        self.assertTrue(accepted.valid)
        self.assertTrue(accepted.accepted)

        data = json.loads(json.dumps({
            "protocol": CONSENSUS_PROTOCOL,
            "verdict": "accept",
            "criteria": {
                "requirements": True,
                "architecture": True,
                "failure_paths": False,
                "compatibility": True,
                "testing": True,
            },
            "agreements": [],
            "remaining_disagreements": [],
            "required_revisions": ["补充失败路径"],
        }))
        rejected = parse_consensus_decision(json.dumps(data))
        self.assertFalse(rejected.accepted)

    def test_invalid_structured_response_fails_closed(self) -> None:
        decision = parse_consensus_decision('{"verdict":"accept"}')

        self.assertFalse(decision.valid)
        self.assertFalse(decision.accepted)

    def test_legacy_marker_remains_compatible(self) -> None:
        decision = parse_consensus_decision("SOLUTION_VERDICT: ACCEPT")

        self.assertTrue(decision.valid)
        self.assertTrue(decision.accepted)
        self.assertFalse(decision.structured)

    def test_legacy_misspelled_protocol_remains_compatible(self) -> None:
        decision = parse_consensus_decision(
            json.dumps(
                {
                    "protocol": "mutiagent.consensus.v1",
                    "verdict": "accept",
                    "criteria": {
                        "requirements": True,
                        "architecture": True,
                        "failure_paths": True,
                        "compatibility": True,
                        "testing": True,
                    },
                    "agreements": [],
                    "remaining_disagreements": [],
                    "required_revisions": [],
                }
            )
        )

        self.assertTrue(decision.valid)
        self.assertTrue(decision.accepted)

    def test_legacy_evidence_protocol_keeps_evidence_semantics(self) -> None:
        decision = parse_consensus_decision(
            json.dumps(
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
                            "text": "保持兼容",
                            "covered": True,
                            "evidence": ["tests/test_api.py"],
                        }
                    ],
                    "issues": [],
                    "agreements": [],
                    "remaining_disagreements": [],
                    "required_revisions": [],
                }
            )
        )

        self.assertTrue(decision.valid)
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.requirements[0].id, "REQ-001")


if __name__ == "__main__":
    unittest.main()
