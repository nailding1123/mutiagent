from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
from typing import TextIO

from .bridge_models import AgentEvent, AgentRunResult, BridgeOutcome
from .consensus import (
    CONSENSUS_CRITERIA,
    EVIDENCE_CONSENSUS_PROTOCOL,
    parse_consensus_decision,
)
from .reviews import parse_review_decision


class ConsoleRenderer:
    """Readable, dependency-free terminal renderer for normalized agent events."""

    def __init__(
        self,
        *,
        color: bool | None = None,
        verbose: bool = False,
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
        self.tui = self.stream.isatty() if tui is None else bool(tui)
        self.tui = self.tui and self.stream.isatty() and not verbose
        self.phase_index = 0
        self._activity_lock = threading.Lock()
        self._activity_stop = threading.Event()
        self._activity_thread: threading.Thread | None = None
        self._activity_started = 0.0
        self._activity_label = ""
        self._activity_operations = 0
        self._run_started = 0.0
        self._run_id = ""
        self._agent_calls: dict[str, int] = {}
        self._input_tokens = 0
        self._output_tokens = 0
        self._consensus_revisions = 0
        self._tui_active = False
        self._tui_suspended = False
        self._tui_phase = "正在准备任务"
        self._tui_notice = ""
        self._tui_last_reply = ""
        self._tui_collaboration: dict[str, object] = {}

    def begin_run(self, run_id: str | None = None) -> None:
        self._stop_activity()
        self.phase_index = 0
        self._run_started = time.monotonic()
        self._run_id = run_id or ""
        self._agent_calls = {}
        self._input_tokens = 0
        self._output_tokens = 0
        self._consensus_revisions = 0
        self._tui_suspended = False
        self._tui_phase = "正在准备任务"
        self._tui_notice = ""
        self._tui_last_reply = ""
        self._tui_collaboration = {}
        if self.tui and not self._tui_active:
            self.stream.write("\033[?1049h\033[?25l")
            self.stream.flush()
            self._tui_active = True
            self._draw_tui()

    def set_verbose(self, enabled: bool) -> None:
        self.verbose = enabled
        if enabled:
            self._stop_activity()
            self._leave_tui()

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
        lead: str,
        reviewer: str,
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
            f">_  MutiAgent  v{version}",
            (
                f"{centered_mark}\n\n"
                "Claude Code 与 Codex CLI 已连接\n"
                "────────────────────────────────\n"
                f"工作区      {workspace}\n"
                f"主 Agent    {lead}\n"
                f"副 Agent    {reviewer}\n"
                f"方案共识    {'开启' if consensus else '关闭'}\n"
                f"审查轮数    {review_rounds}\n\n"
                "› 输入开发需求开始协作\n"
                "  /lead codex 可交换角色，/help 查看全部命令"
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
            self.stream.write("\033[?1049h\033[?25l")
            self.stream.flush()
            self._tui_active = True
            self._tui_suspended = False
        if self._tui_active:
            self._tui_event(event)
            return
        if event.kind == "phase":
            self._stop_activity()
            self.phase_index += 1
            if "自动修订" in event.text and "复审" not in event.text:
                self._consensus_revisions += 1
            self._section(f"{self.phase_index:02d}  {event.text}")
            self._start_activity(event.text)
            return
        if event.kind == "text":
            self._stop_activity()
            if self._render_consensus_review(event.source, event.text):
                return
            if self._render_structured_review(event.source, event.text):
                return
            color = "35" if event.source == "Claude" else "34"
            self._panel(f"{event.source} · 回复", event.text, color)
            return
        if event.kind == "progress":
            self._bump_activity()
            if self.verbose and event.text:
                color = "35" if event.source == "Claude" else "34"
                self._panel(f"{event.source} · 中间过程", event.text, color)
            return
        if event.kind == "tool":
            self._bump_activity()
            if self.verbose:
                self._compact_line(
                    "→", f"{event.source}  {self._format_tool(event.text)}", "36"
                )
            return
        if event.kind == "tool_result":
            self._bump_activity()
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
            self._compact_line("▶", event.text, "36")
            return
        if event.kind == "verification_result":
            passed = "PASS" in event.text
            self._compact_line("✓" if passed else "✗", event.text, "32" if passed else "31")
            return
        if event.kind == "warning":
            self._stop_activity()
            self._panel(f"{event.source} · 注意", event.text, "33")
            return
        if event.kind == "error":
            self._stop_activity()
            self._panel(f"{event.source} · 错误", event.text, "31")
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
            lead_source = "Claude" if outcome.lead == "claude" else "Codex"
            lead_color = "35" if outcome.lead == "claude" else "34"
            self._panel(
                f"{lead_source} · 最终回复",
                outcome.lead_result.final_text,
                lead_color,
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
            f"主 Agent          {outcome.lead}",
            f"需求与方案预审    {'完成' if outcome.requirement_analysis else '未启用'}",
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
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "consensus_revisions": self._consensus_revisions,
        }

    def plan_confirmation(
        self,
        proposal: AgentRunResult,
        review: AgentRunResult,
        revision_count: int,
    ) -> None:
        self._stop_activity()
        self._leave_tui(suspend=True)
        files = _extract_file_candidates(proposal.final_text)
        consensus = parse_consensus_decision(review.final_text)
        verdict = "已达成" if consensus.valid and consensus.accepted else "待确认"
        lines = [
            f"方案状态      {verdict}",
            f"人工修订      {revision_count} 次",
        ]
        if files:
            lines.append(f"涉及文件      {len(files)} 个候选")
            lines.extend(f"  · {path}" for path in files[:6])
            if len(files) > 6:
                lines.append(f"  · 另有 {len(files) - 6} 个")
        else:
            lines.append("涉及文件      未从方案中识别，请查看完整方案")
        lines.extend(
            (
                "",
                "[e] 执行当前方案    [r] 提出修订要求    [c] 取消任务",
            )
        )
        self._panel("实施确认", "\n".join(lines), "36")

    def failure_recovery(self, error: str) -> None:
        self._stop_activity()
        self._leave_tui(suspend=True)
        self._panel(
            "任务暂停",
            (
                f"{error}\n\n"
                "[r] 重试当前任务    [l] 交换主副角色后重试\n"
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
        lines.extend(("", "恢复任务：mutiagent resume <run-id>"))
        self._panel("任务历史", "\n".join(lines), "36")

    def tasks(self, records: list[dict[str, object]]) -> None:
        if not records:
            self._panel("任务中心", "暂无任务。", "36")
            return
        symbols = {
            "complete": "✓",
            "running": "◐",
            "failed": "✗",
            "cancelled": "·",
            "interrupted": "!",
            "discarded": "×",
        }
        lines: list[str] = []
        for record in records:
            status = str(record.get("status", "unknown"))
            run_id = str(record.get("id", ""))
            phase = str(record.get("phase", "未开始"))
            workspace = str(record.get("workspace", ""))
            isolated = "worktree" if isinstance(record.get("worktree"), dict) else "direct"
            lines.append(
                f"{symbols.get(status, '·')} {run_id}  {status} · {phase} · {isolated}"
            )
            lines.append(f"    {_truncate_display(str(record.get('task', '')), 58)}")
            lines.append(f"    {_truncate_display(workspace, 58)}")
        lines.extend(
            (
                "",
                "查看：mutiagent task <run-id>",
                "差异：mutiagent task diff <run-id>",
            )
        )
        self._panel("任务中心", "\n".join(lines), "36")

    def task_detail(self, record: dict[str, object]) -> None:
        lines = [
            f"任务 ID       {record.get('id', '')}",
            f"状态          {record.get('status', 'unknown')}",
            f"阶段          {record.get('phase', '未记录')}",
            f"主 Agent      {record.get('lead', '')}",
            f"工作区        {record.get('workspace', '')}",
            f"需求          {record.get('task', '')}",
        ]
        worktree = record.get("worktree")
        if isinstance(worktree, dict):
            lines.extend(
                (
                    f"隔离分支      {worktree.get('branch', '')}",
                    f"基线提交      {worktree.get('base_head', '')}",
                )
            )
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

    def _render_consensus_review(self, source: str, text: str) -> bool:
        if "mutiagent.consensus.v1" not in text and EVIDENCE_CONSENSUS_PROTOCOL not in text:
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
            f"{source} · 方案共识",
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
            if "自动修订" in event.text and "复审" not in event.text:
                self._consensus_revisions += 1
            self._start_activity(event.text)
        elif event.kind == "collaboration":
            try:
                data = json.loads(event.text)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict):
                self._tui_collaboration = data
        elif event.kind == "text":
            self._stop_activity()
            compact = " ".join(event.text.split())
            self._tui_last_reply = f"{event.source}: {compact}"
        elif event.kind in {"progress", "tool", "tool_result"}:
            self._bump_activity()
        elif event.kind in {"verification", "verification_result"}:
            self._tui_notice = event.text
        elif event.kind == "warning":
            self._tui_notice = f"注意：{event.text}"
        elif event.kind == "error":
            self._stop_activity()
            self._tui_notice = f"错误：{event.text}"
        elif event.kind == "metric":
            self._capture_metric(event)
        self._draw_tui()

    def _draw_tui(self) -> None:
        if not self._tui_active:
            return
        elapsed = time.monotonic() - self._run_started if self._run_started else 0
        terminal = shutil.get_terminal_size(fallback=(self.width, 24))
        width = max(48, min(terminal.columns, 120))
        height = max(16, terminal.lines)
        inner = width - 4
        tasks = self._tui_collaboration.get("tasks", [])
        issues = self._tui_collaboration.get("issues", [])
        messages = self._tui_collaboration.get("messages", [])
        if not isinstance(tasks, list):
            tasks = []
        if not isinstance(issues, list):
            issues = []
        if not isinstance(messages, list):
            messages = []

        title = f">_  MutiAgent  ·  {self._run_id or '当前任务'}"
        lines = [
            _tui_top(title, width),
            _tui_line(
                f"阶段 {self.phase_index:02d}  {self._tui_phase}", inner
            ),
            _tui_line(
                f"耗时 {_format_duration(elapsed)}  Agent 调用 {sum(self._agent_calls.values())}  "
                f"Token {self._input_tokens}/{self._output_tokens}",
                inner,
            ),
            _tui_divider("共享任务", width),
        ]
        symbols = {
            "pending": "○",
            "in_progress": "◐",
            "blocked": "!",
            "done": "✓",
            "failed": "✗",
            "skipped": "·",
        }
        available_tasks = max(3, min(len(tasks), height // 3))
        for raw in tasks[:available_tasks]:
            if not isinstance(raw, dict):
                continue
            status = str(raw.get("status", "pending"))
            owner = str(raw.get("owner", ""))
            title = str(raw.get("title", ""))
            content = f"{symbols.get(status, '·')} {owner:<7} {title}"
            lines.append(_tui_line(content, inner))
        if not tasks:
            lines.append(_tui_line("暂无共享任务", inner))

        blockers = [
            issue
            for issue in issues
            if isinstance(issue, dict)
            and issue.get("severity") in {"P0", "P1"}
            and issue.get("status") != "resolved"
        ]
        lines.append(_tui_divider("争议与证据", width))
        if blockers:
            for issue in blockers[:3]:
                content = (
                    f"{issue.get('id', '')} [{issue.get('severity', '')}] "
                    f"{issue.get('problem', '')}"
                )
                lines.append(_tui_line(content, inner))
        else:
            evidence_count = sum(
                len(item.get("evidence", []))
                for item in issues
                if isinstance(item, dict) and isinstance(item.get("evidence", []), list)
            )
            lines.append(
                _tui_line(
                    f"✓ 当前无未解决 P0/P1 · {len(issues)} 项争议 · {evidence_count} 条证据",
                    inner,
                )
            )
        lines.append(_tui_divider("实时状态", width))
        notice = self._tui_notice or (
            f"已交换 {len(messages)} 条结构化消息 · 内部事件 {self._activity_operations}"
        )
        lines.append(_tui_line(notice, inner))
        if self._tui_last_reply and len(lines) < height - 2:
            lines.append(_tui_line(self._tui_last_reply, inner))
        while len(lines) < height - 1:
            lines.append(_tui_line("", inner))
        lines = lines[: height - 1]
        lines.append("╰" + "─" * (width - 2) + "╯")
        with self._activity_lock:
            self.stream.write("\033[H\033[2J" + "\n".join(lines))
            self.stream.flush()

    def _leave_tui(self, *, suspend: bool = False) -> None:
        if not self._tui_active:
            if suspend:
                self._tui_suspended = True
            return
        self._tui_active = False
        self._tui_suspended = suspend
        self.stream.write("\033[?25h\033[?1049l")
        self.stream.flush()

    def _start_activity(self, label: str) -> None:
        if self.verbose or not self.stream.isatty():
            return
        self._activity_label = label
        self._activity_operations = 0
        self._activity_started = time.monotonic()
        self._activity_stop = threading.Event()
        self._activity_thread = threading.Thread(
            target=self._activity_loop,
            name="mutiagent-status",
            daemon=True,
        )
        self._activity_thread.start()

    def _activity_loop(self) -> None:
        frames = "◐◓◑◒"
        index = 0
        while not self._activity_stop.wait(0.12):
            if self._tui_active:
                self._draw_tui()
                index += 1
                continue
            elapsed = int(time.monotonic() - self._activity_started)
            minutes, seconds = divmod(elapsed, 60)
            operations = (
                f" · 已处理 {self._activity_operations} 个内部事件"
                if self._activity_operations
                else ""
            )
            status = (
                f"  {frames[index % len(frames)]} {self._activity_label}"
                f" · {minutes:02d}:{seconds:02d}{operations}"
            )
            with self._activity_lock:
                self.stream.write(f"\r\033[2K{status}")
                self.stream.flush()
            index += 1

    def _bump_activity(self) -> None:
        self._activity_operations += 1

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


def _format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    minutes, remaining = divmod(total, 60)
    if minutes:
        return f"{minutes}分{remaining:02d}秒"
    return f"{remaining}秒"


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
