from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bridge_models import (
    AgentRunResult,
    ReviewDecision,
    ReviewFinding,
    VerificationResult,
    WorkspaceSnapshot,
)
from .collaboration import CollaborationState


CHECKPOINT_SCHEMA_VERSION = 1


@dataclass
class WorkflowCheckpoint:
    """Durable workflow state written after every externally visible step."""

    task: str
    workspace: str
    lead: str
    phase: str = "initialized"
    baseline: WorkspaceSnapshot | None = None
    artifacts: dict[str, AgentRunResult] = field(default_factory=dict)
    consensus_revisions: int = 0
    plan_revisions: int = 0
    plan_approved: bool = False
    implementation_complete: bool = False
    review_cursor: int = 0
    reviews: list[AgentRunResult] = field(default_factory=list)
    review_decisions: list[ReviewDecision] = field(default_factory=list)
    verifications: list[VerificationResult] = field(default_factory=list)
    pending_verification: list[VerificationResult] = field(default_factory=list)
    final_review_complete: bool = False
    approved: bool | None = None
    collaboration: CollaborationState = field(default_factory=CollaborationState)
    workspace_fingerprint: str = ""
    updated_at: str = field(default_factory=lambda: _timestamp())

    def artifact(self, name: str) -> AgentRunResult | None:
        return self.artifacts.get(name)

    def set_artifact(self, name: str, result: AgentRunResult) -> None:
        self.artifacts[name] = result

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "task": self.task,
            "workspace": self.workspace,
            "lead": self.lead,
            "phase": self.phase,
            "baseline": _snapshot_to_dict(self.baseline),
            "artifacts": {
                name: _agent_result_to_dict(result)
                for name, result in self.artifacts.items()
            },
            "consensus_revisions": self.consensus_revisions,
            "plan_revisions": self.plan_revisions,
            "plan_approved": self.plan_approved,
            "implementation_complete": self.implementation_complete,
            "review_cursor": self.review_cursor,
            "reviews": [_agent_result_to_dict(result) for result in self.reviews],
            "review_decisions": [
                _review_decision_to_dict(decision)
                for decision in self.review_decisions
            ],
            "verifications": [
                _verification_to_dict(result) for result in self.verifications
            ],
            "pending_verification": [
                _verification_to_dict(result) for result in self.pending_verification
            ],
            "final_review_complete": self.final_review_complete,
            "approved": self.approved,
            "collaboration": self.collaboration.to_dict(),
            "workspace_fingerprint": self.workspace_fingerprint,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(
        cls,
        data: object,
        *,
        expected_task: str | None = None,
        expected_workspace: Path | None = None,
        expected_lead: str | None = None,
    ) -> "WorkflowCheckpoint | None":
        if not isinstance(data, dict) or data.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            return None
        task = _text(data.get("task"))
        workspace = _text(data.get("workspace"))
        lead = _text(data.get("lead"))
        if not task or not workspace or lead not in {"claude", "codex"}:
            return None
        if expected_task is not None and task != expected_task.strip():
            return None
        if expected_workspace is not None and Path(workspace).resolve() != expected_workspace.resolve():
            return None
        if expected_lead is not None and lead != expected_lead:
            return None

        collaboration = CollaborationState.from_dict(data.get("collaboration"))
        if collaboration is None:
            return None
        baseline = _snapshot_from_dict(data.get("baseline"))
        artifacts_raw = data.get("artifacts", {})
        reviews_raw = data.get("reviews", [])
        decisions_raw = data.get("review_decisions", [])
        verifications_raw = data.get("verifications", [])
        pending_raw = data.get("pending_verification", [])
        if (
            not isinstance(artifacts_raw, dict)
            or not isinstance(reviews_raw, list)
            or not isinstance(decisions_raw, list)
            or not isinstance(verifications_raw, list)
            or not isinstance(pending_raw, list)
        ):
            return None

        artifacts: dict[str, AgentRunResult] = {}
        for name, raw in artifacts_raw.items():
            if not isinstance(name, str):
                return None
            result = _agent_result_from_dict(raw)
            if result is None:
                return None
            artifacts[name] = result
        reviews = [_agent_result_from_dict(raw) for raw in reviews_raw]
        decisions = [_review_decision_from_dict(raw) for raw in decisions_raw]
        verifications = [_verification_from_dict(raw) for raw in verifications_raw]
        pending = [_verification_from_dict(raw) for raw in pending_raw]
        if any(result is None for result in reviews + decisions + verifications + pending):
            return None

        approved = data.get("approved")
        if approved is not None and not isinstance(approved, bool):
            return None
        return cls(
            task=task,
            workspace=workspace,
            lead=lead,
            phase=_text(data.get("phase")) or "initialized",
            baseline=baseline,
            artifacts=artifacts,
            consensus_revisions=_integer(data.get("consensus_revisions")),
            plan_revisions=_integer(data.get("plan_revisions")),
            plan_approved=data.get("plan_approved") is True,
            implementation_complete=data.get("implementation_complete") is True,
            review_cursor=_integer(data.get("review_cursor")),
            reviews=[result for result in reviews if isinstance(result, AgentRunResult)],
            review_decisions=[
                result for result in decisions if isinstance(result, ReviewDecision)
            ],
            verifications=[
                result for result in verifications if isinstance(result, VerificationResult)
            ],
            pending_verification=[
                result for result in pending if isinstance(result, VerificationResult)
            ],
            final_review_complete=data.get("final_review_complete") is True,
            approved=approved,
            collaboration=collaboration,
            workspace_fingerprint=_text(data.get("workspace_fingerprint")),
            updated_at=_text(data.get("updated_at")) or _timestamp(),
        )


def _agent_result_to_dict(result: AgentRunResult) -> dict[str, Any]:
    return {
        "agent": result.agent,
        "final_text": result.final_text,
        "session_id": result.session_id,
        "exit_code": result.exit_code,
        "duration_seconds": result.duration_seconds,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
    }


def _agent_result_from_dict(data: object) -> AgentRunResult | None:
    if not isinstance(data, dict):
        return None
    agent = _text(data.get("agent"))
    final_text = _text(data.get("final_text"))
    session_id = data.get("session_id")
    if not agent or not final_text or (session_id is not None and not isinstance(session_id, str)):
        return None
    return AgentRunResult(
        agent=agent,
        final_text=final_text,
        session_id=session_id,
        exit_code=_integer(data.get("exit_code")),
        duration_seconds=_number(data.get("duration_seconds")),
        input_tokens=_integer(data.get("input_tokens")),
        output_tokens=_integer(data.get("output_tokens")),
    )


def _snapshot_to_dict(snapshot: WorkspaceSnapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return {
        "is_git_repo": snapshot.is_git_repo,
        "branch": snapshot.branch,
        "head": snapshot.head,
        "status": snapshot.status,
        "diff": snapshot.diff,
    }


def _snapshot_from_dict(data: object) -> WorkspaceSnapshot | None:
    if data is None:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("is_git_repo"), bool):
        return None
    return WorkspaceSnapshot(
        is_git_repo=data["is_git_repo"],
        branch=_text(data.get("branch")),
        head=_text(data.get("head")),
        status=_text(data.get("status")),
        diff=_text(data.get("diff")),
    )


def _review_decision_to_dict(decision: ReviewDecision) -> dict[str, Any]:
    return {
        "verdict": decision.verdict,
        "requirements_covered": list(decision.requirements_covered),
        "structured": decision.structured,
        "findings": [
            {
                "severity": finding.severity,
                "file": finding.file,
                "line": finding.line,
                "requirement": finding.requirement,
                "problem": finding.problem,
                "evidence": finding.evidence,
                "suggestion": finding.suggestion,
            }
            for finding in decision.findings
        ],
    }


def _review_decision_from_dict(data: object) -> ReviewDecision | None:
    if not isinstance(data, dict):
        return None
    verdict = _text(data.get("verdict"))
    covered = data.get("requirements_covered", [])
    findings_raw = data.get("findings", [])
    if verdict not in {"approve", "request_changes"} or not isinstance(covered, list) or not isinstance(findings_raw, list):
        return None
    if not all(isinstance(item, str) for item in covered):
        return None
    findings: list[ReviewFinding] = []
    for raw in findings_raw:
        if not isinstance(raw, dict):
            return None
        line = raw.get("line")
        if line is not None and (isinstance(line, bool) or not isinstance(line, int)):
            return None
        findings.append(
            ReviewFinding(
                severity=_text(raw.get("severity")),
                file=_text(raw.get("file")),
                line=line,
                requirement=_text(raw.get("requirement")),
                problem=_text(raw.get("problem")),
                evidence=_text(raw.get("evidence")),
                suggestion=_text(raw.get("suggestion")),
            )
        )
    return ReviewDecision(
        verdict=verdict,
        findings=tuple(findings),
        requirements_covered=tuple(item.strip() for item in covered if item.strip()),
        structured=data.get("structured") is not False,
    )


def _verification_to_dict(result: VerificationResult) -> dict[str, Any]:
    return {
        "name": result.name,
        "command": list(result.command),
        "exit_code": result.exit_code,
        "output": result.output,
        "duration_seconds": result.duration_seconds,
        "timed_out": result.timed_out,
    }


def _verification_from_dict(data: object) -> VerificationResult | None:
    if not isinstance(data, dict):
        return None
    name = _text(data.get("name"))
    command = data.get("command")
    exit_code = data.get("exit_code")
    if (
        not name
        or not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) for item in command)
        or (exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)))
    ):
        return None
    return VerificationResult(
        name=name,
        command=tuple(command),
        exit_code=exit_code,
        output=_text(data.get("output")),
        duration_seconds=_number(data.get("duration_seconds")),
        timed_out=data.get("timed_out") is True,
    )


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
