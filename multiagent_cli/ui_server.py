from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import io
import json
import os
import queue
import re
import secrets
import shutil
import string
import sys
import threading
import time
import unicodedata
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

from . import __version__
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
    AgentRunResult,
    PlanDecision,
)
from .renderer import ConsoleRenderer
from .group_chat import GroupChatEngine
from .run_store import RUN_ID_RE, RunStore
from .token_api import (
    DEFAULT_TOKEN_API_BASE_URL,
    TokenAPICredentials,
    public_model_catalog,
)


MAX_REQUEST_BYTES = 30_000_000
MAX_UPLOAD_FILES = 5
MAX_UPLOAD_FILE_BYTES = 10_000_000
MAX_UPLOAD_TOTAL_BYTES = 20_000_000
MAX_RUN_TITLE_CHARS = 200
MAX_SESSION_EVENTS = 240
MAX_RETAINED_SESSIONS = 50
ACTIVE_STATUSES = {"starting", "running", "awaiting_plan", "stopping"}
UI_THEMES = {"paper", "ocean", "graphite", "botanical"}
PROJECT_CONFIG_NAME = ".multiagent.json"
DOCUMENT_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docx",
    ".html",
    ".json",
    ".md",
    ".odt",
    ".pdf",
    ".ppt",
    ".pptx",
    ".rtf",
    ".txt",
    ".xls",
    ".xlsx",
    ".xml",
    ".yaml",
    ".yml",
}


class UIError(ValueError):
    """A user-readable error returned by the local UI bridge."""


class LocalUIHTTPServer(ThreadingHTTPServer):
    """Threaded loopback server that ignores normal browser disconnect noise."""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: object, client_address: object) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class UISession:
    def __init__(
        self,
        *,
        run_id: str,
        task: str,
        workspace: Path,
        executor: str,
        consensus: bool,
        notify: Callable[[str, str], None],
        collaboration_mode: str = "workflow",
        agent_task: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        group_chat: object = None,
    ) -> None:
        self.id = run_id
        self.task = task
        self.agent_task = agent_task or task
        self.attachments = list(attachments or [])
        self.workspace = workspace
        self.executor = executor
        self.consensus = consensus
        self.collaboration_mode = collaboration_mode
        self.status = "starting"
        self.error = ""
        self.document = ""
        self.exit_code: int | None = None
        self.started_at = _timestamp()
        self.updated_at = self.started_at
        self.events: list[dict[str, Any]] = []
        self.agent_events: dict[str, dict[str, Any]] = {}
        self.plan: dict[str, Any] | None = None
        self.group_chat = dict(group_chat) if isinstance(group_chat, dict) else None
        self._chat_engine: GroupChatEngine | None = None
        self._pending_decision: PlanDecision | None = None
        self._stop_requested = False
        self._stop_handler: Callable[[], None] | None = None
        self._condition = threading.Condition()
        self._notify = notify

    def bind_stop_handler(self, handler: Callable[[], None]) -> None:
        with self._condition:
            self._stop_handler = handler

    def bind_chat_engine(self, engine: GroupChatEngine) -> None:
        with self._condition:
            self._chat_engine = engine
            self.group_chat = engine.to_dict()

    def rename(self, title: str) -> None:
        """Update only the user-facing title, never the original agent task."""

        with self._condition:
            self.task = title
            self.updated_at = _timestamp()

    def chat_engine(self) -> GroupChatEngine:
        with self._condition:
            if self._chat_engine is None:
                raise UIError("群聊会话尚未初始化")
            return self._chat_engine

    def chat_requires_restore(self) -> bool:
        """Return whether stopped adapters must be recreated before another turn."""
        with self._condition:
            return self.collaboration_mode == "group_chat" and self._stop_requested

    def begin_chat_turn(self) -> None:
        with self._condition:
            if self.collaboration_mode != "group_chat":
                raise UIError("当前对话不是群聊协作模式")
            if self.status in ACTIVE_STATUSES and self.status != "starting":
                raise UIError("上一条群聊消息仍在处理中")
            self.status = "running"
            self.error = ""
            self.updated_at = _timestamp()
        self._notify("chat_turn", self.id)

    def finish_chat_turn(
        self,
        *,
        state: dict[str, Any],
        error: str = "",
        status: str = "ready",
    ) -> None:
        with self._condition:
            self.group_chat = state
            self.status = status
            self.error = error
            self.exit_code = 0 if status == "ready" else 1
            self.updated_at = _timestamp()
        self._notify("chat_message", self.id)

    def request_stop(self) -> None:
        with self._condition:
            if self.status not in ACTIVE_STATUSES:
                raise UIError("当前任务已经结束")
            if self._stop_requested:
                return
            self._stop_requested = True
            self.status = "stopping"
            self.updated_at = _timestamp()
            if self.plan is not None:
                self._pending_decision = PlanDecision("interrupt")
                self._condition.notify_all()
            handler = self._stop_handler
        if handler is not None:
            handler()
        self._notify("stopping", self.id)

    def on_event(self, event: AgentEvent) -> None:
        safe = event.to_dict(safe=True)
        with self._condition:
            self.events.append(safe)
            del self.events[:-MAX_SESSION_EVENTS]
            key = _agent_key(event.source)
            if key:
                self.agent_events[key] = safe
            if self.status not in {"awaiting_plan", "stopping"}:
                self.status = "running"
            self.updated_at = _timestamp()
        self._notify("event", self.id)

    def wait_for_plan(
        self,
        proposal_a: AgentRunResult,
        proposal_b: AgentRunResult,
        cross_reviews: tuple[AgentRunResult, ...],
        unified: AgentRunResult,
        review: AgentRunResult | None,
        revision_count: int,
        *,
        on_export: Callable[[], Path],
    ) -> PlanDecision:
        payload = {
            "proposal_a": _result_payload(proposal_a),
            "proposal_b": _result_payload(proposal_b),
            "cross_reviews": [_result_payload(item) for item in cross_reviews],
            "unified_proposal": _result_payload(unified),
            "consensus_review": _result_payload(review) if review else None,
            "revision_count": revision_count,
        }
        with self._condition:
            self.plan = payload
            self.status = "awaiting_plan"
            self.updated_at = _timestamp()
            self._pending_decision = None
        self._notify("plan", self.id)

        while True:
            with self._condition:
                while self._pending_decision is None:
                    self._condition.wait(timeout=15)
                decision = self._pending_decision
                self._pending_decision = None

            if decision.action == "export":
                try:
                    path = on_export()
                except OSError as exc:
                    with self._condition:
                        self.error = f"技术文档导出失败：{exc}"
                        self.updated_at = _timestamp()
                else:
                    with self._condition:
                        self.document = str(path)
                        self.error = ""
                        self.updated_at = _timestamp()
                self._notify("document", self.id)
                continue

            with self._condition:
                self.plan = None
                self.status = (
                    "stopping" if decision.action == "interrupt" else "running"
                )
                self.updated_at = _timestamp()
            self._notify("plan_decision", self.id)
            return decision

    def submit_action(
        self,
        *,
        action: str,
        feedback: str = "",
        target_agent: str = "",
    ) -> None:
        actions = {
            "execute": "approve",
            "approve": "approve",
            "cancel": "cancel",
            "revise": "revise",
            "targeted_revision": "targeted_revision",
            "export": "export",
        }
        resolved = actions.get(action)
        if resolved is None:
            raise UIError(f"未知方案操作：{action}")
        with self._condition:
            if self.status != "awaiting_plan" or self.plan is None:
                raise UIError("当前任务不在方案确认阶段")
            if self._pending_decision is not None:
                raise UIError("上一项方案操作仍在处理中")
            if resolved in {"revise", "targeted_revision"} and not feedback.strip():
                raise UIError("修订要求不能为空")
            if resolved == "targeted_revision" and target_agent not in {
                "claude",
                "codex",
            }:
                raise UIError("定向修订必须选择 Agent A 或 Agent B")
            self._pending_decision = PlanDecision(
                resolved,
                feedback.strip(),
                target_agent,
            )
            self._condition.notify_all()

    def finish(self, exit_code: int, record: dict[str, Any] | None) -> None:
        with self._condition:
            self.exit_code = exit_code
            self.status = str(record.get("status", "failed")) if record else "failed"
            self.error = str(record.get("error", "")) if record else self.error
            self.document = (
                str(record.get("technical_document", "")) if record else self.document
            )
            self.plan = None
            self.updated_at = _timestamp()
        self._notify("finished", self.id)

    def fail(self, error: str) -> None:
        with self._condition:
            self.status = "failed"
            self.error = error
            self.exit_code = 1
            self.plan = None
            self.updated_at = _timestamp()
        self._notify("finished", self.id)

    def to_dict(self) -> dict[str, Any]:
        with self._condition:
            return {
                "id": self.id,
                "task": self.task,
                "attachments": list(self.attachments),
                "workspace": str(self.workspace),
                "executor": self.executor,
                "consensus": self.consensus,
                "collaboration_mode": self.collaboration_mode,
                "status": self.status,
                "error": _safe_public_error(
                    self.error,
                    status=self.status,
                    collaboration_mode=self.collaboration_mode,
                ),
                "document": self.document,
                "exit_code": self.exit_code,
                "started_at": self.started_at,
                "updated_at": self.updated_at,
                "events": list(self.events),
                "agent_events": dict(self.agent_events),
                "plan": dict(self.plan) if self.plan else None,
                "group_chat": dict(self.group_chat) if self.group_chat else None,
            }


class UISessionManager:
    def __init__(self, *, store: RunStore, default_workspace: Path) -> None:
        self.store = store
        self.default_workspace = default_workspace
        self.attachments_root = store.root / "_attachments"
        self._sessions: dict[str, UISession] = {}
        self._lock = threading.RLock()
        self._subscribers: set[queue.Queue[dict[str, str]]] = set()

    def ensure_shutdown_safe(self) -> None:
        """Refuse to exit while this server still owns running agent processes."""

        with self._lock:
            active = [
                session
                for session in self._sessions.values()
                if session.status in ACTIVE_STATUSES
            ]
        if active:
            raise UIError(
                f"仍有 {len(active)} 个任务正在运行；请先停止任务，"
                "等待状态结束后再关闭本地服务"
            )

    def get_settings(
        self,
        workspace_text: str = "",
        *,
        defaults: bool = False,
    ) -> dict[str, Any]:
        workspace = Path(workspace_text or self.default_workspace).expanduser().resolve()
        if not workspace.is_dir():
            raise UIError(f"工作区不是有效目录：{workspace}")
        source_path = None
        data: dict[str, Any] = {}
        if not defaults:
            try:
                project_path = workspace / PROJECT_CONFIG_NAME
                source_path = (
                    project_path
                    if project_path.is_file()
                    else find_config_path(None, workspace)
                )
                data = load_bridge_config(source_path)
            except ConfigError as exc:
                raise UIError(f"配置错误：{exc}") from exc
        save_path = workspace / PROJECT_CONFIG_NAME
        return {
            "workspace": str(workspace),
            "source_path": str(source_path) if source_path else "",
            "save_path": str(save_path),
            "revision": _file_revision(save_path),
            "values": _config_for_ui(data),
            "token_api_credentials": TokenAPICredentials(self.store.root).status(),
            "model_catalog": public_model_catalog(),
        }

    def browse_directories(self, path_text: str = "") -> dict[str, Any]:
        requested = Path(path_text).expanduser() if path_text else self.default_workspace
        try:
            directory = requested.resolve()
        except OSError as exc:
            raise UIError(f"无法读取目录：{exc}") from exc
        if not directory.is_dir():
            raise UIError(f"目录不存在或不可访问：{directory}")

        try:
            children = [child for child in directory.iterdir() if child.is_dir()]
        except OSError as exc:
            raise UIError(f"无法读取目录：{exc}") from exc
        children.sort(key=lambda child: (child.name.startswith("."), child.name.casefold()))
        limit = 500
        shortcuts: list[Path] = [self.default_workspace, Path.home()]
        for record in self.store.list(limit=50):
            value = record.get("workspace")
            if isinstance(value, str) and value:
                candidate = Path(value).expanduser()
                if candidate.is_dir():
                    shortcuts.append(candidate)

        return {
            "path": str(directory),
            "parent": str(directory.parent) if directory.parent != directory else "",
            "directories": [
                {"name": child.name, "path": str(child.resolve())}
                for child in children[:limit]
            ],
            "shortcuts": _unique_directories(shortcuts),
            "roots": _filesystem_roots(),
            "truncated": len(children) > limit,
        }

    def save_settings(self, payload: object) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise UIError("请求正文必须是 JSON 对象")
        workspace = Path(
            _required_text(payload.get("workspace"), "工作区不能为空")
        ).expanduser().resolve()
        if not workspace.is_dir():
            raise UIError(f"工作区不是有效目录：{workspace}")
        raw_values = payload.get("values")
        if not isinstance(raw_values, dict):
            raise UIError("设置内容必须是 JSON 对象")
        token_api_key = _optional_text(payload.get("token_api_key"))
        credentials = TokenAPICredentials(self.store.root)
        if token_api_key:
            try:
                token_api_key = credentials.validate(token_api_key)
            except ValueError as exc:
                raise UIError(str(exc)) from exc
        save_path = workspace / PROJECT_CONFIG_NAME
        expected_revision = _optional_text(payload.get("revision"))
        current_revision = _file_revision(save_path)
        if expected_revision != current_revision:
            raise UIError("配置文件已被其他程序修改，请重新打开设置后再保存")

        config = _config_from_ui(raw_values)
        try:
            resolved_settings = resolve_bridge_settings(
                config,
                workspace=workspace,
                config_path=save_path,
            )
        except ConfigError as exc:
            raise UIError(f"配置错误：{exc}") from exc
        if (
            resolved_settings.token_api.enabled
            and not token_api_key
            and not credentials.load()
        ):
            raise UIError("启用 Token API 前请填写 API Key")

        existing: dict[str, Any] = {}
        if save_path.is_file():
            try:
                existing = load_bridge_config(save_path)
            except ConfigError as exc:
                raise UIError(f"配置错误：{exc}") from exc
        merged = _merge_known_config(existing, config)
        temporary = save_path.with_suffix(".json.tmp")
        try:
            temporary.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            temporary.replace(save_path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise UIError(f"保存设置失败：{exc}") from exc

        if token_api_key:
            try:
                credentials.save(token_api_key)
            except OSError as exc:
                raise UIError(f"保存 Token API Key 失败：{exc}") from exc

        with self._lock:
            self.default_workspace = workspace
        self.publish("settings", "")
        return self.get_settings(str(workspace))

    def save_ui_preferences(self, payload: object) -> dict[str, Any]:
        """Persist interface preferences without committing other form drafts."""

        if not isinstance(payload, dict):
            raise UIError("请求正文必须是 JSON 对象")
        workspace = Path(
            _required_text(payload.get("workspace"), "工作区不能为空")
        ).expanduser().resolve()
        if not workspace.is_dir():
            raise UIError(f"工作区不是有效目录：{workspace}")
        preferences = payload.get("ui")
        if not isinstance(preferences, dict) or not preferences:
            raise UIError("界面偏好必须是非空 JSON 对象")
        allowed = {"theme", "show_archived", "compact_sidebar"}
        if not set(preferences).issubset(allowed):
            raise UIError("界面偏好包含未知字段")
        if "theme" in preferences and preferences["theme"] not in UI_THEMES:
            raise UIError("界面主题必须是 paper、ocean、graphite 或 botanical")
        for key in ("show_archived", "compact_sidebar"):
            if key in preferences and not isinstance(preferences[key], bool):
                raise UIError("界面开关必须是布尔值")

        save_path = workspace / PROJECT_CONFIG_NAME
        try:
            if save_path.is_file():
                merged = dict(load_bridge_config(save_path))
            else:
                source = load_bridge_config(find_config_path(None, workspace))
                public_config = _config_from_ui(_config_for_ui(source))
                merged = _merge_known_config({}, public_config)
        except ConfigError as exc:
            raise UIError(f"配置错误：{exc}") from exc
        raw_ui = merged.get("ui")
        ui = dict(raw_ui) if isinstance(raw_ui, dict) else {}
        ui.update(preferences)
        merged["ui"] = ui

        temporary = save_path.with_suffix(".json.tmp")
        try:
            temporary.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            temporary.replace(save_path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise UIError(f"保存界面偏好失败：{exc}") from exc

        self.publish("settings", "")
        return self.get_settings(str(workspace))

    def save_ui_theme(self, payload: object) -> dict[str, Any]:
        """Accept the first theme-only endpoint used by earlier Web clients."""

        if not isinstance(payload, dict):
            raise UIError("请求正文必须是 JSON 对象")
        return self.save_ui_preferences(
            {
                "workspace": payload.get("workspace"),
                "ui": {"theme": payload.get("theme")},
            }
        )

    def set_default_workspace(self, payload: object) -> dict[str, str]:
        """Select the workspace supplied by a new CLI invocation."""

        if not isinstance(payload, dict):
            raise UIError("请求正文必须是 JSON 对象")
        workspace = Path(
            _required_text(payload.get("workspace"), "工作区不能为空")
        ).expanduser().resolve()
        if not workspace.is_dir():
            raise UIError(f"工作区不是有效目录：{workspace}")
        with self._lock:
            self.default_workspace = workspace
        self.publish("workspace", "")
        return {"workspace": str(workspace)}

    def start_task(self, payload: object) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise UIError("请求正文必须是 JSON 对象")
        resume_id = _optional_text(payload.get("resume_id"))
        if resume_id and RUN_ID_RE.fullmatch(resume_id) is None:
            raise UIError("任务 ID 格式无效")
        previous = self.store.get(resume_id) if resume_id else None
        if resume_id and previous is None:
            raise UIError(f"找不到可恢复的任务：{resume_id}")

        if previous:
            if str(previous.get("collaboration_mode", "workflow")) == "group_chat":
                raise UIError("群聊对话无需恢复，请直接在原对话中继续发送消息")
            if payload.get("attachments"):
                raise UIError("恢复任务时不能追加新文档")
            agent_task = str(previous.get("task", "")).strip()
            task = str(previous.get("display_task") or agent_task).strip()
            attachments = _stored_attachments(previous.get("attachments"))
        else:
            task = _optional_text(payload.get("task"))
            agent_task = task
            attachments = []
        workspace_text = (
            str(previous.get("workspace", ""))
            if previous
            else _optional_text(payload.get("workspace"))
        )
        workspace = Path(workspace_text or self.default_workspace).expanduser().resolve()
        if not workspace.is_dir():
            raise UIError(f"工作区不是有效目录：{workspace}")

        from .cli import (
            _apply_resume_settings,
            _make_adapters,
            _resume_value,
            _settings_snapshot,
        )

        config_value = _optional_text(payload.get("config"))
        if previous and not config_value:
            saved = previous.get("settings")
            if isinstance(saved, dict):
                config_value = _optional_text(saved.get("config_path"))
        try:
            resolved = _resume_value(previous, "resolved_config")
            if isinstance(resolved, dict):
                data = resolved
                config_path = (
                    Path(config_value).expanduser().resolve() if config_value else None
                )
            else:
                config_path = find_config_path(config_value or None, workspace)
                data = load_bridge_config(config_path)
            executor = payload.get("executor")
            if executor not in {None, "", "claude", "codex"}:
                raise UIError("executor 必须是 claude 或 codex")
            consensus = payload.get("consensus")
            if consensus is not None and not isinstance(consensus, bool):
                raise UIError("consensus 必须是布尔值")
            collaboration_mode = payload.get("collaboration_mode")
            if collaboration_mode is not None and collaboration_mode not in {
                "workflow",
                "group_chat",
                "group-chat",
            }:
                raise UIError("collaboration_mode 必须是 workflow 或 group_chat")
            settings = resolve_bridge_settings(
                data,
                workspace=workspace,
                config_path=config_path,
                executor=(
                    str(executor)
                    if executor in {"claude", "codex"}
                    else _resume_value(previous, "executor")
                ),
                consensus=(
                    consensus
                    if isinstance(consensus, bool)
                    else _resume_value(previous, "consensus")
                ),
                collaboration_mode=(
                    str(collaboration_mode)
                    if collaboration_mode
                    else _resume_value(previous, "collaboration_mode")
                ),
            )
            settings = _apply_resume_settings(settings, previous)
        except ConfigError as exc:
            raise UIError(f"配置错误：{exc}") from exc

        if not previous and not task and settings.collaboration_mode != "group_chat":
            raise UIError("任务不能为空")
        if not previous and not task and payload.get("attachments"):
            raise UIError("添加参考文档时必须同时提供第一条群聊消息")
        try:
            adapters = _make_adapters(settings, state_root=self.store.root)
        except ConfigError as exc:
            raise UIError(f"配置错误：{exc}") from exc

        run_id = resume_id or _new_run_id()
        if not previous:
            attachments = _save_uploaded_documents(
                self.attachments_root,
                run_id,
                payload.get("attachments"),
            )
            agent_task = _task_with_attachments(task, attachments)
        display_task = task or "群聊协作"
        session = UISession(
            run_id=run_id,
            task=display_task,
            agent_task=agent_task,
            attachments=attachments,
            workspace=settings.workspace,
            executor=settings.executor,
            consensus=settings.consensus,
            notify=self.publish,
            collaboration_mode=settings.collaboration_mode,
        )

        def stop_adapters() -> None:
            for adapter in adapters.values():
                adapter.request_stop()

        session.bind_stop_handler(stop_adapters)
        if settings.collaboration_mode == "group_chat":
            engine = GroupChatEngine(settings, adapters)
            session.bind_chat_engine(engine)
        try:
            self._reserve_session(session)
        except Exception:
            if not previous:
                _remove_run_attachments(self.attachments_root, run_id)
            raise
        if settings.collaboration_mode == "group_chat":
            try:
                self.store.start(
                    task=agent_task,
                    workspace=settings.workspace,
                    executor=settings.executor,
                    consensus=False,
                    collaboration_mode="group_chat",
                    run_id=run_id,
                    settings_snapshot=_settings_snapshot(settings),
                    display_task=display_task,
                    attachments=attachments,
                )
                self.store.update(
                    run_id,
                    status="running" if task else "ready",
                    group_chat=engine.to_dict(),
                )
            except OSError as exc:
                with self._lock:
                    self._sessions.pop(run_id, None)
                if not previous:
                    _remove_run_attachments(self.attachments_root, run_id)
                raise UIError(f"无法保存群聊记录：{exc}") from exc
            if not task:
                session.finish_chat_turn(state=engine.to_dict())
                self.publish("started", run_id)
                return session.to_dict()
            session.begin_chat_turn()
            worker = threading.Thread(
                target=self._run_group_chat_turn,
                args=(session, task),
                kwargs={"agent_text": agent_task, "attachments": attachments},
                name=f"multiagent-ui-chat-{run_id}",
                daemon=True,
            )
        else:
            worker = threading.Thread(
                target=self._run_session,
                args=(session, settings, adapters),
                name=f"multiagent-ui-{run_id}",
                daemon=True,
            )
        worker.start()
        self.publish("started", run_id)
        return session.to_dict()

    def _run_group_chat_turn(
        self,
        session: UISession,
        message: str,
        *,
        agent_text: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        engine = session.chat_engine()

        def save_state(state: dict[str, Any]) -> None:
            try:
                self.store.update(
                    session.id,
                    status="running",
                    error="",
                    group_chat=state,
                    collaboration_mode="group_chat",
                )
            except (KeyError, OSError):
                pass

        try:
            turn = engine.ask(
                message,
                agent_text=agent_text,
                attachments=attachments,
                on_event=session.on_event,
                on_state=save_state,
            )
        except KeyboardInterrupt:
            state = engine.to_dict()
            try:
                self.store.update(
                    session.id,
                    status="interrupted",
                    error="用户中断",
                    group_chat=state,
                )
            except (KeyError, OSError):
                pass
            session.finish_chat_turn(
                state=state,
                error="用户中断",
                status="interrupted",
            )
            return
        except Exception:
            safe_error = "群聊处理失败"
            state = engine.to_dict()
            try:
                self.store.update(
                    session.id,
                    status="failed",
                    error=safe_error,
                    group_chat=state,
                )
            except (KeyError, OSError):
                pass
            session.finish_chat_turn(
                state=state,
                error=safe_error,
                status="failed",
            )
            return

        state = engine.to_dict()
        error = "；".join(
            f"{_agent_label(agent)}：本轮执行失败"
            for agent in turn.errors
        )
        status = "ready" if turn.responses else "failed"
        summary = _group_chat_summary(state)
        try:
            self.store.update(
                session.id,
                status=status,
                error=error,
                group_chat=state,
                summary=summary,
            )
        except (KeyError, OSError):
            pass
        if turn.responses:
            session.finish_chat_turn(state=state, error=error)
        else:
            session.finish_chat_turn(
                state=state,
                error=error or "所有 Agent 均未返回群聊回复",
                status="failed",
            )

    def send_chat_message(self, run_id: str, payload: object) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise UIError("请求正文必须是 JSON 对象")
        message = _required_text(payload.get("message"), "群聊消息不能为空")
        record = self.store.get(run_id)
        if record is None:
            raise UIError("找不到群聊对话")
        if str(record.get("collaboration_mode", "workflow")) != "group_chat":
            raise UIError("当前对话不是群聊协作模式")
        if bool(record.get("archived")):
            raise UIError("已归档的群聊不能发送消息，请先取消归档")
        session = self.session(run_id)
        if session is not None and session.chat_requires_restore():
            # request_stop() propagates a permanent stop flag into the native
            # CLI adapters. Rebuild them from the persisted chat state instead
            # of reusing an adapter that can never accept another turn.
            with self._lock:
                if self._sessions.get(run_id) is session:
                    self._sessions.pop(run_id, None)
            session = None
        if session is None:
            session = self._restore_group_chat_session(record)
        session.begin_chat_turn()
        worker = threading.Thread(
            target=self._run_group_chat_turn,
            args=(session, message),
            name=f"multiagent-ui-chat-{run_id}",
            daemon=True,
        )
        worker.start()
        self.publish("chat_turn", run_id)
        return session.to_dict()

    def _restore_group_chat_session(self, record: dict[str, Any]) -> UISession:
        from .cli import _apply_resume_settings, _make_adapters

        workspace = Path(str(record.get("workspace", ""))).expanduser().resolve()
        if not workspace.is_dir():
            raise UIError(f"工作区不是有效目录：{workspace}")
        snapshot = record.get("settings")
        resolved = snapshot.get("resolved_config") if isinstance(snapshot, dict) else None
        if not isinstance(resolved, dict):
            raise UIError("群聊记录缺少可恢复的配置快照")
        try:
            settings = resolve_bridge_settings(
                resolved,
                workspace=workspace,
                collaboration_mode="group_chat",
            )
            settings = _apply_resume_settings(settings, record)
            adapters = _make_adapters(settings, state_root=self.store.root)
        except ConfigError as exc:
            raise UIError(f"配置错误：{exc}") from exc
        engine = GroupChatEngine(settings, adapters, record.get("group_chat"))
        session = UISession(
            run_id=str(record["id"]),
            task=str(record.get("display_task") or record.get("task") or "群聊"),
            agent_task=str(record.get("task") or ""),
            attachments=_stored_attachments(record.get("attachments")),
            workspace=settings.workspace,
            executor=settings.executor,
            consensus=False,
            collaboration_mode="group_chat",
            group_chat=engine.to_dict(),
            notify=self.publish,
        )
        session.status = str(record.get("status", "ready"))
        session.error = str(record.get("error", ""))
        session.bind_chat_engine(engine)

        def stop_adapters() -> None:
            for adapter in adapters.values():
                adapter.request_stop()

        session.bind_stop_handler(stop_adapters)
        self._reserve_session(session)
        return session

    def _run_session(self, session: UISession, settings, adapters) -> None:
        from .cli import _run_once

        renderer = ConsoleRenderer(
            color=False,
            stream=io.StringIO(),
            verbose=False,
            progress=False,
            tui=False,
        )
        try:
            result = _run_once(
                settings,
                adapters,
                session.agent_task,
                renderer,
                store=self.store,
                run_id=session.id,
                plan_confirmation=session.wait_for_plan,
                event_listener=session.on_event,
                display_task=session.task,
                attachments=session.attachments,
            )
            session.finish(result, self.store.get(session.id))
        except Exception:
            safe_error = "任务执行失败"
            try:
                self.store.update(session.id, status="failed", error=safe_error)
            except (KeyError, OSError):
                pass
            session.fail(safe_error)

    def submit_action(self, run_id: str, payload: object) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise UIError("请求正文必须是 JSON 对象")
        session = self.session(run_id)
        if session is None:
            raise UIError(f"找不到活动 UI 任务：{run_id}")
        session.submit_action(
            action=_required_text(payload.get("action"), "缺少 action"),
            feedback=_optional_text(payload.get("feedback")),
            target_agent=_optional_text(payload.get("target_agent")),
        )
        self.publish("action", run_id)
        return session.to_dict()

    def stop_task(self, run_id: str) -> dict[str, Any]:
        session = self.session(run_id)
        if session is None:
            raise UIError(
                "该任务不属于当前 UI 服务的活动进程，已不能发送停止信号；"
                "刷新后可从最近检查点恢复"
            )
        session.request_stop()
        return session.to_dict()

    def session(self, run_id: str) -> UISession | None:
        with self._lock:
            return self._sessions.get(run_id)

    def _reserve_session(self, session: UISession) -> None:
        with self._lock:
            # HTTP handlers run concurrently. Check and reserve the workspace in
            # the same critical section so two simultaneous POSTs cannot launch
            # competing writers in one workspace.
            for active in self._sessions.values():
                if (
                    active.status in ACTIVE_STATUSES
                    and active.workspace == session.workspace
                ):
                    raise UIError("该工作区已有正在运行的 UI 任务")
            self._sessions[session.id] = session
            self._prune_sessions_locked()

    def _prune_sessions_locked(self) -> None:
        overflow = len(self._sessions) - MAX_RETAINED_SESSIONS
        if overflow <= 0:
            return
        removable = [
            run_id
            for run_id, session in self._sessions.items()
            if session.status not in ACTIVE_STATUSES
        ]
        for run_id in removable[:overflow]:
            self._sessions.pop(run_id, None)

    def list_runs(self) -> list[dict[str, Any]]:
        records = self.store.list(limit=100)
        by_id = {
            str(record.get("id", "")): _detached_record(_record_summary(record))
            for record in records
        }
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            live = session.to_dict()
            summary = by_id.get(session.id, {})
            summary.update(
                {
                    "id": session.id,
                    "task": session.task,
                    "workspace": str(session.workspace),
                    "executor": session.executor,
                    "consensus": session.consensus,
                    "collaboration_mode": session.collaboration_mode,
                    "status": live["status"],
                    "updated_at": live["updated_at"],
                    "error": live["error"],
                    "live": live["status"] in ACTIVE_STATUSES,
                    "resumable": False,
                }
            )
            by_id[session.id] = summary
        return sorted(
            by_id.values(),
            key=lambda item: str(item.get("updated_at", "")),
            reverse=True,
        )

    def run_detail(self, run_id: str) -> dict[str, Any] | None:
        record = self.store.get(run_id)
        session = self.session(run_id)
        if record is None and session is None:
            return None
        return {
            "record": (
                _detached_record(_public_record(record))
                if record and session is None
                else _public_record(record) if record else None
            ),
            "session": session.to_dict() if session else None,
        }

    def set_archived(self, run_id: str, archived: bool) -> dict[str, Any]:
        """Archive a run that is not owned by a live UI session."""

        session = self.session(run_id)
        record = self.store.get(run_id)
        if record is None:
            raise UIError("找不到任务")
        if session is not None and session.status in ACTIVE_STATUSES:
            raise UIError("运行中的任务不能归档")

        changes: dict[str, Any] = {
            "archived": archived,
            "archived_at": _timestamp() if archived else "",
        }
        if session is None and str(record.get("status", "")) in ACTIVE_STATUSES:
            changes["status"] = "interrupted"
            if not str(record.get("error", "")).strip():
                changes["error"] = "任务已不在当前 UI 服务中运行"

        saved = self.store.update(run_id, **changes)
        self.publish("archive", run_id)
        return _public_record(saved)

    def rename_run(self, run_id: str, value: object) -> dict[str, Any]:
        """Persist a user-facing task title without changing its original prompt."""

        title = re.sub(r"\s+", " ", _required_text(value, "任务名称不能为空"))
        if len(title) > MAX_RUN_TITLE_CHARS:
            raise UIError(f"任务名称不能超过 {MAX_RUN_TITLE_CHARS} 个字符")
        if self.store.get(run_id) is None:
            raise UIError("找不到任务")
        try:
            saved = self.store.update(run_id, display_task=title)
        except (KeyError, OSError) as exc:
            raise UIError(f"重命名任务失败：{exc}") from exc
        session = self.session(run_id)
        if session is not None:
            session.rename(title)
        self.publish("rename", run_id)
        return _public_record(saved)

    def delete_run(self, run_id: str) -> dict[str, Any]:
        """Permanently delete an archived run and its uploaded documents."""

        record = self.store.get(run_id)
        if record is None:
            raise UIError("找不到任务")
        session = self.session(run_id)
        if session is not None and session.status in ACTIVE_STATUSES:
            raise UIError("运行中的任务不能删除")
        if not bool(record.get("archived")):
            raise UIError("只能删除已归档的任务")
        try:
            deleted = self.store.delete(run_id)
        except (KeyError, ValueError, OSError) as exc:
            raise UIError(f"删除任务失败：{exc}") from exc
        with self._lock:
            self._sessions.pop(run_id, None)
        _remove_run_attachments(self.attachments_root, run_id)
        self.publish("delete", run_id)
        return _public_record(deleted)

    def subscribe(self) -> queue.Queue[dict[str, str]]:
        subscriber: queue.Queue[dict[str, str]] = queue.Queue(maxsize=64)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, str]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self, kind: str, run_id: str) -> None:
        message = {"type": kind, "run_id": run_id, "timestamp": _timestamp()}
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(message)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(message)
                except (queue.Empty, queue.Full):
                    pass


def serve_ui(
    *,
    workspace: Path,
    store: RunStore,
    port: int = 8765,
    open_browser: bool = True,
    quiet: bool = False,
) -> int:
    if not workspace.is_dir():
        if not quiet:
            print(f"错误：工作区不是有效目录：{workspace}")
        return 2
    if port < 1 or port > 65535:
        if not quiet:
            print("错误：UI 端口必须在 1 到 65535 之间")
        return 2
    url = f"http://127.0.0.1:{port}/"
    if ui_is_running(url):
        selected = select_ui_workspace(url, workspace)
        if not selected and not quiet:
            print(
                "警告：已有 UI 服务不支持切换默认工作区；"
                "请停止旧服务后重新运行命令。"
            )
        _open_existing_ui(url, open_browser=open_browser, quiet=quiet)
        return 0
    manager = UISessionManager(store=store, default_workspace=workspace)
    static_root = Path(__file__).resolve().parent / "web"
    if not (static_root / "index.html").is_file():
        if not quiet:
            print("错误：MultiAgent UI 静态资源缺失，请重新安装。")
        return 1
    handler = make_request_handler(manager, static_root)
    try:
        server = LocalUIHTTPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE and _wait_for_ui(url):
            select_ui_workspace(url, workspace)
            _open_existing_ui(url, open_browser=open_browser, quiet=quiet)
            return 0
        if not quiet:
            print(f"错误：无法启动 UI 服务：{exc}")
        return 1
    url = f"http://127.0.0.1:{server.server_port}/"
    if not quiet:
        print(f"MultiAgent UI 已启动：{url}")
        print("仅监听本机回环地址；按 Ctrl+C 停止。")
    if open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        if not quiet:
            print("\nMultiAgent UI 已停止。")
    finally:
        server.server_close()
    return 0


def ui_is_running(url: str, *, timeout: float = 0.6) -> bool:
    """Return whether *url* is a compatible, healthy MultiAgent UI."""

    try:
        with urlopen(
            f"{url.rstrip('/')}/api/health",
            timeout=timeout,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("ok") is True


def select_ui_workspace(
    url: str,
    workspace: Path,
    *,
    timeout: float = 1.0,
) -> bool:
    """Tell an already-running compatible UI which workspace launched it."""

    target = workspace.expanduser().resolve()
    if not target.is_dir():
        return False
    request = Request(
        f"{url.rstrip('/')}/api/workspace",
        data=json.dumps({"workspace": str(target)}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("workspace") == str(target)


def _wait_for_ui(url: str, *, attempts: int = 5, delay: float = 0.1) -> bool:
    for attempt in range(attempts):
        if ui_is_running(url):
            return True
        if attempt + 1 < attempts:
            time.sleep(delay)
    return False


def _open_existing_ui(url: str, *, open_browser: bool, quiet: bool) -> None:
    if not quiet:
        print(f"MultiAgent UI 已在运行：{url}")
    if open_browser:
        webbrowser.open(url)


def make_request_handler(manager: UISessionManager, static_root: Path):
    class UIRequestHandler(BaseHTTPRequestHandler):
        server_version = f"MultiAgentUI/{__version__}"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/health":
                self._json(
                    {
                        "ok": True,
                        "version": __version__,
                        "workspace": str(manager.default_workspace),
                    }
                )
                return
            if path == "/api/settings":
                query = parse_qs(parsed.query)
                workspace = query.get("workspace", [""])[0]
                try:
                    self._json(
                        manager.get_settings(
                            workspace,
                            defaults=query.get("defaults", [""])[0] == "1",
                        )
                    )
                except UIError as exc:
                    self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            if path == "/api/directories":
                query = parse_qs(parsed.query)
                try:
                    self._json(
                        manager.browse_directories(query.get("path", [""])[0])
                    )
                except UIError as exc:
                    self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            if path == "/api/runs":
                self._json({"runs": manager.list_runs()})
                return
            if path == "/api/events":
                self._events()
                return
            match = re.fullmatch(r"/api/runs/([A-Za-z0-9._-]+)", path)
            if match:
                detail = manager.run_detail(match.group(1))
                if detail is None:
                    self._error(HTTPStatus.NOT_FOUND, "找不到任务")
                else:
                    self._json(detail)
                return
            self._static(path)

        def do_POST(self) -> None:  # noqa: N802
            if not self._same_origin():
                self._error(HTTPStatus.FORBIDDEN, "拒绝跨站请求")
                return
            try:
                payload = self._request_json()
                path = urlparse(self.path).path
                if path == "/api/settings":
                    settings = manager.save_settings(payload)
                    self._json(settings)
                    return
                if path == "/api/settings/theme":
                    settings = manager.save_ui_theme(payload)
                    self._json(settings)
                    return
                if path == "/api/settings/interface":
                    settings = manager.save_ui_preferences(payload)
                    self._json(settings)
                    return
                if path == "/api/workspace":
                    selected = manager.set_default_workspace(payload)
                    self._json(selected)
                    return
                if path == "/api/tasks":
                    session = manager.start_task(payload)
                    self._json(session, status=HTTPStatus.ACCEPTED)
                    return
                if path == "/api/shutdown":
                    manager.ensure_shutdown_safe()
                    self._json({"ok": True, "message": "本地服务正在关闭"})
                    self.wfile.flush()
                    self.close_connection = True
                    threading.Thread(
                        target=self.server.shutdown,
                        name="multiagent-ui-shutdown",
                        daemon=True,
                    ).start()
                    return
                match = re.fullmatch(
                    r"/api/sessions/([A-Za-z0-9._-]+)/messages",
                    path,
                )
                if match:
                    session = manager.send_chat_message(match.group(1), payload)
                    self._json(session, status=HTTPStatus.ACCEPTED)
                    return
                match = re.fullmatch(
                    r"/api/sessions/([A-Za-z0-9._-]+)/actions",
                    path,
                )
                if match:
                    session = manager.submit_action(match.group(1), payload)
                    self._json(session, status=HTTPStatus.ACCEPTED)
                    return
                match = re.fullmatch(
                    r"/api/sessions/([A-Za-z0-9._-]+)/stop",
                    path,
                )
                if match:
                    session = manager.stop_task(match.group(1))
                    self._json(session, status=HTTPStatus.ACCEPTED)
                    return
                match = re.fullmatch(
                    r"/api/runs/([A-Za-z0-9._-]+)/rename",
                    path,
                )
                if match:
                    if not isinstance(payload, dict):
                        raise UIError("请求正文必须是 JSON 对象")
                    record = manager.rename_run(match.group(1), payload.get("title"))
                    self._json({"record": record})
                    return
                match = re.fullmatch(
                    r"/api/runs/([A-Za-z0-9._-]+)/archive",
                    path,
                )
                if match:
                    if not isinstance(payload, dict) or not isinstance(
                        payload.get("archived"), bool
                    ):
                        raise UIError("archived 必须是布尔值")
                    record = manager.set_archived(
                        match.group(1),
                        payload["archived"],
                    )
                    self._json({"record": record})
                    return
                self._error(HTTPStatus.NOT_FOUND, "接口不存在")
            except UIError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._error(HTTPStatus.BAD_REQUEST, "请求正文不是有效 JSON")

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._same_origin():
                self._error(HTTPStatus.FORBIDDEN, "拒绝跨站请求")
                return
            path = urlparse(self.path).path
            match = re.fullmatch(r"/api/runs/([A-Za-z0-9._-]+)", path)
            if match is None:
                self._error(HTTPStatus.NOT_FOUND, "接口不存在")
                return
            try:
                record = manager.delete_run(match.group(1))
            except UIError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._json({"record": record})

        def _request_json(self) -> object:
            raw_length = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise UIError("Content-Length 无效") from exc
            if length < 1 or length > MAX_REQUEST_BYTES:
                raise UIError("请求正文为空或过大")
            body = self.rfile.read(length).decode("utf-8")
            return json.loads(body)

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            expected = {
                f"http://127.0.0.1:{self.server.server_port}",
                f"http://localhost:{self.server.server_port}",
            }
            return origin in expected

        def _static(self, path: str) -> None:
            requested = "index.html" if path in {"", "/"} else unquote(path.lstrip("/"))
            if requested not in {
                "index.html",
                "app.css",
                "app.js",
                "slark-license.txt",
            }:
                self._error(HTTPStatus.NOT_FOUND, "资源不存在")
                return
            target = static_root / requested
            try:
                content = target.read_bytes()
            except OSError:
                self._error(HTTPStatus.NOT_FOUND, "资源不存在")
                return
            content_type = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".txt": "text/plain; charset=utf-8",
            }.get(target.suffix, "application/octet-stream")
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def _events(self) -> None:
            subscriber = manager.subscribe()
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self.wfile.write(b"event: ready\ndata: {}\n\n")
                self.wfile.flush()
                while True:
                    try:
                        message = subscriber.get(timeout=15)
                    except queue.Empty:
                        data = b": keepalive\n\n"
                    else:
                        encoded = json.dumps(message, ensure_ascii=False).encode("utf-8")
                        data = b"event: update\ndata: " + encoded + b"\n\n"
                    self.wfile.write(data)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                manager.unsubscribe(subscriber)

        def _json(self, payload: object, *, status: int = HTTPStatus.OK) -> None:
            content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._security_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def _error(self, status: int, message: str) -> None:
            self._json({"error": message}, status=status)

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
            )

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return UIRequestHandler


def _record_summary(record: dict[str, Any]) -> dict[str, Any]:
    summary = {
        key: record.get(key)
        for key in (
            "id",
            "task",
            "display_task",
            "attachments",
            "workspace",
            "executor",
            "consensus",
            "collaboration_mode",
            "status",
            "phase",
            "created_at",
            "updated_at",
            "attempts",
            "error",
            "approved",
            "technical_document",
            "archived",
            "archived_at",
        )
    }
    summary["archived"] = bool(record.get("archived", False))
    summary["error"] = _safe_public_error(
        summary.get("error"),
        status=str(summary.get("status", "")),
        collaboration_mode=str(summary.get("collaboration_mode", "workflow")),
    )
    summary["resumable"] = bool(record.get("checkpoint")) and str(
        record.get("status", "")
    ) in {"failed", "interrupted", "cancelled", *ACTIVE_STATUSES}
    return summary


def _detached_record(record: dict[str, Any]) -> dict[str, Any]:
    """Render orphaned active records as interrupted, without mutating history."""

    if str(record.get("status", "")) not in ACTIVE_STATUSES:
        return record
    record["status"] = "interrupted"
    record["detached"] = True
    if not str(record.get("error", "")).strip():
        record["error"] = (
            "任务已不在当前 UI 服务中运行；上次服务可能退出，"
            "可从最近检查点恢复"
        )
    return record


def _save_uploaded_documents(
    attachments_root: Path,
    run_id: str,
    payload: object,
) -> list[dict[str, Any]]:
    if payload is None or payload == "":
        return []
    if not isinstance(payload, list):
        raise UIError("attachments 必须是数组")
    if len(payload) > MAX_UPLOAD_FILES:
        raise UIError(f"每个任务最多上传 {MAX_UPLOAD_FILES} 个文档")
    if not payload:
        return []

    run_directory = attachments_root / run_id
    saved: list[dict[str, Any]] = []
    total_size = 0
    try:
        run_directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        try:
            attachments_root.chmod(0o700)
            run_directory.chmod(0o700)
        except OSError:
            pass
        used_names: set[str] = set()
        for item in payload:
            if not isinstance(item, dict):
                raise UIError("文档信息格式无效")
            name = _safe_document_name(item.get("name"))
            name = _unique_document_name(name, used_names)
            encoded = item.get("data")
            if not isinstance(encoded, str) or not encoded:
                raise UIError(f"文档 {name} 缺少内容")
            try:
                content = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise UIError(f"文档 {name} 内容编码无效") from exc
            declared_size = item.get("size")
            if type(declared_size) is not int or declared_size != len(content):
                raise UIError(f"文档 {name} 大小校验失败")
            if not content:
                raise UIError(f"文档 {name} 不能为空")
            if len(content) > MAX_UPLOAD_FILE_BYTES:
                raise UIError(f"文档 {name} 超过 10 MB")
            total_size += len(content)
            if total_size > MAX_UPLOAD_TOTAL_BYTES:
                raise UIError("文档合计大小不能超过 20 MB")

            target = run_directory / name
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_bytes(content)
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            temporary.replace(target)
            try:
                target.chmod(0o400)
            except OSError:
                pass
            content_type = _optional_text(item.get("content_type"))[:120]
            saved.append(
                {
                    "name": name,
                    "path": str(target.resolve()),
                    "size": len(content),
                    "content_type": content_type or "application/octet-stream",
                }
            )
    except (OSError, UIError) as exc:
        shutil.rmtree(run_directory, ignore_errors=True)
        if isinstance(exc, UIError):
            raise
        raise UIError(f"保存上传文档失败：{exc}") from exc
    return saved


def _safe_document_name(value: object) -> str:
    if not isinstance(value, str):
        raise UIError("文档名称无效")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or normalized != normalized.replace("\\", "/").split("/")[-1]:
        raise UIError("文档名称不安全")
    normalized = "".join(char for char in normalized if char.isprintable()).strip(" .")
    suffix = Path(normalized).suffix.lower()
    if suffix not in DOCUMENT_EXTENSIONS:
        raise UIError(f"不支持的文档格式：{suffix or '无扩展名'}")
    stem = Path(normalized).stem.strip(" .") or "document"
    maximum_stem = max(1, 150 - len(suffix))
    return f"{stem[:maximum_stem]}{suffix}"


def _unique_document_name(name: str, used: set[str]) -> str:
    candidate = name
    index = 2
    while candidate.casefold() in used:
        path = Path(name)
        candidate = f"{path.stem} ({index}){path.suffix}"
        index += 1
    used.add(candidate.casefold())
    return candidate


def _stored_attachments(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    stored: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = _optional_text(item.get("name"))
        path = _optional_text(item.get("path"))
        size = item.get("size")
        if not name or not path or type(size) is not int or size < 0:
            continue
        stored.append(
            {
                "name": name,
                "path": path,
                "size": size,
                "content_type": _optional_text(item.get("content_type"))
                or "application/octet-stream",
            }
        )
    return stored[:MAX_UPLOAD_FILES]


def _task_with_attachments(task: str, attachments: list[dict[str, Any]]) -> str:
    if not attachments:
        return task
    lines = [
        task,
        "",
        "附加文档（由用户随任务上传，请先读取并纳入需求分析；除非需求明确要求，否则不要修改这些原始文档）：",
    ]
    lines.extend(
        f"- {item['name']}：{item['path']}" for item in attachments
    )
    return "\n".join(lines)


def _remove_run_attachments(attachments_root: Path, run_id: str) -> None:
    if RUN_ID_RE.fullmatch(run_id) is None:
        return
    shutil.rmtree(attachments_root / run_id, ignore_errors=True)


def _config_for_ui(data: dict[str, Any]) -> dict[str, Any]:
    identities = data.get("identities")
    if not isinstance(identities, dict):
        identities = {}
    group_chat_identities = data.get("group_chat_identities")
    if not isinstance(group_chat_identities, dict):
        group_chat_identities = {}
    verification = data.get("verification")
    if not isinstance(verification, dict):
        verification = {}
    ui = data.get("ui")
    if not isinstance(ui, dict):
        ui = {}
    token_api = data.get("token_api")
    if not isinstance(token_api, dict):
        token_api = {}

    def boolean(name: str, default: bool) -> bool:
        value = data.get(name, default)
        return value if isinstance(value, bool) else default

    def integer(name: str, default: int) -> int:
        value = data.get(name, default)
        return value if isinstance(value, int) and not isinstance(value, bool) else default

    def agent_values(name: str) -> dict[str, Any]:
        raw = data.get(name)
        if not isinstance(raw, dict):
            raw = {}
        command = raw.get("command", "")
        if not (
            isinstance(command, str)
            or isinstance(command, list)
            and all(isinstance(item, str) for item in command)
        ):
            command = ""
        model = raw.get("model")
        models = raw.get("models")
        if not (
            isinstance(models, list)
            and all(isinstance(item, str) and item.strip() for item in models)
        ):
            models = [model] if isinstance(model, str) and model.strip() else []
        timeout = raw.get("timeout", 900)
        extra_args = raw.get("extra_args", [])
        return {
            "command": command,
            "model": model if isinstance(model, str) else "",
            "models": models,
            "fallback_on_timeout": (
                raw.get("fallback_on_timeout")
                if isinstance(raw.get("fallback_on_timeout"), bool)
                else True
            ),
            "timeout": (
                timeout
                if isinstance(timeout, (int, float))
                and not isinstance(timeout, bool)
                else 900
            ),
            "extra_args": (
                extra_args
                if isinstance(extra_args, list)
                and all(isinstance(item, str) for item in extra_args)
                else []
            ),
        }

    executor = data.get("executor", "claude")
    collaboration_mode = data.get("collaboration_mode", "workflow")
    group_chat_default_agent = data.get("group_chat_default_agent", "both")
    commands = verification.get("commands", [])
    verification_timeout = verification.get("timeout", 300)
    return {
        "executor": executor if executor in {"claude", "codex"} else "claude",
        "collaboration_mode": (
            collaboration_mode
            if collaboration_mode in {"workflow", "group_chat"}
            else "workflow"
        ),
        "group_chat_default_agent": (
            group_chat_default_agent
            if group_chat_default_agent in {"both", "claude", "codex"}
            else "both"
        ),
        "group_chat_execution": boolean("group_chat_execution", True),
        "planning_collaboration": boolean("planning_collaboration", True),
        "consensus": boolean("consensus", False),
        "max_consensus_rounds": integer("max_consensus_rounds", 3),
        "plan_approval": boolean("plan_approval", True),
        "max_plan_revisions": integer("max_plan_revisions", 2),
        "review_rounds": integer("review_rounds", 1),
        "final_review": boolean("final_review", True),
        "identities": {
            "agent_a": (
                identities.get("agent_a")
                if isinstance(identities.get("agent_a"), str)
                else DEFAULT_AGENT_A_IDENTITY
            ),
            "agent_b": (
                identities.get("agent_b")
                if isinstance(identities.get("agent_b"), str)
                else DEFAULT_AGENT_B_IDENTITY
            ),
        },
        "group_chat_identities": {
            "agent_a": (
                group_chat_identities.get("agent_a")
                if isinstance(group_chat_identities.get("agent_a"), str)
                else DEFAULT_GROUP_CHAT_AGENT_A_IDENTITY
            ),
            "agent_b": (
                group_chat_identities.get("agent_b")
                if isinstance(group_chat_identities.get("agent_b"), str)
                else DEFAULT_GROUP_CHAT_AGENT_B_IDENTITY
            ),
        },
        "verification": {
            "timeout": (
                verification_timeout
                if isinstance(verification_timeout, (int, float))
                and not isinstance(verification_timeout, bool)
                else 300
            ),
            "commands": commands if isinstance(commands, list) else [],
        },
        "claude": agent_values("claude"),
        "codex": agent_values("codex"),
        "token_api": {
            "enabled": (
                token_api.get("enabled")
                if isinstance(token_api.get("enabled"), bool)
                else False
            ),
            "base_url": (
                token_api.get("base_url")
                if isinstance(token_api.get("base_url"), str)
                and token_api.get("base_url", "").strip()
                else DEFAULT_TOKEN_API_BASE_URL
            ),
        },
        "ui": {
            "theme": (
                ui.get("theme")
                if ui.get("theme") in UI_THEMES
                else "paper"
            ),
            "show_archived": (
                ui.get("show_archived")
                if isinstance(ui.get("show_archived"), bool)
                else False
            ),
            "compact_sidebar": (
                ui.get("compact_sidebar")
                if isinstance(ui.get("compact_sidebar"), bool)
                else False
            ),
        },
    }


def _config_from_ui(values: dict[str, Any]) -> dict[str, Any]:
    config = {
        key: values.get(key)
        for key in (
            "executor",
            "collaboration_mode",
            "group_chat_default_agent",
            "group_chat_execution",
            "planning_collaboration",
            "consensus",
            "max_consensus_rounds",
            "plan_approval",
            "max_plan_revisions",
            "review_rounds",
            "final_review",
        )
    }
    identities = values.get("identities")
    group_chat_identities = values.get("group_chat_identities")
    verification = values.get("verification")
    ui = values.get("ui")
    token_api = values.get("token_api")
    if not isinstance(identities, dict):
        raise UIError("共识实施身份设置必须是 JSON 对象")
    if group_chat_identities is None:
        group_chat_identities = {
            "agent_a": DEFAULT_GROUP_CHAT_AGENT_A_IDENTITY,
            "agent_b": DEFAULT_GROUP_CHAT_AGENT_B_IDENTITY,
        }
    if not isinstance(group_chat_identities, dict):
        raise UIError("群聊身份设置必须是 JSON 对象")
    if not isinstance(verification, dict):
        raise UIError("验证设置必须是 JSON 对象")
    if not isinstance(ui, dict):
        raise UIError("界面设置必须是 JSON 对象")
    if not isinstance(token_api, dict):
        raise UIError("Token API 设置必须是 JSON 对象")
    if not all(
        isinstance(ui.get(key), bool)
        for key in ("show_archived", "compact_sidebar")
    ):
        raise UIError("界面开关必须是布尔值")
    if ui.get("theme") not in UI_THEMES:
        raise UIError("界面主题必须是 paper、ocean、graphite 或 botanical")
    config["identities"] = {
        "agent_a": identities.get("agent_a"),
        "agent_b": identities.get("agent_b"),
    }
    config["group_chat_identities"] = {
        "agent_a": group_chat_identities.get("agent_a"),
        "agent_b": group_chat_identities.get("agent_b"),
    }
    config["verification"] = {
        "timeout": verification.get("timeout"),
        "commands": verification.get("commands"),
    }
    config["ui"] = {
        "theme": ui["theme"],
        "show_archived": ui["show_archived"],
        "compact_sidebar": ui["compact_sidebar"],
    }
    config["token_api"] = {
        "enabled": token_api.get("enabled"),
        "base_url": token_api.get("base_url"),
    }
    for name in ("claude", "codex"):
        raw = values.get(name)
        if not isinstance(raw, dict):
            raise UIError(f"{name} 设置必须是 JSON 对象")
        models = raw.get("models")
        legacy_model = raw.get("model")
        if models == [] and isinstance(legacy_model, str) and legacy_model.strip():
            models = [legacy_model.strip()]
        agent = {
            "model": legacy_model or None,
            "models": models,
            "fallback_on_timeout": raw.get("fallback_on_timeout"),
            "timeout": raw.get("timeout"),
            "extra_args": raw.get("extra_args"),
        }
        command = raw.get("command")
        if command is not None and command != "":
            agent["command"] = command
        config[name] = agent
    return config


def _merge_known_config(
    existing: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(existing)
    for key in (
        "executor",
        "collaboration_mode",
        "group_chat_default_agent",
        "group_chat_execution",
        "planning_collaboration",
        "consensus",
        "max_consensus_rounds",
        "plan_approval",
        "max_plan_revisions",
        "review_rounds",
        "final_review",
    ):
        merged[key] = config[key]
    for section, known_keys in (
        ("identities", ("agent_a", "agent_b")),
        ("group_chat_identities", ("agent_a", "agent_b")),
        ("verification", ("timeout", "commands")),
        ("token_api", ("enabled", "base_url")),
        (
            "claude",
            ("command", "model", "models", "fallback_on_timeout", "timeout", "extra_args"),
        ),
        (
            "codex",
            ("command", "model", "models", "fallback_on_timeout", "timeout", "extra_args"),
        ),
        ("ui", ("theme", "show_archived", "compact_sidebar")),
    ):
        current = merged.get(section)
        nested = dict(current) if isinstance(current, dict) else {}
        incoming = config[section]
        for key in known_keys:
            if key in incoming:
                nested[key] = incoming[key]
            else:
                nested.pop(key, None)
        merged[section] = nested
    return merged


def _unique_directories(paths: list[Path]) -> list[dict[str, str]]:
    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in paths:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            continue
        value = str(resolved)
        if value in seen or not resolved.is_dir():
            continue
        seen.add(value)
        unique.append({"name": resolved.name or value, "path": value})
    return unique


def _filesystem_roots() -> list[dict[str, str]]:
    if os.name == "nt":
        roots = [
            Path(f"{letter}:\\")
            for letter in string.ascii_uppercase
            if Path(f"{letter}:\\").is_dir()
        ]
    else:
        roots = [Path("/")]
    return [{"name": str(root), "path": str(root)} for root in roots]


def _file_revision(path: Path) -> str:
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise UIError(f"无法读取配置文件：{exc}") from exc
    return hashlib.sha256(content).hexdigest()


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id",
        "task",
        "display_task",
        "attachments",
        "workspace",
        "executor",
        "consensus",
        "collaboration_mode",
        "status",
        "phase",
        "created_at",
        "updated_at",
        "attempts",
        "error",
        "approved",
        "review_count",
        "technical_document",
        "archived",
        "archived_at",
        "checkpoint",
        "collaboration",
        "events",
        "summary",
        "quality",
        "group_chat",
    }
    public = {key: value for key, value in record.items() if key in allowed}
    public["error"] = _safe_public_error(
        public.get("error"),
        status=str(public.get("status", "")),
        collaboration_mode=str(public.get("collaboration_mode", "workflow")),
    )
    return public


def _safe_public_error(
    error: object,
    *,
    status: str,
    collaboration_mode: str,
) -> str:
    """Map internal failures to stable UI text without exposing native details."""

    if not str(error or "").strip():
        return ""
    if status == "interrupted":
        return "任务已中断"
    if status == "cancelled":
        return "任务已取消"
    if status == "failed":
        return "群聊处理失败" if collaboration_mode == "group_chat" else "任务执行失败"
    if collaboration_mode == "group_chat":
        return "部分 Agent 本轮执行失败"
    return "任务执行未完成"


def _result_payload(result: AgentRunResult) -> dict[str, Any]:
    return {
        "agent": result.agent,
        "final_text": result.final_text,
        "duration_seconds": result.duration_seconds,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
    }


def _group_chat_summary(state: dict[str, Any]) -> dict[str, Any]:
    messages = state.get("messages")
    if not isinstance(messages, list):
        messages = []
    assistant_messages = [
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
            float(item.get("duration_seconds", 0) or 0)
            for item in assistant_messages
        ),
        "input_tokens": sum(
            int(item.get("input_tokens", 0) or 0) for item in assistant_messages
        ),
        "output_tokens": sum(
            int(item.get("output_tokens", 0) or 0) for item in assistant_messages
        ),
        "execution_turns": sum(
            1
            for item in messages
            if isinstance(item, dict)
            and item.get("role") == "user"
            and item.get("action") == "execute"
        ),
    }


def _agent_label(agent: str) -> str:
    return {"claude": "Claude", "codex": "Codex"}.get(agent, agent)


def _agent_key(source: str) -> str:
    lowered = source.strip().lower()
    if "claude" in lowered:
        return "claude"
    if "codex" in lowered:
        return "codex"
    return ""


def _required_text(value: object, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UIError(message)
    return value.strip()


def _optional_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _new_run_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
