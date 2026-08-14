from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from multiagent_cli.workspace_state import capture_change_baseline, summarize_workspace_changes


class WorkspaceStateTests(unittest.TestCase):
    def test_change_summary_counts_files_lines_and_task_scoped_diff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace, check=True)
            tracked = workspace / "tracked.txt"
            tracked.write_text("committed\nuser change\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=workspace, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=workspace, check=True)
            tracked.write_text("committed\nuser change before task\n", encoding="utf-8")
            baseline = capture_change_baseline(workspace)
            tracked.write_text("committed\nuser change before task\nagent line\n", encoding="utf-8")
            (workspace / "created.py").write_text("one = 1\ntwo = 2\n", encoding="utf-8")
            summary = summarize_workspace_changes(workspace, baseline)
        self.assertTrue(summary["available"])
        self.assertEqual(summary["file_count"], 2)
        self.assertEqual(summary["additions"], 3)
        self.assertEqual(summary["deletions"], 0)
        files = {item["path"]: item for item in summary["files"]}
        self.assertEqual(files["created.py"]["status"], "added")
        self.assertIn("+agent line", files["tracked.txt"]["patch"])
        self.assertNotIn("+user change before task", files["tracked.txt"]["patch"])

    def test_change_baseline_round_trip_and_non_git_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline = capture_change_baseline(Path(directory))
            restored = type(baseline).from_dict(baseline.to_dict())
            summary = summarize_workspace_changes(Path(directory), restored)
        self.assertIsNotNone(restored)
        self.assertFalse(summary["available"])
        self.assertIn("不是 Git 仓库", summary["reason"])


if __name__ == "__main__":
    unittest.main()
