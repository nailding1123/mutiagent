from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path

from multiagent_cli.bridge_config import find_config_path, resolve_bridge_settings
from multiagent_cli.config import ConfigError


class BridgeConfigTests(unittest.TestCase):
    def test_worktree_can_be_enabled_in_project_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = resolve_bridge_settings(
                {
                    "worktree": True,
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                },
                workspace=directory,
            )

        self.assertTrue(settings.worktree)

    def test_resolves_explicit_cli_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = resolve_bridge_settings(
                {
                    "lead": "codex",
                    "review_rounds": 2,
                    "claude": {"command": ["/bin/echo", "claude"], "model": "opus"},
                    "codex": {"command": "/bin/echo codex"},
                },
                workspace=directory,
            )

        self.assertEqual(settings.lead, "codex")
        self.assertEqual(settings.review_rounds, 2)
        self.assertTrue(settings.requirement_review)
        self.assertFalse(settings.consensus)
        self.assertEqual(settings.max_consensus_rounds, 3)
        self.assertTrue(settings.plan_approval)
        self.assertEqual(settings.max_plan_revisions, 2)
        self.assertEqual(settings.claude.command, ("/bin/echo", "claude"))
        self.assertEqual(settings.claude.model, "opus")
        self.assertEqual(settings.codex.command, ("/bin/echo", "codex"))

    def test_enables_consensus_from_config_or_cli_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            configured = resolve_bridge_settings(
                {
                    "consensus": True,
                    "max_consensus_rounds": 5,
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                },
                workspace=directory,
            )
            overridden = resolve_bridge_settings(
                {
                    "consensus": True,
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                },
                workspace=directory,
                consensus=False,
            )

        self.assertTrue(configured.consensus)
        self.assertEqual(configured.max_consensus_rounds, 5)
        self.assertFalse(overridden.consensus)

    def test_consensus_requires_requirement_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "requirement_review"):
                resolve_bridge_settings(
                    {
                        "consensus": True,
                        "requirement_review": False,
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    },
                    workspace=directory,
                )

    def test_resolves_custom_identities_and_agent_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = resolve_bridge_settings(
                {
                    "identities": {"lead": "主身份", "reviewer": "审查身份"},
                    "claude": {"command": "/bin/echo", "timeout": 42},
                    "codex": {"command": "/bin/echo"},
                },
                workspace=directory,
            )

        self.assertEqual(settings.lead_identity, "主身份")
        self.assertEqual(settings.reviewer_identity, "审查身份")
        self.assertEqual(settings.claude.timeout, 42)

    def test_rejects_invalid_workspace(self) -> None:
        with self.assertRaisesRegex(ConfigError, "工作区不是有效目录"):
            resolve_bridge_settings({}, workspace=Path("/definitely/missing/workspace"))

    def test_rejects_negative_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "review_rounds"):
                resolve_bridge_settings(
                    {
                        "review_rounds": -1,
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    },
                    workspace=directory,
                )

    def test_parses_deterministic_verification_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = resolve_bridge_settings(
                {
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                    "verification": {
                        "timeout": 10,
                        "commands": [
                            {
                                "name": "unit",
                                "command": [sys.executable, "-m", "unittest"],
                                "timeout": 20,
                            }
                        ],
                    },
                },
                workspace=directory,
            )

        self.assertEqual(settings.verification_commands[0].name, "unit")
        self.assertEqual(settings.verification_commands[0].timeout, 20)

    def test_project_config_is_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / ".mutiagent.json"
            config.write_text("{}", encoding="utf-8")

            found = find_config_path(None, directory)

        self.assertEqual(found, config.resolve())


if __name__ == "__main__":
    unittest.main()
