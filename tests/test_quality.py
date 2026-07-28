from __future__ import annotations

import unittest

from multiagent_cli.quality import build_quality_report


class QualityTests(unittest.TestCase):
    def test_compares_solo_review_and_consensus_history(self) -> None:
        records = [
            {
                "status": "complete",
                "approved": True,
                "settings": {"review_rounds": 0, "requirement_review": False},
                "summary": {"input_tokens": 100, "output_tokens": 20, "elapsed_seconds": 10},
                "quality": {"verification_passed": 1, "verification_total": 1},
            },
            {
                "status": "complete",
                "approved": False,
                "settings": {"consensus": True, "review_rounds": 1},
                "summary": {"input_tokens": 300, "output_tokens": 80, "elapsed_seconds": 30},
                "quality": {
                    "verification_passed": 0,
                    "verification_total": 1,
                    "findings": {"P1": 2},
                },
            },
        ]

        report = build_quality_report(records)

        self.assertEqual(report.total_runs, 2)
        self.assertEqual(report.approval_rate, 0.5)
        self.assertEqual(report.verification_rate, 0.5)
        self.assertEqual(report.findings["P1"], 2)
        self.assertEqual(report.modes["solo"].average_tokens, 120)
        self.assertEqual(report.modes["consensus"].average_tokens, 380)


if __name__ == "__main__":
    unittest.main()
