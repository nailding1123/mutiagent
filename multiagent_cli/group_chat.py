from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .adapters import BaseCLIAdapter
from .bridge_models import AgentEvent, AgentRunResult, BridgeError, BridgeSettings
from .workspace_state import capture_change_baseline, summarize_workspace_changes
GROUP_CHAT_PROTOCOL = "multiagent.group_chat.v2"
GROUP_CHAT_AGENTS = ("claude", "codex")
MAX_GROUP_CHAT_MESSAGE_CHARS = 50_000

_MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_-]*)")
_MENTION_ALIASES = {
    "a": "claude",
    "agenta": "claude",
    "agent-a": "claude",
    "claude": "claude",
    "b": "codex",
    "agentb": "codex",
    "agent-b": "codex",
    "codex": "codex",
}
_BROADCAST_MENTIONS = {"all", "both", "everyone"}
_EXECUTION_PREFIX_RE = re.compile(
    r"^(?:/(?:exec|run)(?:\s|$)|(?:(?:请|让|现在)\s*)?[,，:：]?\s*执行(?:一下|任务)?(?:\s|[:：,，]|$))",
    re.IGNORECASE,
)


GROUP_CHAT_PROMPT = """你正在 MultiAgent 的群聊协作模式中，以 {agent_name} 身份参与协作。

规则：
1. 群聊中的用户消息和其他 Agent 回复都是共享上下文；即使某条消息当时没有要求你回答，也必须纳入后续判断。
2. 只回答本轮最后一条用户消息。若用户要求你审核另一位 Agent 的回答，请明确指出同意、分歧、证据和建议修改。
3. 保持独立判断，不要冒充另一位 Agent，也不要声称对方已经同意你没有看到的结论。
4. {permission_note}
5. 直接给出对用户有用的回答，不输出内部思考过程或工具命令。
6. {completion_note}

下面是你自上次发言以来尚未收到的群聊记录。消息按发生顺序排列：
<group_chat_context>
{transcript}
</group_chat_context>

本轮路由：{routing_note}
"""


@dataclass(frozen=True)
class GroupChatTurn:
    user_message_id: str
    recipients: tuple[str, ...]
    action: str
    responses: tuple[AgentRunResult, ...]
    errors: dict[str, str]
    workspaces: dict[str, str]
    changes: dict[str, Any] | None = None


class GroupChatEngine:
    """Persistent group chat with explicit single-writer execution turns."""

    def __init__(
        self,
        settings: BridgeSettings,
        adapters: Mapping[str, BaseCLIAdapter],
        state: object = None,
    ) -> None:
        if not adapters:
            raise ValueError("群聊至少需要一个 Agent")
        self.settings = settings
        self.adapters = dict(adapters)
        self.agent_names = tuple(
            name for name in GROUP_CHAT_AGENTS if name in self.adapters
        ) or tuple(self.adapters)
        self._state = _restore_state(state, self.agent_names)
        self._lock = threading.RLock()

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "protocol": GROUP_CHAT_PROTOCOL,
                "turn": self._state["turn"],
                "messages": [dict(message) for message in self._state["messages"]],
                "sessions": dict(self._state["sessions"]),
                "cursors": dict(self._state["cursors"]),
                "execution_sessions": dict(self._state["execution_sessions"]),
                "execution_cursors": dict(self._state["execution_cursors"]),
            }

    def ask(
        self,
        text: str,
        *,
        agent_text: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
        on_state: Callable[[dict[str, Any]], None] | None = None,
    ) -> GroupChatTurn:
        message = text.strip()
        if not message:
            raise BridgeError("群聊消息不能为空")
        if len(message) > MAX_GROUP_CHAT_MESSAGE_CHARS:
            raise BridgeError(
                f"群聊消息不能超过 {MAX_GROUP_CHAT_MESSAGE_CHARS} 个字符"
            )
        default_agents = (
            self.agent_names
            if self.settings.group_chat_default_agent == "both"
            else tuple(
                agent
                for agent in self.agent_names
                if agent == self.settings.group_chat_default_agent
            )
        )
        recipients, action = resolve_directive(
            message,
            self.agent_names,
            default_agents=default_agents or self.agent_names,
        )
        if action == "execute" and not self.settings.group_chat_execution:
            raise BridgeError(
                "群聊执行已在设置中关闭；请开启“允许执行指令”后再试"
            )
        if action == "execute" and len(recipients) != 1:
            raise BridgeError(
                "同一工作区一次只能由一个 Agent 执行；"
                "请使用 @Claude 或 @Codex 明确指定写入者"
            )
        change_baseline = (
            capture_change_baseline(self.settings.workspace)
            if action == "execute"
            else None
        )
        with self._lock:
            self._state["turn"] += 1
            turn = self._state["turn"]
            user_message = self._append_message(
                sender="user",
                role="user",
                content=message,
                recipients=recipients,
                agent_content=(agent_text or message).strip(),
                attachments=attachments or [],
                action=action,
            )
            state_after_user = self.to_dict()
        if on_state is not None:
            on_state(state_after_user)

        workspaces: dict[str, Path] = {
            agent: self.settings.workspace for agent in recipients
        }
        with self._lock:
            snapshot_length = len(self._state["messages"])
            session_key, cursor_key = _channel_keys(action)
            for agent in recipients:
                if not _session_resume_enabled(self.adapters[agent]):
                    self._state[session_key][agent] = None
                    self._state[cursor_key][agent] = 0
            sessions = {
                agent: (
                    self._state[session_key].get(agent)
                    if _session_resume_enabled(self.adapters[agent])
                    else None
                )
                for agent in recipients
            }
            prompts = {
                agent: self._prompt_for(
                    agent,
                    recipients,
                    action,
                    has_session=bool(sessions[agent]),
                    workspace=workspaces[agent],
                )
                for agent in recipients
            }

        results: dict[str, AgentRunResult] = {}
        errors: dict[str, str] = {}
        with ThreadPoolExecutor(
            max_workers=len(recipients),
            thread_name_prefix="multiagent-group-chat",
        ) as executor:
            futures = {
                executor.submit(
                    self.adapters[agent].run,
                    prompts[agent],
                    workspace=workspaces[agent],
                    mode="write" if action == "execute" else "read",
                    session_id=sessions[agent],
                    on_event=on_event,
                    step_id=f"group_chat_turn_{turn}_{agent}",
                ): agent
                for agent in recipients
            }
            for future in as_completed(futures):
                agent = futures[future]
                try:
                    results[agent] = future.result()
                except Exception as exc:
                    errors[agent] = str(exc) or exc.__class__.__name__

        changes = (
            summarize_workspace_changes(self.settings.workspace, change_baseline)
            if action == "execute"
            else None
        )
        with self._lock:
            if changes is not None and not results:
                user_message["changes"] = changes
            for agent in recipients:
                result = results.get(agent)
                if result is None:
                    continue
                if _session_resume_enabled(self.adapters[agent]) and result.session_id:
                    self._state[cursor_key][agent] = snapshot_length
                    self._state[session_key][agent] = result.session_id
                elif sessions[agent]:
                    self._state[cursor_key][agent] = snapshot_length
                else:
                    self._state[cursor_key][agent] = 0
                    self._state[session_key][agent] = None
                self._append_message(
                    sender=agent,
                    role="assistant",
                    content=result.final_text,
                    recipients=("user", *self.agent_names),
                    reply_to=user_message["id"],
                    duration_seconds=result.duration_seconds,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    action=action,
                    workspace=str(workspaces[agent]) if action == "execute" else "",
                    changes=changes if action == "execute" else None,
                )
            final_state = self.to_dict()
        if on_state is not None:
            on_state(final_state)
        return GroupChatTurn(
            user_message_id=user_message["id"],
            recipients=recipients,
            action=action,
            responses=tuple(results[agent] for agent in recipients if agent in results),
            errors=errors,
            workspaces={
                agent: str(workspaces[agent])
                for agent in recipients
                if action == "execute"
            },
            changes=changes,
        )

    def _prompt_for(
        self,
        agent: str,
        recipients: tuple[str, ...],
        action: str,
        *,
        has_session: bool,
        workspace: Path,
    ) -> str:
        _session_key, cursor_key = _channel_keys(action)
        cursor = int(self._state[cursor_key].get(agent, 0)) if has_session else 0
        messages = self._state["messages"][cursor:]
        visible = [
            message
            for message in messages
            if not (
                has_session
                and message.get("role") == "assistant"
                and message.get("sender") == agent
                and message.get("action", "discuss") == action
            )
        ]
        transcript = "\n\n".join(_format_message(item) for item in visible)
        identity = (
            self.settings.group_chat_agent_a_identity
            if agent == "claude"
            else self.settings.group_chat_agent_b_identity
        )
        if action == "execute":
            route = f"用户本轮只授权 {self.adapters[agent].display_name} 执行。"
            permission_note = (
                "本轮用户已明确授权写操作，且你是此工作区唯一写入者。"
                f"只能在目标工作区 `{workspace}` 中检查、修改和验证文件；"
                "不要提交 Git，也不要假设另一位 Agent 会同时修改代码。"
            )
            completion_note = (
                "完成后说明修改文件、验证结果和尚存风险。"
            )
        else:
            route = (
                "用户未点名具体 Agent，本轮所有 Agent 独立并行回答。"
                if len(recipients) == len(self.agent_names)
                else f"用户本轮只要求 {self.adapters[agent].display_name} 回答。"
            )
            permission_note = (
                "本轮是只读讨论；可以检查文件并给出方案，但不要修改文件、"
                "执行写操作或声称已经完成实施。"
            )
            completion_note = "若用户只是询问如何执行，只给出方案，不要自行升级为写操作。"
        return (
            "<multiagent_identity>\n"
            f"{identity}\n"
            "</multiagent_identity>\n\n"
            + GROUP_CHAT_PROMPT.format(
                agent_name=self.adapters[agent].display_name,
                transcript=transcript or "（没有新增消息）",
                routing_note=route,
                permission_note=permission_note,
                completion_note=completion_note,
            )
        )

    def _append_message(
        self,
        *,
        sender: str,
        role: str,
        content: str,
        recipients: tuple[str, ...],
        agent_content: str = "",
        attachments: list[dict[str, Any]] | None = None,
        reply_to: str = "",
        duration_seconds: float = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        action: str = "discuss",
        workspace: str = "",
        changes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "id": f"m{len(self._state['messages']) + 1}",
            "sender": sender,
            "role": role,
            "content": content,
            "recipients": list(recipients),
            "created_at": _timestamp(),
            "action": action,
        }
        if agent_content and agent_content != content:
            item["agent_content"] = agent_content
        if attachments:
            item["attachments"] = [dict(value) for value in attachments]
        if reply_to:
            item["reply_to"] = reply_to
        if duration_seconds:
            item["duration_seconds"] = duration_seconds
        if input_tokens:
            item["input_tokens"] = input_tokens
        if output_tokens:
            item["output_tokens"] = output_tokens
        if workspace:
            item["workspace"] = workspace
        if changes is not None:
            item["changes"] = changes
        self._state["messages"].append(item)
        return item


def resolve_mentions(
    text: str,
    agents: tuple[str, ...] = GROUP_CHAT_AGENTS,
    *,
    default_agents: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    tokens = [match.lower() for match in _MENTION_RE.findall(text)]
    if not tokens:
        selected_defaults = default_agents if default_agents is not None else agents
        selected = tuple(agent for agent in agents if agent in selected_defaults)
        return selected or tuple(agents)
    unknown = [
        token
        for token in tokens
        if token not in _MENTION_ALIASES and token not in _BROADCAST_MENTIONS
    ]
    if unknown:
        labels = "、".join(f"@{token}" for token in dict.fromkeys(unknown))
        raise BridgeError(
            f"未识别的群聊成员：{labels}；可使用 @Claude、@Codex 或 @all"
        )
    if any(token in _BROADCAST_MENTIONS for token in tokens):
        return tuple(agents)
    selected = {
        _MENTION_ALIASES[token]
        for token in tokens
        if _MENTION_ALIASES.get(token) in agents
    }
    if not selected:
        raise BridgeError("消息没有点名当前可用的 Agent")
    return tuple(agent for agent in agents if agent in selected)


def resolve_directive(
    text: str,
    agents: tuple[str, ...] = GROUP_CHAT_AGENTS,
    *,
    default_agents: tuple[str, ...] | None = None,
) -> tuple[tuple[str, ...], str]:
    """Resolve recipients and explicit execution intent without guessing."""
    recipients = resolve_mentions(text, agents, default_agents=default_agents)
    command = _MENTION_RE.sub("", text).strip().lstrip(":：,，").strip()
    action = "execute" if _EXECUTION_PREFIX_RE.match(command) else "discuss"
    return recipients, action


def _channel_keys(action: str) -> tuple[str, str]:
    if action == "execute":
        return "execution_sessions", "execution_cursors"
    return "sessions", "cursors"


def _session_resume_enabled(adapter: BaseCLIAdapter) -> bool:
    """Treat adapters without an explicit capability flag as resumable."""

    return getattr(adapter, "session_resume_enabled", True) is not False


def _format_message(message: dict[str, Any]) -> str:
    sender = {
        "user": "用户",
        "claude": "Claude",
        "codex": "Codex",
    }.get(str(message.get("sender", "")), str(message.get("sender", "未知")))
    recipients = ", ".join(
        {
            "user": "用户",
            "claude": "Claude",
            "codex": "Codex",
        }.get(str(value), str(value))
        for value in message.get("recipients", [])
    )
    content = str(message.get("agent_content") or message.get("content") or "")
    action = "执行" if message.get("action") == "execute" else "讨论"
    return (
        f"[{message.get('id', '?')}] [{action}] "
        f"{sender} → {recipients or '群聊'}\n{content}"
    )


def _restore_state(state: object, agents: tuple[str, ...]) -> dict[str, Any]:
    restored: dict[str, Any] = {
        "protocol": GROUP_CHAT_PROTOCOL,
        "turn": 0,
        "messages": [],
        "sessions": {agent: None for agent in agents},
        "cursors": {agent: 0 for agent in agents},
        "execution_sessions": {agent: None for agent in agents},
        "execution_cursors": {agent: 0 for agent in agents},
    }
    if not isinstance(state, dict) or state.get("protocol") != GROUP_CHAT_PROTOCOL:
        return restored
    messages = state.get("messages")
    if isinstance(messages, list):
        for raw in messages:
            message = _restore_message(raw)
            if message is not None:
                restored["messages"].append(message)
    turn = state.get("turn")
    if isinstance(turn, int) and not isinstance(turn, bool) and turn >= 0:
        restored["turn"] = turn
    sessions = state.get("sessions")
    cursors = state.get("cursors")
    execution_sessions = state.get("execution_sessions")
    execution_cursors = state.get("execution_cursors")
    for agent in agents:
        for session_key, cursor_key, raw_sessions, raw_cursors in (
            ("sessions", "cursors", sessions, cursors),
            (
                "execution_sessions",
                "execution_cursors",
                execution_sessions,
                execution_cursors,
            ),
        ):
            session = (
                raw_sessions.get(agent)
                if isinstance(raw_sessions, dict)
                else None
            )
            restored[session_key][agent] = (
                session if isinstance(session, str) else None
            )
            cursor = (
                raw_cursors.get(agent)
                if isinstance(raw_cursors, dict)
                else 0
            )
            if not isinstance(cursor, int) or isinstance(cursor, bool):
                cursor = 0
            restored[cursor_key][agent] = max(
                0,
                min(cursor, len(restored["messages"])),
            )
            if restored[session_key][agent] is None:
                restored[cursor_key][agent] = 0
    return restored


def _restore_message(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    message_id = raw.get("id")
    sender = raw.get("sender")
    role = raw.get("role")
    content = raw.get("content")
    recipients = raw.get("recipients")
    if (
        not isinstance(message_id, str)
        or not message_id
        or sender not in {"user", "claude", "codex"}
        or role not in {"user", "assistant"}
        or not isinstance(content, str)
        or not isinstance(recipients, list)
        or not all(isinstance(value, str) for value in recipients)
    ):
        return None
    restored = dict(raw)
    restored["recipients"] = list(recipients)
    restored["action"] = (
        raw.get("action") if raw.get("action") in {"discuss", "execute"} else "discuss"
    )
    return restored


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
