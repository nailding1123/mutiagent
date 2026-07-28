from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from .adapters import ClaudeAdapter, CodexAdapter
from .bridge_config import (
    find_config_path,
    load_bridge_config,
    resolve_bridge_settings,
)
from .bridge_models import (
    DEFAULT_LEAD_IDENTITY,
    DEFAULT_REVIEWER_IDENTITY,
    BridgeCancelled,
    BridgeError,
    BridgeSettings,
    PlanDecision,
)
from .bridge_orchestrator import BridgeOrchestrator
from .checkpoints import WorkflowCheckpoint
from .config import ConfigError
from .quality import build_quality_report
from .renderer import ConsoleRenderer
from .run_store import RunStore
from .worktrees import WorktreeManager, WorktreeRecord


VERSION = "1.1.0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mutiagent",
        description="在同一终端桥接 Claude Code 与 Codex CLI。",
        epilog=(
            "管理命令：mutiagent init | doctor | tasks | task | eval | resume"
        ),
    )
    parser.add_argument("task", nargs="*", help="开发任务；省略后进入交互模式")
    parser.add_argument("-c", "--config", help="桥接配置 JSON 路径")
    parser.add_argument(
        "-C",
        "--workspace",
        default=".",
        help="目标工作区，默认当前目录",
    )
    parser.add_argument("--lead", choices=("claude", "codex"), help="主写入 Agent")
    parser.add_argument("--rounds", type=int, help="最大审查修订轮数，默认 1")
    consensus_group = parser.add_mutually_exclusive_group()
    consensus_group.add_argument(
        "--consensus",
        dest="consensus",
        action="store_true",
        default=None,
        help="开启方案自动协商，直到两个 Agent 达成共识",
    )
    consensus_group.add_argument(
        "--no-consensus",
        dest="consensus",
        action="store_false",
        help="关闭方案自动协商（默认）",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="自动确认方案，适合非交互调用",
    )
    parser.add_argument(
        "--no-requirement-review",
        action="store_true",
        help="跳过独立需求解析与主方案预审",
    )
    parser.add_argument(
        "--no-final-review",
        action="store_true",
        help="最后一次修订后不再追加最终审查",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查 Claude/Codex CLI 路径和版本，不调用模型",
    )
    parser.add_argument(
        "--probe-models",
        action="store_true",
        help="与 doctor 一起使用，实际发起最小请求验证模型可用性",
    )
    parser.add_argument("--plain", action="store_true", help="禁用彩色输出")
    parser.add_argument(
        "--verbose-events",
        "--show-details",
        action="store_true",
        help="显示默认隐藏的中间文本、工具命令和原生日志",
    )
    worktree_group = parser.add_mutually_exclusive_group()
    worktree_group.add_argument(
        "--worktree",
        dest="worktree",
        action="store_true",
        default=None,
        help="在独立 Git worktree 中运行任务",
    )
    worktree_group.add_argument(
        "--no-worktree",
        dest="worktree",
        action="store_false",
        help="直接在目标工作区运行任务",
    )
    tui_group = parser.add_mutually_exclusive_group()
    tui_group.add_argument(
        "--tui",
        dest="tui",
        action="store_true",
        default=None,
        help="启用固定终端状态面板",
    )
    tui_group.add_argument(
        "--no-tui",
        dest="tui",
        action="store_false",
        help="使用传统滚动输出",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="与 eval 一起使用，输出机器可读 JSON",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="与 task discard 一起使用，确认放弃 worktree 改动",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = RunStore()
    special = args.task[0].lower() if args.task else ""
    if special == "history" and len(args.task) == 1:
        renderer = ConsoleRenderer(color=False if args.plain else None)
        renderer.history(store.list())
        return 0
    if special == "tasks" and len(args.task) == 1:
        renderer = ConsoleRenderer(color=False if args.plain else None)
        renderer.tasks(store.list())
        return 0
    if special == "task":
        renderer = ConsoleRenderer(color=False if args.plain else None)
        return _task_command(args.task[1:], store, renderer, force=args.force)
    if special == "eval" and len(args.task) == 1:
        report = build_quality_report(store.list(limit=10_000))
        if args.json:
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        else:
            renderer = ConsoleRenderer(color=False if args.plain else None)
            renderer.quality_report(report)
        return 0
    if special == "init" and len(args.task) == 1:
        return _init_config(Path(args.workspace).expanduser().resolve())

    resume_id = None
    resumed_task = None
    resume_record = None
    if special == "resume" and len(args.task) <= 2:
        resume_id = args.task[1] if len(args.task) > 1 else None
        record = store.get(resume_id) if resume_id else store.latest()
        if record is None:
            print("错误：没有找到可恢复的任务记录。", file=sys.stderr)
            return 2
        resume_id = str(record.get("id", ""))
        resume_record = record
        resumed_task = str(record.get("task", "")).strip()
        args.workspace = str(record.get("workspace", args.workspace))
        saved_settings = record.get("settings")
        if isinstance(saved_settings, dict) and args.config is None:
            saved_config = saved_settings.get("config_path")
            if isinstance(saved_config, str) and saved_config:
                args.config = saved_config
        if not resumed_task:
            print("错误：运行记录中没有可恢复的任务。", file=sys.stderr)
            return 2
    try:
        config_path = find_config_path(args.config, args.workspace)
        data = load_bridge_config(config_path)
        settings = resolve_bridge_settings(
            data,
            workspace=args.workspace,
            config_path=config_path,
            lead=args.lead or _resume_value(resume_record, "lead"),
            review_rounds=(
                args.rounds
                if args.rounds is not None
                else _resume_value(resume_record, "review_rounds")
            ),
            consensus=(
                args.consensus
                if args.consensus is not None
                else _resume_value(resume_record, "consensus")
            ),
        )
        settings = _apply_resume_settings(settings, resume_record)
        if args.lead is not None:
            settings = replace(settings, lead=args.lead)
        if args.rounds is not None:
            settings = replace(settings, review_rounds=args.rounds)
        if args.consensus is not None:
            settings = replace(settings, consensus=args.consensus)
        if args.no_final_review:
            settings = replace(settings, final_review=False)
        if args.no_requirement_review:
            if settings.consensus:
                raise ConfigError("--no-requirement-review 不能与共识模式同时使用")
            settings = replace(settings, requirement_review=False)
        if args.yes:
            settings = replace(settings, plan_approval=False)
        if args.worktree is not None:
            settings = replace(settings, worktree=args.worktree)
        adapters = _make_adapters(settings)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    if args.check:
        return _check(adapters, settings)

    renderer = ConsoleRenderer(
        color=False if args.plain else None,
        verbose=args.verbose_events,
        tui=args.tui,
    )
    if special == "doctor" and len(args.task) == 1:
        return _doctor(
            adapters,
            settings,
            store,
            renderer,
            probe_models=args.probe_models,
        )

    task = resumed_task or " ".join(args.task).strip()
    if task:
        return _run_once(
            settings,
            adapters,
            task,
            renderer,
            store=store,
            run_id=resume_id,
        )

    if not sys.stdin.isatty():
        task = sys.stdin.read().strip()
        if not task:
            print("错误：标准输入中没有任务", file=sys.stderr)
            return 2
        return _run_once(settings, adapters, task, renderer, store=store)

    return _interactive(settings, adapters, renderer, store)


def _make_adapters(settings: BridgeSettings):
    return {
        "claude": ClaudeAdapter(settings.claude),
        "codex": CodexAdapter(settings.codex),
    }


def _check(adapters, settings: BridgeSettings) -> int:
    print(f"workspace: {settings.workspace}")
    if settings.config_path:
        print(f"config: {settings.config_path}")
    else:
        print("config: built-in defaults")
    try:
        for name in ("claude", "codex"):
            adapter = adapters[name]
            command = " ".join(adapter.settings.command)
            print(f"{name}: {command}")
            print(f"  {adapter.version()}")
    except BridgeError as exc:
        print(f"检查失败：{exc}", file=sys.stderr)
        return 1
    print(f"lead: {settings.lead}")
    print(f"requirement_review: {settings.requirement_review}")
    print(f"consensus: {settings.consensus}")
    print(f"max_consensus_rounds: {settings.max_consensus_rounds}")
    print(f"plan_approval: {settings.plan_approval}")
    print(f"review_rounds: {settings.review_rounds}")
    print(f"worktree: {settings.worktree}")
    if settings.verification_commands:
        for check in settings.verification_commands:
            print(f"verification: {check.name} -> {' '.join(check.command)}")
    else:
        print("verification: not configured")
    return 0


def _init_config(workspace: Path) -> int:
    if not workspace.is_dir():
        print(f"错误：工作区不是有效目录：{workspace}", file=sys.stderr)
        return 2
    target = workspace / ".mutiagent.json"
    if target.exists():
        print(f"配置已存在，未覆盖：{target}")
        return 0
    template = {
        "lead": "claude",
        "requirement_review": True,
        "consensus": False,
        "max_consensus_rounds": 3,
        "plan_approval": True,
        "max_plan_revisions": 2,
        "review_rounds": 1,
        "final_review": True,
        "worktree": False,
        "identities": {
            "lead": DEFAULT_LEAD_IDENTITY,
            "reviewer": DEFAULT_REVIEWER_IDENTITY,
        },
        "verification": {"timeout": 300, "commands": []},
        "claude": {"model": None, "timeout": 900, "extra_args": []},
        "codex": {"model": None, "timeout": 900, "extra_args": []},
    }
    try:
        target.write_text(
            json.dumps(template, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"错误：无法创建配置：{exc}", file=sys.stderr)
        return 1
    print(f"已创建配置：{target}")
    print("下一步：按项目填写 verification.commands，然后运行 mutiagent doctor")
    return 0


def _doctor(
    adapters,
    settings: BridgeSettings,
    store: RunStore,
    renderer,
    *,
    probe_models: bool = False,
) -> int:
    checks: list[tuple[bool, str, str]] = []
    checks.append(
        (
            settings.workspace.is_dir() and os.access(settings.workspace, os.W_OK),
            "工作区",
            f"{settings.workspace}（{'可写' if os.access(settings.workspace, os.W_OK) else '不可写'}）",
        )
    )
    if settings.worktree:
        try:
            repository = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=str(settings.workspace),
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            clean = subprocess.run(
                ["git", "status", "--porcelain=v1"],
                cwd=str(settings.workspace),
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            worktree_ok = (
                repository.returncode == 0
                and clean.returncode == 0
                and not clean.stdout.strip()
            )
            detail = (
                repository.stdout.strip()
                if worktree_ok
                else "需要已有提交且无未提交改动的 Git 仓库"
            )
            checks.append((worktree_ok, "worktree 隔离", detail))
        except (OSError, subprocess.SubprocessError) as exc:
            checks.append((False, "worktree 隔离", str(exc)))
    for name in ("claude", "codex"):
        adapter = adapters[name]
        try:
            version = adapter.version()
            checks.append((True, f"{name} CLI", version))
        except BridgeError as exc:
            checks.append((False, f"{name} CLI", str(exc)))
            continue

        auth_args = ("auth", "status") if name == "claude" else ("login", "status")
        try:
            completed = subprocess.run(
                [*adapter.settings.command, *auth_args],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            raw_output = (completed.stdout or completed.stderr).strip()
            detail = _auth_summary(name, raw_output, completed.returncode == 0)
            checks.append((completed.returncode == 0, f"{name} 认证", detail))
        except (OSError, subprocess.SubprocessError) as exc:
            checks.append((False, f"{name} 认证", str(exc)))

        model = adapter.settings.model or "沿用原生 CLI 默认模型"
        if probe_models:
            try:
                result = adapter.run(
                    "这是环境诊断请求。不要读取或修改文件，只回复 OK。",
                    workspace=settings.workspace,
                    mode="read",
                )
                checks.append(
                    (bool(result.final_text.strip()), f"{name} 模型探测", model)
                )
            except BridgeError as exc:
                checks.append((False, f"{name} 模型探测", str(exc)))
        else:
            checks.append((True, f"{name} 模型", f"{model}（未发起计费探测）"))

    try:
        store.root.mkdir(parents=True, exist_ok=True)
        state_ok = os.access(store.root, os.W_OK)
        checks.append((state_ok, "任务记录目录", str(store.root)))
    except OSError as exc:
        checks.append((False, "任务记录目录", str(exc)))

    renderer.diagnostics(checks)
    return 0 if all(passed for passed, _, _ in checks) else 1


def _auth_summary(name: str, output: str, succeeded: bool) -> str:
    if name == "claude":
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and data.get("loggedIn") is True:
            method = str(data.get("authMethod") or "已配置认证")
            provider = str(data.get("apiProvider") or "")
            return " · ".join(part for part in ("已登录", method, provider) if part)

    login_line = next(
        (line.strip() for line in output.splitlines() if "logged in" in line.lower()),
        "",
    )
    if login_line:
        login_line = re.sub(r"\s+-\s+.*$", "", login_line)
        return _redact_secrets(login_line)
    if succeeded:
        return "认证命令执行成功"
    meaningful = next(
        (
            line.strip()
            for line in output.splitlines()
            if line.strip() and not line.lstrip().upper().startswith("WARNING:")
        ),
        "认证检查失败",
    )
    return _redact_secrets(meaningful)


def _redact_secrets(text: str) -> str:
    return re.sub(r"\b(sk-[A-Za-z0-9_*\-]+)", "sk-***", text)


def _run_once(
    settings,
    adapters,
    task: str,
    renderer: ConsoleRenderer,
    *,
    store: RunStore | None = None,
    run_id: str | None = None,
) -> int:
    if (
        settings.requirement_review
        and settings.plan_approval
        and not sys.stdin.isatty()
    ):
        print(
            "错误：当前无法交互确认方案；请在终端运行，或添加 --yes 自动确认。",
            file=sys.stderr,
        )
        return 2
    active_run_id = run_id
    previous_record = store.get(run_id) if store is not None and run_id else None
    if store is not None:
        try:
            record = store.start(
                task=task,
                workspace=settings.workspace,
                lead=settings.lead,
                consensus=settings.consensus,
                run_id=active_run_id,
                settings_snapshot=_settings_snapshot(settings),
            )
            active_run_id = str(record["id"])
        except OSError as exc:
            print(f"警告：无法保存任务记录：{exc}", file=sys.stderr)
            store = None

    if settings.worktree and store is None:
        print("错误：worktree 隔离依赖可写的任务状态目录。", file=sys.stderr)
        return 2

    if settings.worktree and store is not None and active_run_id:
        worktree_raw = previous_record.get("worktree") if previous_record else None
        existing_worktree = WorktreeRecord.from_dict(worktree_raw)
        if existing_worktree is not None:
            if not existing_worktree.workspace.is_dir():
                print(
                    f"错误：任务 worktree 已不存在：{existing_worktree.workspace}",
                    file=sys.stderr,
                )
                return 2
            settings = replace(settings, workspace=existing_worktree.workspace)
        elif previous_record is None:
            manager = WorktreeManager(store.root.parent / "worktrees")
            try:
                created = manager.create(settings.workspace, active_run_id)
            except BridgeError as exc:
                _update_run(store, active_run_id, status="failed", error=str(exc))
                print(f"错误：{exc}", file=sys.stderr)
                return 2
            source_workspace = settings.workspace
            settings = replace(settings, workspace=created.workspace)
            _update_run(
                store,
                active_run_id,
                source_workspace=str(source_workspace),
                workspace=str(created.workspace),
                worktree=created.to_dict(),
                settings=_settings_snapshot(settings),
            )

    checkpoint_state = None
    raw_checkpoint = previous_record.get("checkpoint") if previous_record else None
    if raw_checkpoint is not None:
        checkpoint_state = WorkflowCheckpoint.from_dict(
            raw_checkpoint,
            expected_task=task,
            expected_workspace=settings.workspace,
            expected_lead=settings.lead,
        )
        if checkpoint_state is None:
            print(
                "错误：运行记录中的精确检查点与当前任务、工作区或主 Agent 不兼容。",
                file=sys.stderr,
            )
            return 2

    def save_checkpoint(checkpoint: WorkflowCheckpoint) -> None:
        nonlocal checkpoint_state
        checkpoint_state = checkpoint
        _update_run(
            store,
            active_run_id,
            phase=checkpoint.phase,
            checkpoint=checkpoint.to_dict(),
            collaboration=checkpoint.collaboration.to_dict(),
        )

    while True:
        renderer.begin_run(active_run_id)
        confirm_plan = None
        if settings.requirement_review and settings.plan_approval:
            confirm_plan = (
                lambda proposal, analysis, review, revision_count: _confirm_plan(
                    renderer,
                    proposal,
                    analysis,
                    review,
                    revision_count,
                )
            )
        try:
            outcome = BridgeOrchestrator(settings, adapters).run(
                task,
                on_event=lambda event: _handle_run_event(
                    event, renderer, store, active_run_id
                ),
                confirm_plan=confirm_plan,
                checkpoint=checkpoint_state,
                on_checkpoint=save_checkpoint,
            )
        except KeyboardInterrupt:
            renderer.close()
            _update_run(store, active_run_id, status="interrupted", error="用户中断")
            print("\n已中断。可运行 mutiagent resume 恢复任务。", file=sys.stderr)
            return 130
        except BridgeCancelled as exc:
            renderer.close()
            _update_run(store, active_run_id, status="cancelled", error=str(exc))
            print(f"\n已取消：{exc}", file=sys.stderr)
            return 2
        except (BridgeError, RuntimeError, ValueError) as exc:
            renderer.close()
            _update_run(store, active_run_id, status="failed", error=str(exc))
            if not sys.stdin.isatty():
                print(f"\n错误：{exc}", file=sys.stderr)
                return 1
            renderer.failure_recovery(str(exc))
            action = _failure_action()
            if action == "quit":
                if active_run_id:
                    print(f"任务记录已保存，可运行 mutiagent resume {active_run_id}")
                return 1
            if action == "details":
                renderer.set_verbose(True)
            elif action == "swap":
                new_lead = "codex" if settings.lead == "claude" else "claude"
                settings = replace(settings, lead=new_lead)
                adapters = _make_adapters(settings)
                checkpoint_state = None
                _update_run(store, active_run_id, checkpoint=None, phase="restarted")
                print(f"已切换主 Agent 为 {new_lead}，将从安全起点重新执行任务。")
            if store is not None and active_run_id is not None:
                try:
                    store.start(
                        task=task,
                        workspace=settings.workspace,
                        lead=settings.lead,
                        consensus=settings.consensus,
                        run_id=active_run_id,
                        settings_snapshot=_settings_snapshot(settings),
                    )
                except OSError:
                    pass
            continue

        renderer.outcome(outcome)
        run_summary = renderer.summary()
        if (
            previous_record
            and previous_record.get("status") == "complete"
            and checkpoint_state is not None
            and checkpoint_state.phase == "complete"
            and isinstance(previous_record.get("summary"), dict)
        ):
            run_summary = previous_record["summary"]
        _update_run(
            store,
            active_run_id,
            status="complete",
            approved=outcome.approved,
            review_count=len(outcome.reviews),
            summary=run_summary,
            quality=_quality_snapshot(outcome),
            collaboration=(
                outcome.collaboration.to_dict() if outcome.collaboration else None
            ),
        )
        return 0


def _failure_action() -> str:
    while True:
        try:
            answer = input("恢复操作 r、l、d 或 q › ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "quit"
        actions = {
            "r": "retry",
            "retry": "retry",
            "l": "swap",
            "lead": "swap",
            "d": "details",
            "details": "details",
            "q": "quit",
            "quit": "quit",
        }
        if answer in actions:
            return actions[answer]
        print("请输入 r（重试）、l（交换角色）、d（展开详情）或 q（结束）。")


def _quality_snapshot(outcome) -> dict[str, object]:
    findings = {severity: 0 for severity in ("P0", "P1", "P2", "P3")}
    covered: set[str] = set()
    for decision in outcome.review_decisions:
        covered.update(decision.requirements_covered)
        for finding in decision.findings:
            if finding.severity in findings:
                findings[finding.severity] += 1
    collaboration = outcome.collaboration
    return {
        "verification_passed": sum(
            result.passed for result in outcome.verifications
        ),
        "verification_total": len(outcome.verifications),
        "requirements_covered": len(covered),
        "findings": findings,
        "consensus_accepted": bool(collaboration and collaboration.accepted),
        "open_blockers": (
            len(collaboration.blocking_issues) if collaboration else 0
        ),
    }


def _task_command(
    arguments: list[str],
    store: RunStore,
    renderer: ConsoleRenderer,
    *,
    force: bool,
) -> int:
    if not arguments:
        print(
            "用法：mutiagent task RUN_ID | task path|diff|discard RUN_ID",
            file=sys.stderr,
        )
        return 2
    if len(arguments) == 1:
        action = "show"
        run_id = arguments[0]
    elif len(arguments) == 2 and arguments[0] in {
        "show",
        "path",
        "diff",
        "discard",
    }:
        action, run_id = arguments
    else:
        print(
            "用法：mutiagent task RUN_ID | task path|diff|discard RUN_ID",
            file=sys.stderr,
        )
        return 2
    record = store.get(run_id)
    if record is None:
        print(f"错误：找不到任务 {run_id}", file=sys.stderr)
        return 2
    worktree = WorktreeRecord.from_dict(record.get("worktree"))
    if action == "show":
        renderer.task_detail(record)
        return 0
    if action == "path":
        print(worktree.workspace if worktree else record.get("workspace", ""))
        return 0
    if worktree is None:
        print("错误：该任务没有独立 worktree。", file=sys.stderr)
        return 2
    manager = WorktreeManager(store.root.parent / "worktrees")
    if action == "diff":
        try:
            diff = manager.diff(worktree)
        except BridgeError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1
        print(diff, end="" if diff.endswith("\n") or not diff else "\n")
        return 0
    if action == "discard":
        try:
            manager.discard(worktree, force=force)
        except BridgeError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1
        store.update(run_id, status="discarded", worktree_discarded=True)
        print(f"已放弃任务 worktree：{run_id}")
        return 0
    return 2


def _update_run(store: RunStore | None, run_id: str | None, **changes) -> None:
    if store is None or not run_id:
        return
    try:
        store.update(run_id, **changes)
    except (KeyError, OSError):
        pass


def _settings_snapshot(settings: BridgeSettings) -> dict[str, object]:
    return {
        "config_path": str(settings.config_path) if settings.config_path else "",
        "lead": settings.lead,
        "review_rounds": settings.review_rounds,
        "requirement_review": settings.requirement_review,
        "consensus": settings.consensus,
        "max_consensus_rounds": settings.max_consensus_rounds,
        "plan_approval": settings.plan_approval,
        "max_plan_revisions": settings.max_plan_revisions,
        "final_review": settings.final_review,
        "worktree": settings.worktree,
        "lead_identity": settings.lead_identity,
        "reviewer_identity": settings.reviewer_identity,
        "claude_model": settings.claude.model,
        "codex_model": settings.codex.model,
        "claude_timeout": settings.claude.timeout,
        "codex_timeout": settings.codex.timeout,
    }


def _resume_value(record, key: str):
    if not isinstance(record, dict):
        return None
    snapshot = record.get("settings")
    return snapshot.get(key) if isinstance(snapshot, dict) else None


def _apply_resume_settings(settings: BridgeSettings, record) -> BridgeSettings:
    if not isinstance(record, dict):
        return settings
    snapshot = record.get("settings")
    if not isinstance(snapshot, dict):
        return settings
    boolean_fields = (
        "requirement_review",
        "consensus",
        "plan_approval",
        "final_review",
        "worktree",
    )
    integer_fields = (
        "review_rounds",
        "max_consensus_rounds",
        "max_plan_revisions",
    )
    changes = {
        name: snapshot[name]
        for name in boolean_fields
        if isinstance(snapshot.get(name), bool)
    }
    changes.update(
        {
            name: snapshot[name]
            for name in integer_fields
            if isinstance(snapshot.get(name), int)
            and not isinstance(snapshot.get(name), bool)
        }
    )
    for name in ("lead_identity", "reviewer_identity"):
        value = snapshot.get(name)
        if isinstance(value, str) and value.strip():
            changes[name] = value
    lead = snapshot.get("lead")
    if lead in {"claude", "codex"}:
        changes["lead"] = lead
    claude_model = snapshot.get("claude_model")
    codex_model = snapshot.get("codex_model")
    claude_timeout = snapshot.get("claude_timeout")
    codex_timeout = snapshot.get("codex_timeout")
    claude = replace(
        settings.claude,
        model=claude_model if isinstance(claude_model, str) else None,
        timeout=(
            float(claude_timeout)
            if isinstance(claude_timeout, (int, float))
            and not isinstance(claude_timeout, bool)
            else settings.claude.timeout
        ),
    )
    codex = replace(
        settings.codex,
        model=codex_model if isinstance(codex_model, str) else None,
        timeout=(
            float(codex_timeout)
            if isinstance(codex_timeout, (int, float))
            and not isinstance(codex_timeout, bool)
            else settings.codex.timeout
        ),
    )
    return replace(settings, claude=claude, codex=codex, **changes)


def _handle_run_event(event, renderer, store, run_id) -> None:
    renderer.event(event)
    if event.kind != "metric" or store is None or not run_id:
        return
    try:
        metric = json.loads(event.text)
    except json.JSONDecodeError:
        return
    if not isinstance(metric, dict):
        return
    session_id = metric.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    record = store.get(run_id)
    if record is None:
        return
    sessions = record.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    agent_sessions = sessions.get(event.source)
    if not isinstance(agent_sessions, list):
        agent_sessions = []
    if session_id not in agent_sessions:
        agent_sessions.append(session_id)
    sessions[event.source] = agent_sessions
    _update_run(store, run_id, sessions=sessions)


def _confirm_plan(
    renderer: ConsoleRenderer,
    proposal,
    _analysis,
    review,
    revision_count: int,
) -> PlanDecision:
    renderer.plan_confirmation(proposal, review, revision_count)
    while True:
        try:
            answer = input("请选择 e、r 或 c › ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return PlanDecision("cancel")
        if answer in {"e", "execute", "y", "yes", "执行"}:
            return PlanDecision("approve")
        if answer in {"c", "cancel", "n", "no", "取消"}:
            return PlanDecision("cancel")
        if answer in {"r", "revise", "修订"}:
            try:
                feedback = input("请输入方案修订要求> ").strip()
            except (EOFError, KeyboardInterrupt):
                return PlanDecision("cancel")
            return PlanDecision("revise", feedback)
        print("请输入 e、r 或 c。")


def _interactive(
    settings,
    adapters,
    renderer: ConsoleRenderer,
    store: RunStore,
) -> int:
    current = settings
    last_task: str | None = None
    reviewer = "codex" if current.lead == "claude" else "claude"
    renderer.clear_screen()
    renderer.welcome(
        version=VERSION,
        workspace=str(current.workspace),
        lead=current.lead,
        reviewer=reviewer,
        review_rounds=current.review_rounds,
        consensus=current.consensus,
    )

    while True:
        try:
            task = input(f"\n{_interactive_prompt(current)} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not task:
            continue
        lowered = task.lower()
        if lowered in {"/exit", "/quit", "exit", "quit"}:
            return 0
        if lowered == "/help":
            print("/lead claude|codex       切换主 Agent")
            print("/consensus on|off        开启或关闭方案自动协商")
            print("/details on|off          显示或隐藏内部执行详情")
            print("/rounds N                设置代码审查轮数")
            print("/model claude|codex NAME 设置模型，NAME 可为 default")
            print("/timeout SECONDS         设置两个 Agent 的超时")
            print("/history                 查看任务历史")
            print("/tasks                   查看任务中心与当前阶段")
            print("/task RUN_ID             查看共享任务、争议和 worktree")
            print("/eval                    查看历史质量评测")
            print("/worktree on|off         开启或关闭新任务隔离")
            print("/resume [run-id]         恢复历史任务")
            print("/retry                   重新运行上一项需求")
            print("/doctor                  检查 CLI、认证与工作区")
            print("/status                  查看当前配置")
            print("/exit                    退出")
            continue
        if lowered == "/status":
            print(f"workspace={current.workspace}")
            print(
                f"lead={current.lead}, requirement_review={current.requirement_review}, "
                f"consensus={current.consensus}, "
                f"plan_approval={current.plan_approval}, "
                f"review_rounds={current.review_rounds}"
            )
            print(
                f"claude_model={current.claude.model or 'default'}, "
                f"codex_model={current.codex.model or 'default'}, "
                f"timeout={current.claude.timeout:g}/{current.codex.timeout:g}s, "
                f"worktree={current.worktree}, tui={renderer.tui}, "
                f"details={renderer.verbose}"
            )
            continue
        if lowered.startswith("/details"):
            parts = lowered.split()
            if len(parts) != 2 or parts[1] not in {"on", "off"}:
                print("用法：/details on 或 /details off")
                continue
            renderer.set_verbose(parts[1] == "on")
            print(f"执行详情已{'开启' if renderer.verbose else '隐藏'}。")
            continue
        if lowered.startswith("/rounds"):
            parts = lowered.split()
            try:
                rounds = int(parts[1]) if len(parts) == 2 else -1
            except ValueError:
                rounds = -1
            if rounds < 0:
                print("用法：/rounds N，其中 N 是大于或等于 0 的整数")
                continue
            current = replace(current, review_rounds=rounds)
            print(f"代码审查轮数已设置为 {rounds}。")
            continue
        if lowered.startswith("/model"):
            parts = task.split(maxsplit=2)
            if len(parts) != 3 or parts[1].lower() not in {"claude", "codex"}:
                print("用法：/model claude|codex MODEL，使用 default 恢复默认模型")
                continue
            agent_name = parts[1].lower()
            model = None if parts[2].lower() == "default" else parts[2]
            updated_agent = replace(getattr(current, agent_name), model=model)
            current = replace(current, **{agent_name: updated_agent})
            adapters = _make_adapters(current)
            print(f"{agent_name} 模型已设置为 {model or 'default'}。")
            continue
        if lowered.startswith("/timeout"):
            parts = lowered.split()
            try:
                timeout = float(parts[1]) if len(parts) == 2 else 0
            except ValueError:
                timeout = 0
            if timeout <= 0:
                print("用法：/timeout SECONDS，其中 SECONDS 必须为正数")
                continue
            current = replace(
                current,
                claude=replace(current.claude, timeout=timeout),
                codex=replace(current.codex, timeout=timeout),
            )
            adapters = _make_adapters(current)
            print(f"两个 Agent 的单次调用超时已设置为 {timeout:g} 秒。")
            continue
        if lowered == "/history":
            renderer.history(store.list())
            continue
        if lowered == "/tasks":
            renderer.tasks(store.list())
            continue
        if lowered.startswith("/task "):
            run_id = task.split(maxsplit=1)[1].strip()
            record = store.get(run_id)
            if record is None:
                print(f"没有找到任务：{run_id}")
            else:
                renderer.task_detail(record)
            continue
        if lowered == "/eval":
            renderer.quality_report(build_quality_report(store.list(limit=10_000)))
            continue
        if lowered.startswith("/worktree"):
            parts = lowered.split()
            if len(parts) != 2 or parts[1] not in {"on", "off"}:
                print("用法：/worktree on 或 /worktree off")
                continue
            current = replace(current, worktree=parts[1] == "on")
            print(f"新任务 worktree 隔离已{'开启' if current.worktree else '关闭'}。")
            continue
        if lowered.startswith("/resume"):
            parts = task.split(maxsplit=1)
            record = store.get(parts[1]) if len(parts) == 2 else store.latest()
            if record is None:
                print("没有找到可恢复的任务记录。")
                continue
            saved_task = str(record.get("task", "")).strip()
            saved_workspace = Path(str(record.get("workspace", current.workspace)))
            if not saved_task or not saved_workspace.is_dir():
                print("运行记录中的任务或工作区已经无效。")
                continue
            resumed = _apply_resume_settings(
                replace(current, workspace=saved_workspace), record
            )
            last_task = saved_task
            _run_once(
                resumed,
                _make_adapters(resumed),
                saved_task,
                renderer,
                store=store,
                run_id=str(record.get("id", "")),
            )
            continue
        if lowered == "/retry":
            if not last_task:
                print("当前会话还没有可重试的需求。")
                continue
            _run_once(current, adapters, last_task, renderer, store=store)
            continue
        if lowered == "/doctor":
            _doctor(adapters, current, store, renderer)
            continue
        if lowered.startswith("/lead "):
            requested = lowered.split(maxsplit=1)[1]
            if requested not in {"claude", "codex"}:
                print("主 Agent 只能是 claude 或 codex。")
                continue
            current = replace(current, lead=requested)
            print(f"主 Agent 已切换为 {requested}。")
            continue
        if lowered.startswith("/consensus"):
            parts = lowered.split()
            if len(parts) != 2 or parts[1] not in {"on", "off"}:
                print("用法：/consensus on 或 /consensus off")
                continue
            enabled = parts[1] == "on"
            current = replace(
                current,
                consensus=enabled,
                requirement_review=True if enabled else current.requirement_review,
            )
            if enabled:
                print(
                    "方案共识模式已开启；副 Agent 未接受时将自动修订，"
                    f"最多 {current.max_consensus_rounds} 次。"
                )
            else:
                print("方案共识模式已关闭。")
            continue
        last_task = task
        _run_once(current, adapters, task, renderer, store=store)


def _interactive_prompt(settings: BridgeSettings) -> str:
    lead = "Claude" if settings.lead == "claude" else "Codex"
    reviewer = "Codex" if settings.lead == "claude" else "Claude"
    separator = " ⇄ " if settings.consensus else " → "
    mode = separator.join((lead, reviewer))
    suffix = " · 共识" if settings.consensus else ""
    return f"mutiagent [{mode}{suffix}] ›"


if __name__ == "__main__":
    raise SystemExit(main())
