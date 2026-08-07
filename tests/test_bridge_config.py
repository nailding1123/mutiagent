from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path

from multiagent_cli.bridge_config import (
    ConfigError,
    _split_command_text,
    _user_config_candidates,
    find_config_path,
    resolve_bridge_settings,
)


class BridgeConfigTests(unittest.TestCase):
    def test_windows_command_string_preserves_paths_and_quoted_arguments(self) -> None:
        values = _split_command_text(
            '"C:\\Program Files\\Claude\\claude.exe" --label "two words"',
            os_name="nt",
        )
        unquoted = _split_command_text(
            "C:\\Tools\\codex.exe --flag",
            os_name="nt",
        )

        self.assertEqual(
            values,
            (r"C:\Program Files\Claude\claude.exe", "--label", "two words"),
        )
        self.assertEqual(unquoted, (r"C:\Tools\codex.exe", "--flag"))

    def test_windows_user_config_uses_roaming_app_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "AppData" / "Roaming"

            current, legacy = _user_config_candidates(
                home=Path(directory) / "home",
                environ={"APPDATA": str(base)},
                os_name="nt",
            )

        self.assertEqual(current, base / "multiagent" / "config.json")
        self.assertEqual(legacy, base / "mutiagent" / "config.json")

    def test_resolves_group_chat_mode_and_cli_spelling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            configured = resolve_bridge_settings(
                {
                    "collaboration_mode": "group_chat",
                    "group_chat_default_agent": "codex",
                    "group_chat_execution": False,
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                },
                workspace=directory,
            )
            overridden = resolve_bridge_settings(
                {
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                },
                workspace=directory,
                collaboration_mode="group-chat",
            )

        self.assertEqual(configured.collaboration_mode, "group_chat")
        self.assertEqual(configured.group_chat_default_agent, "codex")
        self.assertFalse(configured.group_chat_execution)
        self.assertEqual(overridden.collaboration_mode, "group_chat")

    def test_rejects_invalid_group_chat_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "group_chat_default_agent"):
                resolve_bridge_settings(
                    {
                        "group_chat_default_agent": "everyone",
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    },
                    workspace=directory,
                )

    def test_rejects_unknown_collaboration_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "collaboration_mode"):
                resolve_bridge_settings(
                    {
                        "collaboration_mode": "debate",
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    },
                    workspace=directory,
                )

    def test_resolves_explicit_cli_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = resolve_bridge_settings(
                {
                    "executor": "codex",
                    "review_rounds": 2,
                    "claude": {"command": ["/bin/echo", "claude"], "model": "opus"},
                    "codex": {"command": "/bin/echo codex"},
                },
                workspace=directory,
            )

        self.assertEqual(settings.executor, "codex")
        self.assertEqual(settings.review_rounds, 2)
        self.assertTrue(settings.planning_collaboration)
        self.assertFalse(settings.consensus)
        self.assertEqual(settings.max_consensus_rounds, 3)
        self.assertTrue(settings.plan_approval)
        self.assertEqual(settings.max_plan_revisions, 2)
        self.assertEqual(settings.claude.command, ("/bin/echo", "claude"))
        self.assertEqual(settings.claude.model, "opus")
        self.assertEqual(settings.codex.command, ("/bin/echo", "codex"))

    def test_resolves_token_api_and_ordered_model_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = resolve_bridge_settings(
                {
                    "token_api": {
                        "enabled": True,
                        "base_url": "https://tokencheap.io/",
                    },
                    "claude": {
                        "command": "/bin/echo",
                        "models": ["claude-opus-5", "gemini-3.5-flash"],
                        "fallback_on_timeout": True,
                    },
                    "codex": {
                        "command": "/bin/echo",
                        "models": ["gpt-5.6-sol", "gpt-5.5"],
                        "fallback_on_timeout": False,
                    },
                },
                workspace=directory,
            )

        self.assertTrue(settings.token_api.enabled)
        self.assertEqual(settings.token_api.base_url, "https://tokencheap.io")
        self.assertEqual(settings.claude.model, "claude-opus-5")
        self.assertEqual(
            settings.claude.models,
            ("claude-opus-5", "gemini-3.5-flash"),
        )
        self.assertFalse(settings.codex.fallback_on_timeout)

    def test_rejects_known_cross_protocol_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "不兼容"):
                resolve_bridge_settings(
                    {
                        "claude": {"command": "/bin/echo"},
                        "codex": {
                            "command": "/bin/echo",
                            "models": ["claude-opus-5"],
                        },
                    },
                    workspace=directory,
                )

    def test_resolves_equal_collaboration_executor_and_agent_identities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = resolve_bridge_settings(
                {
                    "executor": "codex",
                    "identities": {
                        "agent_a": "Claude 的对等身份",
                        "agent_b": "Codex 的对等身份",
                    },
                    "group_chat_identities": {
                        "agent_a": "Claude 的群聊身份",
                        "agent_b": "Codex 的群聊身份",
                    },
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                },
                workspace=directory,
            )

        self.assertEqual(settings.executor, "codex")
        self.assertEqual(settings.agent_a_identity, "Claude 的对等身份")
        self.assertEqual(settings.agent_b_identity, "Codex 的对等身份")
        self.assertEqual(
            settings.group_chat_agent_a_identity,
            "Claude 的群聊身份",
        )
        self.assertEqual(
            settings.group_chat_agent_b_identity,
            "Codex 的群聊身份",
        )

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

    def test_consensus_requires_planning_collaboration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "planning_collaboration"):
                resolve_bridge_settings(
                    {
                        "consensus": True,
                        "planning_collaboration": False,
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    },
                    workspace=directory,
                )

    def test_resolves_custom_identities_and_agent_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = resolve_bridge_settings(
                {
                    "identities": {
                        "agent_a": "Agent A 身份",
                        "agent_b": "Agent B 身份",
                    },
                    "claude": {"command": "/bin/echo", "timeout": 42},
                    "codex": {"command": "/bin/echo"},
                },
                workspace=directory,
            )

        self.assertEqual(settings.agent_a_identity, "Agent A 身份")
        self.assertEqual(settings.agent_b_identity, "Agent B 身份")
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
            config = Path(directory) / ".multiagent.json"
            config.write_text("{}", encoding="utf-8")

            found = find_config_path(None, directory)

        self.assertEqual(found, config.resolve())

    def test_legacy_misspelled_project_config_is_still_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / ".mutiagent.json"
            config.write_text("{}", encoding="utf-8")

            found = find_config_path(None, directory)

        self.assertEqual(found, config.resolve())


if __name__ == "__main__":
    unittest.main()
