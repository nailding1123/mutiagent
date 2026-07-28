from __future__ import annotations

from collections.abc import Callable

from .client import OpenAICompatibleClient
from .models import Agent, Turn


DRAFTER_PROMPT = """你是多 Agent 协作链中的第一位回答者 {name}。
请针对用户请求给出准确、完整、可以直接使用的初稿。说明必要的假设；不要讨论协作流程。
使用与用户相同的主要语言回答。"""

REVISER_PROMPT = """你是多 Agent 协作链中的修订者 {name}。
你的职责是检查上一位 Agent 的答案，修正事实、逻辑、遗漏和表达问题。
保留正确内容，并直接输出一份完整、更好的新答案；不要只给点评、修改建议或差异列表。
使用与用户相同的主要语言回答。"""

FINALIZER_PROMPT = """你是多 Agent 协作链中的最终编辑 {name}。
请根据原始请求对上一版答案进行最终校验和润色，消除残留的事实、逻辑、遗漏和表达问题。
直接输出可以交付给用户的完整最终答案；不要输出审稿记录、过程说明或差异列表。
使用与用户相同的主要语言回答。"""


class Orchestrator:
    """Runs agents sequentially, feeding each answer to the next agent."""

    def __init__(self, client: OpenAICompatibleClient, agents: tuple[Agent, ...]) -> None:
        if len(agents) < 2:
            raise ValueError("至少需要配置两个 Agent")
        self.client = client
        self.agents = agents

    def run(
        self,
        question: str,
        *,
        stream: bool = True,
        on_agent_start: Callable[[Agent], None] | None = None,
        on_token: Callable[[Agent, str], None] | None = None,
        on_agent_end: Callable[[Turn], None] | None = None,
    ) -> list[Turn]:
        question = question.strip()
        if not question:
            raise ValueError("问题不能为空")

        turns: list[Turn] = []
        previous_answer: str | None = None
        previous_name: str | None = None

        for index, agent in enumerate(self.agents):
            if on_agent_start:
                on_agent_start(agent)

            messages = self._messages_for(
                agent=agent,
                index=index,
                question=question,
                previous_answer=previous_answer,
                previous_name=previous_name,
            )
            chunks: list[str] = []
            for chunk in self.client.iter_chat(
                model=agent.model,
                messages=messages,
                parameters=agent.parameters,
                stream=stream,
            ):
                chunks.append(chunk)
                if on_token:
                    on_token(agent, chunk)

            answer = "".join(chunks).strip()
            if not answer:
                raise RuntimeError(f"{agent.name} 没有返回文本内容")

            turn = Turn(agent=agent, answer=answer)
            turns.append(turn)
            if on_agent_end:
                on_agent_end(turn)

            previous_answer = answer
            previous_name = agent.name

        return turns

    @staticmethod
    def _messages_for(
        *,
        agent: Agent,
        index: int,
        question: str,
        previous_answer: str | None,
        previous_name: str | None,
    ) -> list[dict[str, str]]:
        agent_type = agent.type
        if agent_type == "auto":
            agent_type = "draft" if index == 0 else "review"
        default_prompt = {
            "draft": DRAFTER_PROMPT,
            "review": REVISER_PROMPT,
            "final": FINALIZER_PROMPT,
        }.get(agent_type, REVISER_PROMPT)
        system_prompt = agent.system_prompt or default_prompt.format(name=agent.name)

        if index == 0:
            user_content = question
        else:
            user_content = f"""原始用户请求：
<original_request>
{question}
</original_request>

上一位 Agent（{previous_name}）的答案：
<previous_answer>
{previous_answer}
</previous_answer>

请基于原始请求审查并修订上一版，直接给出完整的新版答案。"""

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
