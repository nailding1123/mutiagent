from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO

from .bridge_models import AgentEvent, AgentRunResult, BridgeOutcome
from .consensus import (
    CONSENSUS_CRITERIA,
    SUPPORTED_CONSENSUS_PROTOCOLS,
    parse_consensus_decision,
)
from .reviews import parse_review_decision


_ACTIVE_AGENT_STATUSES = {
    "queued",
    "starting",
    "waiting_model",
    "working",
    "in_progress",
}


@dataclass(frozen=True)
class _AgentActivity:
    status: str
    detail: str
    elapsed_seconds: float
    updated_at: float


class ConsoleRenderer:
    """Readable, dependency-free terminal renderer for normalized agent events."""

    def __init__(
        self,
        *,
        color: bool | None = None,
        verbose: bool = False,
        progress: bool = True,
        tui: bool | None = None,
        stream: TextIO | None = None,
        width: int | None = None,
    ) -> None:
        self.stream = stream or sys.stdout
        if color is None:
            color = self.stream.isatty() and "NO_COLOR" not in os.environ
        terminal_width = shutil.get_terminal_size(fallback=(92, 24)).columns
        self.width = max(48, min(width or terminal_width, 110))
        self.color = color
        self.verbose = verbose
        self.progress = progress
        self.tui = self.stream.isatty() if tui is None else bool(tui)
        self.tui = self.tui and self.stream.isatty() and not verbose
        self.phase_index = 0
        self._activity_lock = threading.Lock()
        self._activity_stop = threading.Event()
        self._activity_thread: threading.Thread | None = None
        self._activity_started = 0.0
        self._activity_label = ""
        self._activity_detail = "等待模型响应"
        self._activity_frame = ""
        self._activity_operations = 0
        self._agent_activity: dict[str, _AgentActivity] = {}
        self._run_started = 0.0
        self._run_id = ""
        self._agent_calls: dict[str, int] = {}
        self._agent_durations: dict[str, float] = {}
        self._input_tokens = 0
        self._output_tokens = 0
        self._consensus_revisions = 0
        self._plan_was_rendered = False
        self._tui_active = False
        self._tui_suspended = False
        self._tui_phase = "正在准备任务"
        self._tui_notice = ""
        self._tui_last_reply = ""
        self._tui_collaboration: dict[str, object] = {}
        self._tui_last_frame = ""
        self._tui_dimensions = (self.width, 24)
        self._tui_dimensions_checked = 0.0

    def begin_run(self, run_id: str | None = None) -> None:
        self._stop_activity()
        self.phase_index = 0
        self._run_started = time.monotonic()
        self._run_id = run_id or ""
        self._activity_started = 0.0
        self._activity_label = ""
        self._activity_detail = "等待模型响应"
        self._activity_frame = "◐"
        self._activity_operations = 0
        self._agent_activity = {}
        self._agent_calls = {}
        self._agent_durations = {}
        self._input_tokens = 0
        self._output_tokens = 0
        self._consensus_revisions = 0
        self._plan_was_rendered = False
        self._tui_suspended = False
        self._tui_phase = "正在准备任务"
        self._tui_notice = ""
        self._tui_last_reply = ""
        self._tui_collaboration = {}
        self._tui_last_frame = ""
        self._tui_dimensions_checked = 0.0
        if self.tui and not self._tui_active:
            self.stream.write("\033[?1049h\033[2J\033[H\033[?25l")
            self.stream.flush()
            self._tui_active = True
            self._draw_tui()

    def set_verbose(self, enabled: bool) -> None:
        self.verbose = enabled
        if enabled:
            self._stop_activity()
            self._leave_tui()

    def set_progress(self, enabled: bool) -> None:
        self.progress = enabled
        if not enabled:
            self._stop_activity()

    def clear_screen(self) -> None:
        """Clear an interactive terminal and move the cursor to the top-left."""
        if self.stream.isatty():
            self.stream.write("\033[2J\033[H")
            self.stream.flush()

    def welcome(
        self,
        *,
        version: str,
        workspace: str,
        executor: str,
        review_rounds: int,
        consensus: bool,
    ) -> None:
        inner_width = self.width - 4
        bridge_mark = (
            "       \\  |  /                 ╭────────╮",
            "     ---  *  ---       ⇄        │   >_   │",
            "       /  |  \\                 ╰────────╯",
            "    CLAUDE CODE                CODEX CLI",
        )
        centered_mark = "\n".join(
            _center_display(line, inner_width) for line in bridge_mark
        )
        self._panel(
            f">_  MultiAgent  v{version}",
            (
                f"{centered_mark}\n\n"
                "Claude Code 与 Codex CLI 已连接\n"
                "────────────────────────────────\n"
                f"工作区      {workspace}\n"
                "Agent A     Claude\n"
                "Agent B     Codex\n"
                f"执行协调    {executor}\n"
                f"方案共识    {'开启' if consensus else '关闭'}\n"
                f"审查轮数    {review_rounds}\n\n"
                "› 输入开发需求开始协作\n"
                "  /executor codex 可切换写权限，/help 查看全部命令"
            ),
            "36",
        )

    def event(self, event: AgentEvent) -> None:
        if (
            not self._tui_active
            and self.tui
            and self._tui_suspended
            and not self.verbose
            and self.stream.isatty()
            and event.kind == "phase"
        ):
            self.stream.write("\033[?1049h\033[2J\033[H\033[?25l")
            self.stream.flush()
            self._tui_active = True
            self._tui_suspended = False
        if self._tui_active:
            self._tui_event(event)
            return
        if event.kind == "phase":
            self._stop_activity()
            self.phase_index += 1
            if (
                "自动修订" in event.text and "复审" not in event.text
            ) or "接棒整合统一方案" in event.text:
                self._consensus_revisions += 1
            self._section(f"{self.phase_index:02d}  {event.text}")
            self._start_activity(event.text, "等待模型响应")
            return
        if event.kind == "lifecycle":
            summary = event.safe_summary or event.text
            self._update_agent_activity(event, summary)
            if event.status in _ACTIVE_AGENT_STATUSES:
                return
            keep_animating = self._has_active_agents()
            self._stop_activity()
            elapsed = (
                f" · {_format_duration(event.elapsed_seconds)}"
                if event.elapsed_seconds is not None
                else ""
            )
            if event.status == "completed":
                self._compact_line("✓", f"{summary}{elapsed}", "32")
            elif event.status == "failed":
                self._compact_line("✗", f"{summary}{elapsed}", "31")
            if keep_animating:
                self._resume_activity()
            return
        if event.kind == "checkpoint":
            if self.verbose:
                self._compact_line("◇", event.safe_summary or event.text, "2")
            return
        if event.kind == "text":
            keep_animating = self._has_active_agents()
            self._stop_activity()
            if self._render_consensus_review(event.source, event.text):
                if keep_animating:
                    self._resume_activity()
                return
            if self._render_structured_review(event.source, event.text):
                if keep_animating:
                    self._resume_activity()
                return
            color = "35" if event.source == "Claude" else "34"
            self._panel(f"{event.source} · 回复", event.text, color)
            if keep_animating:
                self._resume_activity()
            return
        if event.kind == "progress":
            self._update_agent_activity(event, _safe_progress_text(event))
            if self.verbose and event.text:
                color = "35" if event.source == "Claude" else "34"
                self._panel(f"{event.source} · 中间过程", event.text, color)
            return
        if event.kind == "tool":
            self._update_agent_activity(event, _safe_progress_text(event))
            if self.verbose:
                self._compact_line(
                    "→", f"{event.source}  {self._format_tool(event.text)}", "36"
                )
            return
        if event.kind == "tool_result":
            self._update_agent_activity(event, _safe_progress_text(event))
            if self.verbose:
                status = event.text.strip()
                symbol = (
                    "✓"
                    if status.lower() in {"completed", "success", "0", "passed"}
                    else "•"
                )
                self._compact_line(symbol, f"{event.source}  {status}", "32")
            return
        if event.kind == "verification":
            self._stop_activity()
            detail = event.text if self.verbose else event.safe_summary or "正在运行验证"
            self._update_agent_activity(event, detail)
            self._compact_line("▶", detail, "36")
            self._resume_activity()
            return
        if event.kind == "verification_result":
            self._update_agent_activity(event, event.safe_summary or event.text)
            self._stop_activity()
            passed = event.status == "completed" or "PASS" in event.text
            detail = event.text if self.verbose else event.safe_summary or event.text
            self._compact_line("✓" if passed else "✗", detail, "32" if passed else "31")
            if self._has_active_agents():
                self._resume_activity()
            return
        if event.kind == "warning":
            self._stop_activity()
            self._panel(f"{event.source} · 注意", event.text, "33")
            return
        if event.kind == "error":
            self._update_agent_activity(
                event, event.safe_summary or f"{event.source} · 本轮执行失败"
            )
            keep_animating = self._has_active_agents()
            self._stop_activity()
            self._panel(f"{event.source} · 错误", event.text, "31")
            if keep_animating:
                self._resume_activity()
            return
        if event.kind == "metric":
            self._capture_metric(event)
            return
        if event.kind == "log" and event.text and self.verbose:
            self._compact_line("·", f"{event.source}  {event.text}", "2")

    def outcome(self, outcome: BridgeOutcome) -> None:
        used_tui = self._tui_active or (
            self.tui and self.stream.isatty() and not self.verbose
        )
        self._stop_activity()
        self._leave_tui()
        if used_tui:
            executor_source = "Claude" if outcome.executor == "claude" else "Codex"
            executor_color = "35" if outcome.executor == "claude" else "34"
            if outcome.agent_proposals and not self._plan_was_rendered:
                for index, independent in enumerate(outcome.agent_proposals):
                    label = chr(ord("A") + index)
                    self._panel(
                        f"Agent {label} · {independent.agent} · 独立方案",
                        independent.final_text,
                        "35" if independent.agent == "Claude" else "34",
                    )
            if not self._plan_was_rendered:
                for index, review in enumerate(outcome.cross_reviews):
                    if not self._render_consensus_review(
                        review.agent,
                        review.final_text,
                        title=f"交叉审核 {index + 1}",
                    ):
                        self._panel(
                            f"{review.agent} · 交叉审核",
                            review.final_text,
                            "35" if review.agent == "Claude" else "34",
                        )
            final_plan = outcome.unified_proposal
            if final_plan and not self._plan_was_rendered:
                self._panel(
                    "双方统一方案",
                    final_plan.final_text,
                    "36",
                )
                if outcome.consensus_reviews:
                    final_consensus_review = outcome.consensus_reviews[-1]
                    rendered_review = self._render_consensus_review(
                        final_consensus_review.agent,
                        final_consensus_review.final_text,
                        title="统一方案审核",
                    )
                    if not rendered_review:
                        self._panel(
                            f"{final_consensus_review.agent} · 统一方案审核",
                            final_consensus_review.final_text,
                            "34" if final_consensus_review.agent == "Codex" else "35",
                        )
            self._panel(
                f"{executor_source} · 执行结果",
                outcome.execution_result.final_text,
                executor_color,
            )
            if outcome.reviews:
                final_review = outcome.reviews[-1]
                if not self._render_structured_review(
                    final_review.agent, final_review.final_text
                ):
                    self._panel(
                        f"{final_review.agent} · 最终审查",
                        final_review.final_text,
                        "34" if final_review.agent == "Codex" else "35",
                    )
        if outcome.approved is True:
            status = "✓ 通过：需求、代码审查与已配置验证均满足"
            color = "32"
        elif outcome.approved is False:
            status = "✗ 未通过：仍有审查问题或验证失败"
            color = "31"
        else:
            status = "• 完成：未启用完整代码验收"
            color = "33"

        lines = [
            status,
            "协作关系          Agent A / Agent B 对等",
            f"执行协调 Agent    {outcome.executor}",
            f"双方独立方案      {len(outcome.agent_proposals)} 份",
            f"双向交叉审核      {len(outcome.cross_reviews)} 份",
            f"代码审查          {len(outcome.reviews)} 次",
        ]
        if self._run_started:
            lines.append(
                f"总耗时            {_format_duration(time.monotonic() - self._run_started)}"
            )
        if self._agent_calls:
            calls = " / ".join(
                f"{name} {count} 次" for name, count in self._agent_calls.items()
            )
            lines.append(f"Agent 调用        {calls}")
        if self._agent_durations:
            durations = " / ".join(
                f"{name} {_format_duration(seconds)}"
                for name, seconds in self._agent_durations.items()
            )
            lines.append(f"Agent 累计耗时    {durations}")
        if self._consensus_revisions:
            lines.append(f"共识自动修订      {self._consensus_revisions} 次")
        if self._input_tokens or self._output_tokens:
            lines.append(
                f"Token             输入 {self._input_tokens} / 输出 {self._output_tokens}"
            )
        if self._run_id:
            lines.append(f"运行记录          {self._run_id}")
        if outcome.verifications:
            passed = sum(result.passed for result in outcome.verifications)
            lines.append(f"独立验证          {passed}/{len(outcome.verifications)} 次通过")
        else:
            lines.append("独立验证          未配置")

        if outcome.collaboration:
            collaboration = outcome.collaboration
            completed_tasks = sum(
                task.status in {"done", "skipped"}
                for task in collaboration.tasks.values()
            )
            evidence_count = sum(
                len(requirement.evidence)
                for requirement in collaboration.requirements.values()
            ) + sum(len(issue.evidence) for issue in collaboration.issues.values())
            lines.append(
                f"共享任务          {completed_tasks}/{len(collaboration.tasks)} 完成"
            )
            lines.append(
                f"争议与证据        {len(collaboration.issues)} 项 / "
                f"{evidence_count} 条证据 / "
                f"{len(collaboration.blocking_issues)} 项阻塞"
            )
            if collaboration.proposal_version:
                approval = (
                    "双方已批准" if collaboration.accepted else "未启用或未达成共识"
                )
                digest = collaboration.proposal_digest[:12] or "未记录"
                lines.append(
                    f"统一方案          v{collaboration.proposal_version} · "
                    f"{approval} · {digest}"
                )

        if outcome.baseline and outcome.baseline.is_git_repo:
            before = _status_count(outcome.baseline.status)
            after = _status_count(outcome.final_snapshot.status if outcome.final_snapshot else "")
            lines.append(f"Git 状态          任务前 {before} 项 / 当前 {after} 项")

        final_decision = outcome.review_decisions[-1] if outcome.review_decisions else None
        if final_decision and final_decision.findings:
            lines.append("")
            lines.append("最终阻塞项：")
            for finding in final_decision.findings:
                location = finding.file or "(未定位文件)"
                if finding.line is not None:
                    location = f"{location}:{finding.line}"
                lines.append(f"[{finding.severity}] {location}  {finding.problem}")
                if finding.suggestion:
                    lines.append(f"  建议：{finding.suggestion}")

        self._panel("运行结果", "\n".join(lines), color)

    def summary(self) -> dict[str, object]:
        elapsed = time.monotonic() - self._run_started if self._run_started else 0
        return {
            "elapsed_seconds": round(elapsed, 3),
            "agent_calls": dict(self._agent_calls),
            "agent_durations": {
                name: round(seconds, 3)
                for name, seconds in self._agent_durations.items()
            },
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "consensus_revisions": self._consensus_revisions,
        }

    def collaboration_confirmation(
        self,
        proposal_a: AgentRunResult,
        proposal_b: AgentRunResult,
        cross_reviews: tuple[AgentRunResult, ...],
        unified_proposal: AgentRunResult,
        consensus_review: AgentRunResult | None,
        revision_count: int,
    ) -> None:
        self._stop_activity()
        self._leave_tui(suspend=True)
        self._plan_was_rendered = True
        for label, proposal in (("A", proposal_a), ("B", proposal_b)):
            self._panel(
                f"Agent {label} · {proposal.agent} · 独立方案",
                proposal.final_text,
                "35" if proposal.agent == "Claude" else "34",
            )
        for index, review in enumerate(cross_reviews, start=1):
            if not self._render_consensus_review(
                review.agent,
                review.final_text,
                title=f"交叉审核 {index}",
            ):
                self._panel(
                    f"{review.agent} · 交叉审核 {index}",
                    review.final_text,
                    "35" if review.agent == "Claude" else "34",
                )
        self._panel("双方统一方案", unified_proposal.final_text, "36")
        if consensus_review is not None:
            self._render_consensus_review(
                consensus_review.agent,
                consensus_review.final_text,
                title="统一方案审核",
            )
        files = _extract_file_candidates(unified_proposal.final_text)
        lines = [
            f"协作状态      {'双方已批准' if consensus_review else '快速协作已完成'}",
            f"人工修订      {revision_count} 次",
            f"涉及文件      {len(files)} 个候选" if files else "涉及文件      未识别",
            "",
            "[e] 执行统一方案    [r] 提出整体修订要求\n"
            "[t] 单独给某个 Agent 提要求\n"
            "[d] 导出最终技术文档    [c] 取消任务",
        ]
        self._panel("实施确认", "\n".join(lines), "36")

    def document_exported(
        self,
        path: Path,
        *,
        consensus_incomplete: bool = False,
    ) -> None:
        self._stop_activity()
        self._leave_tui(suspend=True)
        note = (
            "共识轮次已达到上限；文档已标注未达成共识的内容和原因。"
            if consensus_incomplete
            else "文档已导出；你仍可继续执行、修订或取消当前方案。"
        )
        self._panel("技术文档", f"{path}\n{note}", "36")

    def failure_recovery(self, error: str) -> None:
        self._stop_activity()
        self._leave_tui(suspend=True)
        self._panel(
            "任务暂停",
            (
                f"{error}\n\n"
                "[r] 重试当前任务    [l] 切换执行协调 Agent 后重试\n"
                "[d] 展开执行详情后重试    [q] 结束本次任务"
            ),
            "31",
        )

    def history(self, records: list[dict[str, object]]) -> None:
        if not records:
            self._panel("任务历史", "暂无运行记录。", "36")
            return
        symbols = {
            "complete": "✓",
            "ready": "●",
            "running": "◐",
            "failed": "✗",
            "cancelled": "·",
            "interrupted": "!",
        }
        lines: list[str] = []
        for record in records:
            status = str(record.get("status", "unknown"))
            task = _truncate_display(str(record.get("task", "")), 46)
            run_id = str(record.get("id", ""))
            attempts = record.get("attempts", 1)
            attempt_text = f" · {attempts} 次尝试" if attempts != 1 else ""
            lines.append(
                f"{symbols.get(status, '·')} {run_id}  {status}{attempt_text}"
            )
            lines.append(f"    {task}")
        lines.extend(("", "恢复任务：multiagent resume <run-id>"))
        self._panel("任务历史", "\n".join(lines), "36")

    def tasks(self, records: list[dict[str, object]]) -> None:
        if not records:
            self._panel("任务中心", "暂无任务。", "36")
            return
        symbols = {
            "complete": "✓",
            "ready": "●",
            "running": "◐",
            "failed": "✗",
            "cancelled": "·",
            "interrupted": "!",
        }
        lines: list[str] = []
        for record in records:
            status = str(record.get("status", "unknown"))
            run_id = str(record.get("id", ""))
            phase = str(record.get("phase", "未开始"))
            workspace = str(record.get("workspace", ""))
            lines.append(
                f"{symbols.get(status, '·')} {run_id}  {status} · {phase}"
            )
            lines.append(f"    {_truncate_display(str(record.get('task', '')), 58)}")
            lines.append(f"    {_truncate_display(workspace, 58)}")
        lines.extend(
            (
                "",
                "查看：multiagent task <run-id>",
            )
        )
        self._panel("任务中心", "\n".join(lines), "36")

    def task_detail(self, record: dict[str, object]) -> None:
        lines = [
            f"任务 ID       {record.get('id', '')}",
            f"状态          {record.get('status', 'unknown')}",
            f"阶段          {record.get('phase', '未记录')}",
            f"执行协调      {record.get('executor', '')}",
            f"工作区        {record.get('workspace', '')}",
            f"需求          {record.get('task', '')}",
        ]
        if record.get("collaboration_mode") == "group_chat":
            group_chat = record.get("group_chat")
            messages = group_chat.get("messages", []) if isinstance(group_chat, dict) else []
            turns = group_chat.get("turn", 0) if isinstance(group_chat, dict) else 0
            lines.extend(
                (
                    "协作模式      群聊协作（讨论只读，单 Agent 写目标工作区）",
                    f"群聊记录      {turns} 轮 · {len(messages) if isinstance(messages, list) else 0} 条消息",
                )
            )
        technical_document = record.get("technical_document")
        if isinstance(technical_document, str) and technical_document:
            lines.append(f"技术文档      {technical_document}")
        collaboration = record.get("collaboration")
        if isinstance(collaboration, dict):
            tasks = collaboration.get("tasks", [])
            issues = collaboration.get("issues", [])
            messages = collaboration.get("messages", [])
            if isinstance(tasks, list):
                lines.extend(("", "共享任务："))
                for task in tasks:
                    if isinstance(task, dict):
                        lines.append(
                            f"  {task.get('id', '')} [{task.get('status', '')}] "
                            f"{task.get('owner', '')} · {task.get('title', '')}"
                        )
            if isinstance(issues, list):
                blockers = [
                    issue
                    for issue in issues
                    if isinstance(issue, dict)
                    and issue.get("severity") in {"P0", "P1"}
                    and issue.get("status") != "resolved"
                ]
                lines.append(f"争议          {len(issues)} 项，阻塞 {len(blockers)} 项")
            if isinstance(messages, list):
                lines.append(f"结构化消息    {len(messages)} 条")
        events = record.get("events")
        if isinstance(events, list) and events:
            lines.extend(("", "最近事件："))
            for raw in events[-10:]:
                if not isinstance(raw, dict):
                    continue
                clock = _event_clock(raw.get("timestamp"))
                source = str(raw.get("source", ""))
                status = str(raw.get("status", ""))
                step = str(raw.get("step_id", ""))
                summary = str(raw.get("safe_summary") or raw.get("text") or "")
                elapsed = raw.get("elapsed_seconds")
                elapsed_text = (
                    f" · {_format_duration(float(elapsed))}"
                    if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool)
                    else ""
                )
                context = f"{step} · " if step else ""
                lines.append(
                    f"  {clock} {source} [{status}] {context}{summary}{elapsed_text}"
                )
        self._panel("任务详情", "\n".join(lines), "36")

    def quality_report(self, report) -> None:
        lines = [
            f"样本任务      {report.total_runs}",
            f"完成率        {_format_rate(report.completion_rate)}",
            f"验收通过率    {_format_rate(report.approval_rate)} "
            f"（{report.evaluated_runs} 个有验收结论的任务）",
            f"独立验证通过  {report.verification_passed}/{report.verification_total} "
            f"({_format_rate(report.verification_rate)})",
            "",
            "审查发现："
            + " / ".join(
                f"{severity} {report.findings.get(severity, 0)}"
                for severity in ("P0", "P1", "P2", "P3")
            ),
        ]
        if report.modes:
            lines.extend(("", "模式对比："))
            labels = {"solo": "单 Agent", "review": "双 Agent", "consensus": "共识"}
            for name in ("solo", "review", "consensus"):
                metrics = report.modes.get(name)
                if metrics is None:
                    continue
                lines.append(
                    f"  {labels[name]:<8} {metrics.runs} 次 · "
                    f"通过 {_format_rate(metrics.approval_rate)} · "
                    f"平均 Token {metrics.average_tokens:.0f} · "
                    f"平均 {_format_duration(metrics.average_elapsed)}"
                )
        if report.total_runs < 5:
            lines.extend(("", "提示：样本少于 5 次，模式差异仅供参考。"))
        self._panel("质量评测", "\n".join(lines), "36")

    def diagnostics(self, checks: list[tuple[bool, str, str]]) -> None:
        lines = [
            f"{'✓' if passed else '✗'} {name}\n    {detail}"
            for passed, name, detail in checks
        ]
        passed_count = sum(passed for passed, _, _ in checks)
        lines.extend(("", f"结果：{passed_count}/{len(checks)} 项通过"))
        self._panel(
            "环境诊断",
            "\n".join(lines),
            "32" if passed_count == len(checks) else "33",
        )

    def _render_consensus_review(
        self,
        source: str,
        text: str,
        *,
        title: str = "方案共识",
    ) -> bool:
        if not any(protocol in text for protocol in SUPPORTED_CONSENSUS_PROTOCOLS):
            return False
        decision = parse_consensus_decision(text)
        if not decision.valid or not decision.structured:
            return False
        labels = {
            "requirements": "需求边界",
            "architecture": "技术方案",
            "failure_paths": "异常路径",
            "compatibility": "兼容性",
            "testing": "测试计划",
        }
        lines = ["✓ 已达成方案共识" if decision.accepted else "△ 方案仍需修订"]
        for name in CONSENSUS_CRITERIA:
            passed = decision.criteria.get(name) is True
            lines.append(f"  {'✓' if passed else '!'} {labels[name]}")
        if decision.remaining_disagreements:
            lines.append("剩余分歧：")
            lines.extend(f"  · {item}" for item in decision.remaining_disagreements)
        if decision.required_revisions:
            lines.append("下一轮必须调整：")
            lines.extend(f"  · {item}" for item in decision.required_revisions)
        if decision.requirements:
            covered = sum(
                requirement.covered and bool(requirement.evidence)
                for requirement in decision.requirements
            )
            lines.append(
                f"需求证据：{covered}/{len(decision.requirements)} 项覆盖且有证据"
            )
        if decision.issues:
            blockers = [
                issue
                for issue in decision.issues
                if issue.severity in {"P0", "P1"}
                and (issue.status != "resolved" or not issue.evidence)
            ]
            lines.append(
                f"争议台账：{len(decision.issues)} 项 · 未解决阻塞 {len(blockers)} 项"
            )
            lines.extend(
                f"  ! {issue.id} [{issue.severity}] {issue.problem}"
                for issue in blockers[:5]
            )
        self._panel(
            f"{source} · {title}",
            "\n".join(lines),
            "32" if decision.accepted else "33",
        )
        return True

    def close(self) -> None:
        self._stop_activity()
        self._leave_tui()

    def _tui_event(self, event: AgentEvent) -> None:
        if event.kind == "phase":
            self._stop_activity()
            self.phase_index += 1
            self._tui_phase = event.text
            self._tui_notice = ""
            if (
                "自动修订" in event.text and "复审" not in event.text
            ) or "接棒整合统一方案" in event.text:
                self._consensus_revisions += 1
            self._start_activity(event.text, "等待模型响应")
        elif event.kind == "collaboration":
            try:
                data = json.loads(event.text)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict):
                self._tui_collaboration = data
        elif event.kind == "text":
            if not self._has_active_agents():
                self._stop_activity()
            compact = " ".join(event.text.split())
            preview_width = max(120, self.width * 3)
            self._tui_last_reply = (
                f"→ {event.source} 已生成输出："
                f"{_truncate_display(compact, preview_width)}"
            )
        elif event.kind == "lifecycle":
            summary = event.safe_summary or event.text
            self._update_agent_activity(event, summary)
            if event.status not in _ACTIVE_AGENT_STATUSES:
                if not self._has_active_agents():
                    self._stop_activity()
        elif event.kind == "checkpoint":
            self._tui_notice = event.safe_summary or event.text
        elif event.kind in {"progress", "tool", "tool_result"}:
            self._update_agent_activity(event, _safe_progress_text(event))
        elif event.kind == "verification":
            self._update_agent_activity(event, event.safe_summary or event.text)
        elif event.kind == "verification_result":
            self._update_agent_activity(event, event.safe_summary or event.text)
            if not self._has_active_agents():
                self._stop_activity()
        elif event.kind == "warning":
            self._tui_notice = f"注意：{event.text}"
        elif event.kind == "error":
            self._update_agent_activity(
                event, event.safe_summary or f"{event.source} · 本轮执行失败"
            )
            if not self._has_active_agents():
                self._stop_activity()
            self._tui_notice = f"错误：{event.text}"
        elif event.kind == "metric":
            self._capture_metric(event)
        self._draw_tui()

    def _draw_tui(self) -> None:
        if not self._tui_active:
            return
        elapsed = time.monotonic() - self._run_started if self._run_started else 0
        width, height = self._terminal_dimensions()
        inner = width - 4
        tasks = self._tui_collaboration.get("tasks", [])
        issues = self._tui_collaboration.get("issues", [])
        messages = self._tui_collaboration.get("messages", [])
        requirements = self._tui_collaboration.get("requirements", [])
        if not isinstance(tasks, list):
            tasks = []
        if not isinstance(issues, list):
            issues = []
        if not isinstance(messages, list):
            messages = []
        if not isinstance(requirements, list):
            requirements = []

        title = f">_  MultiAgent  ·  {self._run_id or '当前任务'}"
        lines = [
            _tui_top(title, width),
            _tui_line(
                f"◆ 阶段 {self.phase_index:02d}  {self._tui_phase}", inner
            ),
            _tui_line(
                f"总耗时 {_format_duration(elapsed)}  ·  调用 {sum(self._agent_calls.values())}  ·  "
                f"Token ↑{self._input_tokens} ↓{self._output_tokens}",
                inner,
            ),
            _tui_divider("实时 Agent", width),
        ]
        activity_lines = self._agent_activity_lines()
        if activity_lines:
            lines.extend(_tui_line(line, inner) for line in activity_lines)
            lines.append(
                _tui_line(self._phase_activity_summary(messages=len(messages)), inner)
            )
            if self._tui_notice:
                lines.append(_tui_line(f"◇ {self._tui_notice}", inner))
        else:
            notice = self._tui_notice or self._activity_status(messages=len(messages))
            lines.append(_tui_line(notice, inner))

        symbols = {
            "pending": "○",
            "in_progress": "◐",
            "blocked": "!",
            "done": "✓",
            "failed": "✗",
            "skipped": "·",
        }
        blockers = [
            issue
            for issue in issues
            if isinstance(issue, dict)
            and issue.get("severity") in {"P0", "P1"}
            and issue.get("status") != "resolved"
        ]
        evidence_count = sum(
            len(item.get("evidence", []))
            for item in issues
            if isinstance(item, dict) and isinstance(item.get("evidence", []), list)
        )
        covered_requirements = sum(
            item.get("covered") is True
            for item in requirements
            if isinstance(item, dict)
        )
        if blockers:
            quality_lines = [
                f"! 未通过质量门禁 · {len(blockers)} 个 P0/P1 阻塞"
            ]
            quality_lines.extend(
                f"  {issue.get('id', '')} [{issue.get('severity', '')}] "
                f"{issue.get('problem', '')}"
                for issue in blockers[:2]
            )
        else:
            requirement_progress = (
                f"{covered_requirements}/{len(requirements)}"
                if requirements
                else "待生成"
            )
            quality_lines = [
                f"✓ 当前无 P0/P1 阻塞 · 争议 {len(issues)} · "
                f"证据 {evidence_count} · 需求 {requirement_progress}"
            ]

        lines.append(_tui_divider("任务进度", width))
        finished_tasks = sum(
            isinstance(item, dict) and item.get("status") in {"done", "skipped"}
            for item in tasks
        )
        lines.append(
            _tui_line(
                f"{_progress_bar(finished_tasks, len(tasks))}  "
                f"已完成 {finished_tasks}/{len(tasks)}",
                inner,
            )
        )
        task_priority = {
            "in_progress": 0,
            "blocked": 1,
            "failed": 1,
            "pending": 2,
            "done": 3,
            "skipped": 4,
        }
        ordered_tasks = sorted(
            enumerate(tasks),
            key=lambda item: (
                task_priority.get(
                    str(item[1].get("status", "pending"))
                    if isinstance(item[1], dict)
                    else "pending",
                    9,
                ),
                item[0],
            ),
        )
        fixed_tail = 1 + len(quality_lines) + 1
        available_tasks = max(1, height - len(lines) - fixed_tail - 1)
        for _, raw in ordered_tasks[:available_tasks]:
            if not isinstance(raw, dict):
                continue
            status = str(raw.get("status", "pending"))
            owner = _display_agent_name(str(raw.get("owner", "")))
            task_title = str(raw.get("title", ""))
            content = f"{symbols.get(status, '·')} {_pad_display(owner, 7)} {task_title}"
            lines.append(_tui_line(content, inner))
        if not tasks:
            lines.append(_tui_line("暂无共享任务", inner))

        lines.append(_tui_divider("质量门禁", width))
        lines.extend(_tui_line(line, inner) for line in quality_lines)
        if self._tui_last_reply and len(lines) < height - 2:
            lines.append(_tui_line(self._tui_last_reply, inner))
        while len(lines) < height - 1:
            lines.append(_tui_line("", inner))
        lines = lines[: height - 1]
        lines.append("╰" + "─" * (width - 2) + "╯")
        frame = "\n".join(lines)
        with self._activity_lock:
            if frame == self._tui_last_frame:
                return
            self.stream.write("\033[H" + frame + "\033[J")
            self.stream.flush()
            self._tui_last_frame = frame

    def _terminal_dimensions(self) -> tuple[int, int]:
        now = time.monotonic()
        if now - self._tui_dimensions_checked >= 0.5:
            terminal = shutil.get_terminal_size(fallback=(self.width, 24))
            self._tui_dimensions = (
                max(48, min(terminal.columns, 120)),
                max(16, terminal.lines),
            )
            self._tui_dimensions_checked = now
        return self._tui_dimensions

    def _leave_tui(self, *, suspend: bool = False) -> None:
        if not self._tui_active:
            if suspend:
                self._tui_suspended = True
            return
        self._tui_active = False
        self._tui_suspended = suspend
        self._tui_last_frame = ""
        self.stream.write("\033[?25h\033[?1049l")
        self.stream.flush()

    def _start_activity(self, label: str, detail: str) -> None:
        with self._activity_lock:
            self._activity_label = label
            self._activity_detail = detail
            self._activity_operations = 0
            self._agent_activity = {}
            self._activity_started = time.monotonic()
            self._activity_frame = "◐"
        self._resume_activity()

    def _resume_activity(self) -> None:
        if self.verbose or not self.progress or not self.stream.isatty():
            return
        if self._activity_thread is not None and self._activity_thread.is_alive():
            return
        self._activity_stop = threading.Event()
        self._activity_thread = threading.Thread(
            target=self._activity_loop,
            name="multiagent-status",
            daemon=True,
        )
        self._activity_thread.start()

    def _activity_loop(self) -> None:
        frames = "◐◓◑◒"
        index = 0
        while not self._activity_stop.wait(0.2):
            frame = frames[index % len(frames)]
            with self._activity_lock:
                self._activity_frame = frame
            if self._tui_active:
                self._draw_tui()
                index += 1
                continue
            status = f"  {self._activity_status()}"
            with self._activity_lock:
                self.stream.write(f"\r\033[2K{status}")
                self.stream.flush()
            index += 1

    def _bump_activity(self, detail: str = "") -> None:
        with self._activity_lock:
            self._activity_operations += 1
            if detail:
                self._activity_detail = detail

    def _update_agent_activity(self, event: AgentEvent, detail: str) -> None:
        source = event.source.strip()
        if not source or source == "Bridge":
            self._bump_activity(detail)
            return
        normalized = _strip_agent_prefix(source, detail) or "等待模型响应"
        now = time.monotonic()
        with self._activity_lock:
            previous = self._agent_activity.get(source)
            elapsed = event.elapsed_seconds
            if elapsed is None:
                elapsed = previous.elapsed_seconds if previous is not None else 0.0
            self._agent_activity[source] = _AgentActivity(
                status=event.status,
                detail=normalized,
                elapsed_seconds=max(0.0, float(elapsed)),
                updated_at=now,
            )
            self._activity_operations += 1
            self._activity_detail = detail

    def _has_active_agents(self) -> bool:
        with self._activity_lock:
            return any(
                activity.status in _ACTIVE_AGENT_STATUSES
                for activity in self._agent_activity.values()
            )

    def _agent_activity_lines(self) -> list[str]:
        now = time.monotonic()
        with self._activity_lock:
            frame = self._activity_frame or "◐"
            activities = dict(self._agent_activity)
        order = {"Claude": 0, "Codex": 1, "Verifier": 2}
        lines: list[str] = []
        for source, activity in sorted(
            activities.items(), key=lambda item: (order.get(item[0], 99), item[0])
        ):
            active = activity.status in _ACTIVE_AGENT_STATUSES
            elapsed = activity.elapsed_seconds
            if active:
                elapsed += max(0.0, now - activity.updated_at)
            symbol = frame if active else "✓" if activity.status == "completed" else "✗"
            lines.append(
                f"{symbol} {source} · {activity.detail} · {_format_clock(elapsed)}"
            )
        return lines

    def _phase_activity_summary(self, *, messages: int | None = None) -> str:
        elapsed = (
            max(0.0, time.monotonic() - self._activity_started)
            if self._activity_started
            else 0.0
        )
        with self._activity_lock:
            operations = self._activity_operations
        details = [f"阶段计时 {_format_clock(elapsed)}"]
        if messages is not None:
            details.append(f"已交换 {messages} 条结构化消息")
        if operations:
            details.append(f"已处理 {operations} 个内部事件")
        return " · ".join(details)

    def _activity_status(self, *, messages: int | None = None) -> str:
        if not self.progress:
            return "安全进度已关闭"
        if not self._activity_started:
            return "◐ 等待任务开始"
        agent_lines = self._agent_activity_lines()
        if agent_lines:
            agents = " │ ".join(agent_lines)
            return f"{agents} · {self._phase_activity_summary(messages=messages)}"
        elapsed = int(time.monotonic() - self._activity_started)
        minutes, seconds = divmod(elapsed, 60)
        with self._activity_lock:
            frame = self._activity_frame or "◐"
            label = self._activity_label
            detail = self._activity_detail
            operations = self._activity_operations
        extra = (
            f" · 已处理 {operations} 个内部事件"
            if operations
            else ""
        )
        if messages is not None:
            extra = f" · 已交换 {messages} 条结构化消息{extra}"
        return f"{frame} {detail} · {minutes:02d}:{seconds:02d} · 阶段：{label}{extra}"

    def _stop_activity(self) -> None:
        thread = self._activity_thread
        if thread is None:
            return
        self._activity_stop.set()
        if thread is not threading.current_thread():
            thread.join(timeout=0.3)
        with self._activity_lock:
            if self.stream.isatty() and not self._tui_active:
                self.stream.write("\r\033[2K")
                self.stream.flush()
        self._activity_thread = None

    def _render_structured_review(self, source: str, text: str) -> bool:
        if '"verdict"' not in text or '"findings"' not in text:
            return False
        decision = parse_review_decision(text)
        if not decision.structured:
            return False

        approved = decision.verdict == "approve"
        lines = [f"{'✓ APPROVE' if approved else '✗ REQUEST CHANGES'}"]
        if decision.requirements_covered:
            lines.append("覆盖的验收项：")
            lines.extend(f"  ✓ {item}" for item in decision.requirements_covered)
        if decision.findings:
            lines.append("发现的问题：")
            for finding in decision.findings:
                location = finding.file or "(未定位文件)"
                if finding.line is not None:
                    location = f"{location}:{finding.line}"
                lines.append(f"  [{finding.severity}] {location}  {finding.problem}")
                if finding.evidence:
                    lines.append(f"      证据：{finding.evidence}")
                if finding.suggestion:
                    lines.append(f"      建议：{finding.suggestion}")
        elif not decision.requirements_covered:
            lines.append("未报告阻塞问题。")
        self._panel(f"{source} · 结构化验收", "\n".join(lines), "32" if approved else "31")
        return True

    def _capture_metric(self, event: AgentEvent) -> None:
        try:
            data = json.loads(event.text)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        self._agent_calls[event.source] = self._agent_calls.get(event.source, 0) + 1
        duration = data.get("duration_seconds", 0)
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            self._agent_durations[event.source] = (
                self._agent_durations.get(event.source, 0.0) + max(0.0, float(duration))
            )
        input_tokens = data.get("input_tokens", 0)
        output_tokens = data.get("output_tokens", 0)
        if isinstance(input_tokens, int) and not isinstance(input_tokens, bool):
            self._input_tokens += max(0, input_tokens)
        if isinstance(output_tokens, int) and not isinstance(output_tokens, bool):
            self._output_tokens += max(0, output_tokens)

    def _section(self, title: str) -> None:
        self._emit("")
        prefix = f"━━ {title} "
        fill = "━" * max(2, self.width - _display_width(prefix))
        self._emit(self._style("1;36", f"{prefix}{fill}"))

    def _panel(self, title: str, text: str, color: str) -> None:
        self._emit("")
        prefix = f"╭─ {title} "
        fill = "─" * max(1, self.width - _display_width(prefix))
        self._emit(self._style(f"1;{color}", f"{prefix}{fill}"))
        inner_width = self.width - 4
        for chunk in _render_content_lines(text, inner_width):
            padding = " " * max(0, inner_width - _display_width(chunk))
            left = self._style(color, "│")
            right = self._style(color, "│")
            self._emit(f"{left} {chunk}{padding} {right}")
        self._emit(self._style(color, f"╰{'─' * (self.width - 1)}"))

    def _compact_line(self, symbol: str, text: str, color: str) -> None:
        available = max(10, self.width - 4)
        compact = _truncate_display(" ".join(text.split()), available)
        self._emit(f"  {self._style(f'1;{color}', symbol)} {compact}")

    def _format_tool(self, text: str) -> str:
        name, separator, detail = text.partition(": ")
        if not separator:
            return _truncate_display(text, self.width - 10)
        try:
            payload = json.loads(detail)
        except json.JSONDecodeError:
            return _truncate_display(text, self.width - 10)
        if not isinstance(payload, dict):
            return _truncate_display(text, self.width - 10)
        preferred = (
            "file_path",
            "path",
            "command",
            "cmd",
            "pattern",
            "query",
            "description",
        )
        values = [f"{key}={payload[key]}" for key in preferred if key in payload]
        if not values:
            values = [f"{key}={value}" for key, value in list(payload.items())[:2]]
        return _truncate_display(f"{name}  {'  '.join(values)}", self.width - 10)

    def _style(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def _emit(self, text: str) -> None:
        print(text, file=self.stream, flush=True)


def _status_count(status: str) -> int:
    return sum(1 for line in status.splitlines() if line.strip())


def _safe_progress_text(event: AgentEvent) -> str:
    source = event.source or "Agent"
    if event.kind == "progress":
        return f"{source} · 正在组织回复"
    if event.kind == "tool_result":
        status = event.text.strip().lower()
        if status in {"failed", "error"}:
            return f"{source} · 工具返回异常"
        return f"{source} · 等待模型响应"
    if event.kind != "tool":
        return f"{source} · 等待模型响应"

    tool_name, _, detail = event.text.partition(": ")
    lower_name = tool_name.strip().lower()
    lower_text = event.text.lower()
    file_kind = _safe_file_kind(detail or event.text)

    if file_kind == "pdf":
        return f"{source} · 正在读取 PDF"
    if lower_name in {"read", "notebookread"}:
        return f"{source} · 正在读取文件"
    if lower_name in {"grep", "glob", "ls", "list", "search"}:
        return f"{source} · 正在检查文件"
    if lower_name in {"edit", "write", "multiedit", "file_change"}:
        return f"{source} · 正在更新文件"
    if lower_name in {"bash", "command_execution", "shell"}:
        return f"{source} · 正在执行检查"
    if lower_name in {"todowrite", "update_plan"}:
        return f"{source} · 正在更新任务状态"
    if "mcp_tool_call" in lower_name:
        return f"{source} · 正在调用外部工具"
    if "web" in lower_name:
        return f"{source} · 正在读取网页"
    if file_kind:
        return f"{source} · 正在处理文件"
    if any(word in lower_text for word in ("grep", "glob", "find", "rg ")):
        return f"{source} · 正在检查文件"
    if any(word in lower_text for word in ("pytest", "test", "lint", "typecheck")):
        return f"{source} · 正在执行检查"
    return f"{source} · 正在调用工具"


def _safe_file_kind(text: str) -> str:
    if not text:
        return ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    candidates: list[str] = []
    if isinstance(data, dict):
        for key in ("file_path", "path", "file", "url"):
            value = data.get(key)
            if isinstance(value, str):
                candidates.append(value)
    candidates.append(text)
    lower = " ".join(candidates).lower()
    if re.search(r"\.pdf(?:\b|[\"'`),}\]])", lower):
        return "pdf"
    if re.search(
        r"\.(?:py|js|jsx|ts|tsx|json|toml|yaml|yml|md|go|rs|java|kt|swift|"
        r"c|h|cpp|hpp|css|scss|html|sql|sh)(?:\b|[\"'`),}\]])",
        lower,
    ):
        return "file"
    return ""


def _format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    minutes, remaining = divmod(total, 60)
    if minutes:
        return f"{minutes}分{remaining:02d}秒"
    return f"{remaining}秒"


def _format_clock(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, remaining = divmod(total, 60)
    return f"{minutes:02d}:{remaining:02d}"


def _strip_agent_prefix(source: str, text: str) -> str:
    value = " ".join(text.split())
    for separator in ("·", ":", "："):
        prefix = f"{source} {separator}" if separator == "·" else f"{source}{separator}"
        if value.startswith(prefix):
            return value[len(prefix) :].strip()
    return value


def _event_clock(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "--:--:--"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime(
            "%H:%M:%S"
        )
    except ValueError:
        return value[11:19] if len(value) >= 19 else value


def _format_rate(value: float | None) -> str:
    return "无数据" if value is None else f"{value * 100:.1f}%"


def _extract_file_candidates(text: str) -> list[str]:
    pattern = re.compile(
        r"(?<![\w.-])(?:[\w.-]+/)*[\w.-]+\."
        r"(?:py|pyi|js|jsx|ts|tsx|json|toml|yaml|yml|md|go|rs|java|kt|swift|"
        r"c|h|cpp|hpp|css|scss|html|sql|sh)\b",
        flags=re.IGNORECASE,
    )
    found: list[str] = []
    for match in pattern.finditer(text):
        candidate = match.group(0)
        if candidate not in found:
            found.append(candidate)
    return found


def _render_content_lines(text: str, width: int) -> list[str]:
    raw_lines = text.strip("\n").splitlines() or [""]
    rendered: list[str] = []
    index = 0
    in_code_fence = False
    while index < len(raw_lines):
        line = raw_lines[index].rstrip()
        if line.lstrip().startswith("```"):
            in_code_fence = not in_code_fence
            rendered.extend(_wrap_display(line, width))
            index += 1
            continue
        if (
            not in_code_fence
            and index + 1 < len(raw_lines)
            and _looks_like_table_row(line)
            and _is_table_separator(raw_lines[index + 1])
        ):
            table_lines = [line, raw_lines[index + 1].rstrip()]
            cursor = index + 2
            while cursor < len(raw_lines) and _looks_like_table_row(raw_lines[cursor]):
                table_lines.append(raw_lines[cursor].rstrip())
                cursor += 1
            rendered.extend(_render_markdown_table(table_lines, width))
            index = cursor
            continue

        display_line = line if in_code_fence else _format_markdown_line(line)
        rendered.extend(_wrap_display(display_line, width))
        index += 1
    return rendered or [""]


def _looks_like_table_row(line: str) -> bool:
    cells = _parse_table_row(line)
    return len(cells) >= 2 and "|" in line.replace("\\|", "")


def _is_table_separator(line: str) -> bool:
    cells = _parse_table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


def _parse_table_row(line: str) -> list[str]:
    protected = line.strip().replace("\\|", "\0")
    if protected.startswith("|"):
        protected = protected[1:]
    if protected.endswith("|"):
        protected = protected[:-1]
    return [cell.replace("\0", "|").strip() for cell in protected.split("|")]


def _render_markdown_table(lines: list[str], width: int) -> list[str]:
    header = [_clean_table_cell(cell) for cell in _parse_table_row(lines[0])]
    separators = _parse_table_row(lines[1])
    data_rows = [
        [_clean_table_cell(cell) for cell in _parse_table_row(line)]
        for line in lines[2:]
    ]
    column_count = max([len(header), *(len(row) for row in data_rows)] or [0])
    if column_count < 2:
        return [line for line in lines]
    header += [""] * (column_count - len(header))
    data_rows = [row + [""] * (column_count - len(row)) for row in data_rows]
    alignments = [_separator_alignment(cell) for cell in separators]
    alignments += ["left"] * (column_count - len(alignments))

    if column_count > 6:
        return _render_table_as_records(header, data_rows, width)

    natural_widths = []
    for column in range(column_count):
        values = [header[column], *(row[column] for row in data_rows)]
        natural_widths.append(max(1, min(36, max(_display_width(value) for value in values))))

    content_budget = width - (3 * column_count + 1)
    if content_budget < 2 * column_count:
        return _render_table_as_records(header, data_rows, width)
    column_widths = [max(2, min(value, 24)) for value in natural_widths]
    while sum(column_widths) > content_budget:
        largest = max(range(column_count), key=lambda i: column_widths[i])
        if column_widths[largest] <= 2:
            return _render_table_as_records(header, data_rows, width)
        column_widths[largest] -= 1
    while sum(column_widths) < content_budget:
        candidates = [
            index
            for index in range(column_count)
            if column_widths[index] < natural_widths[index]
        ]
        if not candidates:
            break
        target = max(candidates, key=lambda i: natural_widths[i] - column_widths[i])
        column_widths[target] += 1

    top = "┌" + "┬".join("─" * (value + 2) for value in column_widths) + "┐"
    middle = "├" + "┼".join("─" * (value + 2) for value in column_widths) + "┤"
    bottom = "└" + "┴".join("─" * (value + 2) for value in column_widths) + "┘"
    output = [top]
    output.extend(_render_table_row(header, column_widths, alignments))
    output.append(middle)
    for row_index, row in enumerate(data_rows):
        output.extend(_render_table_row(row, column_widths, alignments))
        if row_index < len(data_rows) - 1:
            output.append(middle)
    output.append(bottom)
    return output


def _render_table_row(
    row: list[str],
    widths: list[int],
    alignments: list[str],
) -> list[str]:
    wrapped = [_wrap_display(cell, widths[index]) for index, cell in enumerate(row)]
    height = max(len(lines) for lines in wrapped)
    output: list[str] = []
    for line_index in range(height):
        cells = []
        for column, lines in enumerate(wrapped):
            value = lines[line_index] if line_index < len(lines) else ""
            cells.append(_align_cell(value, widths[column], alignments[column]))
        output.append("│ " + " │ ".join(cells) + " │")
    return output


def _render_table_as_records(
    header: list[str],
    rows: list[list[str]],
    width: int,
) -> list[str]:
    output = ["表格列数较多，已转换为逐条记录："]
    if not rows:
        rows = [[""] * len(header)]
    for row_index, row in enumerate(rows, start=1):
        output.append(f"[{row_index}]")
        for key, value in zip(header, row):
            label = key or "字段"
            output.extend(_wrap_display(f"  {label}: {value or '—'}", width))
        if row_index < len(rows):
            output.append("  " + "·" * max(3, min(20, width - 2)))
    return output


def _separator_alignment(separator: str) -> str:
    value = separator.replace(" ", "")
    if value.startswith(":") and value.endswith(":"):
        return "center"
    if value.endswith(":"):
        return "right"
    return "left"


def _align_cell(text: str, width: int, alignment: str) -> str:
    padding = max(0, width - _display_width(text))
    if alignment == "right":
        return f"{' ' * padding}{text}"
    if alignment == "center":
        left = padding // 2
        return f"{' ' * left}{text}{' ' * (padding - left)}"
    return f"{text}{' ' * padding}"


def _clean_table_cell(text: str) -> str:
    value = re.sub(r"<br\s*/?>", " / ", text, flags=re.IGNORECASE)
    value = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", value)
    return value.replace("**", "").replace("__", "").replace("`", "").strip()


def _format_markdown_line(line: str) -> str:
    heading = re.match(r"^\s*#{1,6}\s+(.+)$", line)
    if heading:
        line = f"◆ {heading.group(1)}"
    elif line.startswith("> "):
        line = f"▎ {line[2:]}"
    elif re.fullmatch(r"\s*[-*_]{3,}\s*", line):
        return "─" * 24
    line = line.replace("**", "").replace("__", "")
    return line


def _display_width(text: str) -> int:
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def _pad_display(text: str, width: int) -> str:
    clipped = _truncate_display(text, width)
    return clipped + " " * max(0, width - _display_width(clipped))


def _tui_line(text: str, inner_width: int) -> str:
    return f"│ {_pad_display(text, inner_width)} │"


def _tui_top(title: str, width: int) -> str:
    available = max(1, width - 5)
    clipped = _truncate_display(title, available)
    dashes = max(0, width - 5 - _display_width(clipped))
    return f"╭─ {clipped} {'─' * dashes}╮"


def _tui_divider(label: str, width: int) -> str:
    clipped = _truncate_display(label, max(1, width - 5))
    dashes = max(0, width - 5 - _display_width(clipped))
    return f"├─ {clipped} {'─' * dashes}┤"


def _progress_bar(completed: int, total: int, width: int = 12) -> str:
    ratio = min(1.0, max(0.0, completed / total)) if total else 0.0
    filled = int(round(ratio * width))
    return f"[{'█' * filled}{'░' * (width - filled)}]"


def _display_agent_name(value: str) -> str:
    names = {"claude": "Claude", "codex": "Codex", "bridge": "Bridge"}
    return names.get(value.lower(), value or "未分配")


def _wrap_display(text: str, width: int) -> list[str]:
    if not text:
        return [""]
    lines: list[str] = []
    current: list[str] = []
    current_width = 0
    for char in text:
        char_width = 0 if unicodedata.combining(char) else (
            2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        )
        if current and current_width + char_width > width:
            lines.append("".join(current))
            current = []
            current_width = 0
        current.append(char)
        current_width += char_width
    if current:
        lines.append("".join(current))
    return lines


def _truncate_display(text: str, width: int) -> str:
    if _display_width(text) <= width:
        return text
    target = max(1, width - 1)
    current: list[str] = []
    current_width = 0
    for char in text:
        char_width = 0 if unicodedata.combining(char) else (
            2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        )
        if current_width + char_width > target:
            break
        current.append(char)
        current_width += char_width
    return f"{''.join(current)}…"


def _center_display(text: str, width: int) -> str:
    padding = max(0, (width - _display_width(text)) // 2)
    return f"{' ' * padding}{text}"
