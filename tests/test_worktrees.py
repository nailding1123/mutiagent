from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from multiagent_cli.bridge_models import BridgeError
from multiagent_cli.worktrees import WorktreeManager, WorktreeRecord


class WorktreeTests(unittest.TestCase):
    def test_creates_isolated_branch_and_requires_explicit_force_to_discard_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=repository, check=True
            )
            (repository / "app.py").write_text("print('base')\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repository, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "initial"], cwd=repository, check=True
            )
            manager = WorktreeManager(root / "tasks")
            record = manager.create(repository, "run-1")
            restored = WorktreeRecord.from_dict(record.to_dict())
            (record.workspace / "app.py").write_text(
                "print('changed')\n", encoding="utf-8"
            )
            (record.workspace / "new.py").write_text(
                "print('new')\n", encoding="utf-8"
            )

            self.assertTrue(record.workspace.is_dir())
            self.assertEqual(record.branch, "mutiagent/run-1")
            self.assertIsNotNone(restored)
            self.assertIn("changed", manager.diff(record))
            self.assertIn("new.py", manager.diff(record))
            with self.assertRaisesRegex(BridgeError, "--force"):
                manager.discard(record)
            manager.discard(record, force=True)

            self.assertFalse(record.worktree_root.exists())


if __name__ == "__main__":
    unittest.main()
