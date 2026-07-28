from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from multiagent_cli.bridge_models import AgentRunResult, WorkspaceSnapshot
from multiagent_cli.checkpoints import WorkflowCheckpoint
from multiagent_cli.collaboration import CollaborationState


class CheckpointTests(unittest.TestCase):
    def test_round_trip_preserves_sessions_and_collaboration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = WorkflowCheckpoint(
                task="修复锁",
                workspace=directory,
                lead="claude",
                phase="proposal_complete",
                baseline=WorkspaceSnapshot(False),
                collaboration=CollaborationState.create(
                    lead="claude", reviewer="codex", requirement_review=True
                ),
            )
            state.set_artifact(
                "proposal",
                AgentRunResult("Claude", "方案", session_id="session-1"),
            )

            restored = WorkflowCheckpoint.from_dict(
                state.to_dict(),
                expected_task="修复锁",
                expected_workspace=Path(directory),
                expected_lead="claude",
            )

        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.phase, "proposal_complete")
        self.assertEqual(restored.artifact("proposal").session_id, "session-1")


if __name__ == "__main__":
    unittest.main()
