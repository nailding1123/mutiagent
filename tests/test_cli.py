from __future__ import annotations

import json
import io
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from multiagent_cli.bridge_config import resolve_bridge_settings
from multiagent_cli.cli import (
    VERSION,
    _apply_resume_settings,
    _auth_summary,
    _init_config,
    _settings_snapshot,
    _run_once,
    build_parser,
)
from multiagent_cli.bridge_models import AgentRunResult
from multiagent_cli.renderer import ConsoleRenderer
from multiagent_cli.run_store import RunStore


class CliTests(unittest.TestCase):
    def test_run_uses_isolated_worktree_without_touching_source_checkout(self) -> None:
        class WritingAdapter:
            display_name = "Claude"

            def run(self, _prompt, **kwargs):
                (Path(kwargs["workspace"]) / "agent.txt").write_text(
                    "isolated", encoding="utf-8"
                )
                return AgentRunResult("Claude", "实现完成", session_id="session-1")

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
            (repository / "base.txt").write_text("base", encoding="utf-8")
            subprocess.run(["git", "add", "base.txt"], cwd=repository, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "initial"], cwd=repository, check=True
            )
            base = resolve_bridge_settings(
                {
                    "worktree": True,
                    "requirement_review": False,
                    "review_rounds": 0,
                    "plan_approval": False,
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                },
                workspace=repository,
            )
            store = RunStore(root / "state" / "runs")
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
            self.assertFalse((repository / "agent.txt").exists())
            self.assertTrue((Path(record["workspace"]) / "agent.txt").is_file())
            self.assertIn("worktree", record)

    def test_run_persists_complete_checkpoint_collaboration_and_quality(self) -> None:
        class FakeAdapter:
            display_name = "Claude"

            def run(self, _prompt, **_kwargs):
                return AgentRunResult("Claude", "实现完成", session_id="session-1")

        with tempfile.TemporaryDirectory() as directory:
            base = resolve_bridge_settings(
                {
                    "requirement_review": False,
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

    def test_parser_accepts_bridge_options_and_task(self) -> None:
        args = build_parser().parse_args(
            [
                "--lead",
                "codex",
                "--rounds",
                "2",
                "--consensus",
                "--no-requirement-review",
                "--yes",
                "--verbose-events",
                "修复",
                "测试",
            ]
        )

        self.assertEqual(args.lead, "codex")
        self.assertEqual(args.rounds, 2)
        self.assertTrue(args.consensus)
        self.assertTrue(args.no_requirement_review)
        self.assertTrue(args.yes)
        self.assertTrue(args.verbose_events)
        self.assertEqual(args.task, ["修复", "测试"])

    def test_bridge_version_is_current(self) -> None:
        self.assertEqual(VERSION, "1.1.0")

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
                (Path(directory) / ".mutiagent.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result, 0)
        self.assertFalse(data["consensus"])
        self.assertIn("lead", data["identities"])
        self.assertEqual(data["claude"]["timeout"], 900)

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
                claude=replace(base.claude, model="opus", timeout=45),
                codex=replace(base.codex, model="gpt", timeout=60),
            )
            restored = _apply_resume_settings(
                base, {"settings": _settings_snapshot(original)}
            )

        self.assertFalse(restored.plan_approval)
        self.assertEqual(restored.claude.model, "opus")
        self.assertEqual(restored.codex.model, "gpt")
        self.assertEqual(restored.claude.timeout, 45)
        self.assertEqual(restored.codex.timeout, 60)


if __name__ == "__main__":
    unittest.main()
