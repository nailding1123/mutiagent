from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from multiagent_cli.run_store import RunStore, _default_run_root


class RunStoreTests(unittest.TestCase):
    def test_windows_default_uses_local_app_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "LocalAppData"

            root = _default_run_root(
                home=Path(directory) / "home",
                environ={"LOCALAPPDATA": str(base)},
                os_name="nt",
            )

        self.assertEqual(root, base / "multiagent" / "runs")

    def test_windows_default_preserves_existing_legacy_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "LocalAppData"
            legacy = base / "mutiagent" / "runs"
            legacy.mkdir(parents=True)

            root = _default_run_root(
                home=Path(directory) / "home",
                environ={"LOCALAPPDATA": str(base)},
                os_name="nt",
            )

            self.assertEqual(root, legacy)

    def test_rejects_path_like_run_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = RunStore(root / "runs")
            outside = root / "outside.json"
            outside.write_text('{"id":"outside","task":"secret"}', encoding="utf-8")

            self.assertIsNone(store.get("../outside"))
            with self.assertRaisesRegex(ValueError, "任务 ID 格式无效"):
                store.start(
                    task="invalid",
                    workspace=root,
                    run_id="../outside",
                )

    def test_records_updates_lists_and_resumes_a_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs")
            record = store.start(
                task="修复登录",
                workspace=Path(directory),
            )
            store.update(record["id"], status="failed", error="temporary")
            resumed = store.start(
                task="修复登录",
                workspace=Path(directory),
                run_id=record["id"],
                display_task="修复登录页面",
                attachments=[{"name": "需求.pdf", "size": 4}],
            )

            loaded = store.get(record["id"])
            listed = store.list()
            limited = store.list(limit=1)

        self.assertEqual(resumed["attempts"], 2)
        self.assertEqual(resumed["status"], "running")
        self.assertEqual(loaded["task"], "修复登录")
        self.assertEqual(loaded["display_task"], "修复登录页面")
        self.assertEqual(loaded["attachments"][0]["name"], "需求.pdf")
        self.assertEqual(listed[0]["id"], record["id"])
        self.assertEqual(len(limited), 1)

    def test_deletes_only_the_requested_valid_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "runs")
            first = store.start(
                task="删除我",
                workspace=Path(directory),
                run_id="delete-me",
            )
            store.start(
                task="保留我",
                workspace=Path(directory),
                run_id="keep-me",
            )

            deleted = store.delete("delete-me")

            self.assertEqual(deleted["id"], first["id"])
            self.assertIsNone(store.get("delete-me"))
            self.assertIsNotNone(store.get("keep-me"))
            with self.assertRaisesRegex(ValueError, "任务 ID 格式无效"):
                store.delete("../keep-me")


if __name__ == "__main__":
    unittest.main()
