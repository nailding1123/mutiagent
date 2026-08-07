from __future__ import annotations

import json
import io
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from multiagent_cli.bridge_config import resolve_bridge_settings
from multiagent_cli.cli import (
    VERSION,
    _apply_resume_settings,
    _auth_summary,
    _confirm_plan,
    _handle_run_event,
    _init_config,
    _run_group_chat_message,
    _settings_snapshot,
    _task_command,
    _run_once,
    build_parser,
    main,
)
from multiagent_cli.bridge_models import (
    AgentEvent,
    AgentRunResult,
    ConsensusLimitReached,
    VerificationCommand,
    WorkspaceSnapshot,
)
from multiagent_cli.checkpoints import WorkflowCheckpoint
from multiagent_cli.collaboration import CollaborationState
from multiagent_cli.renderer import ConsoleRenderer
from multiagent_cli.run_store import RunStore
from multiagent_cli.workspace_state import workspace_fingerprint_matches


class CliTests(unittest.TestCase):
    def test_task_command_only_accepts_a_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = RunStore(workspace / "state")
            store.start(
                task="查看任务",
                workspace=workspace,
                executor="claude",
                consensus=False,
                run_id="task-detail",
            )
            output = io.StringIO()
            renderer = ConsoleRenderer(color=False, stream=output)

            shown = _task_command(["task-detail"], store, renderer)
            with patch("sys.stderr", new=io.StringIO()):
                rejected = _task_command(["diff", "task-detail"], store, renderer)

        self.assertEqual(shown, 0)
        self.assertIn("task-detail", output.getvalue())
        self.assertEqual(rejected, 2)

    def test_resume_uses_snapshot_after_original_config_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = RunStore(root / "state")
            settings = resolve_bridge_settings(
                {
                    "planning_collaboration": False,
                    "plan_approval": False,
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                },
                workspace=root,
                config_path=root / "removed.json",
            )
            store.start(
                task="恢复任务",
                workspace=root,
                executor="claude",
                consensus=False,
                run_id="resume-without-config",
                settings_snapshot=_settings_snapshot(settings),
            )
            store.update("resume-without-config", status="failed")

            with (
                patch.dict(os.environ, {"MULTIAGENT_STATE_DIR": str(store.root)}),
                patch("multiagent_cli.cli._make_adapters", return_value={}),
                patch("multiagent_cli.cli._run_once", return_value=0) as run_once,
            ):
                result = main(["resume", "resume-without-config"])

            self.assertEqual(result, 0)
            self.assertEqual(run_once.call_args.args[0].workspace, root.resolve())

    def test_consensus_limit_automatically_exports_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            settings = resolve_bridge_settings(
                {
                    "planning_collaboration": False,
                    "plan_approval": False,
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                },
                workspace=workspace,
            )
            collaboration = CollaborationState.create(
                agent_a="claude",
                agent_b="codex",
                planning_collaboration=False,
                executor="claude",
            )
            collaboration.set_canonical_proposal(
                "尚未通过的统一方案",
                author="claude",
                version=1,
            )
            checkpoint = WorkflowCheckpoint(
                task="实现功能",
                workspace=str(workspace),
                executor="claude",
                phase="consensus_review_v1_complete",
                baseline=WorkspaceSnapshot(False),
                collaboration=collaboration,
            )
            checkpoint.set_artifact(
                "unified_proposal",
                AgentRunResult("Claude", "尚未通过的统一方案"),
            )

            def stop_at_limit(_orchestrator, _task, **kwargs):
                kwargs["on_checkpoint"](checkpoint)
                raise ConsensusLimitReached("共识审核轮次已达到上限")

            store = RunStore(Path(directory) / "state")
            renderer = ConsoleRenderer(color=False, stream=io.StringIO())
            with patch(
                "multiagent_cli.cli.BridgeOrchestrator.run",
                new=stop_at_limit,
            ):
                result = _run_once(
                    settings,
                    {"claude": object(), "codex": object()},
                    "实现功能",
                    renderer,
                    store=store,
                )
            record = store.latest()
            document = Path(record["technical_document"])
            content = document.read_text(encoding="utf-8")
            refreshed = WorkflowCheckpoint.from_dict(record["checkpoint"])
            fingerprint_matches = bool(
                refreshed
                and workspace_fingerprint_matches(
                    refreshed.workspace_fingerprint,
                    workspace,
                )
            )

        self.assertEqual(result, 1)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(document.suffix, ".md")
        self.assertIn("共识状态：**未达成共识**", content)
        self.assertIn("已达到最大共识审核轮次", content)
        self.assertIsNotNone(refreshed)
        self.assertTrue(fingerprint_matches)

    def test_plan_gate_can_export_document_then_execute(self) -> None:
        output = io.StringIO()
        renderer = ConsoleRenderer(color=False, stream=output)
        exports: list[Path] = []

        def export() -> Path:
            path = Path("/tmp/final-technical-plan.md")
            exports.append(path)
            return path

        with patch("builtins.input", side_effect=["d", "e"]):
            decision = _confirm_plan(
                renderer,
                AgentRunResult("Claude", "Agent A 方案"),
                AgentRunResult("Codex", "Agent B 方案"),
                (
                    AgentRunResult("Claude", "A 审核 B"),
                    AgentRunResult("Codex", "B 审核 A"),
                ),
                AgentRunResult("Claude", "统一方案"),
                None,
                0,
                on_export=export,
            )

        self.assertEqual(decision.action, "approve")
        self.assertEqual(exports, [Path("/tmp/final-technical-plan.md")])
        self.assertIn("导出最终技术文档", output.getvalue())
        self.assertIn("final-technical-plan.md", output.getvalue())

    def test_plan_gate_can_send_requirement_to_one_agent(self) -> None:
        output = io.StringIO()
        renderer = ConsoleRenderer(color=False, stream=output)

        with patch(
            "builtins.input",
            side_effect=["t", "b", "只调整数据库迁移策略"],
        ):
            decision = _confirm_plan(
                renderer,
                AgentRunResult("Claude", "Agent A 方案"),
                AgentRunResult("Codex", "Agent B 方案"),
                (
                    AgentRunResult("Claude", "A 审核 B"),
                    AgentRunResult("Codex", "B 审核 A"),
                ),
                AgentRunResult("Claude", "统一方案"),
                None,
                0,
            )

        self.assertEqual(decision.action, "targeted_revision")
        self.assertEqual(decision.target_agent, "codex")
        self.assertEqual(decision.feedback, "只调整数据库迁移策略")
        self.assertIn("单独给某个 Agent 提要求", output.getvalue())

    @unittest.skipIf(os.name == "nt", "POSIX launcher test")
    def test_launcher_resolves_project_path_through_symlink(self) -> None:
        launcher = Path(__file__).resolve().parents[1] / "bin" / "multiagent"
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / "multiagent"
            link.symlink_to(launcher)
            completed = subprocess.run(
                [str(link), "--version"],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.strip(), "multiagent 2.5.0")

    @unittest.skipUnless(os.name == "nt", "Windows launcher test")
    def test_windows_source_launcher_resolves_project_path(self) -> None:
        launcher = Path(__file__).resolve().parents[1] / "bin" / "multiagent.cmd"
        command = f'"{launcher}" --version'
        completed = subprocess.run(
            [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", command],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.strip(), "multiagent 2.5.0")

    def test_new_run_writes_target_workspace(self) -> None:
        class WritingAdapter:
            display_name = "Claude"

            def run(self, _prompt, **kwargs):
                (Path(kwargs["workspace"]) / "agent.txt").write_text(
                    "direct", encoding="utf-8"
                )
                return AgentRunResult("Claude", "实现完成", session_id="session-1")

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            repository.mkdir()
            base = resolve_bridge_settings(
                {
                    "planning_collaboration": False,
                    "review_rounds": 0,
                    "plan_approval": False,
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                },
                workspace=repository,
            )
            store = RunStore(Path(directory) / "state" / "runs")
            renderer = ConsoleRenderer(color=False, stream=io.StringIO())
            result = _run_once(
                base,
                {"claude": WritingAdapter(), "codex": WritingAdapter()},
                "完成任务",
                renderer,
                store=store,
            )
            record = store.latest()

            self.assertEqual(result, 0)
            self.assertTrue((repository / "agent.txt").is_file())
            self.assertEqual(Path(record["workspace"]).resolve(), repository.resolve())

    def test_run_persists_complete_checkpoint_collaboration_and_quality(self) -> None:
        class FakeAdapter:
            display_name = "Claude"

            def run(self, _prompt, **_kwargs):
                return AgentRunResult("Claude", "实现完成", session_id="session-1")

        with tempfile.TemporaryDirectory() as directory:
            base = resolve_bridge_settings(
                {
                    "planning_collaboration": False,
                    "review_rounds": 0,
                    "plan_approval": False,
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                },
                workspace=directory,
            )
            store = RunStore(Path(directory) / "state")
            renderer = ConsoleRenderer(color=False, stream=io.StringIO())
            result = _run_once(
                base,
                {"claude": FakeAdapter(), "codex": FakeAdapter()},
                "完成任务",
                renderer,
                store=store,
            )
            record = store.latest()

        self.assertEqual(result, 0)
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["checkpoint"]["phase"], "complete")
        self.assertIn("tasks", record["collaboration"])
        self.assertIn("verification_total", record["quality"])
        self.assertTrue(record["events"])
        self.assertTrue(
            all(event["protocol"] == "multiagent.event.v2" for event in record["events"])
        )
        self.assertTrue(all(event["timestamp"] for event in record["events"]))

    def test_parser_accepts_bridge_options_and_task(self) -> None:
        args = build_parser().parse_args(
            [
                "--executor",
                "codex",
                "--rounds",
                "2",
                "--mode",
                "group-chat",
                "--consensus",
                "--no-planning-collaboration",
                "--yes",
                "--verbose-events",
                "--no-progress",
                "修复",
                "测试",
            ]
        )

        self.assertEqual(args.executor, "codex")
        self.assertEqual(args.rounds, 2)
        self.assertEqual(args.mode, "group-chat")
        self.assertTrue(args.consensus)
        self.assertTrue(args.no_planning_collaboration)
        self.assertTrue(args.yes)
        self.assertTrue(args.verbose_events)
        self.assertFalse(args.progress)
        self.assertEqual(args.task, ["修复", "测试"])

    def test_bridge_version_is_current(self) -> None:
        self.assertEqual(VERSION, "2.5.0")
        self.assertEqual(build_parser().prog, "multiagent")

    def test_ui_command_starts_local_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("multiagent_cli.ui_server.serve_ui", return_value=0) as serve:
                from multiagent_cli.cli import main

                result = main(
                    [
                        "--workspace",
                        directory,
                        "--port",
                        "9876",
                        "--no-open",
                        "ui",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertEqual(serve.call_args.kwargs["port"], 9876)
        self.assertFalse(serve.call_args.kwargs["open_browser"])

    def test_ui_command_defaults_workspace_to_invocation_directory(self) -> None:
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                with patch(
                    "multiagent_cli.ui_server.serve_ui",
                    return_value=0,
                ) as serve:
                    from multiagent_cli.cli import main

                    result = main(["--no-open", "ui"])
            finally:
                os.chdir(previous)

        self.assertEqual(result, 0)
        self.assertEqual(
            serve.call_args.kwargs["workspace"],
            Path(directory).resolve(),
        )

    def test_show_details_is_verbose_events_alias(self) -> None:
        args = build_parser().parse_args(["--show-details", "检查功能"])

        self.assertTrue(args.verbose_events)

    def test_doctor_can_opt_in_to_a_real_model_probe(self) -> None:
        args = build_parser().parse_args(["--probe-models", "doctor"])

        self.assertTrue(args.probe_models)
        self.assertEqual(args.task, ["doctor"])

    def test_init_creates_a_safe_project_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = _init_config(Path(directory))
            data = json.loads(
                (Path(directory) / ".multiagent.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result, 0)
        self.assertFalse(data["consensus"])
        self.assertEqual(data["collaboration_mode"], "workflow")
        self.assertEqual(data["executor"], "claude")
        self.assertIn("agent_a", data["identities"])
        self.assertIn("agent_b", data["identities"])
        self.assertIn("agent_a", data["group_chat_identities"])
        self.assertIn("agent_b", data["group_chat_identities"])
        self.assertEqual(data["claude"]["timeout"], 900)
        self.assertFalse(data["ui"]["show_archived"])
        self.assertFalse(data["ui"]["compact_sidebar"])
        self.assertEqual(data["ui"]["theme"], "paper")

    def test_cli_group_chat_persists_both_agent_replies(self) -> None:
        class ChatAdapter:
            def __init__(self, name: str, reply: str) -> None:
                self.display_name = name
                self.reply = reply
                self.calls = []

            def run(self, prompt, **kwargs):
                self.calls.append({"prompt": prompt, **kwargs})
                return AgentRunResult(self.display_name, self.reply)

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            settings = resolve_bridge_settings(
                {
                    "collaboration_mode": "group_chat",
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                },
                workspace=workspace,
            )
            store = RunStore(workspace / "state")
            renderer = ConsoleRenderer(color=False, stream=io.StringIO())
            claude = ChatAdapter("Claude", "Claude 回复")
            codex = ChatAdapter("Codex", "Codex 回复")
            with patch("sys.stdout", new=io.StringIO()):
                result, run_id, _engine = _run_group_chat_message(
                    settings,
                    {"claude": claude, "codex": codex},
                    "比较两个方案",
                    renderer,
                    store,
                )
            record = store.get(run_id)

        self.assertEqual(result, 0)
        self.assertEqual(record["status"], "ready")
        self.assertEqual(record["collaboration_mode"], "group_chat")
        self.assertEqual(len(record["group_chat"]["messages"]), 3)

    def test_auth_summary_is_readable_and_never_exposes_key_fragments(self) -> None:
        claude = _auth_summary(
            "claude",
            '{"loggedIn":true,"authMethod":"oauth_token","apiProvider":"gateway"}',
            True,
        )
        codex = _auth_summary(
            "codex", "Logged in using an API key - sk-secret123", True
        )

        self.assertEqual(claude, "已登录 · oauth_token · gateway")
        self.assertEqual(codex, "Logged in using an API key")
        self.assertNotIn("secret123", codex)

    def test_ephemeral_agent_events_do_not_rewrite_the_run_record(self) -> None:
        class CountingStore:
            def __init__(self) -> None:
                self.calls = 0

            def mutate(self, _run_id, _callback) -> None:
                self.calls += 1

        store = CountingStore()
        renderer = ConsoleRenderer(color=False, stream=io.StringIO())

        _handle_run_event(
            AgentEvent("Claude", "tool", "Read: secret.py"),
            renderer,
            store,
            "run-1",
        )
        _handle_run_event(
            AgentEvent("Claude", "progress", "private reasoning"),
            renderer,
            store,
            "run-1",
        )

        self.assertEqual(store.calls, 0)

    def test_resume_snapshot_restores_models_timeouts_and_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = resolve_bridge_settings(
                {
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                },
                workspace=directory,
            )
            original = replace(
                base,
                plan_approval=False,
                verification_commands=(
                    VerificationCommand(
                        "unit", ("python3", "-m", "unittest"), timeout=12
                    ),
                ),
                claude=replace(
                    base.claude,
                    command=("/opt/claude",),
                    model="opus",
                    timeout=45,
                    extra_args=("--strict",),
                ),
                codex=replace(
                    base.codex,
                    command=("/opt/codex",),
                    model="gpt",
                    timeout=60,
                    extra_args=("--profile", "safe"),
                ),
            )
            restored = _apply_resume_settings(
                base, {"settings": _settings_snapshot(original)}
            )

        self.assertFalse(restored.plan_approval)
        self.assertEqual(restored.claude.model, "opus")
        self.assertEqual(restored.codex.model, "gpt")
        self.assertEqual(restored.claude.timeout, 45)
        self.assertEqual(restored.codex.timeout, 60)
        self.assertEqual(restored.claude.command, ("/opt/claude",))
        self.assertEqual(restored.codex.command, ("/opt/codex",))
        self.assertEqual(restored.claude.extra_args, ("--strict",))
        self.assertEqual(restored.codex.extra_args, ("--profile", "safe"))
        self.assertEqual(restored.verification_commands[0].name, "unit")
        self.assertEqual(
            restored.verification_commands[0].command,
            ("python3", "-m", "unittest"),
        )
        self.assertEqual(restored.verification_commands[0].timeout, 12)


if __name__ == "__main__":
    unittest.main()
