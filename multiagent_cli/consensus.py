from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


CONSENSUS_PROTOCOL = "mutiagent.consensus.v1"
EVIDENCE_CONSENSUS_PROTOCOL = "mutiagent.consensus.v2"
SUPPORTED_CONSENSUS_PROTOCOLS = (EVIDENCE_CONSENSUS_PROTOCOL, CONSENSUS_PROTOCOL)
CONSENSUS_CRITERIA = (
    "requirements",
    "architecture",
    "failure_paths",
    "compatibility",
    "testing",
)


@dataclass(frozen=True)
class ConsensusDecision:
    verdict: str
    protocol: str = CONSENSUS_PROTOCOL
    criteria: dict[str, bool] = field(default_factory=dict)
    agreements: tuple[str, ...] = ()
    remaining_disagreements: tuple[str, ...] = ()
    required_revisions: tuple[str, ...] = ()
    proposal_version: int = 1
    requirements: tuple["ConsensusRequirement", ...] = ()
    issues: tuple["ConsensusIssue", ...] = ()
    valid: bool = True
    structured: bool = True

    @property
    def accepted(self) -> bool:
        if self.verdict != "accept" or self.remaining_disagreements:
            return False
        if self.structured:
            if not all(
                self.criteria.get(name) is True for name in CONSENSUS_CRITERIA
            ):
                return False
            if self.protocol == EVIDENCE_CONSENSUS_PROTOCOL:
                if not self.requirements:
                    return False
                if any(
                    not requirement.covered or not requirement.evidence
                    for requirement in self.requirements
                ):
                    return False
                if any(
                    issue.severity in {"P0", "P1"}
                    and (issue.status != "resolved" or not issue.evidence)
                    for issue in self.issues
                ):
                    return False
                if self.required_revisions:
                    return False
            return True
        return True


@dataclass(frozen=True)
class ConsensusRequirement:
    id: str
    text: str
    covered: bool
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConsensusIssue:
    id: str
    severity: str
    requirement: str
    problem: str
    status: str
    resolution: str = ""
    evidence: tuple[str, ...] = ()


def parse_consensus_decision(text: str) -> ConsensusDecision:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return _parse_legacy_verdict(cleaned)
    if not isinstance(data, dict) or data.get("protocol") not in SUPPORTED_CONSENSUS_PROTOCOLS:
        return ConsensusDecision(verdict="revise", valid=False)

    protocol = str(data["protocol"])

    verdict = str(data.get("verdict", "")).strip().lower()
    criteria_raw = data.get("criteria")
    if verdict not in {"accept", "revise"} or not isinstance(criteria_raw, dict):
        return ConsensusDecision(protocol=protocol, verdict="revise", valid=False)
    criteria = {
        name: criteria_raw.get(name)
        for name in CONSENSUS_CRITERIA
        if isinstance(criteria_raw.get(name), bool)
    }
    if len(criteria) != len(CONSENSUS_CRITERIA):
        return ConsensusDecision(
            protocol=protocol, verdict="revise", criteria=criteria, valid=False
        )

    agreements = _string_tuple(data.get("agreements"))
    disagreements = _string_tuple(data.get("remaining_disagreements"))
    revisions = _string_tuple(data.get("required_revisions"))
    if agreements is None or disagreements is None or revisions is None:
        return ConsensusDecision(
            protocol=protocol, verdict="revise", criteria=criteria, valid=False
        )

    proposal_version = data.get("proposal_version", 1)
    requirements: tuple[ConsensusRequirement, ...] = ()
    issues: tuple[ConsensusIssue, ...] = ()
    if protocol == EVIDENCE_CONSENSUS_PROTOCOL:
        if (
            isinstance(proposal_version, bool)
            or not isinstance(proposal_version, int)
            or proposal_version < 1
        ):
            return ConsensusDecision(
                protocol=protocol, verdict="revise", criteria=criteria, valid=False
            )
        parsed_requirements = _parse_requirements(data.get("requirements"))
        parsed_issues = _parse_issues(data.get("issues"))
        if parsed_requirements is None or parsed_issues is None:
            return ConsensusDecision(
                protocol=protocol, verdict="revise", criteria=criteria, valid=False
            )
        requirements = parsed_requirements
        issues = parsed_issues
    return ConsensusDecision(
        protocol=protocol,
        verdict=verdict,
        criteria=criteria,
        agreements=agreements,
        remaining_disagreements=disagreements,
        required_revisions=revisions,
        proposal_version=proposal_version,
        requirements=requirements,
        issues=issues,
    )


def _parse_legacy_verdict(text: str) -> ConsensusDecision:
    match = re.search(
        r"^\s*SOLUTION_VERDICT\s*:\s*(ACCEPT|REVISE)\b",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if not match:
        return ConsensusDecision(
            protocol="legacy", verdict="revise", valid=False, structured=False
        )
    return ConsensusDecision(
        protocol="legacy", verdict=match.group(1).lower(), structured=False
    )


def _string_tuple(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return tuple(item.strip() for item in value if item.strip())


def _parse_requirements(
    value: object,
) -> tuple[ConsensusRequirement, ...] | None:
    if not isinstance(value, list) or not value:
        return None
    requirements: list[ConsensusRequirement] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            return None
        requirement_id = _nonempty(item.get("id"))
        text = _nonempty(item.get("text"))
        covered = item.get("covered")
        evidence = _string_tuple(item.get("evidence"))
        if (
            requirement_id is None
            or requirement_id in seen
            or text is None
            or not isinstance(covered, bool)
            or evidence is None
        ):
            return None
        seen.add(requirement_id)
        requirements.append(
            ConsensusRequirement(requirement_id, text, covered, evidence)
        )
    return tuple(requirements)


def _parse_issues(value: object) -> tuple[ConsensusIssue, ...] | None:
    if not isinstance(value, list):
        return None
    issues: list[ConsensusIssue] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            return None
        issue_id = _nonempty(item.get("id"))
        severity = _nonempty(item.get("severity"))
        requirement = _nonempty(item.get("requirement"))
        problem = _nonempty(item.get("problem"))
        status = _nonempty(item.get("status"))
        resolution_raw = item.get("resolution", "")
        evidence = _string_tuple(item.get("evidence"))
        if (
            issue_id is None
            or issue_id in seen
            or severity not in {"P0", "P1", "P2", "P3"}
            or requirement is None
            or problem is None
            or status not in {"open", "resolved", "wont_fix"}
            or not isinstance(resolution_raw, str)
            or evidence is None
        ):
            return None
        seen.add(issue_id)
        issues.append(
            ConsensusIssue(
                id=issue_id,
                severity=severity,
                requirement=requirement,
                problem=problem,
                status=status,
                resolution=resolution_raw.strip(),
                evidence=evidence,
            )
        )
    return tuple(issues)


def _nonempty(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()
