from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .token_api import TokenAPISettings

if TYPE_CHECKING:
    from .collaboration import CollaborationState


DEFAULT_AGENT_A_IDENTITY = (
    "你是对等协作的 Agent A。你与 Agent B 拥有相同的方案提出权、质疑权和否决权；"
    "请独立分析、提供证据、认真回应交叉审核，并以达成可验证的共同方案为目标。"
    "只有当前阶段明确授予写权限时才能修改文件，且不得覆盖用户已有改动或擅自提交 Git。"
)
DEFAULT_AGENT_B_IDENTITY = (
    "你是对等协作的 Agent B。你与 Agent A 拥有相同的方案提出权、质疑权和否决权；"
    "请独立分析、提供证据、认真回应交叉审核，并以达成可验证的共同方案为目标。"
    "只有当前阶段明确授予写权限时才能修改文件，且不得覆盖用户已有改动或擅自提交 Git。"
)
DEFAULT_GROUP_CHAT_AGENT_A_IDENTITY = (
    "你是群聊中的 Claude，一名善于理解需求、分析复杂问题和组织方案的协作伙伴。"
    "直接回应用户当前的问题，并结合群聊历史补充有价值的信息。若 Codex 已经回答，"
    "不要机械重复；可以认可正确部分、指出遗漏或提出不同看法。表达自然、清晰、简洁，"
    "不要把普通交流强行变成正式方案或评审流程。涉及代码时先依据工作区事实判断，"
    "不确定的内容要明确说明。"
)
DEFAULT_GROUP_CHAT_AGENT_B_IDENTITY = (
    "你是群聊中的 Codex，一名偏重代码实现、工程细节和验证结果的协作伙伴。"
    "直接回应用户当前的问题，并结合群聊历史给出可执行的建议。若 Claude 已经回答，"
    "不要机械重复；优先补充代码事实、边界情况、风险和验证方法，也可以明确提出不同意见。"
    "表达自然、清晰、简洁，不要把普通交流强行变成正式方案或评审流程。涉及代码时以实际"
    "工作区内容为依据，不确定的内容要明确说明。"
)

COLLABORATION_MODES = {"workflow", "group_chat"}

EVENT_PROTOCOL = "multiagent.event.v2"

_EVENT_DEFAULT_STATUSES = {
    "phase": "in_progress",
    "lifecycle": "in_progress",
    "progress": "working",
    "tool": "working",
    "tool_result": "working",
    "text": "completed",
    "metric": "completed",
    "checkpoint": "completed",
    "collaboration": "updated",
    "verification": "working",
    "verification_result": "completed",
    "warning": "warning",
    "error": "failed",
    "log": "working",
}

_SAFE_TEXT_KINDS = {
    "phase",
    "lifecycle",
    "checkpoint",
    "verification_result",
    "warning",
}

_SAFE_METADATA_KEYS = {
    "attempt",
    "check",
    "duration_seconds",
    "exit_code",
    "input_tokens",
    "output_tokens",
    "passed",
    "phase",
    "timed_out",
    "timeout_seconds",
}


class BridgeError(RuntimeError):
    """A readable failure raised by a native CLI or the bridge."""


class AgentTimeoutError(BridgeError):
    """One model attempt exceeded its configured response timeout."""


class BridgeCancelled(BridgeError):
    """The user cancelled before implementation began."""


class ConsensusLimitReached(BridgeError):
    """The plan did not reach unanimous approval within the configured rounds."""


@dataclass(frozen=True)
class AgentCommandSettings:
    """How to launch one native coding-agent CLI."""

    command: tuple[str, ...]
    model: str | None = None
    models: tuple[str, ...] = ()
    fallback_on_timeout: bool = True
    extra_args: tuple[str, ...] = ()
    timeout: float = 900


@dataclass(frozen=True)
class BridgeSettings:
    """Resolved settings for a Claude/Codex bridge session."""

    workspace: Path
    executor: str
    review_rounds: int
    planning_collaboration: bool
    consensus: bool
    max_consensus_rounds: int
    plan_approval: bool
    max_plan_revisions: int
    final_review: bool
    verification_commands: tuple["VerificationCommand", ...]
    claude: AgentCommandSettings
    codex: AgentCommandSettings
    config_path: Path | None = None
    agent_a_identity: str = DEFAULT_AGENT_A_IDENTITY
    agent_b_identity: str = DEFAULT_AGENT_B_IDENTITY
    group_chat_agent_a_identity: str = DEFAULT_GROUP_CHAT_AGENT_A_IDENTITY
    group_chat_agent_b_identity: str = DEFAULT_GROUP_CHAT_AGENT_B_IDENTITY
    collaboration_mode: str = "workflow"
    group_chat_default_agent: str = "both"
    group_chat_execution: bool = True
    token_api: TokenAPISettings = field(default_factory=TokenAPISettings)


@dataclass(frozen=True)
class AgentEvent:
    """Versioned event emitted by native CLIs and the bridge.

    The first three fields intentionally retain the v1 positional API.  v2 adds
    stable workflow context and both wall-clock and relative timing without
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

    def to_dict(self, *, safe: bool = False) -> dict[str, Any]:
        """Serialize the event, optionally replacing sensitive details."""

        text = self.text
        if safe and self.kind not in _SAFE_TEXT_KINDS:
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
        return {
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


@dataclass(frozen=True)
class PlanDecision:
    """User decision at the pre-implementation plan gate."""

    action: str
    feedback: str = ""
    target_agent: str = ""


@dataclass(frozen=True)
class VerificationCommand:
    """One deterministic command run by the bridge, not by an agent."""

    name: str
    command: tuple[str, ...]
    timeout: float = 300


@dataclass(frozen=True)
class VerificationResult:
    """Captured result of one deterministic verification command."""

    name: str
    command: tuple[str, ...]
    exit_code: int | None
    output: str
    duration_seconds: float
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return not self.timed_out and self.exit_code == 0


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """Git state captured before or after an agent workflow."""

    is_git_repo: bool
    branch: str = ""
    head: str = ""
    status: str = ""
    diff: str = ""

    @property
    def is_dirty(self) -> bool:
        return bool(self.status.strip())


@dataclass(frozen=True)
class ReviewFinding:
    severity: str
    file: str
    line: int | None
    requirement: str
    problem: str
    evidence: str
    suggestion: str


@dataclass(frozen=True)
class ReviewDecision:
    verdict: str
    findings: tuple[ReviewFinding, ...] = ()
    requirements_covered: tuple[str, ...] = ()
    structured: bool = True


@dataclass(frozen=True)
class BridgeOutcome:
    """Result of the complete implement-review-revise workflow."""

    task: str
    executor: str
    execution_result: AgentRunResult
    reviews: tuple[AgentRunResult, ...] = field(default_factory=tuple)
    review_decisions: tuple[ReviewDecision, ...] = field(default_factory=tuple)
    verifications: tuple[VerificationResult, ...] = field(default_factory=tuple)
    baseline: WorkspaceSnapshot | None = None
    final_snapshot: WorkspaceSnapshot | None = None
    approved: bool | None = None
    collaboration: "CollaborationState | None" = None
    agent_proposals: tuple[AgentRunResult, ...] = field(default_factory=tuple)
    cross_reviews: tuple[AgentRunResult, ...] = field(default_factory=tuple)
    unified_proposal: AgentRunResult | None = None
    consensus_reviews: tuple[AgentRunResult, ...] = field(default_factory=tuple)


def _event_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
