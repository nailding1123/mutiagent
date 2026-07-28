from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from multiagent_cli.bridge_models import VerificationCommand
from multiagent_cli.verification import run_verifications, verifications_passed


class VerificationTests(unittest.TestCase):
    def test_captures_pass_and_failure_without_shell(self) -> None:
        commands = (
            VerificationCommand(
                "pass", (sys.executable, "-c", "print('passed')"), timeout=5
            ),
            VerificationCommand(
                "fail",
                (sys.executable, "-c", "import sys; print('failed'); sys.exit(3)"),
                timeout=5,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            results = run_verifications(commands, workspace=Path(directory))

        self.assertTrue(results[0].passed)
        self.assertEqual(results[1].exit_code, 3)
        self.assertIn("failed", results[1].output)
        self.assertFalse(verifications_passed(results))

    def test_timeout_is_a_failure(self) -> None:
        command = VerificationCommand(
            "timeout",
            (sys.executable, "-c", "import time; time.sleep(0.2)"),
            timeout=0.05,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = run_verifications((command,), workspace=Path(directory))[0]

        self.assertTrue(result.timed_out)
        self.assertFalse(result.passed)


if __name__ == "__main__":
    unittest.main()

