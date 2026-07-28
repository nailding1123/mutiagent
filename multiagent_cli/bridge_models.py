from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .collaboration import CollaborationState


DEFAULT_LEAD_IDENTITY = (
    "你是本任务的主 Agent，负责准确理解需求、提出可实施方案、在获准后修改代码，"
    "并逐项回应副 Agent 的有效审查意见。保留用户已有改动，不擅自提交 Git。"
)
DEFAULT_REVIEWER_IDENTITY = (
    "你是本任务的独立副 Agent，负责独立理解需求、提出建议方案，并以只读方式审查"
    "主方案和实现。所有结论必须基于当前代码、需求和验证证据。"
)


class BridgeError(RuntimeError):
    """A readable failure raised by a native CLI or the bridge."""


class BridgeCancelled(BridgeError):
    """The user cancelled before implementation began."""


@dataclass(frozen=True)
class AgentCommandSettings:
    """How to launch one native coding-agent CLI."""

    command: tuple[str, ...]
    model: str | None = None
    extra_args: tuple[str, ...] = ()
    timeout: float = 900


@dataclass(frozen=True)
class BridgeSettings:
    """Resolved settings for a Claude/Codex bridge session."""

    workspace: Path
    lead: str
    review_rounds: int
    requirement_review: bool
    consensus: bool
    max_consensus_rounds: int
    plan_approval: bool
    max_plan_revisions: int
    final_review: bool
    verification_commands: tuple["VerificationCommand", ...]
    claude: AgentCommandSettings
    codex: AgentCommandSettings
    config_path: Path | None = None
    lead_identity: str = DEFAULT_LEAD_IDENTITY
    reviewer_identity: str = DEFAULT_REVIEWER_IDENTITY
    worktree: bool = False


@dataclass(frozen=True)
class AgentEvent:
    """Normalized event emitted by either native CLI."""

    source: str
    kind: str
    text: str


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
    lead: str
    lead_result: AgentRunResult
    proposal: AgentRunResult | None = None
    requirement_analysis: AgentRunResult | None = None
    proposal_review: AgentRunResult | None = None
    reviews: tuple[AgentRunResult, ...] = field(default_factory=tuple)
    review_decisions: tuple[ReviewDecision, ...] = field(default_factory=tuple)
    verifications: tuple[VerificationResult, ...] = field(default_factory=tuple)
    baseline: WorkspaceSnapshot | None = None
    final_snapshot: WorkspaceSnapshot | None = None
    approved: bool | None = None
    collaboration: "CollaborationState | None" = None
