from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from . import __version__
from .adapters import ClaudeAdapter, CodexAdapter
from .bridge_config import (
    ConfigError,
    find_config_path,
    load_bridge_config,
    resolve_bridge_settings,
)
from .bridge_models import (
    DEFAULT_AGENT_A_IDENTITY,
    DEFAULT_AGENT_B_IDENTITY,
    DEFAULT_GROUP_CHAT_AGENT_A_IDENTITY,
    DEFAULT_GROUP_CHAT_AGENT_B_IDENTITY,
    AgentEvent,
    BridgeCancelled,
    BridgeError,
    BridgeSettings,
    ConsensusLimitReached,
    PlanDecision,
)
from .bridge_orchestrator import BridgeOrchestrator
from .checkpoints import WorkflowCheckpoint
from .group_chat import GroupChatEngine
from .quality import build_quality_report
from .renderer import ConsoleRenderer
from .run_store import RunStore
from .technical_docs import export_technical_document
from .token_api import TokenAPICredentials
from .workspace_state import current_workspace_fingerprint


VERSION = __version__

TIMELINE_EVENT_KINDS = {
    "phase",
    "lifecycle",
    "checkpoint",
    "verification",
    "verification_result",
    "warning",
    "error",
    "metric",
}
MAX_TIMELINE_EVENTS = 500


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="multiagent",
        description="在同一终端桥接 Claude Code 与 Codex CLI。",
        epilog=(
            "管理命令：multiagent init | doctor | ui | tasks | task | eval | resume"
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
    parser.add_argument(
        "--executor",
        dest="executor",
        choices=("claude", "codex"),
        help="选择本阶段持有写权限的执行协调 Agent",
    )
    parser.add_argument("--rounds", type=int, help="最大审查修订轮数，默认 1")
    parser.add_argument(
        "--mode",
        choices=("workflow", "group-chat"),
        help="协作模式：共识实施 workflow 或可定向执行的群聊 group-chat",
    )
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
        "--no-planning-collaboration",
        action="store_true",
        help="跳过双方独立提案和双向交叉审核",
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
    progress_group = parser.add_mutually_exclusive_group()
    progress_group.add_argument(
        "--progress",
        dest="progress",
        action="store_true",
        default=True,
        help="显示默认安全进度状态（不暴露思考文本和具体命令）",
    )
    progress_group.add_argument(
        "--no-progress",
        dest="progress",
        action="store_false",
        help="关闭等待转圈和安全进度状态",
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
        "--port",
        type=int,
        default=8765,
        help="与 ui 一起使用，本地界面端口（默认 8765）",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="与 ui 一起使用，启动服务但不自动打开浏览器",
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
        return _task_command(args.task[1:], store, renderer)
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
    if special == "ui" and len(args.task) == 1:
        from .ui_server import serve_ui

        return serve_ui(
            workspace=Path(args.workspace).expanduser().resolve(),
            store=store,
            port=args.port,
            open_browser=not args.no_open,
        )

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
        saved_config = _resume_value(resume_record, "resolved_config")
        if isinstance(saved_config, dict):
            data = saved_config
            saved_path = _resume_value(resume_record, "config_path")
            config_path = (
                Path(saved_path).expanduser().resolve()
                if isinstance(saved_path, str) and saved_path
                else None
            )
        else:
            config_path = find_config_path(args.config, args.workspace)
            data = load_bridge_config(config_path)
        settings = resolve_bridge_settings(
            data,
            workspace=args.workspace,
            config_path=config_path,
            executor=(args.executor or _resume_value(resume_record, "executor")),
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
            collaboration_mode=(
                args.mode or _resume_value(resume_record, "collaboration_mode")
            ),
        )
        settings = _apply_resume_settings(settings, resume_record)
        if args.executor is not None:
            settings = replace(settings, executor=args.executor)
        if args.rounds is not None:
            settings = replace(settings, review_rounds=args.rounds)
        if args.consensus is not None:
            settings = replace(settings, consensus=args.consensus)
        if args.mode is not None:
            settings = replace(
                settings,
                collaboration_mode=args.mode.replace("-", "_"),
            )
        if args.no_final_review:
            settings = replace(settings, final_review=False)
        if args.no_planning_collaboration:
            if settings.consensus:
                raise ConfigError("--no-planning-collaboration 不能与共识模式同时使用")
            settings = replace(settings, planning_collaboration=False)
        if args.yes:
            settings = replace(settings, plan_approval=False)
        adapters = _make_adapters(settings)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    if args.check:
        return _check(adapters, settings)

    renderer = ConsoleRenderer(
        color=False if args.plain else None,
        verbose=args.verbose_events,
        progress=args.progress,
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
    if settings.collaboration_mode == "group_chat":
        existing_state = (
            resume_record.get("group_chat")
            if isinstance(resume_record, dict)
            else None
        )
        if special == "resume":
            if not sys.stdin.isatty():
                print("错误：恢复群聊需要交互式终端", file=sys.stderr)
                return 2
            return _interactive_group_chat(
                settings,
                adapters,
                renderer,
                store,
                run_id=resume_id,
                state=existing_state,
            )
        if task:
            result, _run_id, _engine = _run_group_chat_message(
                settings,
                adapters,
                task,
                renderer,
                store,
            )
            return result
        if not sys.stdin.isatty():
            message = sys.stdin.read().strip()
            if not message:
                print("错误：标准输入中没有群聊消息", file=sys.stderr)
                return 2
            result, _run_id, _engine = _run_group_chat_message(
                settings,
                adapters,
                message,
                renderer,
                store,
            )
            return result
        return _interactive_group_chat(settings, adapters, renderer, store)
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


def _make_adapters(
    settings: BridgeSettings,
    *,
    state_root: str | Path | None = None,
):
    claude_environment: dict[str, str] = {}
    codex_environment: dict[str, str] = {}
    token_api_base_url: str | None = None
    if settings.token_api.enabled:
        credential_root = Path(state_root) if state_root is not None else RunStore().root
        api_key = TokenAPICredentials(credential_root).load()
        if not api_key:
            raise ConfigError(
                "已启用 Token API，但尚未保存 API Key；请在 Web 设置的智能体页面填写"
            )
        token_api_base_url = settings.token_api.base_url
        claude_environment = {
            "ANTHROPIC_BASE_URL": token_api_base_url,
            "ANTHROPIC_AUTH_TOKEN": api_key,
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
        }
        codex_environment = {"OPENAI_API_KEY": api_key}
    return {
        "claude": ClaudeAdapter(
            settings.claude,
            environment=claude_environment,
        ),
        "codex": CodexAdapter(
            settings.codex,
            environment=codex_environment,
            token_api_base_url=token_api_base_url,
        ),
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
    print("collaboration: equal (Agent A=claude, Agent B=codex)")
    print(f"collaboration_mode: {settings.collaboration_mode}")
    print(f"executor: {settings.executor}")
    print(f"planning_collaboration: {settings.planning_collaboration}")
    print(f"consensus: {settings.consensus}")
    print(f"max_consensus_rounds: {settings.max_consensus_rounds}")
    print(f"plan_approval: {settings.plan_approval}")
    print(f"review_rounds: {settings.review_rounds}")
    print("write_mode: single-agent-source")
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
    target = workspace / ".multiagent.json"
    if target.exists():
        print(f"配置已存在，未覆盖：{target}")
        return 0
    template = {
        "collaboration_mode": "workflow",
        "executor": "claude",
        "planning_collaboration": True,
        "consensus": False,
        "max_consensus_rounds": 3,
        "plan_approval": True,
        "max_plan_revisions": 2,
        "review_rounds": 1,
        "final_review": True,
        "identities": {
            "agent_a": DEFAULT_AGENT_A_IDENTITY,
            "agent_b": DEFAULT_AGENT_B_IDENTITY,
        },
        "group_chat_identities": {
            "agent_a": DEFAULT_GROUP_CHAT_AGENT_A_IDENTITY,
            "agent_b": DEFAULT_GROUP_CHAT_AGENT_B_IDENTITY,
        },
        "verification": {"timeout": 300, "commands": []},
        "ui": {
            "theme": "paper",
            "show_archived": False,
            "compact_sidebar": False,
        },
        "token_api": {
            "enabled": False,
            "base_url": "https://tokencheap.io",
        },
        "claude": {
            "model": None,
            "models": [],
            "fallback_on_timeout": True,
            "timeout": 900,
            "extra_args": [],
        },
        "codex": {
            "model": None,
            "models": [],
            "fallback_on_timeout": True,
            "timeout": 900,
            "extra_args": [],
        },
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
    print("下一步：按项目填写 verification.commands，然后运行 multiagent doctor")
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
                encoding="utf-8",
                errors="replace",
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
    plan_confirmation: Callable[..., PlanDecision] | None = None,
    event_listener: Callable[[AgentEvent], None] | None = None,
    display_task: str | None = None,
    attachments: list[dict[str, object]] | None = None,
) -> int:
    if (
        settings.planning_collaboration
        and settings.plan_approval
        and plan_confirmation is None
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
                executor=settings.executor,
                consensus=settings.consensus,
                collaboration_mode=settings.collaboration_mode,
                run_id=active_run_id,
                settings_snapshot=_settings_snapshot(settings),
                display_task=display_task,
                attachments=attachments,
            )
            active_run_id = str(record["id"])
        except OSError as exc:
            print(f"警告：无法保存任务记录：{exc}", file=sys.stderr)
            store = None

    checkpoint_state = None
    raw_checkpoint = previous_record.get("checkpoint") if previous_record else None
    if raw_checkpoint is not None:
        checkpoint_state = WorkflowCheckpoint.from_dict(
            raw_checkpoint,
            expected_task=task,
            expected_workspace=settings.workspace,
            expected_executor=settings.executor,
        )
        if checkpoint_state is None:
            print(
                "错误：运行记录中的精确检查点与当前任务、工作区或执行协调 Agent 不兼容。",
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

    def export_document(*, consensus_limit_reached: bool = False) -> Path:
        if checkpoint_state is None:
            raise OSError("当前还没有可导出的完整方案检查点")
        path = export_technical_document(
            workspace=settings.workspace,
            run_id=active_run_id,
            checkpoint=checkpoint_state,
            max_consensus_rounds=settings.max_consensus_rounds,
            consensus_limit_reached=consensus_limit_reached,
        )
        checkpoint_state.workspace_fingerprint = current_workspace_fingerprint(
            settings.workspace
        )
        save_checkpoint(checkpoint_state)
        _update_run(store, active_run_id, technical_document=str(path))
        return path

    def handle_event(event: AgentEvent) -> None:
        _handle_run_event(event, renderer, store, active_run_id)
        if event_listener is None:
            return
        try:
            event_listener(event)
        except Exception:
            # UI/telemetry observers must never interrupt the agent workflow.
            pass

    while True:
        renderer.begin_run(active_run_id)
        confirm_plan = None
        if settings.planning_collaboration and settings.plan_approval:
            def confirm_equal_plan(
                proposal_a,
                proposal_b,
                cross_reviews,
                unified,
                review,
                revision_count,
            ):
                if plan_confirmation is not None:
                    return plan_confirmation(
                        proposal_a,
                        proposal_b,
                        cross_reviews,
                        unified,
                        review,
                        revision_count,
                        on_export=export_document,
                    )
                return _confirm_plan(
                    renderer,
                    proposal_a,
                    proposal_b,
                    cross_reviews,
                    unified,
                    review,
                    revision_count,
                    on_export=export_document,
                )

            confirm_plan = confirm_equal_plan
        try:
            outcome = BridgeOrchestrator(settings, adapters).run(
                task,
                on_event=handle_event,
                confirm_plan=confirm_plan,
                checkpoint=checkpoint_state,
                on_checkpoint=save_checkpoint,
            )
        except KeyboardInterrupt:
            renderer.close()
            _update_run(store, active_run_id, status="interrupted", error="用户中断")
            print("\n已中断。可运行 multiagent resume 恢复任务。", file=sys.stderr)
            return 130
        except BridgeCancelled as exc:
            renderer.close()
            _update_run(store, active_run_id, status="cancelled", error=str(exc))
            print(f"\n已取消：{exc}", file=sys.stderr)
            return 2
        except (BridgeError, RuntimeError, ValueError) as exc:
            renderer.close()
            document_path = None
            document_error = ""
            if isinstance(exc, ConsensusLimitReached):
                try:
                    document_path = export_document(consensus_limit_reached=True)
                except OSError as export_error:
                    document_error = str(export_error)
            failure_changes = {"status": "failed", "error": str(exc)}
            if document_path is not None:
                failure_changes["technical_document"] = str(document_path)
            _update_run(store, active_run_id, **failure_changes)
            if document_path is not None:
                renderer.document_exported(
                    document_path,
                    consensus_incomplete=True,
                )
            elif document_error:
                print(f"警告：技术文档自动导出失败：{document_error}", file=sys.stderr)
            if plan_confirmation is not None or not sys.stdin.isatty():
                print(f"\n错误：{exc}", file=sys.stderr)
                return 1
            renderer.failure_recovery(str(exc))
            action = _failure_action()
            if action == "quit":
                if active_run_id:
                    print(f"任务记录已保存，可运行 multiagent resume {active_run_id}")
                return 1
            if action == "details":
                renderer.set_verbose(True)
            elif action == "swap":
                new_executor = (
                    "codex" if settings.executor == "claude" else "claude"
                )
                settings = replace(settings, executor=new_executor)
                adapters = _make_adapters(settings)
                checkpoint_state = None
                _update_run(store, active_run_id, checkpoint=None, phase="restarted")
                print(
                    f"已切换执行协调 Agent 为 {new_executor}，"
                    "将从安全起点重新执行任务。"
                )
            if store is not None and active_run_id is not None:
                try:
                    store.start(
                        task=task,
                        workspace=settings.workspace,
                        executor=settings.executor,
                        consensus=settings.consensus,
                        collaboration_mode=settings.collaboration_mode,
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
) -> int:
    if len(arguments) != 1:
        print("用法：multiagent task RUN_ID", file=sys.stderr)
        return 2
    run_id = arguments[0]
    record = store.get(run_id)
    if record is None:
        print(f"错误：找不到任务 {run_id}", file=sys.stderr)
        return 2
    renderer.task_detail(record)
    return 0


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
        "collaboration_mode": settings.collaboration_mode,
        "group_chat_default_agent": settings.group_chat_default_agent,
        "group_chat_execution": settings.group_chat_execution,
        "executor": settings.executor,
        "review_rounds": settings.review_rounds,
        "planning_collaboration": settings.planning_collaboration,
        "consensus": settings.consensus,
        "max_consensus_rounds": settings.max_consensus_rounds,
        "plan_approval": settings.plan_approval,
        "max_plan_revisions": settings.max_plan_revisions,
        "final_review": settings.final_review,
        "agent_a_identity": settings.agent_a_identity,
        "agent_b_identity": settings.agent_b_identity,
        "group_chat_agent_a_identity": settings.group_chat_agent_a_identity,
        "group_chat_agent_b_identity": settings.group_chat_agent_b_identity,
        "claude_model": settings.claude.model,
        "codex_model": settings.codex.model,
        "claude_models": list(
            settings.claude.models
            or ((settings.claude.model,) if settings.claude.model else ())
        ),
        "codex_models": list(
            settings.codex.models
            or ((settings.codex.model,) if settings.codex.model else ())
        ),
        "claude_timeout": settings.claude.timeout,
        "codex_timeout": settings.codex.timeout,
        "resolved_config": {
            "collaboration_mode": settings.collaboration_mode,
            "group_chat_default_agent": settings.group_chat_default_agent,
            "group_chat_execution": settings.group_chat_execution,
            "executor": settings.executor,
            "review_rounds": settings.review_rounds,
            "planning_collaboration": settings.planning_collaboration,
            "consensus": settings.consensus,
            "max_consensus_rounds": settings.max_consensus_rounds,
            "plan_approval": settings.plan_approval,
            "max_plan_revisions": settings.max_plan_revisions,
            "final_review": settings.final_review,
            "identities": {
                "agent_a": settings.agent_a_identity,
                "agent_b": settings.agent_b_identity,
            },
            "group_chat_identities": {
                "agent_a": settings.group_chat_agent_a_identity,
                "agent_b": settings.group_chat_agent_b_identity,
            },
            "token_api": {
                "enabled": settings.token_api.enabled,
                "base_url": settings.token_api.base_url,
            },
            "verification": {
                "commands": [
                    {
                        "name": command.name,
                        "command": list(command.command),
                        "timeout": command.timeout,
                    }
                    for command in settings.verification_commands
                ]
            },
            "claude": {
                "command": list(settings.claude.command),
                "model": settings.claude.model,
                "models": list(
                    settings.claude.models
                    or ((settings.claude.model,) if settings.claude.model else ())
                ),
                "fallback_on_timeout": settings.claude.fallback_on_timeout,
                "timeout": settings.claude.timeout,
                "extra_args": list(settings.claude.extra_args),
            },
            "codex": {
                "command": list(settings.codex.command),
                "model": settings.codex.model,
                "models": list(
                    settings.codex.models
                    or ((settings.codex.model,) if settings.codex.model else ())
                ),
                "fallback_on_timeout": settings.codex.fallback_on_timeout,
                "timeout": settings.codex.timeout,
                "extra_args": list(settings.codex.extra_args),
            },
        },
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
    resolved_config = snapshot.get("resolved_config")
    if isinstance(resolved_config, dict):
        return resolve_bridge_settings(
            resolved_config,
            workspace=settings.workspace,
            config_path=settings.config_path,
        )
    boolean_fields = (
        "planning_collaboration",
        "consensus",
        "plan_approval",
        "final_review",
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
    collaboration_mode = snapshot.get("collaboration_mode")
    if collaboration_mode in {"workflow", "group_chat"}:
        changes["collaboration_mode"] = collaboration_mode
    for name in (
        "agent_a_identity",
        "agent_b_identity",
        "group_chat_agent_a_identity",
        "group_chat_agent_b_identity",
    ):
        value = snapshot.get(name)
        if isinstance(value, str) and value.strip():
            changes[name] = value
    executor = snapshot.get("executor")
    if executor in {"claude", "codex"}:
        changes["executor"] = executor
    claude_model = snapshot.get("claude_model")
    codex_model = snapshot.get("codex_model")
    claude_models = snapshot.get("claude_models")
    codex_models = snapshot.get("codex_models")
    claude_timeout = snapshot.get("claude_timeout")
    codex_timeout = snapshot.get("codex_timeout")
    claude = replace(
        settings.claude,
        model=claude_model if isinstance(claude_model, str) else None,
        models=(
            tuple(claude_models)
            if isinstance(claude_models, list)
            and all(isinstance(value, str) for value in claude_models)
            else ((claude_model,) if isinstance(claude_model, str) else ())
        ),
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
        models=(
            tuple(codex_models)
            if isinstance(codex_models, list)
            and all(isinstance(value, str) for value in codex_models)
            else ((codex_model,) if isinstance(codex_model, str) else ())
        ),
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
    if store is None or not run_id:
        return

    persist_timeline = event.kind in TIMELINE_EVENT_KINDS
    metric = event.metadata if event.kind == "metric" else {}
    if event.kind == "metric" and not metric:
        try:
            parsed = json.loads(event.text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            metric = parsed

    session_id = metric.get("session_id") if isinstance(metric, dict) else None
    persist_session = isinstance(session_id, str) and bool(session_id)
    if not persist_timeline and not persist_session:
        return

    def mutate(record) -> None:
        if persist_timeline:
            timeline = record.get("events")
            if not isinstance(timeline, list):
                timeline = []
            timeline.append(event.to_dict(safe=True))
            record["events"] = timeline[-MAX_TIMELINE_EVENTS:]

        if not persist_session:
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
        record["sessions"] = sessions

    try:
        store.mutate(run_id, mutate)
    except (KeyError, OSError):
        pass


def _confirm_plan(
    renderer: ConsoleRenderer,
    proposal_a,
    proposal_b,
    cross_reviews,
    unified_proposal,
    consensus_review,
    revision_count: int,
    *,
    on_export: Callable[[], Path] | None = None,
) -> PlanDecision:
    renderer.collaboration_confirmation(
        proposal_a,
        proposal_b,
        cross_reviews,
        unified_proposal,
        consensus_review,
        revision_count,
    )
    while True:
        try:
            answer = input("请选择 e、r、t、d 或 c › ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return PlanDecision("cancel")
        if answer in {"e", "execute", "y", "yes", "执行"}:
            return PlanDecision("approve")
        if answer in {"c", "cancel", "n", "no", "取消"}:
            return PlanDecision("cancel")
        if answer in {"d", "doc", "document", "export", "导出"}:
            if on_export is None:
                print("当前没有可用的技术文档导出器。")
                continue
            try:
                path = on_export()
            except OSError as exc:
                print(f"技术文档导出失败：{exc}")
            else:
                renderer.document_exported(path)
            continue
        if answer in {"t", "target", "agent", "定向"}:
            try:
                target = input(
                    "请选择目标 Agent（a=Agent A/Claude，b=Agent B/Codex，回车返回）> "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return PlanDecision("cancel")
            if not target:
                continue
            if target in {"a", "agent a", "agent_a", "claude"}:
                target_agent = "claude"
                target_label = "Agent A / Claude"
            elif target in {"b", "agent b", "agent_b", "codex"}:
                target_agent = "codex"
                target_label = "Agent B / Codex"
            else:
                print("请输入 a 或 b；直接回车可返回实施确认。")
                continue
            try:
                feedback = input(f"请输入给 {target_label} 的要求> ").strip()
            except (EOFError, KeyboardInterrupt):
                return PlanDecision("cancel")
            if not feedback:
                print("定向要求不能为空。")
                continue
            return PlanDecision("targeted_revision", feedback, target_agent)
        if answer in {"r", "revise", "修订"}:
            try:
                feedback = input("请输入方案修订要求> ").strip()
            except (EOFError, KeyboardInterrupt):
                return PlanDecision("cancel")
            return PlanDecision("revise", feedback)
        print("请输入 e、r、t、d 或 c。")


def _run_group_chat_message(
    settings: BridgeSettings,
    adapters,
    message: str,
    renderer: ConsoleRenderer,
    store: RunStore,
    *,
    run_id: str | None = None,
    engine: GroupChatEngine | None = None,
) -> tuple[int, str | None, GroupChatEngine]:
    existing = store.get(run_id) if run_id else None
    chat = engine or GroupChatEngine(
        settings,
        adapters,
        existing.get("group_chat") if isinstance(existing, dict) else None,
    )
    active_run_id = run_id
    try:
        if active_run_id is None:
            record = store.start(
                task=message,
                display_task=message,
                workspace=settings.workspace,
                executor=settings.executor,
                consensus=False,
                collaboration_mode="group_chat",
                settings_snapshot=_settings_snapshot(settings),
            )
            active_run_id = str(record["id"])
        else:
            store.update(active_run_id, status="running", error="")
    except OSError as exc:
        print(f"错误：无法保存群聊记录：{exc}", file=sys.stderr)
        return 1, active_run_id, chat

    def save_state(state: dict[str, object]) -> None:
        _update_run(
            store,
            active_run_id,
            status="running",
            collaboration_mode="group_chat",
            group_chat=state,
        )

    def handle_event(event: AgentEvent) -> None:
        _handle_run_event(event, renderer, store, active_run_id)

    try:
        turn = chat.ask(
            message,
            on_event=handle_event,
            on_state=save_state,
        )
    except KeyboardInterrupt:
        renderer.close()
        _update_run(
            store,
            active_run_id,
            status="interrupted",
            error="用户中断",
            group_chat=chat.to_dict(),
        )
        print("\n群聊已中断，记录已保存。", file=sys.stderr)
        return 130, active_run_id, chat
    except BridgeError as exc:
        renderer.close()
        _update_run(
            store,
            active_run_id,
            status="failed",
            error=str(exc),
            group_chat=chat.to_dict(),
        )
        print(f"错误：{exc}", file=sys.stderr)
        return 1, active_run_id, chat

    renderer.close()
    for result in turn.responses:
        label = "目标工作区执行结果" if turn.action == "execute" else "群聊回复"
        print(f"\n{result.agent} · {label}")
        print(result.final_text.rstrip())
        agent_key = next(
            (
                name
                for name, adapter in adapters.items()
                if adapter.display_name == result.agent
            ),
            result.agent.strip().lower(),
        )
        if agent_key in turn.workspaces:
            print(f"workspace: {turn.workspaces[agent_key]}")
    for agent, error in turn.errors.items():
        print(f"\n{agent} 回复失败：{error}", file=sys.stderr)
    state = chat.to_dict()
    summary = _group_chat_cli_summary(state)
    error_text = "；".join(f"{agent}: {error}" for agent, error in turn.errors.items())
    _update_run(
        store,
        active_run_id,
        status="ready" if turn.responses else "failed",
        error=error_text,
        group_chat=state,
        summary=summary,
    )
    return (1 if turn.errors else 0), active_run_id, chat


def _group_chat_cli_summary(state: dict[str, object]) -> dict[str, object]:
    raw_messages = state.get("messages")
    messages = raw_messages if isinstance(raw_messages, list) else []
    replies = [
        item
        for item in messages
        if isinstance(item, dict) and item.get("role") == "assistant"
    ]
    return {
        "turns": sum(
            1
            for item in messages
            if isinstance(item, dict) and item.get("role") == "user"
        ),
        "messages": len(messages),
        "elapsed_seconds": sum(
            float(item.get("duration_seconds", 0) or 0) for item in replies
        ),
        "input_tokens": sum(int(item.get("input_tokens", 0) or 0) for item in replies),
        "output_tokens": sum(
            int(item.get("output_tokens", 0) or 0) for item in replies
        ),
        "execution_turns": sum(
            1
            for item in messages
            if isinstance(item, dict)
            and item.get("role") == "user"
            and item.get("action") == "execute"
        ),
    }


def _interactive_group_chat(
    settings: BridgeSettings,
    adapters,
    renderer: ConsoleRenderer,
    store: RunStore,
    *,
    run_id: str | None = None,
    state: object = None,
) -> int:
    engine = GroupChatEngine(settings, adapters, state)
    active_run_id = run_id
    renderer.clear_screen()
    print("MultiAgent 群聊 · Claude + Codex")
    print("未 @ 时按默认响应者回答；@谁由谁响应；执行时只允许一个 Agent 写目标工作区。")
    print("示例：@Claude 执行：修复测试；/exit 退出。")
    if active_run_id:
        print(f"已继续群聊：{active_run_id}")
    while True:
        try:
            message = input("\n群聊 › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not message:
            continue
        if message.lower() in {"/exit", "/quit", "exit", "quit"}:
            return 0
        if message.lower() == "/help":
            print("@Claude 只让 Claude 回答")
            print("@Codex 只让 Codex 回答")
            print("@all 或不写 @ 让双方并行回答")
            print("@Claude 执行：... 让 Claude 独占目标工作区执行")
            print("@Codex 执行：... 让 Codex 独占目标工作区执行")
            print("@all 执行会被拒绝，以防两个 Agent 同时写代码")
            print("也可使用 /exec @Claude ... 或 /exec @Codex ...")
            print("普通消息保持只读；所有消息都会进入双方后续共享上下文。")
            continue
        _result, active_run_id, engine = _run_group_chat_message(
            settings,
            adapters,
            message,
            renderer,
            store,
            run_id=active_run_id,
            engine=engine,
        )


def _interactive(
    settings,
    adapters,
    renderer: ConsoleRenderer,
    store: RunStore,
) -> int:
    current = settings
    last_task: str | None = None
    renderer.clear_screen()
    renderer.welcome(
        version=VERSION,
        workspace=str(current.workspace),
        executor=current.executor,
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
            print("/executor claude|codex   切换执行协调 Agent")
            print("/consensus on|off        开启或关闭方案自动协商")
            print("/progress on|off         显示或隐藏安全进度状态")
            print("/details on|off          显示或隐藏内部执行详情")
            print("/rounds N                设置代码审查轮数")
            print("/model claude|codex NAME 设置模型，NAME 可为 default")
            print("/timeout SECONDS         设置两个 Agent 的超时")
            print("/history                 查看任务历史")
            print("/tasks                   查看任务中心与当前阶段")
            print("/task RUN_ID             查看共享任务与争议")
            print("/eval                    查看历史质量评测")
            print("/resume [run-id]         恢复历史任务")
            print("/retry                   重新运行上一项需求")
            print("/doctor                  检查 CLI、认证与工作区")
            print("/status                  查看当前配置")
            print("/exit                    退出")
            continue
        if lowered == "/status":
            print(f"workspace={current.workspace}")
            print(
                f"executor={current.executor}, collaboration=equal, "
                f"planning_collaboration={current.planning_collaboration}, "
                f"consensus={current.consensus}, "
                f"plan_approval={current.plan_approval}, "
                f"review_rounds={current.review_rounds}"
            )
            print(
                f"claude_model={current.claude.model or 'default'}, "
                f"codex_model={current.codex.model or 'default'}, "
                f"timeout={current.claude.timeout:g}/{current.codex.timeout:g}s, "
                f"write_mode=single-agent-source, tui={renderer.tui}, "
                f"progress={renderer.progress}, details={renderer.verbose}"
            )
            continue
        if lowered.startswith("/progress"):
            parts = lowered.split()
            if len(parts) != 2 or parts[1] not in {"on", "off"}:
                print("用法：/progress on 或 /progress off")
                continue
            renderer.set_progress(parts[1] == "on")
            print(f"安全进度已{'开启' if renderer.progress else '隐藏'}。")
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
            updated_agent = replace(
                getattr(current, agent_name),
                model=model,
                models=(model,) if model else (),
            )
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
        if lowered.startswith("/executor "):
            requested = lowered.split(maxsplit=1)[1]
            if requested not in {"claude", "codex"}:
                print("执行协调 Agent 只能是 claude 或 codex。")
                continue
            current = replace(current, executor=requested)
            print(f"执行协调 Agent 已切换为 {requested}；双方方案权保持对等。")
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
                planning_collaboration=(
                    True if enabled else current.planning_collaboration
                ),
            )
            if enabled:
                print(
                    "对等共识模式已开启；双方将轮换整合与审核统一方案，"
                    f"最多 {current.max_consensus_rounds} 个审核轮次。"
                )
            else:
                print("方案共识模式已关闭。")
            continue
        last_task = task
        _run_once(current, adapters, task, renderer, store=store)


def _interactive_prompt(settings: BridgeSettings) -> str:
    mode = "Claude ⇄ Codex"
    suffix = " · 共识" if settings.consensus else ""
    executor = "Claude" if settings.executor == "claude" else "Codex"
    return f"multiagent [{mode}{suffix} · 执行 {executor}] ›"


if __name__ == "__main__":
    raise SystemExit(main())
