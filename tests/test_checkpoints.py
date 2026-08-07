from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from multiagent_cli.bridge_models import AgentRunResult, WorkspaceSnapshot
from multiagent_cli.checkpoints import WorkflowCheckpoint
from multiagent_cli.collaboration import CollaborationState
from multiagent_cli.workspace_state import WorkspaceChangeBaseline


class CheckpointTests(unittest.TestCase):
    def test_round_trip_preserves_sessions_and_collaboration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = WorkflowCheckpoint(
                task="修复锁",
                workspace=directory,
                executor="claude",
                phase="proposal_a_complete",
                baseline=WorkspaceSnapshot(False),
                change_baseline=WorkspaceChangeBaseline(
                    available=True,
                    repository=directory,
                    tree="a" * 40,
                ),
                change_summary={
                    "available": True,
                    "file_count": 1,
                    "additions": 2,
                    "deletions": 1,
                    "files": [],
                },
                collaboration=CollaborationState.create(
                    agent_a="claude",
                    agent_b="codex",
                    planning_collaboration=True,
                    executor="claude",
                ),
            )
            state.set_artifact(
                "proposal_a",
                AgentRunResult("Claude", "方案", session_id="session-1"),
            )

            restored = WorkflowCheckpoint.from_dict(
                state.to_dict(),
                expected_task="修复锁",
                expected_workspace=Path(directory),
                expected_executor="claude",
            )

        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.phase, "proposal_a_complete")
        self.assertEqual(restored.artifact("proposal_a").session_id, "session-1")
        self.assertEqual(restored.change_baseline.tree, "a" * 40)
        self.assertEqual(restored.change_summary["file_count"], 1)


if __name__ == "__main__":
    unittest.main()
