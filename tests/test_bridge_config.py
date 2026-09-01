from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from multiagent_cli.bridge_config import (
    ConfigError,
    _split_command_text,
    _user_config_candidates,
    find_config_path,
    resolve_bridge_settings,
)
from multiagent_cli.bridge_models import (
    LEGACY_GROUP_CHAT_AGENT_A_IDENTITY,
    LEGACY_GROUP_CHAT_AGENT_B_IDENTITY,
)


class BridgeConfigTests(unittest.TestCase):
    def test_windows_command_string_preserves_paths_and_quoted_arguments(self) -> None:
        self.assertEqual(
            _split_command_text('"C:\\Program Files\\Claude\\claude.exe" --label "two words"', os_name="nt"),
            (r"C:\Program Files\Claude\claude.exe", "--label", "two words"),
        )
        self.assertEqual(_split_command_text(r"C:\Tools\codex.exe --flag", os_name="nt"), (r"C:\Tools\codex.exe", "--flag"))

    def test_windows_user_config_uses_roaming_app_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "AppData" / "Roaming"
            current, legacy = _user_config_candidates(home=Path(directory) / "home", environ={"APPDATA": str(base)}, os_name="nt")
        self.assertEqual(current, base / "multiagent" / "config.json")
        self.assertEqual(legacy, base / "mutiagent" / "config.json")

    def test_resolves_group_chat_options_and_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = resolve_bridge_settings(
                {
                    "group_chat_default_agent": "codex",
                    "group_chat_execution": False,
                    "group_chat_identities": {"agent_a": "分析者", "agent_b": "实现者"},
                    "context_compaction": {
                        "enabled": True,
                        "threshold_tokens": 12000,
                        "target_tokens": 6000,
                        "recent_messages": 6,
                    },
                    "claude": {"command": ["/bin/echo", "claude"], "models": ["claude-opus-5", "fallback"]},
                    "codex": {"command": "/bin/echo codex", "models": ["gpt-5.6-sol"], "timeout": 42, "reasoning_effort": "high"},
                },
                workspace=directory,
            )
        self.assertEqual(settings.group_chat_default_agent, "codex")
        self.assertFalse(hasattr(settings, "group_chat_execution"))
        self.assertEqual(settings.group_chat_agent_a_identity, "分析者")
        self.assertEqual(settings.group_chat_agent_b_identity, "实现者")
        self.assertEqual(settings.context_compaction.threshold_tokens, 12000)
        self.assertEqual(settings.context_compaction.target_tokens, 6000)
        self.assertEqual(settings.context_compaction.recent_messages, 6)
        self.assertEqual(settings.claude.models, ("claude-opus-5", "fallback"))
        self.assertEqual(settings.codex.timeout, 42)
        self.assertEqual(settings.codex.reasoning_effort, "high")

    def test_codex_reasoning_effort_defaults_to_native_and_rejects_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            automatic = resolve_bridge_settings(
                {
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo", "reasoning_effort": "auto"},
                },
                workspace=directory,
            )
            self.assertIsNone(automatic.codex.reasoning_effort)
            with self.assertRaisesRegex(ConfigError, "reasoning_effort"):
                resolve_bridge_settings(
                    {
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo", "reasoning_effort": "extreme"},
                    },
                    workspace=directory,
                )

    def test_resolves_claude_permission_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = resolve_bridge_settings(
                {
                    "claude": {"command": "/bin/echo", "permission_mode": "auto"},
                    "codex": {"command": "/bin/echo"},
                },
                workspace=directory,
            )
            self.assertEqual(settings.claude.permission_mode, "auto")
            with self.assertRaisesRegex(ConfigError, "permission_mode"):
                resolve_bridge_settings(
                    {
                        "claude": {"command": "/bin/echo", "permission_mode": "bypassPermissions"},
                        "codex": {"command": "/bin/echo"},
                    },
                    workspace=directory,
                )
            with self.assertRaisesRegex(ConfigError, "reasoning_effort"):
                resolve_bridge_settings(
                    {
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo", "reasoning_effort": []},
                    },
                    workspace=directory,
                )

    def test_resolves_worktree_toggle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = resolve_bridge_settings(
                {
                    "worktree": False,
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                },
                workspace=directory,
            )
        self.assertFalse(settings.worktree)

    def test_worktree_defaults_to_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = resolve_bridge_settings(
                {
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                },
                workspace=directory,
            )
        self.assertTrue(settings.worktree)

    def test_rejects_invalid_worktree_toggle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "worktree"):
                resolve_bridge_settings({"worktree": "false"}, workspace=directory)

    def test_default_group_chat_identities_are_shared(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = resolve_bridge_settings(
                {
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                },
                workspace=directory,
            )

        self.assertEqual(
            settings.group_chat_agent_a_identity,
            settings.group_chat_agent_b_identity,
        )
        self.assertIn("MultiAgent 群聊", settings.group_chat_agent_a_identity)

    def test_legacy_builtin_group_chat_identities_migrate_to_shared_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = resolve_bridge_settings(
                {
                    "group_chat_identities": {
                        "agent_a": LEGACY_GROUP_CHAT_AGENT_A_IDENTITY,
                        "agent_b": LEGACY_GROUP_CHAT_AGENT_B_IDENTITY,
                    },
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                },
                workspace=directory,
            )

        self.assertEqual(
            settings.group_chat_agent_a_identity,
            settings.group_chat_agent_b_identity,
        )
        self.assertIn("MultiAgent 群聊", settings.group_chat_agent_a_identity)

    def test_custom_group_chat_identities_remain_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = resolve_bridge_settings(
                {
                    "group_chat_identities": {
                        "agent_a": "自定义身份 A",
                        "agent_b": "自定义身份 B",
                    },
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                },
                workspace=directory,
            )

        self.assertEqual(settings.group_chat_agent_a_identity, "自定义身份 A")
        self.assertEqual(settings.group_chat_agent_b_identity, "自定义身份 B")

    def test_resolves_token_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = resolve_bridge_settings(
                {
                    "token_api": {"enabled": True, "base_url": "https://tokencheap.io/"},
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                },
                workspace=directory,
            )
        self.assertTrue(settings.token_api.enabled)
        self.assertEqual(settings.token_api.base_url, "https://tokencheap.io")

    def test_rejects_invalid_group_chat_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "group_chat_default_agent"):
                resolve_bridge_settings({"group_chat_default_agent": "everyone"}, workspace=directory)

    def test_rejects_context_compaction_target_at_or_above_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "target_tokens"):
                resolve_bridge_settings(
                    {
                        "context_compaction": {
                            "threshold_tokens": 1000,
                            "target_tokens": 1000,
                        },
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    },
                    workspace=directory,
                )

    def test_rejects_known_cross_protocol_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "不兼容"):
                resolve_bridge_settings({"claude": {"command": "/bin/echo"}, "codex": {"command": "/bin/echo", "models": ["claude-opus-5"]}}, workspace=directory)

    def test_rejects_invalid_workspace(self) -> None:
        with self.assertRaisesRegex(ConfigError, "工作区不是有效目录"):
            resolve_bridge_settings({}, workspace=Path("/definitely/missing/workspace"))

    def test_parses_project_config_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / ".multiagent.json"
            project.write_text("{}", encoding="utf-8")
            self.assertEqual(find_config_path(None, directory), project.resolve())
            project.unlink()
            legacy = Path(directory) / ".mutiagent.json"
            legacy.write_text("{}", encoding="utf-8")
            self.assertEqual(find_config_path(None, directory), legacy.resolve())


if __name__ == "__main__":
    unittest.main()
