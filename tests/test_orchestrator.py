from __future__ import annotations

import unittest
from typing import Any

from multiagent_cli.models import Agent
from multiagent_cli.orchestrator import Orchestrator


class FakeClient:
    def __init__(self, answers: list[str]) -> None:
        self.answers = iter(answers)
        self.calls: list[dict[str, Any]] = []

    def iter_chat(self, **kwargs: Any):
        self.calls.append(kwargs)
        answer = next(self.answers)
        midpoint = max(1, len(answer) // 2)
        yield answer[:midpoint]
        yield answer[midpoint:]


class OrchestratorTests(unittest.TestCase):
    def test_agents_revise_the_immediately_previous_answer(self) -> None:
        client = FakeClient(["初稿", "修订稿", "最终稿"])
        agents = (
            Agent("A", "model-a"),
            Agent("B", "model-b"),
            Agent("C", "model-c"),
        )
        seen_tokens: list[str] = []
        orchestrator = Orchestrator(client, agents)  # type: ignore[arg-type]

        turns = orchestrator.run(
            "解释这个问题",
            on_token=lambda _agent, token: seen_tokens.append(token),
        )

        self.assertEqual([turn.answer for turn in turns], ["初稿", "修订稿", "最终稿"])
        self.assertEqual("".join(seen_tokens), "初稿修订稿最终稿")
        self.assertEqual(client.calls[0]["model"], "model-a")
        self.assertIn("初稿", client.calls[1]["messages"][1]["content"])
        self.assertIn("修订稿", client.calls[2]["messages"][1]["content"])
        self.assertNotIn("初稿", client.calls[2]["messages"][1]["content"])

    def test_custom_prompt_and_parameters_are_forwarded(self) -> None:
        client = FakeClient(["one", "two"])
        agents = (
            Agent("A", "m1", system_prompt="custom", parameters={"temperature": 0.1}),
            Agent("B", "m2"),
        )

        Orchestrator(client, agents).run("question", stream=False)  # type: ignore[arg-type]

        self.assertEqual(client.calls[0]["messages"][0]["content"], "custom")
        self.assertEqual(client.calls[0]["parameters"], {"temperature": 0.1})
        self.assertFalse(client.calls[0]["stream"])

    def test_final_agent_gets_finalizer_prompt(self) -> None:
        client = FakeClient(["draft", "final"])
        agents = (
            Agent("作者", "m1", type="draft"),
            Agent("主编", "m2", type="final"),
        )

        Orchestrator(client, agents).run("question")  # type: ignore[arg-type]

        final_prompt = client.calls[1]["messages"][0]["content"]
        self.assertIn("最终编辑", final_prompt)
        self.assertIn("完整最终答案", final_prompt)

    def test_at_least_two_agents_are_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "至少需要配置两个 Agent"):
            Orchestrator(FakeClient([]), (Agent("A", "m1"),))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
