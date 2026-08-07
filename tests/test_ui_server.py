from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from multiagent_cli.bridge_models import AgentRunResult
from multiagent_cli.run_store import RunStore
from multiagent_cli import ui_server
from multiagent_cli.ui_server import (
    UIError,
    LocalUIHTTPServer,
    UISession,
    UISessionManager,
    make_request_handler,
)


class FakeChatAdapter:
    def __init__(self, name: str, results: list[AgentRunResult]) -> None:
        self.display_name = name
        self.results = iter(results)
        self.calls: list[dict[str, Any]] = []
        self.stop_requested = False

    def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
        self.calls.append({"prompt": prompt, **kwargs})
        return next(self.results)

    def request_stop(self) -> None:
        self.stop_requested = True


class UIServerTests(unittest.TestCase):
    def test_serve_ui_reuses_an_existing_compatible_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with (
                patch.object(ui_server, "ui_is_running", return_value=True),
                patch.object(
                    ui_server,
                    "select_ui_workspace",
                    return_value=True,
                ) as select_workspace,
                patch.object(ui_server.webbrowser, "open") as open_browser,
                patch.object(ui_server, "LocalUIHTTPServer") as http_server,
            ):
                result = ui_server.serve_ui(
                    workspace=workspace,
                    store=RunStore(workspace / "state"),
                    port=8765,
                    open_browser=True,
                    quiet=True,
                )

        self.assertEqual(result, 0)
        select_workspace.assert_called_once_with(
            "http://127.0.0.1:8765/",
            workspace,
        )
        open_browser.assert_called_once_with("http://127.0.0.1:8765/")
        http_server.assert_not_called()

    def test_group_chat_routes_mentions_and_persists_shared_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".multiagent.json").write_text(
                json.dumps(
                    {
                        "collaboration_mode": "group_chat",
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    }
                ),
                encoding="utf-8",
            )
            claude = FakeChatAdapter(
                "Claude",
                [AgentRunResult("Claude", "Claude 初始回答", session_id="ca")],
            )
            codex = FakeChatAdapter(
                "Codex",
                [
                    AgentRunResult("Codex", "Codex 初始回答", session_id="cb"),
                    AgentRunResult("Codex", "Codex 审核意见", session_id="cb"),
                ],
            )
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            with patch(
                "multiagent_cli.cli._make_adapters",
                return_value={"claude": claude, "codex": codex},
            ):
                started = manager.start_task({"task": "分别给出初始方案"})
                run_id = started["id"]
                self._wait_for_status(manager, run_id, "ready")
                manager.send_chat_message(
                    run_id,
                    {"message": "@Codex 请审核 Claude 的初始回答"},
                )
                self._wait_for_status(manager, run_id, "ready", message_count=5)

            record = manager.store.get(run_id)

        self.assertEqual(record["collaboration_mode"], "group_chat")
        self.assertEqual(record["status"], "ready")
        self.assertEqual(len(record["group_chat"]["messages"]), 5)
        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(len(codex.calls), 2)
        self.assertIn("Claude 初始回答", codex.calls[1]["prompt"])
        self.assertIn("@Codex 请审核", codex.calls[1]["prompt"])

    def test_group_chat_can_start_empty_and_wait_for_first_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".multiagent.json").write_text(
                json.dumps(
                    {
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    }
                ),
                encoding="utf-8",
            )
            claude = FakeChatAdapter(
                "Claude",
                [AgentRunResult("Claude", "收到第一条消息", session_id="ca")],
            )
            codex = FakeChatAdapter("Codex", [])
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            with patch(
                "multiagent_cli.cli._make_adapters",
                return_value={"claude": claude, "codex": codex},
            ):
                started = manager.start_task({"collaboration_mode": "group_chat"})
                run_id = started["id"]
                initial_record = manager.store.get(run_id) or {}

                self.assertEqual(started["status"], "ready")
                self.assertEqual(initial_record["status"], "ready")
                self.assertEqual(initial_record["task"], "")
                self.assertEqual(initial_record["display_task"], "群聊协作")
                self.assertEqual(initial_record["group_chat"]["messages"], [])
                self.assertEqual(claude.calls, [])
                self.assertEqual(codex.calls, [])

                manager.send_chat_message(run_id, {"message": "@Claude 你好"})
                self._wait_for_status(manager, run_id, "ready", message_count=2)

            record = manager.store.get(run_id) or {}

        self.assertEqual(len(record["group_chat"]["messages"]), 2)
        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(codex.calls, [])

    def test_workflow_still_requires_an_initial_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )

            with self.assertRaisesRegex(UIError, "任务不能为空"):
                manager.start_task({"collaboration_mode": "workflow"})

    def test_rename_run_changes_display_title_but_preserves_original_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = RunStore(workspace / "state")
            store.start(
                task="原始需求内容",
                display_task="原名称",
                workspace=workspace,
                executor="claude",
                consensus=False,
                run_id="rename-run",
            )
            manager = UISessionManager(store=store, default_workspace=workspace)
            session = UISession(
                run_id="rename-run",
                task="原名称",
                workspace=workspace,
                executor="claude",
                consensus=False,
                notify=manager.publish,
            )
            manager._reserve_session(session)

            renamed = manager.rename_run("rename-run", "  新的\n任务名称  ")
            record = store.get("rename-run") or {}

            self.assertEqual(renamed["display_task"], "新的 任务名称")
            self.assertEqual(record["display_task"], "新的 任务名称")
            self.assertEqual(record["task"], "原始需求内容")
            self.assertEqual(session.to_dict()["task"], "新的 任务名称")
            with self.assertRaisesRegex(UIError, "任务名称不能为空"):
                manager.rename_run("rename-run", "  ")
            with self.assertRaisesRegex(UIError, "不能超过 200"):
                manager.rename_run("rename-run", "长" * 201)

    def test_group_chat_single_agent_execution_writes_target_workspace(self) -> None:
        class WritingChatAdapter(FakeChatAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                workspace = Path(kwargs["workspace"])
                (workspace / f"{self.display_name.lower()}.txt").write_text(
                    self.display_name,
                    encoding="utf-8",
                )
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "repo"
            workspace.mkdir()
            (workspace / ".multiagent.json").write_text(
                json.dumps(
                    {
                        "collaboration_mode": "group_chat",
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    }
                ),
                encoding="utf-8",
            )
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
            subprocess.run(["git", "add", "."], cwd=workspace, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "initial"],
                cwd=workspace,
                check=True,
            )
            claude = WritingChatAdapter(
                "Claude",
                [AgentRunResult("Claude", "Claude 已执行", session_id="ca-write")],
            )
            codex = WritingChatAdapter(
                "Codex",
                [AgentRunResult("Codex", "Codex 已执行", session_id="cb-write")],
            )
            manager = UISessionManager(
                store=RunStore(root / "state" / "runs"),
                default_workspace=workspace,
            )
            with patch(
                "multiagent_cli.cli._make_adapters",
                return_value={"claude": claude, "codex": codex},
            ):
                started = manager.start_task({"task": "@Claude 执行：生成结果文件"})
                run_id = started["id"]
                self._wait_for_status(manager, run_id, "ready", message_count=2)

            record = manager.store.get(run_id) or {}
            chat = record.get("group_chat") or {}
            self.assertTrue((workspace / "claude.txt").is_file())
            self.assertFalse((workspace / "codex.txt").exists())
            self.assertEqual(claude.calls[0]["mode"], "write")
            self.assertEqual(codex.calls, [])
            self.assertEqual(
                [message.get("action") for message in chat["messages"]],
                ["execute", "execute"],
            )
            changes = chat["messages"][-1]["changes"]
            self.assertTrue(changes["available"])
            self.assertEqual(changes["file_count"], 1)
            self.assertEqual(changes["additions"], 1)
            self.assertEqual(changes["deletions"], 0)
            self.assertEqual(changes["files"][0]["path"], "claude.txt")
            self.assertIn("+Claude", changes["files"][0]["patch"])

    @staticmethod
    def _wait_for_status(
        manager: UISessionManager,
        run_id: str,
        status: str,
        *,
        message_count: int = 0,
    ) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            record = manager.store.get(run_id) or {}
            messages = (record.get("group_chat") or {}).get("messages") or []
            if record.get("status") == status and len(messages) >= message_count:
                return
            time.sleep(0.01)
        raise AssertionError(f"群聊未进入 {status} 状态")

    def test_new_task_saves_uploaded_documents_and_builds_agent_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".multiagent.json").write_text(
                json.dumps(
                    {
                        "planning_collaboration": False,
                        "plan_approval": False,
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    }
                ),
                encoding="utf-8",
            )
            store = RunStore(workspace / "state")
            manager = UISessionManager(store=store, default_workspace=workspace)
            content = b"uploaded requirements"

            with patch.object(UISessionManager, "_run_session"):
                started = manager.start_task(
                    {
                        "task": "根据文档实现功能",
                        "attachments": [
                            {
                                "name": "requirements.md",
                                "size": len(content),
                                "content_type": "text/markdown",
                                "data": base64.b64encode(content).decode("ascii"),
                            }
                        ],
                    }
                )

            session = manager.session(started["id"])
            attachment = started["attachments"][0]
            attachment_path = Path(attachment["path"])

            self.assertEqual(started["task"], "根据文档实现功能")
            self.assertEqual(attachment["name"], "requirements.md")
            self.assertEqual(attachment_path.read_bytes(), content)
            self.assertEqual(attachment_path.stat().st_mode & 0o777, 0o400)
            self.assertIn(str(attachment_path), session.agent_task)
            self.assertIn("请先读取", session.agent_task)
            self.assertNotIn("data", attachment)

    def test_upload_rejects_unsafe_document_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".multiagent.json").write_text(
                json.dumps(
                    {
                        "planning_collaboration": False,
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    }
                ),
                encoding="utf-8",
            )
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            with self.assertRaisesRegex(UIError, "不支持的文档格式"):
                manager.start_task(
                    {
                        "task": "不要运行附件",
                        "attachments": [
                            {
                                "name": "payload.sh",
                                "size": 4,
                                "content_type": "text/plain",
                                "data": base64.b64encode(b"exit").decode("ascii"),
                            }
                        ],
                    }
                )

    def test_manager_deletes_only_archived_run_and_its_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = RunStore(workspace / "state")
            manager = UISessionManager(store=store, default_workspace=workspace)
            record = store.start(
                task="待删除任务",
                workspace=workspace,
                executor="claude",
                consensus=False,
                run_id="delete-archived",
            )
            attachment_directory = manager.attachments_root / record["id"]
            attachment_directory.mkdir(parents=True)
            (attachment_directory / "notes.txt").write_text("notes", encoding="utf-8")

            with self.assertRaisesRegex(UIError, "只能删除已归档"):
                manager.delete_run(record["id"])
            store.update(record["id"], status="complete", archived=True)
            deleted = manager.delete_run(record["id"])

            self.assertEqual(deleted["id"], record["id"])
            self.assertIsNone(store.get(record["id"]))
            self.assertFalse(attachment_directory.exists())

    def test_orphaned_running_record_is_shown_as_interrupted_and_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = RunStore(workspace / "state")
            store.start(
                task="旧服务遗留任务",
                workspace=workspace,
                executor="claude",
                consensus=False,
                run_id="orphaned-run",
            )
            store.update(
                "orphaned-run",
                phase="initialized",
                checkpoint={"phase": "initialized"},
            )
            manager = UISessionManager(store=store, default_workspace=workspace)

            summary = manager.list_runs()[0]
            detail = manager.run_detail("orphaned-run")

            self.assertEqual(summary["status"], "interrupted")
            self.assertTrue(summary["resumable"])
            self.assertTrue(summary["detached"])
            self.assertEqual(detail["record"]["status"], "interrupted")
            self.assertIn("不在当前 UI 服务", detail["record"]["error"])
            self.assertEqual(store.get("orphaned-run")["status"], "running")
            with self.assertRaisesRegex(UIError, "不属于当前 UI 服务"):
                manager.stop_task("orphaned-run")

            archived = manager.set_archived("orphaned-run", True)
            self.assertTrue(archived["archived"])
            self.assertEqual(archived["status"], "interrupted")
            self.assertEqual(store.get("orphaned-run")["status"], "interrupted")

            live = UISession(
                run_id="orphaned-run",
                task="恢复后的活动任务",
                workspace=workspace,
                executor="claude",
                consensus=False,
                notify=manager.publish,
            )
            manager._reserve_session(live)
            active_summary = manager.list_runs()[0]

        self.assertEqual(active_summary["status"], "starting")
        self.assertTrue(active_summary["live"])
        self.assertFalse(active_summary["resumable"])

    def test_public_sessions_and_records_hide_internal_error_details(self) -> None:
        secret_error = "failed at /private/project with token sk-example-secret"
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = RunStore(workspace / "state")
            store.start(
                task="失败任务",
                workspace=workspace,
                executor="codex",
                consensus=False,
                run_id="failed-secret",
            )
            store.update("failed-secret", status="failed", error=secret_error)
            manager = UISessionManager(store=store, default_workspace=workspace)
            session = UISession(
                run_id="live-secret",
                task="活动失败任务",
                workspace=workspace,
                executor="codex",
                consensus=False,
                notify=manager.publish,
            )
            session.fail(secret_error)

            summary = manager.list_runs()[0]
            detail = manager.run_detail("failed-secret")
            live = session.to_dict()

        self.assertEqual(summary["error"], "任务执行失败")
        self.assertEqual(detail["record"]["error"], "任务执行失败")
        self.assertEqual(live["error"], "任务执行失败")
        self.assertNotIn("sk-example-secret", json.dumps(detail))

    def test_running_ui_task_stops_both_agents_and_keeps_checkpoint(self) -> None:
        script_text = """#!/usr/bin/env python3
import sys
import time
sys.stdin.read()
time.sleep(30)
"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            fake_cli = workspace / "slow-agent.py"
            fake_cli.write_text(script_text, encoding="utf-8")
            (workspace / ".multiagent.json").write_text(
                json.dumps(
                    {
                        "planning_collaboration": True,
                        "plan_approval": False,
                        "claude": {
                            "command": [sys.executable, str(fake_cli)]
                        },
                        "codex": {
                            "command": [sys.executable, str(fake_cli)]
                        },
                    }
                ),
                encoding="utf-8",
            )
            store = RunStore(workspace / "state")
            manager = UISessionManager(store=store, default_workspace=workspace)
            started = manager.start_task({"task": "停止双 Agent"})
            run_id = started["id"]

            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                session = manager.session(run_id)
                if session is not None and len(session.to_dict()["agent_events"]) == 2:
                    break
                time.sleep(0.02)
            else:
                self.fail("两个 Agent 未在限定时间内启动")

            stopping = manager.stop_task(run_id)
            self.assertEqual(stopping["status"], "stopping")
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                finished = manager.session(run_id).to_dict()
                if finished["status"] == "interrupted":
                    break
                time.sleep(0.02)
            else:
                self.fail("任务未在限定时间内停止")

            record = store.get(run_id)

        self.assertEqual(finished["exit_code"], 130)
        self.assertEqual(record["status"], "interrupted")
        self.assertEqual(record["checkpoint"]["phase"], "initialized")

    def test_resume_uses_saved_snapshot_when_config_file_was_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = RunStore(workspace / "state")
            store.start(
                task="恢复任务",
                workspace=workspace,
                executor="claude",
                consensus=False,
                run_id="resume-snapshot",
                settings_snapshot={
                    "config_path": str(workspace / "removed.json"),
                    "resolved_config": {
                        "planning_collaboration": False,
                        "plan_approval": False,
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    },
                },
            )
            store.update("resume-snapshot", status="failed")
            manager = UISessionManager(store=store, default_workspace=workspace)

            with patch.object(UISessionManager, "_run_session"):
                session = manager.start_task({"resume_id": "resume-snapshot"})

            self.assertEqual(session["id"], "resume-snapshot")
            self.assertEqual(session["status"], "starting")

    def test_resume_rejects_path_like_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            with self.assertRaisesRegex(UIError, "任务 ID 格式无效"):
                manager.start_task({"resume_id": "../outside"})

    def test_manager_reserves_one_active_writer_per_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            first = UISession(
                run_id="first",
                task="first",
                workspace=workspace,
                executor="claude",
                consensus=False,
                notify=manager.publish,
            )
            second = UISession(
                run_id="second",
                task="second",
                workspace=workspace,
                executor="codex",
                consensus=False,
                notify=manager.publish,
            )

            manager._reserve_session(first)
            with self.assertRaisesRegex(UIError, "已有正在运行"):
                manager._reserve_session(second)

    def test_manager_refuses_shutdown_until_active_tasks_finish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            session = UISession(
                run_id="active-shutdown",
                task="运行中任务",
                workspace=workspace,
                executor="claude",
                consensus=False,
                notify=manager.publish,
            )
            manager._reserve_session(session)

            with self.assertRaisesRegex(UIError, "仍有 1 个任务正在运行"):
                manager.ensure_shutdown_safe()

            session.status = "complete"
            manager.ensure_shutdown_safe()

    def test_manager_archives_only_finished_runs_and_can_restore_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = RunStore(workspace / "state")
            store.start(
                task="归档任务",
                workspace=workspace,
                executor="claude",
                consensus=False,
                run_id="archive-run",
            )
            manager = UISessionManager(store=store, default_workspace=workspace)
            live = UISession(
                run_id="archive-run",
                task="归档任务",
                workspace=workspace,
                executor="claude",
                consensus=False,
                notify=manager.publish,
            )
            manager._reserve_session(live)

            with self.assertRaisesRegex(UIError, "运行中的任务不能归档"):
                manager.set_archived("archive-run", True)

            live.finish(0, store.update("archive-run", status="complete"))
            archived = manager.set_archived("archive-run", True)
            self.assertTrue(archived["archived"])
            self.assertTrue(archived["archived_at"])
            self.assertTrue(manager.list_runs()[0]["archived"])

            restored = manager.set_archived("archive-run", False)
            self.assertFalse(restored["archived"])
            self.assertEqual(restored["archived_at"], "")

    def test_settings_round_trip_validates_and_preserves_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config_path = workspace / ".multiagent.json"
            config_path.write_text(
                json.dumps(
                    {
                        "custom_extension": {"enabled": True},
                        "api_key": "never-return-this-value",
                        "claude": {
                            "command": "/bin/echo",
                            "custom_agent_field": "keep-me",
                        },
                        "codex": {"command": "/bin/echo"},
                    }
                ),
                encoding="utf-8",
            )
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            loaded = manager.get_settings()
            self.assertNotIn("never-return-this-value", json.dumps(loaded))
            values = loaded["values"]
            values["executor"] = "codex"
            values["collaboration_mode"] = "group_chat"
            values["group_chat_default_agent"] = "codex"
            values["group_chat_execution"] = False
            values["identities"]["agent_a"] = "共识 Claude 身份"
            values["identities"]["agent_b"] = "共识 Codex 身份"
            values["group_chat_identities"]["agent_a"] = "群聊 Claude 身份"
            values["group_chat_identities"]["agent_b"] = "群聊 Codex 身份"
            values["consensus"] = True
            values["max_consensus_rounds"] = 5
            values["claude"]["model"] = "claude-test"
            values["codex"]["model"] = "codex-test"
            values["verification"]["commands"] = [
                {"name": "echo", "command": ["/bin/echo", "ok"]}
            ]
            values["ui"] = {
                "theme": "ocean",
                "show_archived": True,
                "compact_sidebar": True,
            }

            values["ui"]["theme"] = "unknown"
            with self.assertRaisesRegex(UIError, "界面主题必须"):
                manager.save_settings(
                    {
                        "workspace": str(workspace),
                        "revision": loaded["revision"],
                        "values": values,
                    }
                )
            values["ui"]["theme"] = "ocean"

            saved = manager.save_settings(
                {
                    "workspace": str(workspace),
                    "revision": loaded["revision"],
                    "values": values,
                }
            )
            persisted = json.loads(config_path.read_text(encoding="utf-8"))

            self.assertEqual(saved["values"]["executor"], "codex")
            self.assertEqual(saved["values"]["collaboration_mode"], "group_chat")
            self.assertEqual(saved["values"]["group_chat_default_agent"], "codex")
            self.assertFalse(saved["values"]["group_chat_execution"])
            self.assertEqual(Path(saved["source_path"]), config_path.resolve())
            self.assertEqual(persisted["custom_extension"], {"enabled": True})
            self.assertEqual(persisted["api_key"], "never-return-this-value")
            self.assertEqual(persisted["claude"]["custom_agent_field"], "keep-me")
            self.assertEqual(persisted["max_consensus_rounds"], 5)
            self.assertEqual(persisted["collaboration_mode"], "group_chat")
            self.assertEqual(persisted["group_chat_default_agent"], "codex")
            self.assertFalse(persisted["group_chat_execution"])
            self.assertEqual(persisted["identities"]["agent_a"], "共识 Claude 身份")
            self.assertEqual(
                persisted["group_chat_identities"]["agent_a"],
                "群聊 Claude 身份",
            )
            self.assertEqual(persisted["ui"]["theme"], "ocean")
            self.assertTrue(persisted["ui"]["compact_sidebar"])
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
            defaults = manager.get_settings(defaults=True)
            self.assertEqual(defaults["values"]["executor"], "claude")
            self.assertEqual(defaults["values"]["ui"]["theme"], "paper")
            self.assertFalse(defaults["values"]["ui"]["compact_sidebar"])

            with self.assertRaisesRegex(UIError, "已被其他程序修改"):
                manager.save_settings(
                    {
                        "workspace": str(workspace),
                        "revision": loaded["revision"],
                        "values": values,
                    }
                )

    def test_ui_preferences_can_be_saved_without_other_form_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config_path = workspace / ".multiagent.json"
            config_path.write_text(
                json.dumps(
                    {
                        "executor": "codex",
                        "custom_extension": {"keep": True},
                        "ui": {"show_archived": True},
                    }
                ),
                encoding="utf-8",
            )
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )

            saved = manager.save_ui_preferences(
                {
                    "workspace": str(workspace),
                    "ui": {
                        "theme": "botanical",
                        "show_archived": False,
                        "compact_sidebar": True,
                    },
                }
            )
            persisted = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(saved["values"]["ui"]["theme"], "botanical")
        self.assertEqual(persisted["executor"], "codex")
        self.assertEqual(persisted["custom_extension"], {"keep": True})
        self.assertFalse(persisted["ui"]["show_archived"])
        self.assertTrue(persisted["ui"]["compact_sidebar"])
        self.assertEqual(persisted["ui"]["theme"], "botanical")

        with tempfile.TemporaryDirectory() as directory:
            manager = UISessionManager(
                store=RunStore(Path(directory) / "state"),
                default_workspace=Path(directory),
            )
            with self.assertRaisesRegex(UIError, "界面主题必须"):
                manager.save_ui_preferences(
                    {"workspace": directory, "ui": {"theme": "unknown"}}
                )
            with self.assertRaisesRegex(UIError, "界面开关必须"):
                manager.save_ui_preferences(
                    {"workspace": directory, "ui": {"compact_sidebar": 1}}
                )

    def test_token_api_key_is_stored_privately_and_never_returned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            state_root = workspace / "state"
            manager = UISessionManager(
                store=RunStore(state_root),
                default_workspace=workspace,
            )
            loaded = manager.get_settings()
            values = loaded["values"]
            values["token_api"]["enabled"] = True
            values["claude"]["models"] = [
                "claude-opus-5",
                "gemini-3.5-flash",
            ]
            values["codex"]["models"] = ["gpt-5.6-sol", "gpt-5.5"]

            saved = manager.save_settings(
                {
                    "workspace": str(workspace),
                    "revision": loaded["revision"],
                    "values": values,
                    "token_api_key": "company-private-key-7890",
                }
            )
            config_text = (workspace / ".multiagent.json").read_text(encoding="utf-8")
            credentials_path = state_root / "_credentials" / "token_api.json"
            response_text = json.dumps(saved, ensure_ascii=False)
            credentials_mode = credentials_path.stat().st_mode & 0o777

        self.assertNotIn("company-private-key-7890", config_text)
        self.assertNotIn("company-private-key-7890", response_text)
        self.assertTrue(saved["token_api_credentials"]["configured"])
        self.assertEqual(saved["token_api_credentials"]["masked"], "••••7890")
        self.assertEqual(saved["values"]["claude"]["models"][1], "gemini-3.5-flash")
        self.assertEqual(saved["values"]["codex"]["models"][1], "gpt-5.5")
        self.assertEqual(credentials_mode, 0o600)

    def test_directory_browser_lists_children_and_workspace_shortcuts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            child = workspace / "child-project"
            child.mkdir(parents=True)
            manager = UISessionManager(
                store=RunStore(root / "state"),
                default_workspace=workspace,
            )

            listing = manager.browse_directories(str(workspace))

        self.assertEqual(listing["path"], str(workspace.resolve()))
        self.assertIn(
            {"name": "child-project", "path": str(child.resolve())},
            listing["directories"],
        )
        self.assertIn(str(workspace.resolve()), {
            item["path"] for item in listing["shortcuts"]
        })

    def test_plan_gate_exports_then_returns_targeted_agent_decision(self) -> None:
        plan_ready = threading.Event()
        document_ready = threading.Event()
        notices: list[tuple[str, str]] = []
        document = Path("/tmp/final-plan.md")

        def notify(kind: str, run_id: str) -> None:
            notices.append((kind, run_id))
            if kind == "plan":
                plan_ready.set()
            if kind == "document":
                document_ready.set()

        session = UISession(
            run_id="run-ui",
            task="实现界面",
            workspace=Path("/tmp"),
            executor="claude",
            consensus=True,
            notify=notify,
        )
        returned = []

        def wait_for_decision() -> None:
            returned.append(
                session.wait_for_plan(
                    AgentRunResult("Claude", "Agent A 方案"),
                    AgentRunResult("Codex", "Agent B 方案"),
                    (
                        AgentRunResult("Claude", "A 审核 B"),
                        AgentRunResult("Codex", "B 审核 A"),
                    ),
                    AgentRunResult("Claude", "统一方案"),
                    None,
                    0,
                    on_export=lambda: document,
                )
            )

        worker = threading.Thread(target=wait_for_decision)
        worker.start()
        self.assertTrue(plan_ready.wait(timeout=2))

        session.submit_action(action="export")
        self.assertTrue(document_ready.wait(timeout=2))
        self.assertEqual(session.to_dict()["document"], str(document))

        session.submit_action(
            action="targeted_revision",
            feedback="只让 Codex 补充失败路径",
            target_agent="codex",
        )
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(returned[0].action, "targeted_revision")
        self.assertEqual(returned[0].target_agent, "codex")
        self.assertEqual(returned[0].feedback, "只让 Codex 补充失败路径")
        self.assertIn(("plan_decision", "run-ui"), notices)

    def test_stop_wakes_plan_gate_and_is_idempotent(self) -> None:
        plan_ready = threading.Event()
        stop_calls = []
        returned = []
        session = UISession(
            run_id="stop-plan",
            task="停止任务",
            workspace=Path("/tmp"),
            executor="claude",
            consensus=False,
            notify=lambda kind, _run_id: plan_ready.set() if kind == "plan" else None,
        )
        session.bind_stop_handler(lambda: stop_calls.append("stop"))

        def wait_for_decision() -> None:
            returned.append(
                session.wait_for_plan(
                    AgentRunResult("Claude", "Agent A 方案"),
                    AgentRunResult("Codex", "Agent B 方案"),
                    (),
                    AgentRunResult("Claude", "统一方案"),
                    None,
                    0,
                    on_export=lambda: Path("/tmp/unused.md"),
                )
            )

        worker = threading.Thread(target=wait_for_decision)
        worker.start()
        self.assertTrue(plan_ready.wait(timeout=2))
        session.request_stop()
        session.request_stop()
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(stop_calls, ["stop"])
        self.assertEqual(returned[0].action, "interrupt")
        self.assertEqual(session.to_dict()["status"], "stopping")

    def test_local_http_server_serves_health_history_and_static_ui(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            selected_workspace = workspace / "opened-project"
            selected_workspace.mkdir()
            store = RunStore(workspace / "state")
            record = store.start(
                task="检查界面",
                workspace=workspace,
                executor="claude",
                consensus=False,
                run_id="run-http",
            )
            store.update(record["id"], status="complete")
            manager = UISessionManager(store=store, default_workspace=workspace)
            static_root = Path(__file__).resolve().parents[1] / "multiagent_cli" / "web"
            try:
                server = LocalUIHTTPServer(
                    ("127.0.0.1", 0),
                    make_request_handler(manager, static_root),
                )
            except PermissionError:
                self.skipTest("当前沙箱禁止绑定本机测试端口")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                health = json.loads(urlopen(f"{base}/api/health", timeout=2).read())
                workspace_request = Request(
                    f"{base}/api/workspace",
                    data=json.dumps(
                        {"workspace": str(selected_workspace)}
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Origin": base},
                    method="POST",
                )
                selected = json.loads(urlopen(workspace_request, timeout=2).read())
                switched_health = json.loads(
                    urlopen(f"{base}/api/health", timeout=2).read()
                )
                runs = json.loads(urlopen(f"{base}/api/runs", timeout=2).read())
                settings = json.loads(
                    urlopen(f"{base}/api/settings", timeout=2).read()
                )
                theme_request = Request(
                    f"{base}/api/settings/interface",
                    data=json.dumps(
                        {
                            "workspace": settings["workspace"],
                            "ui": {
                                "theme": "ocean",
                                "show_archived": True,
                            },
                        }
                    ).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": base,
                    },
                    method="POST",
                )
                settings = json.loads(urlopen(theme_request, timeout=2).read())
                directories = json.loads(
                    urlopen(
                        f"{base}/api/directories?path={workspace}",
                        timeout=2,
                    ).read()
                )
                html = urlopen(f"{base}/", timeout=2).read().decode("utf-8")
                script = urlopen(f"{base}/app.js", timeout=2).read().decode("utf-8")
                style = urlopen(f"{base}/app.css", timeout=2).read().decode("utf-8")
                detail = json.loads(
                    urlopen(f"{base}/api/runs/run-http", timeout=2).read()
                )
                rename_request = Request(
                    f"{base}/api/runs/run-http/rename",
                    data=json.dumps({"title": "界面检查已重命名"}).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Origin": base},
                    method="POST",
                )
                renamed = json.loads(urlopen(rename_request, timeout=2).read())
                renamed_detail = json.loads(
                    urlopen(f"{base}/api/runs/run-http", timeout=2).read()
                )
                archive_request = Request(
                    f"{base}/api/runs/run-http/archive",
                    data=json.dumps({"archived": True}).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": base,
                    },
                    method="POST",
                )
                archived = json.loads(urlopen(archive_request, timeout=2).read())
                runs_after_archive = json.loads(
                    urlopen(f"{base}/api/runs", timeout=2).read()
                )
                delete_request = Request(
                    f"{base}/api/runs/run-http",
                    headers={"Origin": base},
                    method="DELETE",
                )
                deleted = json.loads(urlopen(delete_request, timeout=2).read())
                settings["values"]["claude"]["command"] = "/bin/echo"
                settings["values"]["codex"]["command"] = "/bin/echo"
                settings["values"]["ui"]["compact_sidebar"] = True
                settings_request = Request(
                    f"{base}/api/settings",
                    data=json.dumps(
                        {
                            "workspace": settings["workspace"],
                            "revision": settings["revision"],
                            "values": settings["values"],
                        }
                    ).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": base,
                    },
                    method="POST",
                )
                saved_settings = json.loads(
                    urlopen(settings_request, timeout=2).read()
                )
                active = UISession(
                    run_id="live-http",
                    task="可停止任务",
                    workspace=workspace,
                    executor="claude",
                    consensus=False,
                    notify=manager.publish,
                )
                active.bind_stop_handler(lambda: None)
                manager._reserve_session(active)
                stop_request = Request(
                    f"{base}/api/sessions/live-http/stop",
                    data=b"{}",
                    headers={"Content-Type": "application/json", "Origin": base},
                    method="POST",
                )
                stopped = json.loads(urlopen(stop_request, timeout=2).read())
                request = Request(
                    f"{base}/api/tasks",
                    data=json.dumps({"task": "x"}).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "https://malicious.example",
                    },
                    method="POST",
                )
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(request, timeout=2)
                self.assertEqual(rejected.exception.code, 403)
                active.status = "complete"
                shutdown_request = Request(
                    f"{base}/api/shutdown",
                    data=b"{}",
                    headers={"Content-Type": "application/json", "Origin": base},
                    method="POST",
                )
                shutdown = json.loads(urlopen(shutdown_request, timeout=2).read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertTrue(health["ok"])
        self.assertEqual(selected["workspace"], str(selected_workspace.resolve()))
        self.assertEqual(
            switched_health["workspace"],
            str(selected_workspace.resolve()),
        )
        self.assertEqual(runs["runs"][0]["id"], "run-http")
        self.assertIn("MultiAgent 工作台", html)
        self.assertIn("<strong>Claude Code</strong>", html)
        self.assertNotIn("<strong>Claude</strong>", html)
        self.assertIn("新建任务", html)
        self.assertIn('data-context-action="rename"', html)
        self.assertIn('id="rename-run-dialog"', html)
        self.assertIn("已归档", html)
        self.assertIn('id="settings-dialog"', html)
        self.assertIn('id="stop-task-button"', html)
        self.assertIn('id="shutdown-ui-button"', html)
        self.assertIn('id="settings-workspace-browse"', html)
        self.assertIn('id="settings-group-chat-default-agent"', html)
        self.assertIn('id="settings-token-api-key"', html)
        self.assertIn('id="settings-claude-model-order"', html)
        self.assertIn('id="settings-codex-model-order"', html)
        self.assertIn('id="settings-group-chat-agent-a-identity"', html)
        self.assertIn('id="settings-group-chat-agent-b-identity"', html)
        self.assertIn('name="settings-theme"', html)
        self.assertIn('value="ocean"', html)
        self.assertIn('value="graphite"', html)
        self.assertIn('value="botanical"', html)
        self.assertIn('id="composer-mention-menu"', html)
        self.assertIn('data-mention="@Claude"', html)
        self.assertIn('data-mention="@Codex"', html)
        self.assertIn('data-mention="@all"', html)
        self.assertIn('id="run-timeline"', html)
        self.assertIn('id="run-timeline-count"', html)
        self.assertIn("API Key", html)
        self.assertNotIn(">New Task<", html)
        self.assertNotIn(">Run details<", html)
        self.assertIn('class="channel-tabs"', html)
        self.assertIn("formnovalidate", html)
        self.assertIn('href="./app.css"', html)
        self.assertIn("function renderKanban", script)
        self.assertIn("return 'Claude Code';", script)
        self.assertIn("function renderTimeline", script)
        self.assertNotIn("function eventMessage", script)
        self.assertNotIn("phase.includes('review')", script)
        self.assertIn("function renderDirectFileNotice", script)
        self.assertIn("function shutdownUiService", script)
        self.assertIn("function loadWorkspaceDirectory", script)
        self.assertIn("function renderModelOrder", script)
        self.assertIn("dragHandle.addEventListener('dragstart'", script)
        self.assertNotIn("不能直接接入的模型与原因", html)
        self.assertNotIn("function renderModelCompatibility", script)
        self.assertIn("function handleComposerKeydown", script)
        self.assertIn("function updateMentionMenu", script)
        self.assertIn("function insertMention", script)
        self.assertIn("function changeSummaryMarkup", script)
        self.assertIn("function changeFileMarkup", script)
        self.assertIn("checkpoint.change_summary", script)
        self.assertIn("function queuePendingChatMessage", script)
        self.assertIn("function optimisticChatRecipients", script)
        self.assertIn("function reconcilePendingChatMessages", script)
        self.assertIn("function replyLoadingMarkup", script)
        self.assertIn("function refreshDefaultWorkspace", script)
        self.assertIn("function openRunRename", script)
        self.assertIn("function submitRunRename", script)
        self.assertIn("function updateNewTaskMode", script)
        self.assertIn("function applyTheme", script)
        self.assertIn("function saveInterfacePreferences", script)
        self.assertIn("function currentInterfaceSettings", script)
        self.assertIn("/api/settings/interface", script)
        self.assertIn("saveInterfacePreferences({ show_archived:", script)
        self.assertIn("saveInterfacePreferences({ compact_sidebar:", script)
        self.assertIn("saveInterfacePreferences(defaults.values?.ui || {})", script)
        self.assertIn("document.body.dataset.theme = theme", script)
        self.assertIn("el.taskInput.required = !groupChat", script)
        self.assertIn("第一条消息（可选）", script)
        self.assertIn("update.type === 'workspace'", script)
        self.assertIn("run.workspace === workspace", script)
        self.assertIn("scrollChatToBottom", script)
        self.assertIn("!event.shiftKey", script)
        self.assertIn("requestSubmit(el.quickTaskSubmit)", script)
        self.assertIn(".message-row.message-user", style)
        self.assertIn(".message-row.message-claude", style)
        self.assertIn(".message-row.message-codex", style)
        self.assertIn(".change-summary", style)
        self.assertIn(".diff-preview", style)
        self.assertIn(".message-row.message-pending", style)
        self.assertIn(".message-row.message-loading", style)
        self.assertIn("@keyframes reply-bounce", style)
        self.assertIn('body[data-theme="ocean"]', style)
        self.assertIn('body[data-theme="graphite"]', style)
        self.assertIn('body[data-theme="botanical"]', style)
        self.assertEqual(directories["path"], str(workspace.resolve()))
        self.assertEqual(detail["record"]["status"], "complete")
        self.assertEqual(renamed["record"]["display_task"], "界面检查已重命名")
        self.assertEqual(
            renamed_detail["record"]["display_task"],
            "界面检查已重命名",
        )
        self.assertEqual(renamed_detail["record"]["task"], "检查界面")
        self.assertTrue(archived["record"]["archived"])
        self.assertTrue(runs_after_archive["runs"][0]["archived"])
        self.assertEqual(deleted["record"]["id"], "run-http")
        self.assertIsNone(manager.store.get("run-http"))
        self.assertTrue(saved_settings["values"]["ui"]["compact_sidebar"])
        self.assertTrue(saved_settings["values"]["ui"]["show_archived"])
        self.assertEqual(saved_settings["values"]["ui"]["theme"], "ocean")
        self.assertEqual(stopped["status"], "stopping")
        self.assertTrue(shutdown["ok"])


if __name__ == "__main__":
    unittest.main()
