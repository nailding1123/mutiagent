from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from typing import Any

from .bridge_models import ReviewDecision
from .consensus import ConsensusDecision


TASK_STATUSES = {"pending", "in_progress", "blocked", "done", "failed", "skipped"}
MESSAGE_KINDS = {"proposal", "analysis", "review", "revision", "evidence", "status"}


@dataclass
class SharedTask:
    id: str
    title: str
    owner: str
    status: str = "pending"
    phase: str = ""
    evidence: list[str] = field(default_factory=list)
    updated_at: str = field(default_factory=lambda: _timestamp())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "owner": self.owner,
            "status": self.status,
            "phase": self.phase,
            "evidence": list(self.evidence),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: object) -> "SharedTask | None":
        if not isinstance(data, dict):
            return None
        task_id = _text(data.get("id"))
        title = _text(data.get("title"))
        owner = _text(data.get("owner"))
        status = _text(data.get("status"))
        if not task_id or not title or not owner or status not in TASK_STATUSES:
            return None
        evidence = _string_list(data.get("evidence", []))
        if evidence is None:
            return None
        return cls(
            id=task_id,
            title=title,
            owner=owner,
            status=status,
            phase=_text(data.get("phase")),
            evidence=evidence,
            updated_at=_text(data.get("updated_at")) or _timestamp(),
        )


@dataclass
class AgentMessage:
    id: str
    sender: str
    recipient: str
    kind: str
    body: str
    related_issue: str = ""
    created_at: str = field(default_factory=lambda: _timestamp())

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "sender": self.sender,
            "recipient": self.recipient,
            "kind": self.kind,
            "body": self.body,
            "related_issue": self.related_issue,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: object) -> "AgentMessage | None":
        if not isinstance(data, dict):
            return None
        message_id = _text(data.get("id"))
        sender = _text(data.get("sender"))
        recipient = _text(data.get("recipient"))
        kind = _text(data.get("kind"))
        body = _text(data.get("body"))
        if (
            not message_id
            or not sender
            or not recipient
            or kind not in MESSAGE_KINDS
            or not body
        ):
            return None
        return cls(
            id=message_id,
            sender=sender,
            recipient=recipient,
            kind=kind,
            body=body,
            related_issue=_text(data.get("related_issue")),
            created_at=_text(data.get("created_at")) or _timestamp(),
        )


@dataclass
class RequirementRecord:
    id: str
    text: str
    covered: bool
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "covered": self.covered,
            "evidence": list(self.evidence),
        }


@dataclass
class DisputeRecord:
    id: str
    severity: str
    requirement: str
    problem: str
    status: str
    resolution: str = ""
    evidence: list[str] = field(default_factory=list)
    first_seen_round: int = 0
    last_seen_round: int = 0

    @property
    def blocking(self) -> bool:
        return self.severity in {"P0", "P1"} and (
            self.status != "resolved" or not self.evidence
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity,
            "requirement": self.requirement,
            "problem": self.problem,
            "status": self.status,
            "resolution": self.resolution,
            "evidence": list(self.evidence),
            "first_seen_round": self.first_seen_round,
            "last_seen_round": self.last_seen_round,
        }


@dataclass
class CollaborationState:
    """Serializable task board, mailbox and evidence ledger for one run."""

    proposal_version: int = 0
    consensus_round: int = 0
    tasks: dict[str, SharedTask] = field(default_factory=dict)
    messages: list[AgentMessage] = field(default_factory=list)
    requirements: dict[str, RequirementRecord] = field(default_factory=dict)
    issues: dict[str, DisputeRecord] = field(default_factory=dict)
    accepted: bool = False

    @classmethod
    def create(cls, *, lead: str, reviewer: str, requirement_review: bool) -> "CollaborationState":
        state = cls()
        if requirement_review:
            state.add_task("plan", "提出只读实施方案", lead, phase="proposal")
            state.add_task(
                "requirements", "独立解析需求与风险", reviewer, phase="requirements"
            )
            state.add_task("plan-review", "比较并审查方案", reviewer, phase="plan_review")
        state.add_task("implementation", "实施确认后的方案", lead, phase="implementation")
        state.add_task("verification", "运行独立验证", "bridge", phase="verification")
        state.add_task("code-review", "验收代码与验证证据", reviewer, phase="review")
        return state

    def add_task(
        self, task_id: str, title: str, owner: str, *, phase: str = ""
    ) -> SharedTask:
        task = SharedTask(task_id, title, owner, phase=phase)
        self.tasks[task_id] = task
        return task

    def set_task(
        self,
        task_id: str,
        status: str,
        *,
        evidence: str | None = None,
    ) -> None:
        if status not in TASK_STATUSES:
            raise ValueError(f"未知任务状态：{status}")
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(f"找不到共享任务：{task_id}")
        task.status = status
        if evidence and evidence not in task.evidence:
            task.evidence.append(evidence)
        task.updated_at = _timestamp()

    def post(
        self,
        sender: str,
        recipient: str,
        kind: str,
        body: str,
        *,
        related_issue: str = "",
    ) -> AgentMessage:
        if kind not in MESSAGE_KINDS:
            raise ValueError(f"未知消息类型：{kind}")
        message = AgentMessage(
            id=f"MSG-{len(self.messages) + 1:04d}",
            sender=sender,
            recipient=recipient,
            kind=kind,
            body=body.strip(),
            related_issue=related_issue,
        )
        self.messages.append(message)
        return message

    def apply_consensus(self, decision: ConsensusDecision, round_index: int) -> None:
        self.consensus_round = round_index
        self.proposal_version = max(self.proposal_version, decision.proposal_version)
        for requirement in decision.requirements:
            self.requirements[requirement.id] = RequirementRecord(
                id=requirement.id,
                text=requirement.text,
                covered=requirement.covered,
                evidence=list(requirement.evidence),
            )
        seen_issues: set[str] = set()
        for issue in decision.issues:
            seen_issues.add(issue.id)
            previous = self.issues.get(issue.id)
            self.issues[issue.id] = DisputeRecord(
                id=issue.id,
                severity=issue.severity,
                requirement=issue.requirement,
                problem=issue.problem,
                status=issue.status,
                resolution=issue.resolution,
                evidence=list(issue.evidence),
                first_seen_round=(
                    previous.first_seen_round if previous else round_index
                ),
                last_seen_round=round_index,
            )
        # An issue omitted in a later structured round is retained as unresolved;
        # this prevents consensus from being reached by silently dropping blockers.
        for issue_id, issue in self.issues.items():
            if issue_id not in seen_issues and issue.status != "resolved":
                issue.last_seen_round = round_index
        self.accepted = decision.accepted and not self.blocking_issues

    def apply_code_review(self, decision: ReviewDecision, round_index: int) -> None:
        if decision.verdict == "approve":
            for issue in self.issues.values():
                if issue.id.startswith("CODE-") and issue.status == "open":
                    issue.status = "resolved"
                    issue.resolution = "后续结构化代码验收已通过"
                    if "reviewer approval" not in issue.evidence:
                        issue.evidence.append("reviewer approval")
                    issue.last_seen_round = round_index
            return
        for finding in decision.findings:
            identity = "\0".join(
                (
                    finding.severity,
                    finding.file,
                    str(finding.line or ""),
                    finding.requirement,
                    finding.problem,
                )
            )
            issue_id = "CODE-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8].upper()
            previous = self.issues.get(issue_id)
            self.issues[issue_id] = DisputeRecord(
                id=issue_id,
                severity=finding.severity,
                requirement=finding.requirement or "代码验收",
                problem=finding.problem,
                status="open",
                resolution=finding.suggestion,
                evidence=[finding.evidence] if finding.evidence else [],
                first_seen_round=previous.first_seen_round if previous else round_index,
                last_seen_round=round_index,
            )

    @property
    def blocking_issues(self) -> tuple[DisputeRecord, ...]:
        return tuple(issue for issue in self.issues.values() if issue.blocking)

    def shared_context(self, *, message_limit: int = 8) -> str:
        lines = [
            f"proposal_version: {self.proposal_version}",
            f"consensus_round: {self.consensus_round}",
            "tasks:",
        ]
        for task in self.tasks.values():
            lines.append(f"- {task.id} [{task.status}] owner={task.owner}: {task.title}")
        lines.append("requirements:")
        for requirement in self.requirements.values():
            evidence = "; ".join(requirement.evidence) or "none"
            lines.append(
                f"- {requirement.id} covered={str(requirement.covered).lower()}: "
                f"{requirement.text} | evidence={evidence}"
            )
        lines.append("issues:")
        for issue in self.issues.values():
            evidence = "; ".join(issue.evidence) or "none"
            lines.append(
                f"- {issue.id} [{issue.severity}/{issue.status}] {issue.problem} "
                f"| resolution={issue.resolution or 'none'} | evidence={evidence}"
            )
        lines.append("recent_messages:")
        for message in self.messages[-message_limit:]:
            body = " ".join(message.body.split())
            if len(body) > 500:
                body = f"{body[:497]}..."
            lines.append(
                f"- {message.id} {message.sender}->{message.recipient} "
                f"[{message.kind}]: {body}"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "proposal_version": self.proposal_version,
            "consensus_round": self.consensus_round,
            "accepted": self.accepted,
            "tasks": [task.to_dict() for task in self.tasks.values()],
            "messages": [message.to_dict() for message in self.messages],
            "requirements": [
                requirement.to_dict() for requirement in self.requirements.values()
            ],
            "issues": [issue.to_dict() for issue in self.issues.values()],
        }

    @classmethod
    def from_dict(cls, data: object) -> "CollaborationState | None":
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            return None
        state = cls(
            proposal_version=_integer(data.get("proposal_version")),
            consensus_round=_integer(data.get("consensus_round")),
            accepted=data.get("accepted") is True,
        )
        tasks = data.get("tasks", [])
        messages = data.get("messages", [])
        requirements = data.get("requirements", [])
        issues = data.get("issues", [])
        if not all(isinstance(value, list) for value in (tasks, messages, requirements, issues)):
            return None
        for raw in tasks:
            task = SharedTask.from_dict(raw)
            if task is None:
                return None
            state.tasks[task.id] = task
        for raw in messages:
            message = AgentMessage.from_dict(raw)
            if message is None:
                return None
            state.messages.append(message)
        for raw in requirements:
            if not isinstance(raw, dict):
                return None
            requirement_id = _text(raw.get("id"))
            text = _text(raw.get("text"))
            covered = raw.get("covered")
            evidence = _string_list(raw.get("evidence", []))
            if not requirement_id or not text or not isinstance(covered, bool) or evidence is None:
                return None
            state.requirements[requirement_id] = RequirementRecord(
                requirement_id, text, covered, evidence
            )
        for raw in issues:
            if not isinstance(raw, dict):
                return None
            issue_id = _text(raw.get("id"))
            severity = _text(raw.get("severity"))
            requirement = _text(raw.get("requirement"))
            problem = _text(raw.get("problem"))
            status = _text(raw.get("status"))
            evidence = _string_list(raw.get("evidence", []))
            if (
                not issue_id
                or severity not in {"P0", "P1", "P2", "P3"}
                or not requirement
                or not problem
                or status not in {"open", "resolved", "wont_fix"}
                or evidence is None
            ):
                return None
            state.issues[issue_id] = DisputeRecord(
                id=issue_id,
                severity=severity,
                requirement=requirement,
                problem=problem,
                status=status,
                resolution=_text(raw.get("resolution")),
                evidence=evidence,
                first_seen_round=_integer(raw.get("first_seen_round")),
                last_seen_round=_integer(raw.get("last_seen_round")),
            )
        return state


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string_list(value: object) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return [item.strip() for item in value if item.strip()]


def _integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
