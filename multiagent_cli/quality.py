from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class ModeMetrics:
    runs: int = 0
    completed: int = 0
    evaluated: int = 0
    approved: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_seconds: float = 0.0

    @property
    def approval_rate(self) -> float | None:
        return self.approved / self.evaluated if self.evaluated else None

    @property
    def average_tokens(self) -> float:
        return (self.input_tokens + self.output_tokens) / self.runs if self.runs else 0

    @property
    def average_elapsed(self) -> float:
        return self.elapsed_seconds / self.runs if self.runs else 0


@dataclass(frozen=True)
class QualityReport:
    total_runs: int
    completed_runs: int
    evaluated_runs: int
    approved_runs: int
    failed_runs: int
    verification_passed: int
    verification_total: int
    findings: dict[str, int] = field(default_factory=dict)
    modes: dict[str, ModeMetrics] = field(default_factory=dict)

    @property
    def completion_rate(self) -> float | None:
        return self.completed_runs / self.total_runs if self.total_runs else None

    @property
    def approval_rate(self) -> float | None:
        return self.approved_runs / self.evaluated_runs if self.evaluated_runs else None

    @property
    def verification_rate(self) -> float | None:
        return (
            self.verification_passed / self.verification_total
            if self.verification_total
            else None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_runs": self.total_runs,
            "completed_runs": self.completed_runs,
            "evaluated_runs": self.evaluated_runs,
            "approved_runs": self.approved_runs,
            "failed_runs": self.failed_runs,
            "completion_rate": self.completion_rate,
            "approval_rate": self.approval_rate,
            "verification_passed": self.verification_passed,
            "verification_total": self.verification_total,
            "verification_rate": self.verification_rate,
            "findings": dict(self.findings),
            "modes": {
                name: {
                    "runs": metrics.runs,
                    "completed": metrics.completed,
                    "evaluated": metrics.evaluated,
                    "approved": metrics.approved,
                    "approval_rate": metrics.approval_rate,
                    "average_tokens": metrics.average_tokens,
                    "average_elapsed_seconds": metrics.average_elapsed,
                }
                for name, metrics in self.modes.items()
            },
        }


def build_quality_report(records: Iterable[dict[str, Any]]) -> QualityReport:
    total = completed = evaluated = approved = failed = 0
    verification_passed = verification_total = 0
    findings = {severity: 0 for severity in ("P0", "P1", "P2", "P3")}
    accumulators: dict[str, dict[str, float]] = {}

    for record in records:
        total += 1
        status = str(record.get("status", ""))
        is_complete = status == "complete"
        is_evaluated = is_complete and isinstance(record.get("approved"), bool)
        is_approved = is_evaluated and record.get("approved") is True
        completed += int(is_complete)
        evaluated += int(is_evaluated)
        approved += int(is_approved)
        failed += int(status in {"failed", "cancelled", "interrupted"})

        quality = record.get("quality")
        if isinstance(quality, dict):
            verification_passed += _integer(quality.get("verification_passed"))
            verification_total += _integer(quality.get("verification_total"))
            raw_findings = quality.get("findings")
            if isinstance(raw_findings, dict):
                for severity in findings:
                    findings[severity] += _integer(raw_findings.get(severity))

        settings = record.get("settings")
        mode = _mode(settings if isinstance(settings, dict) else {})
        metrics = accumulators.setdefault(
            mode,
            {
                "runs": 0,
                "completed": 0,
                "evaluated": 0,
                "approved": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "elapsed_seconds": 0.0,
            },
        )
        metrics["runs"] += 1
        metrics["completed"] += int(is_complete)
        metrics["evaluated"] += int(is_evaluated)
        metrics["approved"] += int(is_approved)
        summary = record.get("summary")
        if isinstance(summary, dict):
            metrics["input_tokens"] += _integer(summary.get("input_tokens"))
            metrics["output_tokens"] += _integer(summary.get("output_tokens"))
            metrics["elapsed_seconds"] += _number(summary.get("elapsed_seconds"))

    modes = {
        name: ModeMetrics(
            runs=int(values["runs"]),
            completed=int(values["completed"]),
            evaluated=int(values["evaluated"]),
            approved=int(values["approved"]),
            input_tokens=int(values["input_tokens"]),
            output_tokens=int(values["output_tokens"]),
            elapsed_seconds=float(values["elapsed_seconds"]),
        )
        for name, values in accumulators.items()
    }
    return QualityReport(
        total_runs=total,
        completed_runs=completed,
        evaluated_runs=evaluated,
        approved_runs=approved,
        failed_runs=failed,
        verification_passed=verification_passed,
        verification_total=verification_total,
        findings=findings,
        modes=modes,
    )


def _mode(settings: dict[str, Any]) -> str:
    if settings.get("consensus") is True:
        return "consensus"
    review_rounds = settings.get("review_rounds")
    planning_collaboration = settings.get("planning_collaboration") is True
    if planning_collaboration or (
        isinstance(review_rounds, int)
        and not isinstance(review_rounds, bool)
        and review_rounds > 0
    ):
        return "review"
    return "solo"


def _integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
