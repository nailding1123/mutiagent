from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from multiagent_cli.workspace_state import capture_workspace, format_snapshot


class WorkspaceStateTests(unittest.TestCase):
    def test_non_git_directory_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = capture_workspace(Path(directory))

        self.assertFalse(snapshot.is_git_repo)
        self.assertIn("不是 Git 仓库", format_snapshot(snapshot))

    def test_dirty_git_baseline_captures_untracked_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            subprocess.run(
                ["git", "init", "-q"], cwd=directory, check=True, capture_output=True
            )
            (workspace / "existing.txt").write_text("user change", encoding="utf-8")

            snapshot = capture_workspace(workspace)

        self.assertTrue(snapshot.is_git_repo)
        self.assertTrue(snapshot.is_dirty)
        self.assertIn("existing.txt", snapshot.status)


if __name__ == "__main__":
    unittest.main()
