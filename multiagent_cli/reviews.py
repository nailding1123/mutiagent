from __future__ import annotations

import json
from typing import Any

from .bridge_models import ReviewDecision, ReviewFinding


VALID_VERDICTS = {"approve", "request_changes"}
VALID_SEVERITIES = {"P0", "P1", "P2", "P3"}


def parse_review_decision(text: str) -> ReviewDecision:
    try:
        data = _extract_json_object(text)
        verdict = data.get("verdict")
        if verdict not in VALID_VERDICTS:
            raise ValueError("verdict 必须是 approve 或 request_changes")
        findings_raw = data.get("findings", [])
        if not isinstance(findings_raw, list):
            raise ValueError("findings 必须是数组")
        findings = tuple(_parse_finding(item) for item in findings_raw)
        covered_raw = data.get("requirements_covered", [])
        if not isinstance(covered_raw, list) or not all(
            isinstance(value, str) for value in covered_raw
        ):
            raise ValueError("requirements_covered 必须是字符串数组")
        if any(finding.severity in {"P0", "P1"} for finding in findings):
            verdict = "request_changes"
        return ReviewDecision(
            verdict=verdict,
            findings=findings,
            requirements_covered=tuple(covered_raw),
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        finding = ReviewFinding(
            severity="P1",
            file="",
            line=None,
            requirement="结构化审查协议",
            problem="验收 Agent 没有返回有效的结构化审查结果",
            evidence=str(exc),
            suggestion="按约定 JSON Schema 重新执行审查",
        )
        return ReviewDecision(
            verdict="request_changes",
            findings=(finding,),
            structured=False,
        )


def format_review_for_revision(decision: ReviewDecision, raw_text: str) -> str:
    if decision.structured:
        return raw_text
    finding = decision.findings[0]
    return (
        f"验收 Agent 输出格式无效：{finding.evidence}\n"
        "请独立检查当前实现是否满足需求，并特别关注测试和边界条件。\n"
        f"原始输出：\n{raw_text}"
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("未找到 JSON 对象")
    data = json.loads(stripped[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("审查结果顶层必须是对象")
    return data


def _parse_finding(raw: Any) -> ReviewFinding:
    if not isinstance(raw, dict):
        raise ValueError("finding 必须是对象")
    severity = raw.get("severity", "P2")
    if severity not in VALID_SEVERITIES:
        raise ValueError(f"无效 severity：{severity}")
    line = raw.get("line")
    if line is not None and (isinstance(line, bool) or not isinstance(line, int)):
        raise ValueError("finding.line 必须是整数或 null")
    return ReviewFinding(
        severity=severity,
        file=_string(raw, "file"),
        line=line,
        requirement=_string(raw, "requirement"),
        problem=_string(raw, "problem", required=True),
        evidence=_string(raw, "evidence"),
        suggestion=_string(raw, "suggestion"),
    )


def _string(raw: dict[str, Any], key: str, *, required: bool = False) -> str:
    value = raw.get(key, "")
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"finding.{key} 必须是{'非空' if required else ''}字符串")
    return value.strip()
