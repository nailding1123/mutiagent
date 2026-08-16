from __future__ import annotations

import itertools
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .adapters import BaseCLIAdapter
from .bridge_models import (
    AgentEvent,
    AgentRunResult,
    AgentTimeoutError,
    BridgeError,
    BridgeSettings,
)
from .workspace_state import capture_change_baseline, summarize_workspace_changes
from .workspace_coordinator import WorkspaceCoordinator, WorkspaceCoordinatorError
GROUP_CHAT_PROTOCOL = "multiagent.group_chat.v2"
GROUP_CHAT_AGENTS = ("claude", "codex")
MAX_GROUP_CHAT_MESSAGE_CHARS = 50_000

# 单调递增计数器，避免删除/编辑消息后出现位置派生 ID 碰撞
_MESSAGE_ID_COUNTER = itertools.count(1)

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
_READ_INTENT_RE = re.compile(
    r"(?:读取|读一下|阅读|查看|检查|看看|分析|解释|说明|审核|审阅|总结|比较|评估|为什么|怎么|是否|有哪些|告诉我|回答)",
    re.IGNORECASE,
)
_WRITE_INTENT_RE = re.compile(
    r"(?:修改|改动|改一下|修复|实现|添加|删除|创建|写入|重构|重命名|生成文件|落地|提交代码)",
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


@dataclass(frozen=True)
class GroupChatReservation:
    """An agent slot reserved for one in-flight group-chat turn."""

    token: str
    message: str
    recipients: tuple[str, ...]
    action: str


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
        self._busy_agents: dict[str, str] = {}
        self._reservation_sequence = itertools.count(1)
        self.workspace_coordinator = WorkspaceCoordinator()

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

    def reserve(
        self,
        text: str,
        *,
        forced_recipients: tuple[str, ...] | None = None,
    ) -> GroupChatReservation:
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
        if forced_recipients is not None:
            recipients = tuple(
                agent for agent in self.agent_names if agent in forced_recipients
            )
            if not recipients:
                raise BridgeError("没有可用的目标 Agent")
        if action == "execute" and not self.settings.group_chat_execution:
            raise BridgeError(
                "群聊执行已在设置中关闭；请开启“允许 Agent 自主写入工作区”后再试"
            )
        if action == "execute" and len(recipients) != 1:
            raise BridgeError(
                "同一工作区一次只能由一个 Agent 执行；"
                "请使用 @Claude 或 @Codex 明确指定写入者"
            )
        with self._lock:
            busy = [agent for agent in recipients if agent in self._busy_agents]
            if busy:
                labels = "、".join(self.adapters[agent].display_name for agent in busy)
                raise BridgeError(
                    f"{labels} 正在回复上一条消息；请先点名另一个空闲 Agent，"
                    "避免同一个 Agent 的上下文并发交错"
                )
            token = f"chat-turn-{next(self._reservation_sequence)}"
            for agent in recipients:
                self._busy_agents[agent] = token
        return GroupChatReservation(token, message, recipients, action)

    def release(self, reservation: GroupChatReservation) -> None:
        with self._lock:
            for agent in reservation.recipients:
                if self._busy_agents.get(agent) == reservation.token:
                    self._busy_agents.pop(agent, None)

    def active_agents(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(agent for agent in self.agent_names if agent in self._busy_agents)

    def find_message(self, message_id: str) -> dict[str, Any] | None:
        with self._lock:
            for message in self._state["messages"]:
                if message.get("id") == message_id:
                    return dict(message)
        return None

    def find_parent_user(self, message_id: str) -> dict[str, Any] | None:
        with self._lock:
            target = next(
                (item for item in self._state["messages"] if item.get("id") == message_id),
                None,
            )
            if not target:
                return None
            parent_id = str(target.get("reply_to") or "")
            if target.get("role") == "user":
                return dict(target)
            return next(
                (dict(item) for item in self._state["messages"] if item.get("id") == parent_id),
                None,
            )

    def ask(
        self,
        text: str,
        *,
        agent_text: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
        on_state: Callable[[dict[str, Any]], None] | None = None,
        reservation: GroupChatReservation | None = None,
        edited_from: str = "",
        hidden_user: bool = False,
        reply_to: str = "",
        retry_of: str = "",
        retry_mode: str = "",
    ) -> GroupChatTurn:
        current = reservation or self.reserve(text)
        try:
            return self._ask_reserved(
                text,
                agent_text=agent_text,
                attachments=attachments,
                on_event=on_event,
                on_state=on_state,
                reservation=current,
                edited_from=edited_from,
                hidden_user=hidden_user,
                reply_to=reply_to,
                retry_of=retry_of,
                retry_mode=retry_mode,
            )
        finally:
            self.release(current)

    def _ask_reserved(
        self,
        text: str,
        *,
        agent_text: str | None,
        attachments: list[dict[str, Any]] | None,
        on_event: Callable[[AgentEvent], None] | None,
        on_state: Callable[[dict[str, Any]], None] | None,
        reservation: GroupChatReservation,
        edited_from: str,
        hidden_user: bool,
        reply_to: str,
        retry_of: str,
        retry_mode: str,
    ) -> GroupChatTurn:
        message = reservation.message
        recipients = reservation.recipients
        action = reservation.action
        # Write access is decided per agent, not by the /exec gate alone:
        # a single-mentioned recipient with the execution toggle on gets the
        # workspace in write mode and decides from the message whether edits
        # are warranted; multi-recipient turns stay read-only so two agents
        # never write the same workspace concurrently.
        write_recipients = frozenset(
            recipients
            if self.settings.group_chat_execution
            and len(recipients) == 1
            and _should_grant_write(message, action)
            else ()
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
                edited_from=edited_from,
                hidden=hidden_user,
                retry_of=retry_of,
                retry_mode=retry_mode,
            )
            snapshot_length = len(self._state["messages"])
            # Hidden retry messages are hidden only from the browser feed. They
            # must remain in the prompt so a resumed Agent receives an explicit
            # new-generation instruction instead of silently reusing the old
            # turn without any new user input.
            prompt_messages = list(self._state["messages"][:snapshot_length])
            state_after_user = self.to_dict()
        if on_state is not None:
            on_state(state_after_user)

        workspaces: dict[str, Path] = {agent: self.settings.workspace for agent in recipients}
        with self._lock:
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
                    can_write=agent in write_recipients,
                    messages=prompt_messages,
                )
                for agent in recipients
            }

        results: dict[str, AgentRunResult] = {}
        errors: dict[str, str] = {}
        failure_reasons: dict[str, str] = {}
        changes_by_agent: dict[str, dict[str, Any] | None] = {}
        with ThreadPoolExecutor(
            max_workers=len(recipients),
            thread_name_prefix="multiagent-group-chat",
        ) as executor:
            futures = {
                executor.submit(
                    self._run_agent,
                    agent,
                    prompts[agent],
                    session_id=sessions[agent],
                    write=agent in write_recipients,
                    on_event=on_event,
                    step_id=f"group_chat_turn_{turn}_{agent}",
                ): agent
                for agent in recipients
            }
            for future in as_completed(futures):
                agent = futures[future]
                try:
                    result, changes = future.result()
                    results[agent] = result
                    changes_by_agent[agent] = changes
                except Exception as exc:
                    errors[agent] = str(exc) or exc.__class__.__name__
                    failure_reasons[agent] = _agent_failure_reason(exc)
        reportable_changes = next(
            (changes_by_agent.get(agent) for agent in recipients if changes_by_agent.get(agent)),
            None,
        )
        with self._lock:
            if reportable_changes is not None and not results:
                user_message["changes"] = reportable_changes
            for agent in recipients:
                result = results.get(agent)
                if result is None:
                    if agent in errors:
                        failure_reason = failure_reasons.get(agent, "error")
                        self._append_message(
                            sender=agent,
                            role="assistant",
                            content=_agent_failure_content(
                                self.adapters[agent].display_name,
                                failure_reason,
                            ),
                            recipients=("user", *self.agent_names),
                            reply_to=reply_to or user_message["id"],
                            action=action,
                            failure_reason=failure_reason,
                            retry_of=retry_of,
                            retry_mode=retry_mode,
                        )
                    continue
                if _session_resume_enabled(self.adapters[agent]) and result.session_id:
                    self._state[cursor_key][agent] = snapshot_length
                    self._state[session_key][agent] = result.session_id
                elif sessions[agent]:
                    self._state[cursor_key][agent] = snapshot_length
                else:
                    self._state[cursor_key][agent] = 0
                    self._state[session_key][agent] = None
                agent_can_write = agent in write_recipients
                self._append_message(
                    sender=agent,
                    role="assistant",
                    content=result.final_text,
                    recipients=("user", *self.agent_names),
                    reply_to=reply_to or user_message["id"],
                    duration_seconds=result.duration_seconds,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    action=action,
                    workspace=str(workspaces[agent]) if agent_can_write else "",
                    changes=changes_by_agent.get(agent) if agent_can_write else None,
                    retry_of=retry_of,
                    retry_mode=retry_mode,
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
                if agent in write_recipients
            },
            changes=reportable_changes,
        )

    def _run_agent(
        self,
        agent: str,
        prompt: str,
        *,
        session_id: str | None,
        write: bool,
        on_event: Callable[[AgentEvent], None] | None,
        step_id: str,
    ) -> tuple[AgentRunResult, dict[str, Any] | None]:
        try:
            lease = self.workspace_coordinator.acquire(
                self.settings.workspace,
                owner=step_id,
                access="write" if write else "read",
            )
        except WorkspaceCoordinatorError as exc:
            raise BridgeError(str(exc)) from exc
        baseline = capture_change_baseline(lease.workspace) if write else None
        try:
            result = self.adapters[agent].run(
                prompt,
                workspace=lease.workspace,
                mode="write" if write else "read",
                session_id=session_id,
                on_event=on_event,
                step_id=step_id,
            )
            if baseline is None:
                changes = None
            else:
                raw_changes = summarize_workspace_changes(lease.workspace, baseline)
                changes = (
                    raw_changes
                    if _has_file_changes(raw_changes) or raw_changes.get("available") is False
                    else None
                )
            release = lease.release()
            if not release.get("merged", True):
                raise BridgeError(str(release.get("error") or "隔离 Worktree 合并失败"))
            return result, changes
        except BaseException:
            lease.release()
            raise

    def _prompt_for(
        self,
        agent: str,
        recipients: tuple[str, ...],
        action: str,
        *,
        has_session: bool,
        workspace: Path,
        can_write: bool = False,
        messages: list[dict[str, Any]] | None = None,
    ) -> str:
        _session_key, cursor_key = _channel_keys(action)
        cursor = int(self._state[cursor_key].get(agent, 0)) if has_session else 0
        messages = (messages if messages is not None else self._state["messages"])[cursor:]
        visible = [
            message
            for message in messages
            if not (
                has_session
                and message.get("role") == "assistant"
                and message.get("sender") == agent
                and message.get("action", "discuss") == action
                and bool(message.get("workspace")) == can_write
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
                "只能在本轮进程实际打开的工作区中检查、修改和验证文件；"
                "不要提交 Git，也不要假设另一位 Agent 会同时修改代码。"
            )
            completion_note = (
                "完成后说明修改文件、验证结果和尚存风险。"
            )
        elif can_write:
            route = (
                f"用户本轮只要求 {self.adapters[agent].display_name} 回答，"
                "写权限已下放给你自主判断。"
            )
            permission_note = (
                "本轮你持有目标工作区的写权限，且是唯一写入者。"
                "根据用户消息自行判断是否需要修改文件："
                "只是提问、讨论或审核就保持只读作答；"
                "消息明确要求或隐含需要改动时才写入。"
                "只能在本轮进程实际打开的工作区中检查、修改和验证文件；"
                "不要提交 Git。"
            )
            completion_note = (
                "若修改了文件，完成后说明修改文件、验证结果和尚存风险；"
                "若判断无需修改，正常作答即可。"
            )
        else:
            route = (
                "用户未点名具体 Agent，本轮所有 Agent 独立并行回答。"
                if len(recipients) == len(self.agent_names)
                else f"用户本轮只要求 {self.adapters[agent].display_name} 回答。"
            )
            permission_note = (
                "本轮是只读讨论；可以检查文件并给出方案，但不要修改文件、"
                "执行写操作或声称已经完成实施。若完成任务需要额外的读取、网络、"
                "命令执行或写入权限，可以发起原生权限申请；获得用户批准前不得越权。"
            )
            completion_note = (
                "若用户只是询问如何执行，只给出方案；任务确实需要升级权限时，"
                "必须通过原生审批请求用户决定。"
            )
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
        edited_from: str = "",
        hidden: bool = False,
        retry_of: str = "",
        retry_mode: str = "",
        failure_reason: str = "",
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "id": _next_message_id(),
            "sender": sender,
            "role": role,
            "content": content,
            "recipients": list(recipients),
            "created_at": _timestamp(),
            "action": action,
        }
        if edited_from:
            item["edited_from"] = edited_from
        if hidden:
            item["hidden"] = True
        if retry_of:
            item["retry_of"] = retry_of
        if retry_mode:
            item["retry_mode"] = retry_mode
        if failure_reason:
            item["status"] = "failed"
            item["failure_reason"] = failure_reason
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


def _agent_failure_reason(error: object) -> str:
    if isinstance(error, AgentTimeoutError):
        return "timeout"
    text = str(error or "").lower()
    return (
        "timeout"
        if "timeout" in text or "timed out" in text or "超时" in text
        else "error"
    )


def _agent_failure_content(agent_name: str, reason: str) -> str:
    if reason == "timeout":
        return f"响应超时。{agent_name} 未能在限定时间内完成本轮回复。"
    return f"响应失败。{agent_name} 未能完成本轮回复。"


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


def _has_file_changes(changes: dict[str, Any] | None) -> bool:
    """True only when Git actually reported changed files in the workspace.

    ``summarize_workspace_changes`` always returns a dict -- including an
    "unavailable" one for non-Git workspaces -- so truthiness alone would treat
    every turn as if the agent had written something.
    """

    if not isinstance(changes, dict) or changes.get("available") is False:
        return False
    count = changes.get("file_count")
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        return True
    files = changes.get("files")
    return isinstance(files, list) and bool(files)


def _should_grant_write(message: str, action: str) -> bool:
    """Grant solo write access only when the wording implies implementation."""

    if action == "execute" or _WRITE_INTENT_RE.search(message):
        return True
    return not bool(_READ_INTENT_RE.search(message))


def _next_message_id() -> str:
    """Monotonically-increasing message id, seeded from persisted state on restore."""

    return f"m{next(_MESSAGE_ID_COUNTER)}"


def _seed_message_id_counter(messages: list[dict[str, Any]]) -> None:
    """Advance the global counter past any persisted ids to avoid collisions.

    Older runs stored positional ids (m1, m2, ...); after restart or session
    eviction, appending a new message must not reuse an id that already exists
    in the persisted record.
    """

    highest = 0
    for message in messages:
        raw = message.get("id") if isinstance(message, dict) else None
        if not isinstance(raw, str):
            continue
        match = re.fullmatch(r"m(\d+)", raw)
        if not match:
            continue
        highest = max(highest, int(match.group(1)))
    if highest <= 0:
        return
    # itertools.count has no public seek; advance by consuming values.
    while True:
        current = next(_MESSAGE_ID_COUNTER)
        if current >= highest:
            break


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
    if message.get("retry_of"):
        mode = "继续生成" if message.get("retry_mode") == "continue" else "重新生成"
        content = f"[系统：请对关联回复执行{mode}，不要把本条系统指令直接回复给用户。]\n{content}"
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
    _seed_message_id_counter(restored["messages"])
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
