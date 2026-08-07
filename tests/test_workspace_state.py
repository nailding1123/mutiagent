from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from multiagent_cli.workspace_state import (
    capture_change_baseline,
    capture_workspace,
    current_workspace_fingerprint,
    format_snapshot,
    summarize_workspace_changes,
    workspace_fingerprint,
    workspace_fingerprint_matches,
)


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

    def test_fast_fingerprint_detects_content_and_index_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=directory, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=directory,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=directory, check=True
            )
            tracked = workspace / "tracked.txt"
            tracked.write_text("base", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=directory, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "initial"], cwd=directory, check=True
            )

            clean = current_workspace_fingerprint(workspace)
            tracked.write_text("changed", encoding="utf-8")
            modified = current_workspace_fingerprint(workspace)
            subprocess.run(["git", "add", "tracked.txt"], cwd=directory, check=True)
            staged = current_workspace_fingerprint(workspace)
            (workspace / "new.txt").write_text("one", encoding="utf-8")
            untracked = current_workspace_fingerprint(workspace)
            (workspace / "new.txt").write_text("two", encoding="utf-8")
            untracked_changed = current_workspace_fingerprint(workspace)

        self.assertTrue(clean.startswith("v2:"))
        self.assertEqual(len({clean, modified, staged, untracked, untracked_changed}), 5)

    def test_fingerprint_matcher_accepts_legacy_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            legacy = workspace_fingerprint(capture_workspace(workspace), workspace)

            self.assertTrue(workspace_fingerprint_matches(legacy, workspace))

    def test_fast_fingerprint_handles_a_workspace_inside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            workspace = repository / "nested"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"],
                cwd=repository,
                check=True,
            )
            target = workspace / "data.txt"
            target.write_text("base", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "initial"], cwd=repository, check=True
            )
            target.write_text("first", encoding="utf-8")
            first = current_workspace_fingerprint(workspace)
            target.write_text("second", encoding="utf-8")
            second = current_workspace_fingerprint(workspace)

        self.assertNotEqual(first, second)

    def test_change_summary_counts_files_lines_and_task_scoped_diff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=workspace,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"],
                cwd=workspace,
                check=True,
            )
            tracked = workspace / "tracked.txt"
            tracked.write_text("committed\nuser change\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=workspace, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "initial"], cwd=workspace, check=True
            )
            tracked.write_text(
                "committed\nuser change before task\n",
                encoding="utf-8",
            )

            baseline = capture_change_baseline(workspace)
            tracked.write_text(
                "committed\nuser change before task\nagent line\n",
                encoding="utf-8",
            )
            (workspace / "created.py").write_text(
                "one = 1\ntwo = 2\n",
                encoding="utf-8",
            )
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
