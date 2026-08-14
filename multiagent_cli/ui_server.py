from __future__ import annotations

import base64
import binascii
import errno
import hashlib
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
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

from . import __version__
from .bridge_config import (
    ConfigError,
    find_config_path,
    load_bridge_config,
    resolve_bridge_settings,
)
from .bridge_models import (
    DEFAULT_GROUP_CHAT_AGENT_A_IDENTITY,
    DEFAULT_GROUP_CHAT_AGENT_B_IDENTITY,
    AgentEvent,
    BridgeError,
    NativeInteractionRequest,
    NativeInteractionResponse,
)
from .group_chat import GroupChatEngine
from .run_store import RUN_ID_RE, RunStore
from .token_api import (
    DEFAULT_TOKEN_API_BASE_URL,
    TokenAPICredentials,
    public_model_catalog,
)


MAX_REQUEST_BYTES = 30_000_000
MAX_UPLOAD_FILES = 5
# Per-message uploads stay capped at MAX_UPLOAD_FILES, but a group chat
# accumulates attachments over many turns. Reading the record back must not
# re-apply the per-message cap: an attachment missing from the record can no
# longer be downloaded, so trimming to 5 would make an image you just sent
# unreachable after your next message. Bound the run instead, keeping the most
# recent uploads.
MAX_RUN_ATTACHMENTS = 50
MAX_UPLOAD_FILE_BYTES = 10_000_000
MAX_UPLOAD_TOTAL_BYTES = 20_000_000
MAX_RUN_TITLE_CHARS = 200
MAX_SESSION_EVENTS = 240
MAX_RETAINED_SESSIONS = 50
# 持久化到 run record 的公开活动事件上限。
MAX_RECORD_EVENTS = 500
# 不持久化 progress 原文，避免把模型输出全文写进记录。
TIMELINE_EVENT_KINDS = {
    "lifecycle",
    "tool",
    "tool_result",
    "warning",
    "error",
    "metric",
    "interaction_request",
    "interaction_response",
}
ACTIVE_STATUSES = {
    "starting",
    "running",
    "awaiting_interaction",
    "stopping",
}
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
# Raster images only. SVG is deliberately excluded: served inline it would be
# an HTML/XML document under our origin, i.e. a stored-XSS vector.
IMAGE_EXTENSIONS = {
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".webp",
}
UPLOAD_EXTENSIONS = DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS
# Served inline only when the extension implies a safe raster type; the stored
# content_type is uploader-controlled and must never be trusted for rendering.
INLINE_IMAGE_TYPES = {
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
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
        notify: Callable[..., None],
        agent_task: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        group_chat: object = None,
        stream_gate: Callable[[], bool] | None = None,
    ) -> None:
        self.id = run_id
        self.task = task
        self.agent_task = agent_task or task
        self.attachments = list(attachments or [])
        self.workspace = workspace
        self.status = "starting"
        self.error = ""
        self.exit_code: int | None = None
        self.started_at = _timestamp()
        self.updated_at = self.started_at
        self.events: list[dict[str, Any]] = []
        self.agent_events: dict[str, dict[str, Any]] = {}
        self.group_chat = dict(group_chat) if isinstance(group_chat, dict) else None
        self._chat_engine: GroupChatEngine | None = None
        self._native_interactions: dict[str, dict[str, Any]] = {}
        self._native_responses: dict[str, NativeInteractionResponse] = {}
        self._stop_requested = False
        self._stop_handler: Callable[[], None] | None = None
        self._condition = threading.Condition()
        self._notify = notify
        self._record_events: Callable[[AgentEvent], None] | None = None
        # 每个 agent 已经推送过的原文，用于把「全量重发」换算成增量
        self._stream_sent: dict[str, str] = {}
        self._stream_gate = stream_gate
        self._active_chat_turns: set[str] = set()

    def bind_record_persistence(self, persist: Callable[[AgentEvent], None]) -> None:
        """Attach the store-backed timeline writer for this session.

        Group-chat turns never pass through ``cli._handle_run_event``, so
        without this hook their timeline lives only in memory and disappears
        when the server restarts or the session is evicted.
        """

        self._record_events = persist

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
            return self._stop_requested

    def has_active_chat_turns(self) -> bool:
        with self._condition:
            return bool(self._active_chat_turns)

    def begin_chat_turn(self, token: str | None = None) -> str:
        with self._condition:
            if self._stop_requested:
                raise UIError("当前群聊正在停止，暂时不能发送新消息")
            token = token or f"chat-{len(self._active_chat_turns) + 1}-{time.monotonic_ns()}"
            self._active_chat_turns.add(token)
            self.status = "running"
            self.error = ""
            self.updated_at = _timestamp()
            self._stream_sent.clear()
        self._notify("chat_turn", self.id)
        return token

    def finish_chat_turn(
        self,
        *,
        state: dict[str, Any],
        error: str = "",
        status: str = "ready",
        token: str | None = None,
    ) -> None:
        with self._condition:
            self.group_chat = state
            if token is not None:
                self._active_chat_turns.discard(token)
            active = bool(self._active_chat_turns or self._native_interactions)
            self.status = "running" if active else status
            self.error = "" if active else error
            self.exit_code = None if active else (0 if status == "ready" else 1)
            self.updated_at = _timestamp()
            if not active:
                self._stream_sent.clear()
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
            for interaction_id in self._native_interactions:
                self._native_responses.setdefault(
                    interaction_id,
                    NativeInteractionResponse("cancel"),
                )
            self._condition.notify_all()
            handler = self._stop_handler
        if handler is not None:
            handler()
        self._notify("stopping", self.id)

    def on_event(self, event: AgentEvent) -> None:
        safe = event.to_dict(safe=True, include_activity=True)
        with self._condition:
            if (
                event.kind == "progress"
                and self.events
                and self.events[-1].get("kind") == "progress"
                and self.events[-1].get("source") == safe.get("source")
                and self.events[-1].get("step_id") == safe.get("step_id")
            ):
                # Providers frequently resend the whole current response block.
                # Keep one compact public progress row; raw deltas use the
                # separate transient SSE channel below.
                self.events[-1] = safe
            else:
                self.events.append(safe)
            del self.events[:-MAX_SESSION_EVENTS]
            key = _agent_key(event.source)
            if key:
                self.agent_events[key] = safe
            # Whitelist, not blacklist: adapter subprocesses may flush one last
            # progress event after a turn has finished. A late event must not
            # move a ready/failed/interrupted session back to running.
            if self.status in {"starting", "running"}:
                self.status = "running"
            self.updated_at = _timestamp()
        persist = self._record_events
        if persist is not None:
            try:
                persist(event)
            except Exception:
                # Persistence must never interrupt the active agent turn.
                pass
        delta = self._stream_delta(event, safe)
        if delta:
            self._notify(
                "event",
                self.id,
                {
                    "stream_text": delta,
                    "source": event.source,
                    "step_id": event.step_id,
                },
            )
            return
        self._notify("event", self.id)

    def _stream_delta(self, event: AgentEvent, safe: dict[str, Any]) -> str:
        """Return the unsent tail of this event's raw model text.

        Both CLI parsers re-emit the *whole* current block on every update
        (Codex sends ``agent_message`` twice, on ``item.updated`` and again on
        ``item.completed``), so forwarding the raw text would duplicate it in
        the browser.  The session therefore tracks what each agent has already
        streamed and publishes only the increment.  Raw text is transient: it
        goes out over SSE but is never written to the run record.
        """

        gate = self._stream_gate
        if gate is None:
            return ""
        try:
            if not gate():
                return ""
        except Exception:
            return ""
        raw = event.to_dict(safe=True, allow_stream=True).get("text") or ""
        # If opting into the stream channel changed nothing, this kind is not a
        # streaming kind. Deriving it this way keeps the privacy policy in
        # bridge_models instead of duplicating its kind list here.
        if not raw or raw == (safe.get("text") or ""):
            return ""
        key = event.step_id or _agent_key(event.source) or event.source
        with self._condition:
            previous = self._stream_sent.get(key, "")
            if raw == previous:
                return ""
            if previous and raw.startswith(previous):
                delta = raw[len(previous):]
            else:
                # A genuinely new block: keep it readable instead of glued to
                # the previous one.
                delta = f"\n\n{raw}" if previous else raw
            if not delta:
                return ""
            self._stream_sent[key] = previous + delta
            return delta

    def wait_for_native_interaction(
        self,
        request: NativeInteractionRequest,
    ) -> NativeInteractionResponse:
        """Expose one native request and block only the Agent that issued it."""

        interaction_id = secrets.token_urlsafe(12)
        public_request = request.to_dict()
        # Native request ids are provider-local and can collide across Agents.
        # The browser receives a fresh opaque id; the adapter retains the
        # provider id in its own stack when translating the response.
        public_request["id"] = interaction_id
        with self._condition:
            if self._stop_requested:
                return NativeInteractionResponse("cancel")
            self._native_interactions[interaction_id] = public_request
            if self.status != "stopping" and len(self._active_chat_turns) <= 1:
                self.status = "awaiting_interaction"
            self.updated_at = _timestamp()
        self._notify("native_interaction", self.id, {"interaction_id": interaction_id})

        with self._condition:
            while interaction_id not in self._native_responses:
                self._condition.wait(timeout=15)
            response = self._native_responses.pop(interaction_id)
            self._native_interactions.pop(interaction_id, None)
            if self.status != "stopping":
                self.status = (
                    "awaiting_interaction"
                    if self._native_interactions and len(self._active_chat_turns) <= 1
                    else "running"
                )
            self.updated_at = _timestamp()
        self._notify(
            "native_interaction_resolved",
            self.id,
            {"interaction_id": interaction_id},
        )
        return response

    def submit_native_interaction(
        self,
        interaction_id: str,
        payload: object,
    ) -> None:
        if not isinstance(payload, dict):
            raise UIError("请求正文必须是 JSON 对象")
        with self._condition:
            request = self._native_interactions.get(interaction_id)
            if request is None:
                raise UIError("该原生交互请求已处理或不存在")
            action = _required_text(payload.get("action"), "请选择如何处理此请求")
            allowed = {
                str(option.get("value") or "")
                for option in request.get("options", [])
                if isinstance(option, dict)
            }
            if action not in allowed:
                raise UIError("不支持的原生交互操作")

            answers: dict[str, tuple[str, ...]] = {}
            raw_answers = payload.get("answers")
            if isinstance(raw_answers, dict):
                for key, raw_value in raw_answers.items():
                    values = raw_value if isinstance(raw_value, list) else [raw_value]
                    answers[str(key)] = tuple(
                        str(value).strip()
                        for value in values
                        if str(value).strip()
                    )
            if action == "submit":
                for question in request.get("questions", []):
                    if not isinstance(question, dict):
                        continue
                    question_id = str(question.get("id") or "")
                    if question_id and not answers.get(question_id):
                        raise UIError("请回答所有问题后再提交")
            self._native_responses[interaction_id] = NativeInteractionResponse(
                action,
                answers,
                _optional_text(payload.get("text")),
            )
            self._native_interactions.pop(interaction_id, None)
            if self.status != "stopping":
                self.status = (
                    "awaiting_interaction"
                    if self._native_interactions and len(self._active_chat_turns) <= 1
                    else "running"
                )
            self.updated_at = _timestamp()
            self._condition.notify_all()

    def _cancel_native_interactions_locked(self) -> None:
        for interaction_id in self._native_interactions:
            self._native_responses.setdefault(
                interaction_id,
                NativeInteractionResponse("cancel"),
            )
        self._condition.notify_all()

    def finish(self, exit_code: int, record: dict[str, Any] | None) -> None:
        with self._condition:
            self.exit_code = exit_code
            self.status = str(record.get("status", "failed")) if record else "failed"
            self.error = str(record.get("error", "")) if record else self.error
            self._cancel_native_interactions_locked()
            self.updated_at = _timestamp()
        self._notify("finished", self.id)

    def fail(self, error: str) -> None:
        with self._condition:
            self.status = "failed"
            self.error = error
            self.exit_code = 1
            self._cancel_native_interactions_locked()
            self.updated_at = _timestamp()
        self._notify("finished", self.id)

    def to_dict(self) -> dict[str, Any]:
        with self._condition:
            return {
                "id": self.id,
                "task": self.task,
                "attachments": list(self.attachments),
                "workspace": str(self.workspace),
                "status": self.status,
                "error": _safe_public_error(
                    self.error,
                    status=self.status,
                ),
                "exit_code": self.exit_code,
                "started_at": self.started_at,
                "updated_at": self.updated_at,
                "events": list(self.events),
                "agent_events": dict(self.agent_events),
                "native_interactions": [
                    dict(request) for request in self._native_interactions.values()
                ],
                "group_chat": dict(self.group_chat) if self.group_chat else None,
            }


class UISessionManager:
    def __init__(self, *, store: RunStore, default_workspace: Path) -> None:
        self.store = store
        self.default_workspace = default_workspace
        self.attachments_root = store.root / "_attachments"
        self._sessions: dict[str, UISession] = {}
        self._lock = threading.RLock()
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()
        # 界面偏好「显示模型原文流」的内存镜像，避免每个事件都读配置文件
        self._stream_model_text = _stream_preference(default_workspace)

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
        self._refresh_stream_preference(workspace)
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
        allowed = {"theme", "show_archived", "compact_sidebar", "stream_model_text", "browser_notifications"}
        if not set(preferences).issubset(allowed):
            raise UIError("界面偏好包含未知字段")
        if "theme" in preferences and preferences["theme"] not in UI_THEMES:
            raise UIError("界面主题必须是 paper、ocean、graphite 或 botanical")
        for key in ("show_archived", "compact_sidebar", "stream_model_text", "browser_notifications"):
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

        self._refresh_stream_preference(workspace)
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
        self._refresh_stream_preference(workspace)
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

        from .runtime import (
            apply_resume_settings,
            make_adapters,
            resume_value,
            settings_snapshot,
        )

        config_value = _optional_text(payload.get("config"))
        if previous and not config_value:
            saved = previous.get("settings")
            if isinstance(saved, dict):
                config_value = _optional_text(saved.get("config_path"))
        try:
            resolved = resume_value(previous, "resolved_config")
            if isinstance(resolved, dict):
                data = resolved
                config_path = (
                    Path(config_value).expanduser().resolve() if config_value else None
                )
            else:
                config_path = find_config_path(config_value or None, workspace)
                data = load_bridge_config(config_path)
            settings = resolve_bridge_settings(
                data,
                workspace=workspace,
                config_path=config_path,
            )
            settings = apply_resume_settings(settings, previous)
        except ConfigError as exc:
            raise UIError(f"配置错误：{exc}") from exc

        if not previous and not task and payload.get("attachments"):
            raise UIError("添加参考文档时必须同时提供第一条群聊消息")
        try:
            adapters = make_adapters(settings, state_root=self.store.root)
        except ConfigError as exc:
            raise UIError(f"配置错误：{exc}") from exc

        run_id = resume_id or _new_run_id()
        if not previous:
            attachments = _save_uploaded_documents(
                self.attachments_root,
                run_id,
                payload.get("attachments"),
            )
            # Mirror into the workspace so workspace-sandboxed agents can
            # actually read the files; the record keeps the original path for
            # the download route.
            _workspace_attachment_mirror(settings.workspace, run_id, attachments)
            agent_task = _task_with_attachments(task, attachments)
        display_task = task or "群聊协作"
        session = UISession(
            run_id=run_id,
            task=display_task,
            agent_task=agent_task,
            attachments=attachments,
            workspace=settings.workspace,
            notify=self.publish,
            stream_gate=self._stream_enabled,
        )

        def stop_adapters() -> None:
            for adapter in adapters.values():
                adapter.request_stop()

        for adapter in adapters.values():
            bind_interaction = getattr(adapter, "bind_interaction_handler", None)
            if callable(bind_interaction):
                bind_interaction(session.wait_for_native_interaction)
        session.bind_stop_handler(stop_adapters)
        session.bind_record_persistence(
            self._record_timeline_persister(session.id)
        )
        engine = GroupChatEngine(settings, adapters)
        session.bind_chat_engine(engine)
        try:
            self._reserve_session(session)
        except Exception:
            if not previous:
                _remove_run_attachments(self.attachments_root, run_id)
            raise
        try:
            self.store.start(
                task=agent_task,
                workspace=settings.workspace,
                run_id=run_id,
                settings_snapshot=settings_snapshot(settings),
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
        reservation = engine.reserve(task)
        try:
            session.begin_chat_turn(reservation.token)
        except Exception:
            engine.release(reservation)
            raise
        worker = threading.Thread(
            target=self._run_session,
            args=(session, task),
            kwargs={
                "agent_text": agent_task,
                "attachments": attachments,
                "reservation": reservation,
            },
            name=f"multiagent-ui-chat-{run_id}",
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
        reservation: object = None,
        edited_from: str = "",
        hidden_user: bool = False,
        retry_of: str = "",
        retry_mode: str = "",
        reply_to: str = "",
    ) -> None:
        engine = session.chat_engine()

        def save_state(state: dict[str, Any]) -> None:
            try:
                self.store.update(
                    session.id,
                    status="running",
                    error="",
                    group_chat=state,
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
                reservation=reservation,
                edited_from=edited_from,
                hidden_user=hidden_user,
                retry_of=retry_of,
                retry_mode=retry_mode,
                reply_to=reply_to,
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
                token=getattr(reservation, "token", None),
            )
            self.store.update(session.id, status=session.status, group_chat=state)
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
                token=getattr(reservation, "token", None),
            )
            self.store.update(session.id, status=session.status, group_chat=state)
            return

        state = engine.to_dict()
        error = "；".join(
            f"{_agent_label(agent)}：本轮执行失败"
            for agent in turn.errors
        )
        status = "ready" if turn.responses else "failed"
        summary = _group_chat_summary(state)
        if turn.responses:
            session.finish_chat_turn(
                state=state,
                error=error,
                token=getattr(reservation, "token", None),
            )
        else:
            session.finish_chat_turn(
                state=state,
                error=error or "所有 Agent 均未返回群聊回复",
                status="failed",
                token=getattr(reservation, "token", None),
            )
        try:
            self.store.update(
                session.id,
                status=session.status,
                error=session.error,
                group_chat=state,
                summary=summary,
            )
        except (KeyError, OSError):
            pass

    def _run_session(self, *args: object, **kwargs: object) -> None:
        """Run one group-chat turn in its worker thread."""

        self._run_group_chat_turn(*args, **kwargs)

    def send_chat_message(self, run_id: str, payload: object) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise UIError("请求正文必须是 JSON 对象")
        message = _optional_text(payload.get("message"))
        edited_from = _optional_text(payload.get("edited_from"))
        retry_of = _optional_text(payload.get("retry_of"))
        retry_mode = _optional_text(payload.get("retry_mode"))
        if retry_mode and retry_mode not in {"regenerate", "continue"}:
            raise UIError("retry_mode 必须是 regenerate 或 continue")
        hidden_user = bool(payload.get("hidden_user", False))
        forced_agent = _optional_text(payload.get("agent"))
        raw_recipients = payload.get("recipients")
        forced_recipients = None
        if isinstance(raw_recipients, list):
            forced_recipients = tuple(
                value for value in raw_recipients
                if isinstance(value, str) and value in {"claude", "codex"}
            )
            if not forced_recipients:
                raise UIError("recipients 必须包含 claude 或 codex")
        if forced_agent and forced_agent not in {"claude", "codex"}:
            raise UIError("agent 必须是 claude 或 codex")
        record = self.store.get(run_id)
        if record is None:
            raise UIError("找不到群聊对话")
        if bool(record.get("archived")):
            raise UIError("已归档的群聊不能发送消息，请先取消归档")
        uploaded_attachments = _save_uploaded_documents(
            self.attachments_root,
            run_id,
            payload.get("attachments"),
        )
        attachments = list(uploaded_attachments)
        if uploaded_attachments:
            workspace = Path(str(record.get("workspace", ""))).expanduser().resolve()
            if workspace.is_dir():
                _workspace_attachment_mirror(workspace, run_id, uploaded_attachments)
        try:
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
            engine = session.chat_engine()
            try:
                source_message = None
                retry_source = None
                if edited_from:
                    source_message = engine.find_message(edited_from)
                    if source_message is None or source_message.get("role") != "user":
                        raise UIError("找不到可编辑的用户消息")
                    if not attachments:
                        attachments = [
                            dict(item)
                            for item in source_message.get("attachments", [])
                            if isinstance(item, dict)
                        ]
                    if isinstance(source_message.get("recipients"), list) and not forced_recipients:
                        forced_recipients = tuple(
                            value for value in source_message["recipients"]
                            if value in {"claude", "codex"}
                        ) or None
                if retry_of:
                    retry_source = engine.find_message(retry_of)
                    if retry_source is None or retry_source.get("role") != "assistant":
                        raise UIError("找不到可重新生成的 Agent 消息")
                    parent = engine.find_parent_user(retry_of)
                    if parent is None:
                        raise UIError("找不到 Agent 消息对应的用户问题")
                    message = str(parent.get("content") or "").strip()
                    hidden_user = True
                    if not forced_agent:
                        forced_agent = str(retry_source.get("sender") or "")
                    forced_recipients = (forced_agent,)
                if not message:
                    raise UIError("群聊消息不能为空")
                reservation = engine.reserve(
                    message,
                    forced_recipients=forced_recipients
                    or ((forced_agent,) if forced_agent else None),
                )
            except BridgeError as exc:
                raise UIError(str(exc)) from exc
            try:
                session.begin_chat_turn(reservation.token)
            except Exception:
                engine.release(reservation)
                if uploaded_attachments:
                    _remove_unreferenced_uploads(self.attachments_root, run_id, uploaded_attachments)
                raise
        except Exception:
            if uploaded_attachments:
                _remove_unreferenced_uploads(self.attachments_root, run_id, uploaded_attachments)
            raise
        # The upload lives in the same per-run directory as the task-level
        # documents; the record must know about it so the download route can
        # authorize serving it later.
        if uploaded_attachments:
            try:
                self.store.mutate(
                    run_id,
                    lambda record: record.__setitem__(
                        "attachments",
                        _stored_attachments(record.get("attachments")) + uploaded_attachments,
                    ),
                )
            except (KeyError, OSError):
                _remove_unreferenced_uploads(self.attachments_root, run_id, uploaded_attachments)
                engine.release(reservation)
                session.finish_chat_turn(
                    state=engine.to_dict(),
                    status="ready",
                    token=reservation.token,
                )
                raise UIError("无法保存随消息上传的附件")
        worker = threading.Thread(
            target=self._run_session,
            args=(session, message),
            kwargs={
                "agent_text": _task_with_attachments(
                    message,
                    attachments,
                    mid_chat=True,
                ),
                "attachments": attachments,
                "reservation": reservation,
                "edited_from": edited_from,
                "hidden_user": hidden_user,
                "retry_of": retry_of,
                "retry_mode": retry_mode,
                "reply_to": str((engine.find_parent_user(retry_of) or {}).get("id") or "") if retry_of else "",
            },
            name=f"multiagent-ui-chat-{run_id}",
            daemon=True,
        )
        worker.start()
        self.publish("chat_turn", run_id)
        return session.to_dict()

    def _restore_group_chat_session(self, record: dict[str, Any]) -> UISession:
        from .runtime import apply_resume_settings, make_adapters

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
            )
            settings = apply_resume_settings(settings, record)
            adapters = make_adapters(settings, state_root=self.store.root)
        except ConfigError as exc:
            raise UIError(f"配置错误：{exc}") from exc
        engine = GroupChatEngine(settings, adapters, record.get("group_chat"))
        session = UISession(
            run_id=str(record["id"]),
            task=str(record.get("display_task") or record.get("task") or "群聊"),
            agent_task=str(record.get("task") or ""),
            attachments=_stored_attachments(record.get("attachments")),
            workspace=settings.workspace,
            group_chat=engine.to_dict(),
            notify=self.publish,
            stream_gate=self._stream_enabled,
        )
        session.status = str(record.get("status", "ready"))
        session.error = str(record.get("error", ""))
        session.bind_chat_engine(engine)

        def stop_adapters() -> None:
            for adapter in adapters.values():
                adapter.request_stop()

        for adapter in adapters.values():
            bind_interaction = getattr(adapter, "bind_interaction_handler", None)
            if callable(bind_interaction):
                bind_interaction(session.wait_for_native_interaction)
        session.bind_stop_handler(stop_adapters)
        session.bind_record_persistence(
            self._record_timeline_persister(session.id)
        )
        self._reserve_session(session)
        return session

    def submit_native_interaction(
        self,
        run_id: str,
        interaction_id: str,
        payload: object,
    ) -> dict[str, Any]:
        session = self.session(run_id)
        if session is None:
            raise UIError(f"找不到活动 UI 任务：{run_id}")
        session.submit_native_interaction(interaction_id, payload)
        self.publish(
            "native_interaction_submitted",
            run_id,
            {"interaction_id": interaction_id},
        )
        return session.to_dict()

    def stop_task(self, run_id: str) -> dict[str, Any]:
        session = self.session(run_id)
        if session is None:
            raise UIError("该任务不属于当前 UI 服务的活动进程，已不能发送停止信号")
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

    def _stream_enabled(self) -> bool:
        """Whether sessions may publish raw model text over SSE."""

        with self._lock:
            return self._stream_model_text

    def _refresh_stream_preference(self, workspace: Path) -> None:
        """Re-read the streaming toggle so it applies without a restart."""

        enabled = _stream_preference(workspace)
        with self._lock:
            self._stream_model_text = enabled

    def _record_timeline_persister(
        self,
        run_id: str,
    ) -> Callable[[AgentEvent], None]:
        """Persist timeline-worthy events into the run record.

        Only public activity is stored (never streaming ``progress`` text),
        capped at ``MAX_RECORD_EVENTS``.
        """

        def persist(event: AgentEvent) -> None:
            if event.kind not in TIMELINE_EVENT_KINDS:
                return

            def mutate(record: dict[str, Any]) -> None:
                timeline = record.get("events")
                if not isinstance(timeline, list):
                    timeline = []
                timeline.append(event.to_dict(safe=True, include_activity=True))
                record["events"] = timeline[-MAX_RECORD_EVENTS:]

            self.store.mutate(run_id, mutate)

        return persist

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
        workspace_text = str(record.get("workspace", "")).strip()
        if workspace_text:
            _remove_workspace_attachment_mirror(
                Path(workspace_text).expanduser(),
                run_id,
            )
        self.publish("delete", run_id)
        return _public_record(deleted)

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=64)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(
        self,
        kind: str,
        run_id: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Broadcast one SSE notification.

        ``extra`` carries transient payload (streaming deltas) that is never
        stored in the run record; clients that do not understand the keys just
        fall back to the normal refresh path.
        """

        message: dict[str, Any] = {
            "type": kind,
            "run_id": run_id,
            "timestamp": _timestamp(),
        }
        if extra:
            message.update(extra)
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
            match = re.fullmatch(
                r"/api/runs/([A-Za-z0-9._-]+)/attachments/(.+)",
                path,
            )
            if match:
                if not self._same_origin():
                    self._error(HTTPStatus.FORBIDDEN, "拒绝跨站请求")
                    return
                inline = parse_qs(parsed.query).get("inline", [""])[0] == "1"
                self._attachment(match.group(1), match.group(2), inline=inline)
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
                    r"/api/sessions/([A-Za-z0-9._-]+)/interactions/([A-Za-z0-9_-]+)",
                    path,
                )
                if match:
                    session = manager.submit_native_interaction(
                        match.group(1),
                        match.group(2),
                        payload,
                    )
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

        def _attachment(self, run_id: str, raw_name: str, *, inline: bool = False) -> None:
            """Serve one stored upload. The run must exist so attachments of a
            deleted task disappear with it, and the filename is validated the
            same way as at upload time to rule out path traversal."""
            record = manager.store.get(run_id)
            if record is None:
                self._error(HTTPStatus.NOT_FOUND, "找不到任务")
                return
            try:
                name = _safe_document_name(unquote(raw_name))
            except UIError:
                self._error(HTTPStatus.NOT_FOUND, "附件不存在")
                return
            stored = next(
                (
                    item
                    for item in _stored_attachments(record.get("attachments"))
                    if item["name"] == name
                ),
                None,
            )
            if stored is None:
                self._error(HTTPStatus.NOT_FOUND, "附件不存在")
                return
            base = manager.attachments_root.resolve()
            try:
                target = (base / run_id / name).resolve()
            except OSError:
                self._error(HTTPStatus.NOT_FOUND, "附件不存在")
                return
            if base not in target.parents:
                self._error(HTTPStatus.NOT_FOUND, "附件不存在")
                return
            try:
                content = target.read_bytes()
            except OSError:
                self._error(HTTPStatus.NOT_FOUND, "附件不存在")
                return
            suffix = Path(name).suffix.lower()
            if inline:
                # Inline rendering is allowed only for raster types derived
                # from the validated extension — never from the uploader's
                # declared content_type, which would let an HTML/JS payload
                # ride into the page as a same-origin document.
                inline_type = INLINE_IMAGE_TYPES.get(suffix)
                if inline_type is None:
                    self._error(HTTPStatus.NOT_FOUND, "附件不存在")
                    return
                content_type = inline_type
                disposition = "inline; filename*=UTF-8''" + quote(name, encoding="utf-8")
            else:
                # Downloads never echo the uploader-declared content_type, and
                # never claim a renderable type: .html and .xml are accepted
                # uploads, so anything but a known raster image is handed back
                # as an opaque byte stream instead of relying on
                # Content-Disposition alone to stop the browser rendering it.
                content_type = INLINE_IMAGE_TYPES.get(
                    suffix,
                    "application/octet-stream",
                )
                disposition = "attachment; filename*=UTF-8''" + quote(
                    name,
                    encoding="utf-8",
                )
            # Both branches above pick from a fixed table, so no
            # uploader-controlled string can reach the response headers.
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Content-Disposition", disposition)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

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
            "status",
            "created_at",
            "updated_at",
            "attempts",
            "error",
            "archived",
            "archived_at",
        )
    }
    summary["archived"] = bool(record.get("archived", False))
    summary["error"] = _safe_public_error(
        summary.get("error"),
        status=str(summary.get("status", "")),
    )
    summary["resumable"] = False
    return summary


def _detached_record(record: dict[str, Any]) -> dict[str, Any]:
    """Render orphaned active records as interrupted, without mutating history."""

    if str(record.get("status", "")) not in ACTIVE_STATUSES:
        return record
    record["status"] = "interrupted"
    record["detached"] = True
    if not str(record.get("error", "")).strip():
        record["error"] = "任务已不在当前 UI 服务中运行；上次服务可能退出"
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
        raise UIError(f"每条消息最多上传 {MAX_UPLOAD_FILES} 个附件")
    if not payload:
        return []

    run_directory = attachments_root / run_id
    saved: list[dict[str, Any]] = []
    total_size = 0
    # Mid-chat uploads reuse the per-run directory created by the task-level
    # documents, so by then it usually already exists. Remember whether we
    # created it: on failure we may only remove a directory we own, never one
    # that still holds attachments from earlier turns.
    directory_existed = run_directory.exists()
    try:
        run_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            attachments_root.chmod(0o700)
            run_directory.chmod(0o700)
        except OSError:
            pass
        # Seed the taken names from what is already on disk so a later upload
        # cannot overwrite an earlier attachment the record still points at.
        # Keys are casefolded to match _unique_document_name.
        used_names: set[str] = set()
        if directory_existed:
            try:
                used_names = {
                    entry.name.casefold()
                    for entry in run_directory.iterdir()
                    if entry.is_file()
                }
            except OSError:
                used_names = set()
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
        if directory_existed:
            # Roll back only this call's files; the directory predates us.
            for item in saved:
                try:
                    Path(item["path"]).unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                for leftover in list(run_directory.glob("*.tmp")):
                    leftover.unlink(missing_ok=True)
            except OSError:
                pass
        else:
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
    if suffix not in UPLOAD_EXTENSIONS:
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


def _stream_preference(workspace: Path) -> bool:
    """Read ``ui.stream_model_text`` for one workspace, defaulting to off.

    Streaming raw model text widens what the browser sees, so a missing or
    unreadable config must fail closed.
    """

    try:
        project_path = workspace / PROJECT_CONFIG_NAME
        source = (
            project_path
            if project_path.is_file()
            else find_config_path(None, workspace)
        )
        data = load_bridge_config(source)
    except Exception:
        # Any resolution or parse failure must leave streaming disabled.
        return False
    ui = data.get("ui")
    if not isinstance(ui, dict):
        return False
    return ui.get("stream_model_text") is True


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
                **(
                    {"workspace_path": item["workspace_path"]}
                    if isinstance(item.get("workspace_path"), str)
                    and item.get("workspace_path")
                    else {}
                ),
            }
        )
    return stored[-MAX_RUN_ATTACHMENTS:]


def _task_with_attachments(
    task: str,
    attachments: list[dict[str, Any]],
    *,
    mid_chat: bool = False,
) -> str:
    if not attachments:
        return task
    heading = (
        "附加文档（由用户随本条消息上传，请先读取并纳入本轮判断；除非消息明确要求，否则不要修改这些原始文档）："
        if mid_chat
        else "附加文档（由用户随任务上传，请先读取并纳入需求分析；除非需求明确要求，否则不要修改这些原始文档）："
    )
    lines = [task, "", heading]
    lines.extend(
        f"- {item['name']}：{_agent_attachment_path(item)}" for item in attachments
    )
    return "\n".join(lines)


def _agent_attachment_path(item: dict[str, Any]) -> str:
    """Path the agent should read. Uploads live outside the workspace (the
    store's ``_attachments`` root), where an agent sandboxed to the workspace
    cannot open them; when a workspace mirror exists we hand the agent that
    path instead, falling back to the original when mirroring failed."""

    mirror = item.get("workspace_path")
    if isinstance(mirror, str) and mirror:
        return mirror
    return str(item.get("path", ""))


def _workspace_attachment_mirror(
    workspace: Path,
    run_id: str,
    attachments: list[dict[str, Any]],
) -> None:
    """Copy each upload into ``workspace/.multiagent/attachments/<run_id>/``.

    Agents run sandboxed to the workspace, so the store's attachment root is
    unreadable to them; the mirror gives them an in-workspace path. The
    record keeps pointing at the original copy (the download route authorizes
    against it), and the repo-level .gitignore already excludes .multiagent/
    so mirrors never show up in diffs. Best-effort: a failed copy leaves the
    entry without ``workspace_path`` and the prompt falls back to the
    original path.
    """

    if not attachments or RUN_ID_RE.fullmatch(run_id) is None:
        return
    mirror_dir = workspace / ".multiagent" / "attachments" / run_id
    try:
        mirror_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for item in attachments:
        source = Path(str(item.get("path", "")))
        name = str(item.get("name", ""))
        if not name or not source.is_file():
            continue
        target = mirror_dir / name
        try:
            if target.resolve() == source.resolve():
                continue
            shutil.copyfile(source, target)
            try:
                target.chmod(0o600)
            except OSError:
                pass
            item["workspace_path"] = str(target.resolve())
        except OSError:
            continue


def _remove_workspace_attachment_mirror(workspace: Path, run_id: str) -> None:
    """Drop a run's in-workspace mirror. Never raises: the mirror is a cache."""

    if RUN_ID_RE.fullmatch(run_id) is None:
        return
    mirror_dir = workspace / ".multiagent" / "attachments" / run_id
    try:
        shutil.rmtree(mirror_dir, ignore_errors=True)
    except OSError:
        pass


def _remove_run_attachments(attachments_root: Path, run_id: str) -> None:
    if RUN_ID_RE.fullmatch(run_id) is None:
        return
    shutil.rmtree(attachments_root / run_id, ignore_errors=True)


def _remove_unreferenced_uploads(
    attachments_root: Path,
    run_id: str,
    attachments: list[dict[str, Any]],
) -> None:
    """Best-effort cleanup for uploads saved but never attached to a turn."""

    if RUN_ID_RE.fullmatch(run_id) is None:
        return
    for item in attachments:
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        try:
            (attachments_root / run_id / name).unlink(missing_ok=True)
        except OSError:
            pass


def _config_for_ui(data: dict[str, Any]) -> dict[str, Any]:
    group_chat_identities = data.get("group_chat_identities")
    if not isinstance(group_chat_identities, dict):
        group_chat_identities = {}
    ui = data.get("ui")
    if not isinstance(ui, dict):
        ui = {}
    token_api = data.get("token_api")
    if not isinstance(token_api, dict):
        token_api = {}

    def boolean(name: str, default: bool) -> bool:
        value = data.get(name, default)
        return value if isinstance(value, bool) else default

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

    group_chat_default_agent = data.get("group_chat_default_agent", "both")
    return {
        "group_chat_default_agent": (
            group_chat_default_agent
            if group_chat_default_agent in {"both", "claude", "codex"}
            else "both"
        ),
        "group_chat_execution": boolean("group_chat_execution", True),
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
            "stream_model_text": ui.get("stream_model_text") is True,
            "browser_notifications": ui.get("browser_notifications") is True,
        },
    }


def _config_from_ui(values: dict[str, Any]) -> dict[str, Any]:
    config = {
        key: values.get(key)
        for key in (
            "group_chat_default_agent",
            "group_chat_execution",
        )
    }
    group_chat_identities = values.get("group_chat_identities")
    ui = values.get("ui")
    token_api = values.get("token_api")
    if group_chat_identities is None:
        group_chat_identities = {
            "agent_a": DEFAULT_GROUP_CHAT_AGENT_A_IDENTITY,
            "agent_b": DEFAULT_GROUP_CHAT_AGENT_B_IDENTITY,
        }
    if not isinstance(group_chat_identities, dict):
        raise UIError("群聊身份设置必须是 JSON 对象")
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
    config["group_chat_identities"] = {
        "agent_a": group_chat_identities.get("agent_a"),
        "agent_b": group_chat_identities.get("agent_b"),
    }
    if "stream_model_text" in ui and not isinstance(ui["stream_model_text"], bool):
        raise UIError("界面开关必须是布尔值")
    if "browser_notifications" in ui and not isinstance(ui["browser_notifications"], bool):
        raise UIError("浏览器通知开关必须是布尔值")
    config["ui"] = {
        "theme": ui["theme"],
        "show_archived": ui["show_archived"],
        "compact_sidebar": ui["compact_sidebar"],
        # 旧客户端不发这个字段，缺省视为关闭而不是拒绝请求
        "stream_model_text": ui.get("stream_model_text") is True,
        "browser_notifications": ui.get("browser_notifications") is True,
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
    # These fields belonged to the removed workflow/consensus/CLI verifier.
    # Preserve genuinely unknown extension fields, but do not keep rewriting
    # settings that this product can no longer execute.
    for obsolete in (
        "collaboration_mode",
        "executor",
        "planning_collaboration",
        "consensus",
        "max_consensus_rounds",
        "plan_approval",
        "max_plan_revisions",
        "review_rounds",
        "final_review",
        "identities",
        "verification",
    ):
        merged.pop(obsolete, None)
    for key in (
        "group_chat_default_agent",
        "group_chat_execution",
    ):
        merged[key] = config[key]
    for section, known_keys in (
        ("group_chat_identities", ("agent_a", "agent_b")),
        ("token_api", ("enabled", "base_url")),
        (
            "claude",
            ("command", "model", "models", "fallback_on_timeout", "timeout", "extra_args"),
        ),
        (
            "codex",
            ("command", "model", "models", "fallback_on_timeout", "timeout", "extra_args"),
        ),
        ("ui", ("theme", "show_archived", "compact_sidebar", "stream_model_text", "browser_notifications")),
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
        "status",
        "created_at",
        "updated_at",
        "attempts",
        "error",
        "archived",
        "archived_at",
        "events",
        "summary",
        "group_chat",
    }
    public = {key: value for key, value in record.items() if key in allowed}
    public["error"] = _safe_public_error(
        public.get("error"),
        status=str(public.get("status", "")),
    )
    return public


def _safe_public_error(
    error: object,
    *,
    status: str,
) -> str:
    """Map internal failures to stable UI text without exposing native details."""

    if not str(error or "").strip():
        return ""
    if status == "interrupted":
        return "任务已中断"
    if status == "cancelled":
        return "任务已取消"
    if status == "failed":
        return "群聊处理失败"
    return "部分 Agent 本轮执行失败"


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
