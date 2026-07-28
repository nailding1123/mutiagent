from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from multiagent_cli.run_store import RunStore


class RunStoreTests(unittest.TestCase):
    def test_records_updates_lists_and_resumes_a_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs")
            record = store.start(
                task="修复登录",
                workspace=Path(directory),
                lead="claude",
                consensus=True,
            )
            store.update(record["id"], status="failed", error="temporary")
            resumed = store.start(
                task="修复登录",
                workspace=Path(directory),
                lead="codex",
                consensus=True,
                run_id=record["id"],
            )

            loaded = store.get(record["id"])
            listed = store.list()

        self.assertEqual(resumed["attempts"], 2)
        self.assertEqual(resumed["status"], "running")
        self.assertEqual(loaded["task"], "修复登录")
        self.assertEqual(listed[0]["id"], record["id"])


if __name__ == "__main__":
    unittest.main()
