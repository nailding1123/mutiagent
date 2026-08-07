from __future__ import annotations

import subprocess
import unittest
from unittest.mock import Mock, patch

from multiagent_cli import process_control


class ProcessControlTests(unittest.TestCase):
    def test_windows_processes_start_in_a_new_group(self) -> None:
        with (
            patch.object(process_control, "IS_POSIX", False),
            patch.object(process_control, "IS_WINDOWS", True),
        ):
            options = process_control.isolated_process_kwargs()

        self.assertEqual(
            options,
            {"creationflags": process_control.WINDOWS_NEW_PROCESS_GROUP},
        )

    def test_windows_graceful_stop_signals_the_process_group(self) -> None:
        process = Mock()
        process.pid = 42
        process.poll.return_value = None
        with (
            patch.object(process_control, "IS_POSIX", False),
            patch.object(process_control, "IS_WINDOWS", True),
            patch.object(process_control, "WINDOWS_CTRL_BREAK_EVENT", 1),
            patch.object(process_control.os, "kill") as kill,
        ):
            process_control.signal_process_tree(process)

        kill.assert_called_once_with(42, 1)
        process.terminate.assert_not_called()

    def test_windows_force_stop_uses_taskkill_for_the_full_tree(self) -> None:
        process = Mock()
        process.pid = 42
        process.poll.return_value = None
        completed = subprocess.CompletedProcess([], 0)
        with (
            patch.object(process_control, "IS_POSIX", False),
            patch.object(process_control, "IS_WINDOWS", True),
            patch.object(process_control.subprocess, "run", return_value=completed) as run,
        ):
            process_control.signal_process_tree(process, force=True)

        self.assertEqual(
            run.call_args.args[0],
            ["taskkill", "/PID", "42", "/T", "/F"],
        )
        process.kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
