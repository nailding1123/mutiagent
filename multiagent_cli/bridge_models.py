from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .token_api import TokenAPISettings

DEFAULT_GROUP_CHAT_IDENTITY = (
    "你是 MultiAgent 群聊中的一名原生 Agent 协作伙伴。直接回应用户当前的问题，"
    "并结合群聊历史补充有价值的信息。若另一位 Agent 已经回答，不要机械重复；"
    "可以认可正确部分、指出遗漏或提出不同看法。表达自然、清晰、简洁，不要把普通交流"
    "强行变成正式方案或评审流程。涉及代码时先依据工作区事实判断，不确定的内容要明确说明。"
)
DEFAULT_GROUP_CHAT_AGENT_A_IDENTITY = DEFAULT_GROUP_CHAT_IDENTITY
DEFAULT_GROUP_CHAT_AGENT_B_IDENTITY = DEFAULT_GROUP_CHAT_IDENTITY

LEGACY_GROUP_CHAT_AGENT_A_IDENTITY = (
    "你是群聊中的 Claude，一名善于理解需求、分析复杂问题和组织方案的协作伙伴。"
    "直接回应用户当前的问题，并结合群聊历史补充有价值的信息。若 Codex 已经回答，"
    "不要机械重复；可以认可正确部分、指出遗漏或提出不同看法。表达自然、清晰、简洁，"
    "不要把普通交流强行变成正式方案或评审流程。涉及代码时先依据工作区事实判断，"
    "不确定的内容要明确说明。"
)
LEGACY_GROUP_CHAT_AGENT_B_IDENTITY = (
    "你是群聊中的 Codex，一名偏重代码实现、工程细节和验证结果的协作伙伴。"
    "直接回应用户当前的问题，并结合群聊历史给出可执行的建议。若 Claude 已经回答，"
    "不要机械重复；优先补充代码事实、边界情况、风险和验证方法，也可以明确提出不同意见。"
    "表达自然、清晰、简洁，不要把普通交流强行变成正式方案或评审流程。"
    "涉及代码时以实际工作区内容为依据，不确定的内容要明确说明。"
)
_LEGACY_GROUP_CHAT_IDENTITIES = {
    LEGACY_GROUP_CHAT_AGENT_A_IDENTITY,
    LEGACY_GROUP_CHAT_AGENT_B_IDENTITY,
}


def normalize_group_chat_identity(value: str) -> str:
    """Map former built-in role prompts to the shared neutral default."""

    normalized = value.strip()
    return (
        DEFAULT_GROUP_CHAT_IDENTITY
        if normalized in _LEGACY_GROUP_CHAT_IDENTITIES
        else normalized
    )

EVENT_PROTOCOL = "multiagent.event.v2"

_EVENT_DEFAULT_STATUSES = {
    "lifecycle": "in_progress",
    "progress": "working",
    "tool": "working",
    "tool_result": "working",
    "text": "completed",
    "metric": "completed",
    "warning": "warning",
    "error": "failed",
    "log": "working",
    "interaction_request": "waiting_user",
    "interaction_response": "working",
}

_SAFE_TEXT_KINDS = {
    "lifecycle",
    "warning",
}

# 在显式开启"显示模型原文流"时允许流式增量通过；默认仍处于 safe 通道
_STREAM_TEXT_KINDS = {
    "progress",
    "text",
}

_SAFE_METADATA_KEYS = {
    "attempt",
    "check",
    "duration_seconds",
    "exit_code",
    "input_tokens",
    "output_tokens",
    "passed",
    "timed_out",
    "timeout_seconds",
}

_PUBLIC_ACTIVITY_KINDS = {"tool", "tool_result"}
_MAX_PUBLIC_ACTIVITY_CHARS = 6_000
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|authorization|password|passwd|secret|token)\b[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER_TOKEN_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_COMMON_SECRET_RE = re.compile(
    r"\b(?:sk|pk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_\-]{8,}\b",
    re.IGNORECASE,
)


class BridgeError(RuntimeError):
    """A readable failure raised by a native CLI or the bridge."""


class AgentTimeoutError(BridgeError):
    """One model attempt exceeded its configured response timeout."""


class AgentModelCompatibilityError(BridgeError):
    """A configured model rejected the native Agent protocol shape."""

    def __init__(self, agent_name: str, model: str | None, reason: str) -> None:
        self.agent_name = agent_name
        self.model = model or "CLI 默认模型"
        self.reason = reason
        super().__init__(
            f"{agent_name} 模型 {self.model} 与当前 CLI/API 协议不兼容：{reason}"
        )


class NativeInteractionUnavailable(BridgeError):
    """A native CLI requested user interaction but no handler was attached."""


@dataclass(frozen=True)
class AgentCommandSettings:
    """How to launch one native coding-agent CLI."""

    command: tuple[str, ...]
    model: str | None = None
    models: tuple[str, ...] = ()
    fallback_on_timeout: bool = True
    extra_args: tuple[str, ...] = ()
    timeout: float = 900
    # Codex Responses reasoning effort. None delegates to the native default.
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class ContextCompactionSettings:
    """How shared chat history is projected into bounded Agent context."""

    enabled: bool = True
    threshold_tokens: int = 16_000
    target_tokens: int = 8_000
    recent_messages: int = 8


@dataclass(frozen=True)
class BridgeSettings:
    """Resolved settings for a Claude/Codex bridge session."""

    workspace: Path
    claude: AgentCommandSettings
    codex: AgentCommandSettings
    config_path: Path | None = None
    group_chat_agent_a_identity: str = DEFAULT_GROUP_CHAT_AGENT_A_IDENTITY
    group_chat_agent_b_identity: str = DEFAULT_GROUP_CHAT_AGENT_B_IDENTITY
    group_chat_default_agent: str = "both"
    # Isolated Git worktrees are opt-in: overlapping Agent edits are safer when
    # serialized in the shared checkout than merged after both have finished.
    worktree: bool = False
    context_compaction: ContextCompactionSettings = field(
        default_factory=ContextCompactionSettings
    )
    token_api: TokenAPISettings = field(default_factory=TokenAPISettings)


@dataclass(frozen=True)
class AgentEvent:
    """Versioned event emitted by native CLIs and the bridge.

    The first three fields intentionally retain the v1 positional API.  v2 adds
    stable turn context and both wall-clock and relative timing without
    forcing renderers or run stores to expose raw commands and intermediate
    model text.
    """

    source: str
    kind: str
    text: str
    status: str = ""
    step_id: str = ""
    timestamp: str = field(default_factory=lambda: _event_timestamp())
    elapsed_seconds: float | None = None
    safe_summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    protocol: str = EVENT_PROTOCOL

    def __post_init__(self) -> None:
        if not self.status:
            object.__setattr__(
                self,
                "status",
                _EVENT_DEFAULT_STATUSES.get(self.kind, "updated"),
            )
        if self.elapsed_seconds is not None and self.elapsed_seconds < 0:
            object.__setattr__(self, "elapsed_seconds", 0.0)

    def to_dict(
        self,
        *,
        safe: bool = False,
        allow_stream: bool = False,
        include_activity: bool = False,
    ) -> dict[str, Any]:
        """Serialize the event, optionally replacing sensitive details.

        When ``allow_stream`` is True, streaming-capable kinds (progress/text)
        keep their raw text so the UI can render incrementally. Other kinds
        still fall back to ``safe_summary`` when ``safe`` is requested.
        """

        text = self.text
        if safe and self.kind not in _SAFE_TEXT_KINDS:
            stream_allowed = allow_stream and self.kind in _STREAM_TEXT_KINDS
            if not stream_allowed:
                text = self.safe_summary or (
                    f"{self.source} · 本轮发生错误" if self.kind == "error" else ""
                )
        metadata = dict(self.metadata)
        if safe:
            metadata = {
                key: value
                for key, value in metadata.items()
                if key in _SAFE_METADATA_KEYS
            }
        payload = {
            "protocol": self.protocol,
            "source": self.source,
            "kind": self.kind,
            "status": self.status,
            "step_id": self.step_id,
            "timestamp": self.timestamp,
            "elapsed_seconds": self.elapsed_seconds,
            "text": text,
            "safe_summary": self.safe_summary,
            "metadata": metadata,
        }
        if safe and include_activity:
            activity = self._public_activity()
            if activity is not None:
                payload["activity"] = activity
        return payload

    def _public_activity(self) -> dict[str, str] | None:
        """Return a bounded, redacted tool/command description for the UI.

        Native agents may put credentials or other private values in tool
        inputs. The Web UI therefore never receives raw event metadata; this
        deliberately small projection is the only detailed activity channel.
        """

        if self.kind not in _PUBLIC_ACTIVITY_KINDS:
            return None
        metadata = self.metadata
        activity_type = _public_activity_value(
            metadata.get("activity_type"),
            fallback="tool",
            limit=40,
        )
        tool_name = _public_activity_value(metadata.get("tool_name"), limit=120)
        activity_id = _public_activity_value(metadata.get("activity_id"), limit=200)
        command = _public_activity_value(metadata.get("command"))
        path = _public_activity_value(metadata.get("path"))
        output = _public_activity_value(metadata.get("output"))
        detail = _public_activity_value(metadata.get("detail"))
        if not any((command, path, output, detail)):
            detail = _public_activity_value(self.text)

        if self.kind == "tool_result":
            title = {
                "command": "命令执行结果",
                "file_change": "文件操作结果",
                "read": "读取结果",
                "search": "搜索结果",
            }.get(activity_type, "工具调用结果")
            selected_detail = output or detail
        else:
            title = {
                "command": "执行命令",
                "file_change": "修改文件",
                "read": "读取文件",
                "search": "搜索内容",
            }.get(activity_type, f"调用工具 · {tool_name}" if tool_name else "调用工具")
            selected_detail = command or path or detail

        return {
            "id": activity_id,
            "type": activity_type,
            "title": title,
            "tool_name": tool_name,
            "detail": selected_detail,
            "detail_label": (
                "命令"
                if command and self.kind == "tool"
                else "输出"
                if output and self.kind == "tool_result"
                else "路径"
                if path and selected_detail == path
                else "详情"
            ),
        }


@dataclass(frozen=True)
class NativeInteractionOption:
    """One user-facing choice exposed by a native coding agent."""

    value: str
    label: str
    description: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "value": self.value,
            "label": self.label,
            "description": self.description,
        }


@dataclass(frozen=True)
class NativeInteractionQuestion:
    """One native question, optionally with a fixed set of choices."""

    id: str
    question: str
    header: str = ""
    options: tuple[NativeInteractionOption, ...] = ()
    allow_other: bool = False
    secret: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "header": self.header,
            "options": [option.to_dict() for option in self.options],
            "allow_other": self.allow_other,
            "secret": self.secret,
        }


@dataclass(frozen=True)
class NativeInteractionRequest:
    """Provider-neutral approval or user-input request from a native CLI."""

    id: str
    source: str
    kind: str
    title: str
    message: str = ""
    command: str = ""
    cwd: str = ""
    options: tuple[NativeInteractionOption, ...] = ()
    questions: tuple[NativeInteractionQuestion, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "kind": self.kind,
            "title": self.title,
            "message": self.message,
            "command": self.command,
            "cwd": self.cwd,
            "options": [option.to_dict() for option in self.options],
            "questions": [question.to_dict() for question in self.questions],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class NativeInteractionResponse:
    """The user's answer to a provider-neutral native request."""

    action: str
    answers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    text: str = ""

    def normalized_answers(self) -> dict[str, tuple[str, ...]]:
        return {
            str(key): (
                tuple(str(value) for value in values)
                if isinstance(values, (list, tuple))
                else (str(values),)
            )
            for key, values in self.answers.items()
        }


@dataclass(frozen=True)
class AgentRunResult:
    """Result of one native CLI turn."""

    agent: str
    final_text: str
    session_id: str | None = None
    exit_code: int = 0
    duration_seconds: float = 0
    input_tokens: int = 0
    output_tokens: int = 0


def _public_activity_value(
    value: object,
    *,
    fallback: str = "",
    limit: int = _MAX_PUBLIC_ACTIVITY_CHARS,
) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return fallback
    text = str(value).strip()
    if not text:
        return fallback
    text = _SECRET_ASSIGNMENT_RE.sub(r"\1***", text)
    text = _BEARER_TOKEN_RE.sub("Bearer ***", text)
    text = _COMMON_SECRET_RE.sub("***", text)
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return f"{text[:head]}\n… 已截断 …\n{text[-tail:]}"


def _event_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
