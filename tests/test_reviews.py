from __future__ import annotations

import unittest

from multiagent_cli.reviews import parse_review_decision


class ReviewDecisionTests(unittest.TestCase):
    def test_parses_structured_approval(self) -> None:
        decision = parse_review_decision(
            '{"verdict":"approve","requirements_covered":["登录"],"findings":[]}'
        )

        self.assertEqual(decision.verdict, "approve")
        self.assertEqual(decision.requirements_covered, ("登录",))
        self.assertTrue(decision.structured)

    def test_p1_finding_forces_request_changes(self) -> None:
        decision = parse_review_decision(
            '{"verdict":"approve","requirements_covered":[],"findings":['
            '{"severity":"P1","file":"auth.py","line":12,"requirement":"释放锁",'
            '"problem":"异常路径泄漏","evidence":"提前返回","suggestion":"finally"}]}'
        )

        self.assertEqual(decision.verdict, "request_changes")
        self.assertEqual(decision.findings[0].file, "auth.py")

    def test_invalid_output_fails_closed(self) -> None:
        decision = parse_review_decision("VERDICT: APPROVE")

        self.assertEqual(decision.verdict, "request_changes")
        self.assertFalse(decision.structured)
        self.assertEqual(decision.findings[0].severity, "P1")


if __name__ == "__main__":
    unittest.main()

