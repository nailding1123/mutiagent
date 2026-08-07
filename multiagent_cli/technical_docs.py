from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .bridge_models import AgentRunResult
from .checkpoints import WorkflowCheckpoint
from .consensus import CONSENSUS_CRITERIA, ConsensusDecision, parse_consensus_decision


CRITERIA_LABELS = {
    "requirements": "需求覆盖",
    "architecture": "架构合理性",
    "failure_paths": "失败与边界路径",
    "compatibility": "兼容性",
    "testing": "测试与验收",
}


def export_technical_document(
    *,
    workspace: Path,
    run_id: str | None,
    checkpoint: WorkflowCheckpoint,
    max_consensus_rounds: int,
    consensus_limit_reached: bool = False,
) -> Path:
    """Export the latest durable planning state as a readable Markdown file."""

    target_directory = workspace / "multiagent-docs"
    target_directory.mkdir(parents=True, exist_ok=True)
    identifier = _safe_identifier(run_id) or datetime.now().strftime("%Y%m%d-%H%M%S")
    target = target_directory / f"{identifier}-technical-plan.md"
    target.write_text(
        render_technical_document(
            checkpoint,
            max_consensus_rounds=max_consensus_rounds,
            consensus_limit_reached=consensus_limit_reached,
        ),
        encoding="utf-8",
    )
    return target


def render_technical_document(
    checkpoint: WorkflowCheckpoint,
    *,
    max_consensus_rounds: int,
    consensus_limit_reached: bool = False,
) -> str:
    collaboration = checkpoint.collaboration
    proposal_a = checkpoint.artifact("proposal_a")
    proposal_b = checkpoint.artifact("proposal_b")
    cross_review_a = checkpoint.artifact("cross_review_a")
    cross_review_b = checkpoint.artifact("cross_review_b")
    unified = checkpoint.artifact("unified_proposal")
    consensus_reviews = _consensus_reviews(checkpoint)
    latest_review = consensus_reviews[-1] if consensus_reviews else None
    decision = (
        parse_consensus_decision(latest_review.final_text)
        if latest_review is not None
        else None
    )

    status, reason = _consensus_status(
        checkpoint,
        decision,
        consensus_limit_reached=consensus_limit_reached,
        max_consensus_rounds=max_consensus_rounds,
    )
    generated = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    approvals = "、".join(sorted(collaboration.approvals)) or "无"
    digest = collaboration.proposal_digest[:12] or "未记录"
    lines = [
        "# MultiAgent 最终技术方案",
        "",
        "> 本文档由 MultiAgent 根据双方独立方案、交叉审核和统一方案自动生成。",
        "",
        "## 文档信息",
        "",
        f"- 任务：{checkpoint.task}",
        f"- 工作区：`{checkpoint.workspace}`",
        f"- 生成时间：{generated}",
        f"- 当前阶段：`{checkpoint.phase}`",
        f"- 共识状态：**{status}**",
        f"- 状态原因：{reason}",
        f"- 统一方案版本：v{collaboration.proposal_version or 1}",
        f"- 方案摘要：`{digest}`",
        f"- 已记录批准：{approvals}",
        "",
        "## 统一技术方案",
        "",
        _result_text(unified, "尚未形成统一方案。"),
        "",
        "## 共识结论",
        "",
    ]
    lines.extend(
        _consensus_details(
            checkpoint,
            decision,
            consensus_limit_reached=consensus_limit_reached,
            max_consensus_rounds=max_consensus_rounds,
        )
    )
    targeted_requests = [
        message
        for message in collaboration.messages
        if message.sender == "user"
        and message.recipient in {"claude", "codex"}
        and message.kind == "instruction"
    ]
    if targeted_requests:
        lines.extend(("", "## 用户定向要求", ""))
        for message in targeted_requests:
            target = (
                "Agent A / Claude"
                if message.recipient == "claude"
                else "Agent B / Codex"
            )
            lines.append(f"- **{target}**：{message.body}")
    lines.extend(
        (
            "",
            "## Agent A 独立方案",
            "",
            _result_text(proposal_a),
            "",
            "## Agent B 独立方案",
            "",
            _result_text(proposal_b),
            "",
            "## 双向交叉审核",
            "",
            "### Agent A 对 Agent B 的审核",
            "",
            _review_text(cross_review_a),
            "",
            "### Agent B 对 Agent A 的审核",
            "",
            _review_text(cross_review_b),
        )
    )
    if consensus_reviews:
        lines.extend(("", "## 统一方案审核记录", ""))
        for index, review in enumerate(consensus_reviews, start=1):
            parsed = parse_consensus_decision(review.final_text)
            verdict = "接受" if parsed.accepted else "要求修订"
            lines.extend(
                (
                    f"### 第 {index} 轮 · {review.agent} · {verdict}",
                    "",
                    _review_text(review),
                    "",
                )
            )
    return "\n".join(lines).rstrip() + "\n"


def _consensus_status(
    checkpoint: WorkflowCheckpoint,
    decision: ConsensusDecision | None,
    *,
    consensus_limit_reached: bool,
    max_consensus_rounds: int,
) -> tuple[str, str]:
    collaboration = checkpoint.collaboration
    if consensus_limit_reached:
        return (
            "未达成共识",
            f"已达到最大共识审核轮次 {max_consensus_rounds}，双方仍未批准同一方案版本。",
        )
    if collaboration.accepted and collaboration.has_unanimous_approval(
        {"claude", "codex"}
    ):
        return "已达成共识", "Claude 与 Codex 已批准相同版本及相同摘要的统一方案。"
    if decision is not None:
        return "尚未达成共识", "最新统一方案审核仍要求修订。"
    return "快速协作完成", "未启用自动共识；统一方案已完成双向交叉审核。"


def _consensus_details(
    checkpoint: WorkflowCheckpoint,
    decision: ConsensusDecision | None,
    *,
    consensus_limit_reached: bool,
    max_consensus_rounds: int,
) -> list[str]:
    collaboration = checkpoint.collaboration
    status, reason = _consensus_status(
        checkpoint,
        decision,
        consensus_limit_reached=consensus_limit_reached,
        max_consensus_rounds=max_consensus_rounds,
    )
    lines = [f"- 结论：**{status}**", f"- 原因：{reason}"]
    if status in {"已达成共识", "快速协作完成"}:
        return lines

    lines.extend(("", "### 未达到共识的内容与原因", ""))
    found = False
    if decision is not None:
        failed_criteria = [
            name
            for name in CONSENSUS_CRITERIA
            if decision.criteria.get(name) is not True
        ]
        if failed_criteria:
            found = True
            lines.append("#### 未通过的审核维度")
            lines.append("")
            for name in failed_criteria:
                lines.append(
                    f"- **{CRITERIA_LABELS.get(name, name)}**：未达到共识。"
                    "原因：最新审核将该维度标记为未通过，具体争议和修订要求见下文。"
                )
            lines.append("")

        incomplete_requirements = [
            requirement
            for requirement in decision.requirements
            if not requirement.covered or not requirement.evidence
        ]
        if incomplete_requirements:
            found = True
            lines.append("#### 未完成或缺少证据的需求")
            lines.append("")
            for requirement in incomplete_requirements:
                cause = (
                    "尚未覆盖"
                    if not requirement.covered
                    else "缺少可验证证据"
                )
                lines.append(
                    f"- `{requirement.id}` {requirement.text}。原因：{cause}。"
                )
            lines.append("")

    unresolved = [
        issue
        for issue in collaboration.issues.values()
        if issue.status != "resolved" or not issue.evidence
    ]
    if unresolved:
        found = True
        lines.append("#### 未解决争议")
        lines.append("")
        for issue in unresolved:
            cause = issue.resolution or "审核记录仍将该事项标记为未解决"
            lines.append(
                f"- `{issue.id}` [{issue.severity}] {issue.problem}。原因：{cause}。"
            )
            if issue.evidence:
                lines.append(f"  - 证据：{'；'.join(issue.evidence)}")
        lines.append("")

    if decision is not None and decision.remaining_disagreements:
        found = True
        lines.append("#### 双方剩余分歧")
        lines.append("")
        lines.extend(f"- {item}" for item in decision.remaining_disagreements)
        lines.append("")

    if decision is not None and decision.required_revisions:
        found = True
        lines.append("#### 尚需完成的修订")
        lines.append("")
        lines.extend(f"- {item}" for item in decision.required_revisions)
        lines.append("")

    if not collaboration.has_unanimous_approval({"claude", "codex"}):
        found = True
        missing = [
            name
            for name in ("claude", "codex")
            if collaboration.approvals.get(name) != collaboration.proposal_digest
        ]
        lines.append("#### 方案批准状态")
        lines.append("")
        lines.append(
            "- 尚未批准当前方案摘要的 Agent："
            + ("、".join(missing) if missing else "批准摘要不一致")
            + "。"
        )
        lines.append("")

    if not found:
        lines.append("- 审核未提供更具体的结构化原因，需要人工复核最后一轮审核记录。")
    return lines


def _consensus_reviews(
    checkpoint: WorkflowCheckpoint,
) -> tuple[AgentRunResult, ...]:
    reviews: list[tuple[int, AgentRunResult]] = []
    prefix = "consensus_review_v"
    for name, result in checkpoint.artifacts.items():
        if name.startswith(prefix) and name[len(prefix) :].isdigit():
            reviews.append((int(name[len(prefix) :]), result))
    reviews.sort(key=lambda item: item[0])
    return tuple(result for _, result in reviews)


def _result_text(result: AgentRunResult | None, fallback: str = "未生成。") -> str:
    return result.final_text.strip() if result is not None else fallback


def _review_text(result: AgentRunResult | None) -> str:
    if result is None:
        return "未生成。"
    decision = parse_consensus_decision(result.final_text)
    if not decision.valid or not decision.structured:
        return result.final_text.strip()

    lines = [
        f"- 审核结论：**{'接受' if decision.accepted else '要求修订'}**",
        f"- 方案版本：v{decision.proposal_version}",
        "- 审核维度：",
    ]
    for name in CONSENSUS_CRITERIA:
        passed = decision.criteria.get(name) is True
        lines.append(
            f"  - {'通过' if passed else '未通过'}：{CRITERIA_LABELS.get(name, name)}"
        )
    if decision.requirements:
        lines.extend(("- 需求覆盖：",))
        for requirement in decision.requirements:
            status = "已覆盖" if requirement.covered else "未覆盖"
            lines.append(f"  - `{requirement.id}` [{status}] {requirement.text}")
            if requirement.evidence:
                lines.append(f"    - 证据：{'；'.join(requirement.evidence)}")
    if decision.issues:
        lines.extend(("- 争议：",))
        for issue in decision.issues:
            lines.append(
                f"  - `{issue.id}` [{issue.severity}/{issue.status}] {issue.problem}"
            )
            if issue.resolution:
                lines.append(f"    - 处理：{issue.resolution}")
            if issue.evidence:
                lines.append(f"    - 证据：{'；'.join(issue.evidence)}")
    for label, values in (
        ("已达成事项", decision.agreements),
        ("剩余分歧", decision.remaining_disagreements),
        ("要求修订", decision.required_revisions),
    ):
        if values:
            lines.append(f"- {label}：")
            lines.extend(f"  - {value}" for value in values)
    return "\n".join(lines)


def _safe_identifier(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")[:80]
