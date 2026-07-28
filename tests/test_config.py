from __future__ import annotations

import unittest
from unittest.mock import patch

from multiagent_cli.config import ConfigError, resolve_settings


class ConfigTests(unittest.TestCase):
    @patch.dict("os.environ", {}, clear=True)
    def test_anthropic_protocol_uses_messages_endpoint(self) -> None:
        settings = resolve_settings(
            {
                "protocol": "anthropic",
                "base_url": "https://tokencheap.io",
                "agents": [{"model": "a"}, {"model": "b"}],
            }
        )

        self.assertEqual(settings.protocol, "anthropic")
        self.assertEqual(settings.endpoint, "v1/messages")
        self.assertEqual(settings.anthropic_version, "2023-06-01")
        self.assertEqual(settings.api_key_env, "ANTHROPIC_AUTH_TOKEN")

    @patch.dict("os.environ", {}, clear=True)
    def test_resolves_agents_and_parameter_overrides(self) -> None:
        settings = resolve_settings(
            {
                "base_url": "https://relay.example/v1",
                "parameters": {"temperature": 0.5, "max_tokens": 1000},
                "agents": [
                    {"name": "作者", "model": "a"},
                    {
                        "name": "编辑",
                        "model": "b",
                        "parameters": {"temperature": 0.1},
                    },
                ],
            }
        )

        self.assertEqual(settings.base_url, "https://relay.example/v1")
        self.assertEqual([agent.type for agent in settings.agents], ["draft", "review"])
        self.assertEqual(settings.agents[0].parameters["temperature"], 0.5)
        self.assertEqual(settings.agents[1].parameters["temperature"], 0.1)
        self.assertEqual(settings.agents[1].parameters["max_tokens"], 1000)

    @patch.dict("os.environ", {}, clear=True)
    def test_repeated_model_flags_create_agents(self) -> None:
        settings = resolve_settings(
            {}, base_url="https://relay.example/v1", models=["a", "b", "c"]
        )

        self.assertEqual([agent.name for agent in settings.agents], ["Agent A", "Agent B", "Agent C"])

    @patch.dict("os.environ", {}, clear=True)
    def test_reads_api_key_and_expands_agent_count(self) -> None:
        settings = resolve_settings(
            {
                "base_url": "https://relay.example/v1",
                "api_key": "secret-key",
                "agents": [
                    {"name": "作者", "type": "draft", "model": "a"},
                    {"name": "审稿", "type": "review", "model": "b", "count": 2},
                    {"name": "主编", "type": "final", "model": "c"},
                ],
            }
        )

        self.assertEqual(settings.api_key, "secret-key")
        self.assertNotIn("secret-key", repr(settings))
        self.assertEqual(
            [agent.name for agent in settings.agents],
            ["作者", "审稿 1", "审稿 2", "主编"],
        )
        self.assertEqual(
            [agent.type for agent in settings.agents],
            ["draft", "review", "review", "final"],
        )

    @patch.dict("os.environ", {}, clear=True)
    def test_rejects_invalid_type_order(self) -> None:
        with self.assertRaisesRegex(ConfigError, "final 类型只能用于最后一个 Agent"):
            resolve_settings(
                {
                    "agents": [
                        {"type": "draft", "model": "a"},
                        {"type": "final", "model": "b"},
                        {"type": "review", "model": "c"},
                    ]
                },
                base_url="https://relay.example/v1",
            )

    @patch.dict("os.environ", {}, clear=True)
    def test_rejects_a_single_agent(self) -> None:
        with self.assertRaisesRegex(ConfigError, "至少需要两个 Agent"):
            resolve_settings({}, base_url="https://relay.example/v1", models=["a"])


if __name__ == "__main__":
    unittest.main()
