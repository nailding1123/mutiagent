from __future__ import annotations

import copy
import itertools
import json
import re
import shlex
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .adapters import BaseCLIAdapter
from .bridge_models import (
    AgentEvent,
    AgentModelCompatibilityError,
    AgentRunResult,
    AgentTimeoutError,
    BridgeError,
    BridgeSettings,
)
from .context_compaction import (
    CONTEXT_PROJECTION_VERSION,
    build_context_projection,
    estimate_tokens,
)
from .workspace_state import (
    WorkspaceChangeBaseline,
    capture_change_baseline,
    summarize_workspace_changes,
)
from .workspace_coordinator import WorkspaceCoordinator, WorkspaceCoordinatorError
GROUP_CHAT_PROTOCOL = "multiagent.group_chat.v2"
GROUP_CHAT_AGENTS = ("claude", "codex")
MAX_GROUP_CHAT_MESSAGE_CHARS = 50_000

# 单调递增计数器，避免删除/编辑消息后出现位置派生 ID 碰撞
_MESSAGE_ID_COUNTER = itertools.count(1)

# Chinese text is a normal place to attach an Agent mention (for example
# ``请检查这个页面@Claude``).  Python's Unicode-aware ``\w`` treats Chinese
# characters as word characters, which made such mentions invisible and caused
# routing to fall back to the default Agent set.  Keep the boundary check ASCII
#-only, matching the browser's mention parser.
_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_@])@([A-Za-z][A-Za-z0-9_-]*)")
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

下面是本轮需要处理的群聊上下文。消息按发生顺序排列；历史过长时，较早内容会显示为带消息 ID 的提取式摘要，最近消息仍保留原文：
<group_chat_context>
{transcript}
</group_chat_context>

{comparison_note}

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
    """Persistent group chat backed by native Agent workspace decisions."""

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
        self._lock = threading.RLock()
        self._busy_agents: dict[str, str] = {}
        self._reservation_sequence = itertools.count(1)
        self.workspace_coordinator = WorkspaceCoordinator()
        self._state = _restore_state(state, self.agent_names)
        recovered_comparison = self.workspace_coordinator.recover_comparison(
            self._state.get("comparison")
        )
        self._state["comparison"] = recovered_comparison

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
                "native_context_tokens": copy.deepcopy(
                    self._state["native_context_tokens"]
                ),
                "context_projections": copy.deepcopy(
                    self._state["context_projections"]
                ),
                "comparison": copy.deepcopy(self._state.get("comparison")),
            }

    def comparison(self) -> dict[str, Any] | None:
        with self._lock:
            value = self._state.get("comparison")
            return copy.deepcopy(value) if isinstance(value, dict) else None

    def apply_comparison(self, agent: str) -> dict[str, Any]:
        if agent not in self.agent_names:
            raise BridgeError("候选 Agent 必须是 Claude 或 Codex")
        with self._lock:
            comparison = self._state.get("comparison")
            if not isinstance(comparison, dict):
                raise BridgeError("当前群聊没有可采用的 A/B 候选方案")
            if comparison.get("status") not in {"review", "previewing", "conflict"}:
                raise BridgeError("当前 A/B 候选方案还不能采用")
            if not _comparison_candidates_finished(comparison):
                raise BridgeError(
                    "另一个 Agent 仍在执行；当前可以预览已完成候选，"
                    "请等待两个候选都结束后再采用方案"
                )
            candidates = comparison.get("candidates")
            candidate = candidates.get(agent) if isinstance(candidates, dict) else None
            if not isinstance(candidate, dict) or candidate.get("status") not in {
                "ready",
                "no_changes",
            }:
                raise BridgeError("该候选方案不可采用，请先等待其完成或查看错误信息")
            comparison["status"] = "applying"
            comparison["selected_agent"] = agent
            selected_candidate = candidate
        try:
            result = self.workspace_coordinator.apply_candidate(comparison, agent)
        except WorkspaceCoordinatorError as exc:
            with self._lock:
                comparison["status"] = "conflict"
                comparison["error"] = str(exc)
                comparison["recovery_patch"] = ""
                return copy.deepcopy(comparison)
        with self._lock:
            if result.get("applied"):
                comparison["status"] = "applied"
                comparison["selected_agent"] = agent
                preview = comparison.get("preview")
                if isinstance(preview, dict):
                    preview["active_agent"] = ""
                candidates = comparison.get("candidates")
                if isinstance(candidates, dict):
                    for name, candidate in candidates.items():
                        if isinstance(candidate, dict):
                            candidate["apply_status"] = (
                                "applied" if name == agent else "discarded"
                            )
                comparison["error"] = ""
                comparison["recovery_patch"] = ""
                rollback = result.get("rollback")
                response_message_id = str(
                    selected_candidate.get("response_message_id") or ""
                )
                if isinstance(rollback, dict) and response_message_id:
                    for message in self._state["messages"]:
                        if message.get("id") != response_message_id:
                            continue
                        message_changes = message.get("changes")
                        if not isinstance(message_changes, dict):
                            message_changes = {}
                            message["changes"] = message_changes
                        message_changes["rollback"] = rollback
                        break
            else:
                comparison["status"] = "conflict"
                comparison["error"] = str(result.get("error") or "候选方案应用失败")
                comparison["recovery_patch"] = str(result.get("recovery_patch") or "")
                comparison["changed_files"] = list(result.get("changed_files") or [])
            return copy.deepcopy(comparison)

    def preview_comparison(self, agent: str) -> dict[str, Any]:
        """Temporarily switch the main checkout to one candidate's files."""

        if agent not in self.agent_names:
            raise BridgeError("候选 Agent 必须是 Claude 或 Codex")
        with self._lock:
            comparison = self._state.get("comparison")
            if not isinstance(comparison, dict):
                raise BridgeError("当前群聊没有可预览的 A/B 候选方案")
            if comparison.get("status") not in {
                "running",
                "review",
                "previewing",
                "conflict",
            }:
                raise BridgeError("当前 A/B 候选方案还不能预览")
            candidate = (
                comparison.get("candidates", {}).get(agent)
                if isinstance(comparison.get("candidates"), dict)
                else None
            )
            if not isinstance(candidate, dict) or candidate.get("status") not in {
                "ready",
                "no_changes",
            }:
                raise BridgeError("该候选方案不可预览，请先等待其完成或查看错误信息")
        try:
            self.workspace_coordinator.preview_candidate(comparison, agent)
        except WorkspaceCoordinatorError as exc:
            with self._lock:
                comparison["status"] = "conflict"
                comparison["error"] = str(exc)
                return copy.deepcopy(comparison)
        with self._lock:
            comparison["status"] = "previewing"
            comparison["selected_agent"] = None
            comparison["error"] = ""
            return copy.deepcopy(comparison)

    def recheck_comparison(self) -> dict[str, Any]:
        """Recheck whether a conflicted comparison can safely continue."""

        with self._lock:
            comparison = self._state.get("comparison")
            if not isinstance(comparison, dict):
                raise BridgeError("当前群聊没有可重新检查的 A/B 对比方案")
            if comparison.get("status") not in {"conflict", "review", "previewing"}:
                raise BridgeError("当前 A/B 对比状态不能重新检查")
        try:
            result = self.workspace_coordinator.recheck_comparison(comparison)
        except WorkspaceCoordinatorError as exc:
            with self._lock:
                comparison["status"] = "conflict"
                comparison["error"] = str(exc)
                return copy.deepcopy(comparison)
        with self._lock:
            comparison["status"] = str(result.get("status") or "conflict")
            comparison["error"] = str(result.get("error") or "")
            comparison["changed_files"] = list(result.get("changed_files") or [])
            if comparison["status"] == "review":
                comparison["recovery_patch"] = ""
            return copy.deepcopy(comparison)

    def assess_comparison_conflict(self, agent: str) -> dict[str, Any]:
        """Ask one native Agent for a read-only conflict safety assessment.

        The assessment is advisory.  It never changes the main checkout and a
        ``safe`` answer does not bypass the comparison tree guard, recheck, or
        the user's explicit apply confirmation.
        """

        if agent not in self.agent_names:
            raise BridgeError("候选 Agent 必须是 Claude 或 Codex")
        with self._lock:
            comparison = self._state.get("comparison")
            if not isinstance(comparison, dict):
                raise BridgeError("当前群聊没有冲突中的 A/B 对比方案")
            if comparison.get("status") != "conflict":
                raise BridgeError("只有发生应用冲突时才能让 Agent 评估")
            candidate = (
                comparison.get("candidates", {}).get(agent)
                if isinstance(comparison.get("candidates"), dict)
                else None
            )
            if not isinstance(candidate, dict) or candidate.get("status") not in {
                "ready",
                "no_changes",
            }:
                raise BridgeError("该候选方案不可评估，请先等待其完成或查看错误信息")
            candidate_name = self.adapters[agent].display_name

        try:
            context = self.workspace_coordinator.collect_conflict_context(
                comparison,
                agent,
            )
            prompt = _conflict_assessment_prompt(candidate_name, context)
            result = self.adapters[agent].run(
                prompt,
                workspace=Path(str(context["candidate_workspace"])),
                mode="read",
                session_id=None,
                on_event=None,
                step_id=f"comparison_conflict_assessment_{comparison.get('id', 'unknown')}_{agent}",
            )
            after_context = self.workspace_coordinator.collect_conflict_context(
                comparison,
                agent,
            )
            candidate_changed = (
                str(after_context.get("candidate_tree") or "")
                != str(context.get("candidate_tree") or "")
            )
            assessment = _parse_conflict_assessment(result.final_text)
            if candidate_changed:
                assessment.update(
                    {
                        "decision": "needs_review",
                        "confidence": "low",
                        "status": "failed",
                        "error": "评估过程中候选 Worktree 发生了变化，未采纳 Agent 判断。",
                        "candidate_invalidated": True,
                    }
                )
            else:
                assessment["status"] = "completed"
        except BaseException as exc:
            assessment = {
                "status": "failed",
                "decision": "needs_review",
                "confidence": "low",
                "summary": "",
                "reason": "",
                "files": [],
                "checks": [],
                "raw": "",
                "error": str(exc) or exc.__class__.__name__,
            }

        assessment["agent"] = agent
        assessment["created_at"] = _timestamp()
        with self._lock:
            comparison = self._state.get("comparison")
            if not isinstance(comparison, dict):
                raise BridgeError("A/B 对比状态已不存在")
            candidate = comparison.get("candidates", {}).get(agent)
            if not isinstance(candidate, dict):
                raise BridgeError("候选方案已不存在")
            if assessment.get("candidate_invalidated") is True:
                candidate["status"] = "unavailable"
                candidate["error"] = str(assessment.get("error") or "评估过程中候选 Worktree 发生了变化")
            candidate["conflict_assessment"] = assessment
            return copy.deepcopy(comparison)

    def resolve_comparison_conflict(self, agent: str) -> dict[str, Any]:
        """Let an Agent re-implement its candidate on the current main tree."""

        if agent not in self.agent_names:
            raise BridgeError("候选 Agent 必须是 Claude 或 Codex")
        with self._lock:
            comparison = self._state.get("comparison")
            if not isinstance(comparison, dict) or comparison.get("status") != "conflict":
                raise BridgeError("只有发生应用冲突时才能让 Agent 重做方案")
            candidate = (
                comparison.get("candidates", {}).get(agent)
                if isinstance(comparison.get("candidates"), dict)
                else None
            )
            if not isinstance(candidate, dict) or candidate.get("status") not in {
                "ready",
                "no_changes",
            }:
                raise BridgeError("该候选方案不可重做，请先等待其完成或查看错误信息")
            agent_name = self.adapters[agent].display_name

        original_context = self.workspace_coordinator.collect_conflict_context(
            comparison,
            agent,
        )
        resolution: dict[str, Any] | None = None
        try:
            resolution = self.workspace_coordinator.prepare_conflict_resolution(
                comparison,
                agent,
            )
            prompt = _conflict_resolution_prompt(agent_name, original_context)
            result = self.adapters[agent].run(
                prompt,
                workspace=Path(str(resolution["workspace"])),
                mode="write",
                session_id=None,
                on_event=None,
                step_id=f"comparison_conflict_resolution_{comparison.get('id', 'unknown')}_{agent}",
            )
            resolved_baseline = capture_change_baseline(
                Path(str(resolution["workspace"]))
            )
            changes = summarize_workspace_changes(
                Path(str(resolution["workspace"])),
                WorkspaceChangeBaseline.from_dict(resolution.get("baseline")),
            )
            if not resolved_baseline.available or not resolved_baseline.tree:
                raise BridgeError("Agent 重做后无法读取候选工作区快照")
            resolution["base_tree"] = str(
                resolution.get("target_baseline", {}).get("tree") or ""
            )
            resolution["result_tree"] = resolved_baseline.tree
            resolution["resolved_at"] = _timestamp()
            resolution["response"] = str(result.final_text or "")[:20_000]
            if not resolution["base_tree"]:
                raise BridgeError("冲突重做缺少主工作区基线")
            current_main = capture_change_baseline(
                Path(str(original_context["main_workspace"]))
            )
            with self._lock:
                comparison = self._state.get("comparison")
                candidate = (
                    comparison.get("candidates", {}).get(agent)
                    if isinstance(comparison, dict)
                    and isinstance(comparison.get("candidates"), dict)
                    else None
                )
                if not isinstance(comparison, dict) or not isinstance(candidate, dict):
                    raise BridgeError("A/B 候选方案已不存在")
                old_root = str(candidate.get("worktree_root") or "")
                candidate.update(
                    {
                        "workspace": resolution["workspace"],
                        "worktree_root": resolution["worktree_root"],
                        "base_commit": resolution["base_commit"],
                        "baseline": resolution["baseline"],
                        "changes": changes,
                        "result_tree": resolved_baseline.tree,
                        "status": _comparison_candidate_status(changes),
                        "error": "",
                        "cleaned": False,
                        "preview_commands": [
                            f"cd {shlex.quote(str(resolution['workspace']))}",
                            "git status --short",
                            "git diff --stat",
                            "git diff",
                            "git diff --check",
                        ],
                        "conflict_assessment": None,
                        "resolution": resolution,
                    }
                )
                comparison["selected_agent"] = None
                comparison["error"] = "Agent 已基于当前主工作区重新实现候选方案。"
                comparison["recovery_patch"] = ""
                comparison["changed_files"] = []
                comparison["status"] = (
                    "review"
                    if current_main.available and current_main.tree == resolution["base_tree"]
                    else "conflict"
                )
                if comparison["status"] == "conflict":
                    comparison["error"] = "Agent 重做期间主工作区又发生了变化，请先重新检查。"
                result_state = copy.deepcopy(comparison)
            if old_root and old_root != str(resolution["worktree_root"]):
                self.workspace_coordinator.discard_conflict_resolution(
                    {"worktree_root": old_root}
                )
            return result_state
        except BaseException:
            if resolution is not None:
                self.workspace_coordinator.discard_conflict_resolution(resolution)
            raise

    def discard_comparison(self) -> dict[str, Any]:
        with self._lock:
            comparison = self._state.get("comparison")
            if not isinstance(comparison, dict):
                raise BridgeError("当前群聊没有可放弃的 A/B 候选方案")
            if comparison.get("status") == "applying":
                raise BridgeError("当前正在应用候选方案，请稍后再试")
            self.workspace_coordinator.discard_comparison(comparison)
            comparison["status"] = "discarded"
            comparison["selected_agent"] = None
            comparison["error"] = ""
            return copy.deepcopy(comparison)

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
        self.validate_comparison_request(message, forced_recipients=recipients)
        with self._lock:
            comparison = self._state.get("comparison")
            if isinstance(comparison, dict) and comparison.get("status") in {
                "running",
                "review",
                "previewing",
                "applying",
                "conflict",
            }:
                raise BridgeError(
                    "当前有待处理的 A/B 候选方案，请先查看并采用其中一个，或放弃全部方案"
                )
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

    def is_comparison_request(
        self,
        text: str,
        *,
        forced_recipients: tuple[str, ...] | None = None,
    ) -> bool:
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
            text,
            self.agent_names,
            default_agents=default_agents or self.agent_names,
        )
        if forced_recipients is not None:
            recipients = tuple(
                agent for agent in self.agent_names if agent in forced_recipients
            )
        return (
            action == "execute"
            and len(recipients) == 2
            and set(recipients) == set(self.agent_names)
        )

    def validate_comparison_request(
        self,
        text: str,
        *,
        forced_recipients: tuple[str, ...] | None = None,
    ) -> None:
        if not self.is_comparison_request(
            text,
            forced_recipients=forced_recipients,
        ):
            return
        try:
            self.workspace_coordinator.validate_comparison_workspace(
                self.settings.workspace
            )
        except WorkspaceCoordinatorError as exc:
            raise BridgeError(str(exc)) from exc

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

    def set_message_context(self, message_id: str, included: bool) -> dict[str, Any]:
        """Include or exclude one Agent reply from future shared context."""

        with self._lock:
            return dict(
                set_message_context_state(self._state, message_id, included)
            )

    def set_message_rollback(
        self,
        message_id: str,
        rollback: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist the latest safe rollback status on one Agent reply."""

        with self._lock:
            target = next(
                (
                    message
                    for message in self._state["messages"]
                    if message.get("id") == message_id
                ),
                None,
            )
            if target is None:
                raise BridgeError("找不到要回撤的 Agent 回复")
            if target.get("role") != "assistant" or target.get("sender") not in {
                "claude",
                "codex",
            }:
                raise BridgeError("只有 Agent 回复可以回撤代码改动")
            changes = target.get("changes")
            if not isinstance(changes, dict):
                changes = {}
                target["changes"] = changes
            changes["rollback"] = dict(rollback)
            if rollback.get("status") == "rolled_back":
                # Native sessions may still remember the now-reverted files;
                # force the next turn to receive a fresh, truthful context.
                _reset_native_sessions(self._state)
            return dict(target)

    def delete_assistant_message(self, message_id: str) -> dict[str, Any]:
        """Delete one Agent reply before generating its replacement."""

        with self._lock:
            return dict(delete_assistant_message_state(self._state, message_id))

    def recall_user_message(self, message_id: str) -> dict[str, Any]:
        """Recall one user message and invalidate native context snapshots."""

        with self._lock:
            return dict(recall_user_message_state(self._state, message_id))

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
        comparison: dict[str, Any] | None = None
        comparison_mode = action == "execute" and set(recipients) == set(self.agent_names) and len(recipients) == 2
        if comparison_mode:
            comparison_id = f"comparison-{turn}-{next(self._reservation_sequence)}"
            try:
                comparison = self.workspace_coordinator.prepare_comparison(
                    self.settings.workspace,
                    tuple(recipients),
                    comparison_id=comparison_id,
                )
            except WorkspaceCoordinatorError as exc:
                raise BridgeError(str(exc)) from exc
            comparison["trigger_message_id"] = user_message["id"]
            with self._lock:
                self._state["comparison"] = comparison
                for agent in recipients:
                    candidate = comparison["candidates"][agent]
                    workspaces[agent] = Path(str(candidate["workspace"]))
                state_after_comparison = self.to_dict()
            if on_state is not None:
                on_state(state_after_comparison)
        with self._lock:
            session_key, cursor_key = _channel_keys(action)
            for agent in recipients:
                if not _session_resume_enabled(self.adapters[agent]):
                    self._state[session_key][agent] = None
                    self._state[cursor_key][agent] = 0
            sessions: dict[str, str | None] = {}
            prompts: dict[str, str] = {}
            for agent in recipients:
                existing_session = (
                    self._state[session_key].get(agent)
                    if _session_resume_enabled(self.adapters[agent])
                    else None
                )
                prompt, effective_session = self._prompt_for(
                    agent,
                    recipients,
                    action,
                    session_id=existing_session,
                    messages=prompt_messages,
                )
                sessions[agent] = effective_session
                prompts[agent] = prompt

        results: dict[str, AgentRunResult] = {}
        errors: dict[str, str] = {}
        failure_reasons: dict[str, str] = {}
        failure_contents: dict[str, str] = {}
        changes_by_agent: dict[str, dict[str, Any] | None] = {}

        def commit_result(
            agent: str,
            result: AgentRunResult,
            changes: dict[str, Any] | None,
        ) -> dict[str, Any]:
            """Persist one completed Agent immediately, without waiting for peers."""

            with self._lock:
                if _session_resume_enabled(self.adapters[agent]) and result.session_id:
                    self._state[cursor_key][agent] = snapshot_length
                    self._state[session_key][agent] = result.session_id
                    prompt_tokens = estimate_tokens(prompts[agent])
                    output_tokens = max(0, int(result.output_tokens or 0))
                    if sessions[agent] is None:
                        self._state["native_context_tokens"][action][agent] = (
                            prompt_tokens + output_tokens
                        )
                    else:
                        self._state["native_context_tokens"][action][agent] += (
                            prompt_tokens + output_tokens
                        )
                elif sessions[agent]:
                    self._state[cursor_key][agent] = snapshot_length
                    self._state["native_context_tokens"][action][agent] += (
                        estimate_tokens(prompts[agent])
                        + max(0, int(result.output_tokens or 0))
                    )
                else:
                    self._state[cursor_key][agent] = 0
                    self._state[session_key][agent] = None
                    self._state["native_context_tokens"][action][agent] = 0
                # A recall can race with a native process finishing. Keep the
                # placeholder in the transcript, but never append a late reply
                # for the recalled turn.
                if user_message.get("recalled") is True:
                    return self.to_dict()
                reply = self._append_message(
                    sender=agent,
                    role="assistant",
                    content=result.final_text,
                    recipients=("user", *self.agent_names),
                    reply_to=reply_to or user_message["id"],
                    duration_seconds=result.duration_seconds,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    action=action,
                    workspace=str(workspaces[agent]),
                    changes=changes,
                    retry_of=retry_of,
                    retry_mode=retry_mode,
                )
                if comparison_mode and comparison is not None:
                    comparison["candidates"][agent]["response_message_id"] = reply["id"]
                self._normalize_turn_reply_order(
                    reply_to or user_message["id"],
                    recipients,
                )
                return self.to_dict()

        def commit_failure(agent: str) -> dict[str, Any]:
            with self._lock:
                failure_reason = failure_reasons.get(agent, "error")
                if user_message.get("recalled") is True:
                    return self.to_dict()
                reply = self._append_message(
                    sender=agent,
                    role="assistant",
                    content=failure_contents.get(
                        agent,
                        _agent_failure_content(
                            self.adapters[agent].display_name,
                            failure_reason,
                        ),
                    ),
                    recipients=("user", *self.agent_names),
                    reply_to=reply_to or user_message["id"],
                    action=action,
                    failure_reason=failure_reason,
                    retry_of=retry_of,
                    retry_mode=retry_mode,
                )
                if comparison_mode and comparison is not None:
                    comparison["candidates"][agent]["response_message_id"] = reply["id"]
                self._normalize_turn_reply_order(
                    reply_to or user_message["id"],
                    recipients,
                )
                return self.to_dict()

        with ThreadPoolExecutor(
            max_workers=len(recipients),
            thread_name_prefix="multiagent-group-chat",
        ) as executor:
            futures = {}
            for agent in recipients:
                run_kwargs = {
                    "session_id": sessions[agent],
                    "on_event": on_event,
                    "step_id": f"group_chat_turn_{turn}_{agent}",
                }
                if comparison_mode:
                    future = executor.submit(
                        self._run_comparison_agent,
                        agent,
                        prompts[agent],
                        comparison=comparison,
                        **run_kwargs,
                    )
                else:
                    future = executor.submit(
                        self._run_agent,
                        agent,
                        prompts[agent],
                        **run_kwargs,
                    )
                futures[future] = agent
            for future in as_completed(futures):
                agent = futures[future]
                state_after_candidate: dict[str, Any] | None = None
                try:
                    result, changes = future.result()
                    results[agent] = result
                    changes_by_agent[agent] = changes
                    if comparison_mode and comparison is not None:
                        with self._lock:
                            candidate = comparison["candidates"][agent]
                            candidate["changes"] = changes
                            candidate["status"] = _comparison_candidate_status(changes)
                            if candidate["status"] == "unavailable":
                                candidate["error"] = str(
                                    (changes or {}).get("reason")
                                    or "无法生成候选变更预览"
                                )
                    state_after_candidate = commit_result(agent, result, changes)
                except Exception as exc:
                    errors[agent] = str(exc) or exc.__class__.__name__
                    failure_reasons[agent] = _agent_failure_reason(exc)
                    failure_contents[agent] = _agent_failure_content(
                        self.adapters[agent].display_name,
                        failure_reasons[agent],
                        exc,
                    )
                    if comparison_mode and comparison is not None:
                        with self._lock:
                            candidate = comparison["candidates"][agent]
                            candidate["status"] = "failed"
                            candidate["error"] = errors[agent]
                    state_after_candidate = commit_failure(agent)
                if on_state is not None and state_after_candidate is not None:
                    on_state(state_after_candidate)
        reportable_changes = next(
            (changes_by_agent.get(agent) for agent in recipients if changes_by_agent.get(agent)),
            None,
        )
        with self._lock:
            if reportable_changes is not None and not results:
                user_message["changes"] = reportable_changes
            if comparison_mode and comparison is not None:
                preview = comparison.get("preview")
                comparison["status"] = (
                    "previewing"
                    if isinstance(preview, dict) and preview.get("active_agent")
                    else "review"
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
            },
            changes=reportable_changes,
        )

    def _normalize_turn_reply_order(
        self,
        parent_id: str,
        recipients: tuple[str, ...],
    ) -> None:
        """Keep persisted replies in recipient order even when futures finish out of order."""

        if not parent_id:
            return
        rank = {agent: index for index, agent in enumerate(recipients)}
        positions = [
            index
            for index, message in enumerate(self._state["messages"])
            if message.get("reply_to") == parent_id
            and message.get("sender") in rank
        ]
        if len(positions) < 2:
            return
        replies = [self._state["messages"][index] for index in positions]
        replies.sort(key=lambda item: rank.get(str(item.get("sender")), len(rank)))
        for index, reply in zip(positions, replies):
            self._state["messages"][index] = reply

    def _run_agent(
        self,
        agent: str,
        prompt: str,
        *,
        session_id: str | None,
        on_event: Callable[[AgentEvent], None] | None,
        step_id: str,
    ) -> tuple[AgentRunResult, dict[str, Any] | None]:
        def notify_workspace_wait() -> None:
            if on_event is None:
                return
            display_name = self.adapters[agent].display_name
            on_event(
                AgentEvent(
                    display_name,
                    "lifecycle",
                    "waiting_workspace",
                    status="waiting_workspace",
                    step_id=step_id,
                    safe_summary=f"{display_name} · 等待工作区租约",
                )
            )

        try:
            lease = self.workspace_coordinator.acquire(
                self.settings.workspace,
                owner=step_id,
                access="write",
                isolate=self.settings.worktree,
                on_wait=notify_workspace_wait,
            )
        except WorkspaceCoordinatorError as exc:
            raise BridgeError(str(exc)) from exc
        baseline = capture_change_baseline(lease.workspace)
        lease_isolated = bool(getattr(lease, "isolated", False))
        lease_target_workspace = getattr(
            lease,
            "target_workspace",
            getattr(lease, "workspace", self.settings.workspace),
        )
        target_baseline = (
            capture_change_baseline(lease_target_workspace)
            if not lease_isolated
            else None
        )
        try:
            result = self.adapters[agent].run(
                prompt,
                workspace=lease.workspace,
                mode="write",
                session_id=session_id,
                on_event=on_event,
                step_id=step_id,
            )
            if baseline is None:
                changes = None
            else:
                raw_changes = summarize_workspace_changes(lease.workspace, baseline)
                has_file_changes = _has_file_changes(raw_changes)
                changes = (
                    raw_changes
                    if has_file_changes or raw_changes.get("available") is False
                    else None
                )
            release = lease.release()
            rollback = release.get("rollback")
            if not lease_isolated and target_baseline is not None and release.get("merged", True):
                save_rollback = getattr(
                    self.workspace_coordinator,
                    "save_rollback",
                    None,
                )
                rollback = (
                    save_rollback(
                        lease_target_workspace,
                        target_baseline,
                        capture_change_baseline(lease_target_workspace),
                        step_id,
                    )
                    if callable(save_rollback)
                    else None
                )
            if isinstance(rollback, dict) and _has_file_changes(changes):
                changes = dict(changes or {})
                changes["rollback"] = rollback
            if not release.get("merged", True):
                # A failed lease cleanup/merge must not be presented as a code
                # conflict when the native Agent produced no file diff. This
                # can happen if the coordinator failed after a read-only turn
                # or if a temporary workspace was removed without a patch.
                # The Agent's successful answer remains valid; only a real
                # file diff can have a meaningful merge conflict.
                if not (baseline is not None and _has_file_changes(changes)):
                    return result, changes
                merge_error = str(release.get("error") or "隔离 Worktree 合并失败")
                # The native Agent already completed successfully. A merge
                # conflict is a workspace delivery problem, not a failed
                # model response; preserve its answer and expose the precise
                # recovery state in the change summary.
                changes = dict(changes or {})
                changes["merge_status"] = "conflict"
                changes["merge_error"] = merge_error
                return result, changes
            return result, changes
        except BaseException:
            lease.release()
            raise

    def _run_comparison_agent(
        self,
        agent: str,
        prompt: str,
        *,
        session_id: str | None,
        on_event: Callable[[AgentEvent], None] | None,
        step_id: str,
        comparison: dict[str, Any] | None,
    ) -> tuple[AgentRunResult, dict[str, Any] | None]:
        if comparison is None:
            raise BridgeError("A/B 对比任务缺少候选工作区")
        try:
            workspace = self.workspace_coordinator.candidate_workspace(
                comparison,
                agent,
            )
            result = self.adapters[agent].run(
                prompt,
                workspace=workspace,
                mode="write",
                session_id=session_id,
                on_event=on_event,
                step_id=step_id,
            )
            changes = self.workspace_coordinator.collect_candidate_diff(
                comparison,
                agent,
            )
            return result, changes
        except WorkspaceCoordinatorError as exc:
            raise BridgeError(str(exc)) from exc

    def _prompt_for(
        self,
        agent: str,
        recipients: tuple[str, ...],
        action: str,
        *,
        session_id: str | None,
        messages: list[dict[str, Any]] | None = None,
    ) -> tuple[str, str | None]:
        _session_key, cursor_key = _channel_keys(action)
        all_messages = messages if messages is not None else self._state["messages"]
        has_session = bool(session_id)
        cursor = int(self._state[cursor_key].get(agent, 0)) if has_session else 0
        visible = _visible_messages(
            all_messages,
            agent=agent,
            action=action,
            start=cursor,
            exclude_agent_replies=has_session,
        )
        resumable = _session_resume_enabled(self.adapters[agent])
        effective_session = session_id
        if not resumable:
            projection = build_context_projection(
                _visible_messages(all_messages, agent=agent, action=action),
                self.settings.context_compaction,
                _format_message,
            )
            self._state["context_projections"][action][agent] = (
                dict(projection.record) if projection.record is not None else None
            )
            transcript = projection.text
        elif not session_id:
            projection = build_context_projection(
                _visible_messages(all_messages, agent=agent, action=action),
                self.settings.context_compaction,
                _format_message,
            )
            self._state["context_projections"][action][agent] = (
                dict(projection.record) if projection.record is not None else None
            )
            transcript = projection.text
        else:
            delta_transcript = "\n\n".join(
                _format_message(item) for item in visible
            )
            current_usage = int(
                self._state["native_context_tokens"][action].get(agent, 0)
            )
            projected_usage = current_usage + estimate_tokens(delta_transcript)
            if (
                self.settings.context_compaction.enabled
                and projected_usage > self.settings.context_compaction.threshold_tokens
            ):
                projection = build_context_projection(
                    _visible_messages(all_messages, agent=agent, action=action),
                    self.settings.context_compaction,
                    _format_message,
                    force=True,
                )
                if projection.compacted:
                    self._state["context_projections"][action][agent] = dict(
                        projection.record or {}
                    )
                    transcript = projection.text
                    effective_session = None
                    self._state[_session_key][agent] = None
                    self._state[cursor_key][agent] = 0
                    self._state["native_context_tokens"][action][agent] = 0
                else:
                    transcript = delta_transcript
            else:
                transcript = delta_transcript
        identity = (
            self.settings.group_chat_agent_a_identity
            if agent == "claude"
            else self.settings.group_chat_agent_b_identity
        )
        if action == "execute":
            route = f"用户本轮明确要求 {self.adapters[agent].display_name} 执行。"
        else:
            route = (
                "用户未点名具体 Agent，本轮所有 Agent 独立并行回答。"
                if len(recipients) == len(self.agent_names)
                else f"用户本轮只要求 {self.adapters[agent].display_name} 回答。"
            )
        permission_note = (
            "MultiAgent 不替你判断本轮应只读还是写入。"
            "当前进程已经具备本轮工作区的读取和写入能力；"
            "请根据用户的实际请求，自行决定检查、修改和验证范围。"
            "用户明确要求修改时，不得以只读模式或没有工作区写权限为由拒绝；"
            "只有网络、工作区外访问、危险命令等原生受限操作，才通过审批请求用户决定。"
            "只能在本轮进程实际打开的工作区中操作，不要提交 Git。"
        )
        completion_note = (
            "若修改了文件，说明修改内容、验证结果和尚存风险；"
            "若无需修改，直接回答用户问题。"
        )
        comparison_note = _comparison_outcome_note(
            self._state.get("comparison"),
            self.adapters,
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
                comparison_note=comparison_note,
            )
        ), effective_session

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
    if isinstance(error, AgentModelCompatibilityError):
        return "model_incompatible"
    if isinstance(error, AgentTimeoutError):
        return "timeout"
    text = str(error or "").lower()
    return (
        "timeout"
        if "timeout" in text or "timed out" in text or "超时" in text
        else "error"
    )


def _agent_failure_content(
    agent_name: str,
    reason: str,
    error: object | None = None,
) -> str:
    if reason == "timeout":
        return f"响应超时。{agent_name} 连续在限定时间内没有新活动，本轮已终止。"
    if reason == "model_incompatible":
        model = (
            error.model
            if isinstance(error, AgentModelCompatibilityError)
            else "当前候选模型"
        )
        return (
            f"模型不兼容。{agent_name} 的 {model} 不接受当前原生 Agent 请求格式；"
            "请更换模型或网关。"
        )
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


def is_explicit_comparison_execution_request(text: str) -> bool:
    """Return whether a message explicitly targets both Agents for execution."""

    if not _MENTION_RE.search(text):
        return False
    try:
        recipients, action = resolve_directive(
            text,
            GROUP_CHAT_AGENTS,
            default_agents=GROUP_CHAT_AGENTS,
        )
    except BridgeError:
        return False
    return action == "execute" and set(recipients) == set(GROUP_CHAT_AGENTS)


def _channel_keys(action: str) -> tuple[str, str]:
    if action == "execute":
        return "execution_sessions", "execution_cursors"
    return "sessions", "cursors"


def _visible_messages(
    messages: list[dict[str, Any]],
    *,
    agent: str,
    action: str | None = None,
    start: int = 0,
    exclude_agent_replies: bool = False,
) -> list[dict[str, Any]]:
    """Filter messages for one Agent without mutating the persisted record."""

    visible: list[dict[str, Any]] = []
    recalled_ids = {
        str(message.get("id"))
        for message in messages
        if isinstance(message, dict) and message.get("recalled") is True
    }
    for message in messages[max(0, start) :]:
        if message.get("recalled") is True:
            continue
        if str(message.get("reply_to") or "") in recalled_ids:
            continue
        if message.get("include_in_context") is False:
            continue
        if (
            exclude_agent_replies
            and message.get("role") == "assistant"
            and message.get("sender") == agent
            and (action is None or message.get("action", "discuss") == action)
        ):
            continue
        visible.append(message)
    return visible


def _estimate_history_tokens(
    messages: list[dict[str, Any]],
    *,
    agent: str,
    action: str,
) -> int:
    return estimate_tokens(
        "\n\n".join(
            _format_message(item)
            for item in _visible_messages(messages, agent=agent, action=action)
        )
    )


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


def _comparison_candidates_finished(comparison: dict[str, Any]) -> bool:
    candidates = comparison.get("candidates")
    if not isinstance(candidates, dict):
        return False
    return all(
        isinstance(candidate, dict)
        and str(candidate.get("status") or "")
        in {"ready", "no_changes", "failed", "unavailable"}
        for candidate in candidates.values()
    )


def _comparison_candidate_status(changes: dict[str, Any] | None) -> str:
    if not isinstance(changes, dict) or changes.get("available") is False:
        return "unavailable"
    return "ready" if _has_file_changes(changes) else "no_changes"


def _comparison_outcome_note(
    comparison: object,
    adapters: Mapping[str, BaseCLIAdapter],
) -> str:
    """Render the durable A/B decision into each subsequent Agent prompt."""

    if not isinstance(comparison, dict):
        return ""
    status = str(comparison.get("status") or "")
    if status == "applied":
        selected = str(comparison.get("selected_agent") or "")
        if selected not in adapters:
            return ""
        selected_name = adapters[selected].display_name
        candidates = comparison.get("candidates")
        discarded = []
        if isinstance(candidates, dict):
            for agent, candidate in candidates.items():
                if agent == selected or not isinstance(candidate, dict):
                    continue
                if candidate.get("apply_status") == "discarded":
                    discarded.append(
                        adapters.get(agent).display_name
                        if adapters.get(agent) is not None
                        else str(agent)
                    )
        discarded_text = "、".join(discarded) or "另一候选"
        return (
            "<comparison_outcome>\n"
            f"上一轮 A/B 对比已结束：用户采用了 {selected_name} 方案；"
            f"{discarded_text} 方案未被采用，候选工作区已清理。"
            "主工作区保留所采用方案的未提交修改。\n"
            "</comparison_outcome>"
        )
    if status == "discarded":
        return (
            "<comparison_outcome>\n"
            "上一轮 A/B 对比已放弃：Claude Code 和 Codex 的候选方案都未被采用，"
            "主工作区未因该对比任务发生修改。\n"
            "</comparison_outcome>"
        )
    return ""


def _conflict_assessment_prompt(
    agent_name: str,
    context: dict[str, Any],
) -> str:
    changed_files = context.get("changed_files")
    changed_summary = "\n".join(
        f"- {item.get('status', 'M')} {item.get('path', '')}"
        for item in changed_files
        if isinstance(item, dict) and item.get("path")
    ) if isinstance(changed_files, list) else ""
    return f"""你是 {agent_name}，正在为 MultiAgent 做一次只读的候选应用冲突评估。

这不是实现任务。禁止修改、创建或删除任何文件，也不要执行会改变 Git 索引或工作区的命令。
你只需要判断：把候选方案相对共同基线的完整修改应用到当前主工作区，是否能够在不覆盖用户新改动、不丢失功能的前提下安全完成。

评估规则：
1. 同一文件甚至同一行被双方修改时，不能仅凭 Git 可能三方合并就判定安全；必须考虑语义冲突。
2. 删除、重命名、二进制文件、生成文件或信息不足时，优先返回 needs_review。
3. safe 仅表示你认为可以继续尝试；系统仍会执行 Git 校验并要求用户确认。
4. 不要尝试应用补丁，不要给出内部思考过程。

主工作区：{context.get('main_workspace', '')}
候选工作区：{context.get('candidate_workspace', '')}
共同基线树：{context.get('base_tree', '')}
当前主工作区树：{context.get('main_tree', '')}
候选树：{context.get('candidate_tree', '')}

主工作区在对比开始后的变化文件：
{changed_summary or '- 未能列出'}

主工作区相对共同基线的变更：
{_compact_change_context(context.get('main_changes'))}

候选方案相对共同基线的变更：
{_compact_change_context(context.get('candidate_changes'))}

只输出一个 JSON 对象，不要使用 Markdown 代码块：
{{"decision":"safe|unsafe|needs_review","confidence":"high|medium|low","reason":"简洁说明判断依据","files":["需要关注的文件"],"checks":["应用前后应执行的校验"]}}
"""


def _conflict_resolution_prompt(
    agent_name: str,
    context: dict[str, Any],
) -> str:
    return f"""你是 {agent_name}，正在为 MultiAgent 解决一次候选方案应用冲突。

当前工作目录已经是“冲突发生后主工作区”的隔离副本。请在这个目录中直接修改代码，重新实现你原候选方案的目标，同时完整保留主工作区中用户刚刚已有的修改。不要修改主工作区路径中的文件。

要求：
1. 先阅读当前工作区和下面给出的原候选 Diff，理解原方案想解决的问题。
2. 将原候选方案的有效意图改写成适配当前工作区的实现；不要机械覆盖用户修改。
3. 只在当前隔离工作区中写入文件；可以运行必要的只读检查或项目测试。
4. 完成后说明实际修改的文件、冲突如何处理、验证结果。不要输出内部思考过程。

主工作区基线（当前隔离副本已包含）：{context.get('main_tree', '')}
原对比基线：{context.get('base_tree', '')}
主工作区发生变化的文件：
{_compact_change_context({"available": True, "files": [{"path": item.get("path"), "status": item.get("status")} for item in context.get("changed_files", []) if isinstance(item, dict)]})}

主工作区相对原基线的变更：
{_compact_change_context(context.get('main_changes'))}

你的原候选方案相对原基线的变更：
{_compact_change_context(context.get('candidate_changes'))}
"""


def _compact_change_context(value: object, *, limit: int = 45_000) -> str:
    if not isinstance(value, dict):
        return "无法读取变更摘要"
    if value.get("available") is False:
        return str(value.get("reason") or "无法生成变更摘要")
    files = value.get("files")
    if not isinstance(files, list) or not files:
        return "没有文件变化"
    parts: list[str] = []
    used = 0
    for item in files:
        if not isinstance(item, dict):
            continue
        header = (
            f"\n--- {item.get('status', 'modified')} {item.get('path', '')} "
            f"(+{item.get('additions', '?')} -{item.get('deletions', '?')}) ---\n"
        )
        patch = str(item.get("patch") or "")
        block = header + (patch or "[二进制文件或无可用文本 Diff]")
        remaining = limit - used
        if remaining <= 0:
            break
        if len(block) > remaining:
            parts.append(block[:remaining].rstrip() + "\n… 评估上下文已截断 …")
            used = limit
            break
        parts.append(block)
        used += len(block)
    return "".join(parts).strip() or "没有可用的文本 Diff"


def _parse_conflict_assessment(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    parsed: dict[str, Any] | None = None
    candidates = [raw]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL | re.IGNORECASE)
    if fenced:
        candidates.append(fenced.group(1))
    first = raw.find("{")
    last = raw.rfind("}")
    if first >= 0 and last > first:
        candidates.append(raw[first : last + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            parsed = value
            break

    data = parsed or {}
    decision = str(data.get("decision") or "").strip().lower()
    decision_aliases = {
        "yes": "safe",
        "can_apply": "safe",
        "可应用": "safe",
        "no": "unsafe",
        "cannot_apply": "unsafe",
        "不可应用": "unsafe",
        "review": "needs_review",
        "manual_review": "needs_review",
        "需人工复核": "needs_review",
    }
    decision = decision_aliases.get(decision, decision)
    if decision not in {"safe", "unsafe", "needs_review"}:
        decision = "needs_review"
    confidence = str(data.get("confidence") or "").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    reason = str(data.get("reason") or data.get("summary") or "").strip()
    if not reason:
        reason = raw[:2_000] if raw else "Agent 未返回可解析的结构化判断。"

    def string_list(key: str) -> list[str]:
        values = data.get(key)
        if not isinstance(values, list):
            return []
        return [str(value).strip() for value in values if str(value).strip()][:50]

    return {
        "decision": decision,
        "confidence": confidence,
        "summary": reason,
        "reason": reason,
        "files": string_list("files"),
        "checks": string_list("checks"),
        "raw": raw[:10_000],
        "error": "" if parsed is not None else "Agent 输出不是有效 JSON，已按需人工复核处理。",
    }


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
        "native_context_tokens": {
            action: {agent: 0 for agent in agents}
            for action in ("discuss", "execute")
        },
        "context_projections": _empty_context_projections(agents),
        "comparison": None,
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
    native_context_tokens = state.get("native_context_tokens")
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
    if isinstance(native_context_tokens, dict):
        for action in ("discuss", "execute"):
            channel = native_context_tokens.get(action)
            if not isinstance(channel, dict):
                continue
            for agent in agents:
                value = channel.get(agent)
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                ):
                    restored["native_context_tokens"][action][agent] = value
    else:
        for action in ("discuss", "execute"):
            session_key, _cursor_key = _channel_keys(action)
            for agent in agents:
                if restored[session_key].get(agent):
                    restored["native_context_tokens"][action][agent] = (
                        _estimate_history_tokens(
                            restored["messages"],
                            agent=agent,
                            action=action,
                        )
                    )
    raw_projections = state.get("context_projections")
    if isinstance(raw_projections, dict):
        for action in ("discuss", "execute"):
            raw_channel = raw_projections.get(action)
            if not isinstance(raw_channel, dict):
                continue
            for agent in agents:
                record = _restore_context_projection(raw_channel.get(agent))
                if record is not None:
                    restored["context_projections"][action][agent] = record
    comparison = state.get("comparison")
    if isinstance(comparison, dict):
        restored["comparison"] = copy.deepcopy(comparison)
    return restored


def _empty_context_projections(
    agents: tuple[str, ...],
) -> dict[str, dict[str, dict[str, Any] | None]]:
    return {
        action: {agent: None for agent in agents}
        for action in ("discuss", "execute")
    }


def _restore_context_projection(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    if raw.get("version") != CONTEXT_PROJECTION_VERSION:
        return None
    required_text = ("mode", "through_message_id", "source_hash", "summary")
    if not all(isinstance(raw.get(key), str) for key in required_text):
        return None
    required_counts = (
        "source_message_count",
        "recent_message_count",
        "estimated_tokens_before",
        "estimated_tokens_after",
    )
    if not all(
        isinstance(raw.get(key), int)
        and not isinstance(raw.get(key), bool)
        and raw.get(key) >= 0
        for key in required_counts
    ):
        return None
    return dict(raw)


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
    if role == "assistant" and raw.get("include_in_context") is False:
        restored["include_in_context"] = False
    else:
        restored.pop("include_in_context", None)
    if raw.get("recalled") is True:
        restored["recalled"] = True
        if isinstance(raw.get("recalled_at"), str):
            restored["recalled_at"] = raw["recalled_at"]
    else:
        restored.pop("recalled", None)
        restored.pop("recalled_at", None)
    return restored


def set_message_context_state(
    state: dict[str, Any],
    message_id: str,
    included: bool,
) -> dict[str, Any]:
    """Mutate persisted chat state and invalidate native session snapshots."""

    if not isinstance(included, bool):
        raise BridgeError("included 必须是布尔值")
    messages = state.get("messages")
    if not isinstance(messages, list):
        raise BridgeError("群聊记录缺少消息列表")
    target = next(
        (
            message
            for message in messages
            if isinstance(message, dict) and message.get("id") == message_id
        ),
        None,
    )
    if target is None:
        raise BridgeError("找不到要调整共同上下文的消息")
    if target.get("role") != "assistant" or target.get("sender") not in {
        "claude",
        "codex",
    }:
        raise BridgeError("只有 Agent 回复可以调整共同上下文")
    if included:
        target.pop("include_in_context", None)
    else:
        target["include_in_context"] = False
    _reset_native_sessions(state)
    return target


def delete_assistant_message_state(
    state: dict[str, Any],
    message_id: str,
) -> dict[str, Any]:
    """Delete one persisted Agent reply and invalidate native sessions."""

    messages = state.get("messages")
    if not isinstance(messages, list):
        raise BridgeError("群聊记录缺少消息列表")
    index = next(
        (
            index
            for index, message in enumerate(messages)
            if isinstance(message, dict) and message.get("id") == message_id
        ),
        None,
    )
    if index is None:
        raise BridgeError("找不到要重试的 Agent 消息")
    target = messages[index]
    if target.get("role") != "assistant" or target.get("sender") not in {
        "claude",
        "codex",
    }:
        raise BridgeError("只有 Agent 回复可以重试")
    removed = messages.pop(index)
    _reset_native_sessions(state)
    return removed


def recall_user_message_state(
    state: dict[str, Any],
    message_id: str,
) -> dict[str, Any]:
    """Recall a user turn while retaining a visible, non-context placeholder."""

    messages = state.get("messages")
    if not isinstance(messages, list):
        raise BridgeError("群聊记录缺少消息列表")
    target = next(
        (
            message
            for message in messages
            if isinstance(message, dict) and message.get("id") == message_id
        ),
        None,
    )
    if target is None:
        raise BridgeError("找不到要撤回的消息")
    if target.get("role") != "user" or target.get("sender") != "user":
        raise BridgeError("只有用户消息可以撤回")
    if target.get("recalled") is True:
        return target

    target["recalled"] = True
    target["recalled_at"] = _timestamp()
    target["hidden"] = False
    target["content"] = "消息已撤回"
    target.pop("agent_content", None)
    target.pop("attachments", None)
    for message in messages:
        if (
            isinstance(message, dict)
            and message.get("reply_to") == message_id
            and message.get("role") == "assistant"
        ):
            message["recalled"] = True
            message["hidden"] = True
    _reset_native_sessions(state)
    return target


def _reset_native_sessions(state: dict[str, Any]) -> None:
    for session_key, empty_value in (
        ("sessions", None),
        ("cursors", 0),
        ("execution_sessions", None),
        ("execution_cursors", 0),
    ):
        values = state.get(session_key)
        if not isinstance(values, dict):
            continue
        for agent in values:
            values[agent] = empty_value
    native_context_tokens = state.get("native_context_tokens")
    if isinstance(native_context_tokens, dict):
        for channel in native_context_tokens.values():
            if not isinstance(channel, dict):
                continue
            for agent in channel:
                channel[agent] = 0
    projections = state.get("context_projections")
    if isinstance(projections, dict):
        for channel in projections.values():
            if not isinstance(channel, dict):
                continue
            for agent in channel:
                channel[agent] = None


def reset_native_context_state(state: dict[str, Any]) -> None:
    """Invalidate native Agent snapshots after an external workspace revert."""

    _reset_native_sessions(state)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
