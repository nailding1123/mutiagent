from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

from multiagent_cli.bridge_models import VerificationCommand
from multiagent_cli.verification import (
    MAX_CAPTURE_CHARS,
    run_verifications,
    verifications_passed,
)


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

    def test_large_output_is_drained_into_a_bounded_head_tail_capture(self) -> None:
        command = VerificationCommand(
            "large-output",
            (
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('HEAD' + 'x' * 200000 + 'TAIL')",
            ),
            timeout=5,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = run_verifications((command,), workspace=Path(directory))[0]

        self.assertTrue(result.passed)
        self.assertLessEqual(len(result.output), MAX_CAPTURE_CHARS + 80)
        self.assertTrue(result.output.startswith("HEAD"))
        self.assertTrue(result.output.endswith("TAIL"))
        self.assertIn("chars truncated", result.output)

    def test_stop_interrupts_an_active_verification_process(self) -> None:
        command = VerificationCommand(
            "slow",
            (sys.executable, "-c", "import time; time.sleep(30)"),
            timeout=60,
        )
        stop = threading.Event()
        started = threading.Event()
        failures: list[BaseException] = []

        def run_check(workspace: Path) -> None:
            try:
                run_verifications(
                    (command,),
                    workspace=workspace,
                    on_event=lambda event: started.set(),
                    should_stop=stop.is_set,
                )
            except BaseException as exc:
                failures.append(exc)

        with tempfile.TemporaryDirectory() as directory:
            worker = threading.Thread(target=run_check, args=(Path(directory),))
            worker.start()
            self.assertTrue(started.wait(timeout=2))
            stop.set()
            worker.join(timeout=3)

        self.assertFalse(worker.is_alive())
        self.assertIsInstance(failures[0], KeyboardInterrupt)


if __name__ == "__main__":
    unittest.main()
