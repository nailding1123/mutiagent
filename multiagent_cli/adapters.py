from __future__ import annotations

import itertools
import json
import os
import posixpath
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from .bridge_models import (
    AgentCommandSettings,
    AgentEvent,
    AgentModelCompatibilityError,
    AgentRunResult,
    AgentTimeoutError,
    BridgeError,
    NativeInteractionOption,
    NativeInteractionQuestion,
    NativeInteractionRequest,
    NativeInteractionResponse,
    NativeInteractionUnavailable,
)
from .process_control import isolated_process_kwargs, signal_process_tree, stop_process_tree


EventCallback = Callable[[AgentEvent], None]
InteractionCallback = Callable[[NativeInteractionRequest], NativeInteractionResponse]
STARTUP_TIMEOUT_GRACE_SECONDS = 0.5


class _InactivityTimeout:
    """Kill one process after a continuous period without Agent activity."""

    def __init__(self, seconds: float, on_expire: Callable[[], None]) -> None:
        self._seconds = seconds
        self._on_expire = on_expire
        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None
        self._paused = False
        self._closed = False

    def start(self, *, startup_grace_seconds: float = 0) -> None:
        """Arm the timeout after allowing the native process to start.

        CI machines and first-run CLIs can spend longer than a deliberately
        short test timeout before producing their first event. Once the first
        event arrives, ``reset`` switches back to the configured inactivity
        timeout. This grace period only affects the initial arm.
        """

        with self._lock:
            if self._closed or self._paused:
                return
            if self._timer is not None:
                self._timer.cancel()
            delay = max(self._seconds, max(0.0, float(startup_grace_seconds)))
            timer = threading.Timer(delay, self._expire)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def reset(self) -> None:
        with self._lock:
            if self._closed or self._paused:
                return
            if self._timer is not None:
                self._timer.cancel()
            timer = threading.Timer(self._seconds, self._expire)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def pause(self) -> None:
        with self._lock:
            self._paused = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def resume(self) -> None:
        with self._lock:
            if self._closed or not self._paused:
                return
            self._paused = False
        self.reset()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _expire(self) -> None:
        with self._lock:
            if self._closed or self._paused:
                return
            self._timer = None
            self._closed = True
        self._on_expire()


class _TimeoutBridge:
    """Pause a run timeout while Claude's MCP permission broker waits for UI."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._timeout: _InactivityTimeout | None = None
        self._waiting = 0

    def bind(self, timeout: _InactivityTimeout) -> None:
        with self._lock:
            self._timeout = timeout
            waiting = self._waiting > 0
        if waiting:
            timeout.pause()

    def unbind(self, timeout: _InactivityTimeout) -> None:
        with self._lock:
            if self._timeout is timeout:
                self._timeout = None

    def begin_wait(self) -> None:
        with self._lock:
            self._waiting += 1
            timeout = self._timeout
        if timeout is not None:
            timeout.pause()

    def end_wait(self) -> None:
        with self._lock:
            self._waiting = max(0, self._waiting - 1)
            timeout = self._timeout if self._waiting == 0 else None
        if timeout is not None:
            timeout.resume()


class _ClaudePermissionBroker:
    def __init__(self, resolver: InteractionCallback) -> None:
        self._resolver = resolver
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self._directory: Path | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def __enter__(self) -> "_ClaudePermissionBroker":
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="multiagent-claude-permission-"
        )
        self._directory = Path(self._temporary_directory.name)
        self._thread = threading.Thread(
            target=self._serve,
            name="multiagent-claude-permission",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type
        del exc
        del traceback
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()

    def command_args(self) -> tuple[str, ...]:
        if self._directory is None:
            raise BridgeError("Claude 权限代理尚未启动")
        config = {
            "mcpServers": {
                "multiagent_permission": {
                    "command": sys.executable,
                    "args": ["-m", "multiagent_cli.claude_permission_mcp"],
                    "env": {
                        "MULTIAGENT_CLAUDE_PERMISSION_DIR": str(self._directory),
                    },
                }
            }
        }
        return (
            "--mcp-config",
            json.dumps(config, ensure_ascii=False, separators=(",", ":")),
            "--permission-prompt-tool",
            "mcp__multiagent_permission__approve",
        )

    def _serve(self) -> None:
        assert self._directory is not None
        while not self._stop.is_set():
            for request_path in self._directory.glob("request-*.json"):
                request_id = request_path.stem.removeprefix("request-")
                processing_path = self._directory / f"processing-{request_id}.json"
                try:
                    request_path.replace(processing_path)
                except FileNotFoundError:
                    continue
                try:
                    payload = json.loads(processing_path.read_text(encoding="utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("permission payload must be an object")
                    request = _claude_permission_prompt_request(payload, "Claude")
                    response = self._resolver(request)
                    result = _claude_permission_prompt_response(payload, response)
                except Exception as exc:
                    result = {"behavior": "deny", "message": str(exc)}
                response_path = self._directory / f"response-{request_id}.json"
                pending_path = self._directory / f".response-{request_id}.tmp"
                pending_path.write_text(
                    json.dumps(result, ensure_ascii=False),
                    encoding="utf-8",
                )
                os.replace(pending_path, response_path)
                processing_path.unlink(missing_ok=True)
            self._stop.wait(0.05)


class CodexEventParser:
    def __init__(self) -> None:
        self.session_id: str | None = None
        self.final_text = ""
        self.errors: list[str] = []
        self.input_tokens = 0
        self.output_tokens = 0

    def feed(self, line: str) -> list[AgentEvent]:
        data = _json_line(line)
        if data is None:
            text = line.strip()
            return [AgentEvent("Codex", "log", text)] if text else []

        event_type = str(data.get("type", ""))
        if event_type == "thread.started":
            thread_id = data.get("thread_id")
            if isinstance(thread_id, str):
                self.session_id = thread_id
            return []

        if event_type in {"error", "turn.failed"}:
            text = _error_text(data)
            self.errors.append(text)
            return [AgentEvent("Codex", "error", text)]

        if event_type == "turn.completed":
            self._capture_usage(data.get("usage"))
            return []

        item = data.get("item")
        if not isinstance(item, dict):
            return []
        item_type = str(item.get("type", ""))

        if item_type == "agent_message":
            text = _first_text(item, "text", "content")
            if text and event_type in {"item.completed", "item.updated"}:
                self.final_text = text
                return [AgentEvent("Codex", "progress", text)]
            return []

        if item_type in {"command_execution", "mcp_tool_call", "file_change"}:
            if event_type == "item.started":
                detail = _first_text(item, "command", "name", "path") or item_type
                return [
                    AgentEvent(
                        "Codex",
                        "tool",
                        detail,
                        metadata=_tool_event_metadata(item_type, item),
                    )
                ]
            if event_type == "item.completed":
                status = item.get("status") or item.get("exit_code") or "completed"
                return [
                    AgentEvent(
                        "Codex",
                        "tool_result",
                        str(status),
                        status=_tool_result_event_status(item),
                        metadata=_tool_event_metadata(item_type, item),
                    )
                ]
        return []

    def _capture_usage(self, usage: object) -> None:
        if not isinstance(usage, dict):
            return
        self.input_tokens = _token_value(usage, "input_tokens", "inputTokens")
        self.output_tokens = _token_value(usage, "output_tokens", "outputTokens")


class ClaudeEventParser:
    def __init__(self) -> None:
        self.session_id: str | None = None
        self.final_text = ""
        self.errors: list[str] = []
        self.input_tokens = 0
        self.output_tokens = 0
        self._stream_prefix = ""
        self._stream_blocks: dict[int, str] = {}

    def feed(self, line: str) -> list[AgentEvent]:
        data = _json_line(line)
        if data is None:
            text = line.strip()
            return [AgentEvent("Claude", "log", text)] if text else []

        event_type = str(data.get("type", ""))
        session_id = data.get("session_id")
        if isinstance(session_id, str):
            self.session_id = session_id

        if event_type == "result":
            self._capture_usage(data.get("usage"))
            result = data.get("result")
            if isinstance(result, str) and result.strip():
                self.final_text = result.strip()
            if data.get("is_error"):
                text = self.final_text or _error_text(data)
                self.errors.append(text)
                return [AgentEvent("Claude", "error", text)]
            return []

        if event_type == "stream_event":
            return self._stream_events(data.get("event"))

        message = data.get("message")
        if not isinstance(message, dict):
            return []
        content = message.get("content")
        if not isinstance(content, list):
            return []

        events: list[AgentEvent] = []
        if event_type == "assistant":
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text" and isinstance(block.get("text"), str):
                    text = block["text"]
                    if text.strip():
                        if self._stream_blocks:
                            current = self._join_stream_text(
                                self._stream_prefix,
                                text,
                            )
                        else:
                            current = text.strip()
                        self.final_text = current
                        events.append(AgentEvent("Claude", "progress", current))
                elif block_type == "tool_use":
                    name = str(block.get("name") or "tool")
                    tool_input = block.get("input")
                    detail = _compact_json(tool_input)
                    events.append(
                        AgentEvent(
                            "Claude",
                            "tool",
                            f"{name}: {detail}" if detail else name,
                            metadata=_tool_event_metadata(
                                "tool",
                                tool_input if isinstance(tool_input, dict) else {},
                                tool_name=name,
                                activity_id=block.get("id"),
                            ),
                        )
                    )
        elif event_type == "user":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    failed = block.get("is_error") is True
                    output = block.get("content")
                    metadata = _tool_event_metadata(
                        "tool_result",
                        {"output": output},
                        activity_id=block.get("tool_use_id"),
                    )
                    events.append(
                        AgentEvent(
                            "Claude",
                            "tool_result",
                            "failed" if failed else "completed",
                            status="failed" if failed else "completed",
                            metadata=metadata,
                        )
                    )
        return events

    def _stream_events(self, raw_event: object) -> list[AgentEvent]:
        """Convert Claude's partial-message protocol into cumulative text.

        The UI session computes network deltas from cumulative progress events.
        Keeping that contract here also makes completed assistant messages
        harmless duplicates instead of rendering every token as a new block.
        """

        if not isinstance(raw_event, dict):
            return []
        stream_type = str(raw_event.get("type") or "")
        if stream_type == "message_start":
            current = self._current_stream_text()
            if current:
                self._stream_prefix = self._join_stream_text(
                    self._stream_prefix,
                    current,
                )
            self._stream_blocks.clear()
            return []
        if stream_type == "content_block_start":
            index = _stream_block_index(raw_event)
            block = raw_event.get("content_block")
            if index is None or not isinstance(block, dict):
                return []
            if block.get("type") == "text":
                text = str(block.get("text") or "")
                self._stream_blocks[index] = text
                if text:
                    cumulative = self._join_stream_text(
                        self._stream_prefix,
                        self._current_stream_text(),
                    )
                    self.final_text = cumulative
                    return [AgentEvent("Claude", "progress", cumulative)]
            return []
        if stream_type != "content_block_delta":
            return []
        index = _stream_block_index(raw_event)
        delta = raw_event.get("delta")
        if index is None or not isinstance(delta, dict):
            return []
        if delta.get("type") != "text_delta":
            return []
        text = delta.get("text")
        if not isinstance(text, str) or not text:
            return []
        self._stream_blocks[index] = self._stream_blocks.get(index, "") + text
        cumulative = self._join_stream_text(
            self._stream_prefix,
            self._current_stream_text(),
        )
        if not cumulative:
            return []
        self.final_text = cumulative
        return [AgentEvent("Claude", "progress", cumulative)]

    def _current_stream_text(self) -> str:
        return "".join(
            self._stream_blocks[index]
            for index in sorted(self._stream_blocks)
        )

    @staticmethod
    def _join_stream_text(prefix: str, current: str) -> str:
        if not prefix:
            return current
        if not current:
            return prefix
        return f"{prefix}\n\n{current}"

    def _capture_usage(self, usage: object) -> None:
        if not isinstance(usage, dict):
            return
        self.input_tokens = _token_value(usage, "input_tokens", "inputTokens")
        self.output_tokens = _token_value(usage, "output_tokens", "outputTokens")


class _CodexMessageCollector:
    """Separate transient Codex commentary from the final chat answer."""

    def __init__(self) -> None:
        self._phases: dict[str, str] = {}
        self._parts: dict[str, list[str]] = {}
        self._completed: set[str] = set()
        self._final_text = ""
        self._legacy_texts: list[str] = []

    def start(self, item: dict[str, Any]) -> None:
        if str(item.get("type") or "") != "agentMessage":
            return
        item_id = str(item.get("id") or "")
        if not item_id:
            return
        self._phases[item_id] = _codex_message_phase(item)
        self._parts.setdefault(item_id, [])

    def delta(self, params: dict[str, Any]) -> str:
        item_id = str(params.get("itemId") or params.get("item_id") or "")
        delta = str(params.get("delta") or "")
        if not item_id or not delta:
            return ""
        self._parts.setdefault(item_id, []).append(delta)
        phase = self._phases.get(item_id, "")
        if phase == "commentary":
            return ""
        current = "".join(self._parts[item_id])
        if phase == "final_answer":
            return current
        return "".join((*self._legacy_texts, current))

    def complete(self, item: dict[str, Any]) -> str:
        if str(item.get("type") or "") != "agentMessage":
            return ""
        item_id = str(item.get("id") or "")
        if item_id in self._completed:
            return ""
        if item_id:
            self._completed.add(item_id)
        phase = _codex_message_phase(item) or self._phases.get(item_id, "")
        text = _first_text(item, "text", "content")
        if not text and item_id:
            text = "".join(self._parts.get(item_id, ())).strip()
        if not text or phase == "commentary":
            return ""
        if phase == "final_answer":
            self._final_text = text
            return text
        self._legacy_texts.append(text)
        return "".join(self._legacy_texts)

    @property
    def final_text(self) -> str:
        return (self._final_text or "".join(self._legacy_texts)).strip()


class BaseCLIAdapter:
    display_name = "Agent"

    def __init__(
        self,
        settings: AgentCommandSettings,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.settings = settings
        self.environment = dict(environment or {})
        self._stop_requested = threading.Event()
        self._process_lock = threading.Lock()
        self._active_processes: set[subprocess.Popen] = set()
        self._interaction_handler: InteractionCallback | None = None
        self._incompatible_models: dict[str, str] = {}
        self._model_lock = threading.Lock()

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    def request_stop(self) -> None:
        """Stop every active native CLI process owned by this adapter."""

        self._stop_requested.set()
        with self._process_lock:
            processes = tuple(self._active_processes)
        for process in processes:
            signal_process_tree(process)
            escalation = threading.Timer(
                1.0,
                lambda item=process: signal_process_tree(item, force=True),
            )
            escalation.daemon = True
            escalation.start()

    def bind_interaction_handler(
        self,
        handler: InteractionCallback | None,
    ) -> None:
        """Attach the UI/CLI bridge that answers native approval requests."""

        self._interaction_handler = handler

    def version(self) -> str:
        try:
            completed = subprocess.run(
                [*self.settings.command, "--version"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BridgeError(f"无法启动 {self.display_name} CLI：{exc}") from exc
        output = (completed.stdout or completed.stderr).strip()
        if completed.returncode != 0:
            raise BridgeError(f"{self.display_name} CLI 检查失败：{output}")
        return output

    def build_command(
        self,
        *,
        workspace: Path,
        mode: str,
        session_id: str | None,
        model: str | None = None,
        interactive: bool = False,
    ) -> list[str]:
        raise NotImplementedError

    def new_parser(self) -> CodexEventParser | ClaudeEventParser:
        raise NotImplementedError

    @property
    def session_resume_enabled(self) -> bool:
        """Whether this adapter can safely keep one native model session."""

        candidates = self.settings.models or (
            (self.settings.model,) if self.settings.model else (None,)
        )
        return len(candidates) == 1

    def run(
        self,
        prompt: str,
        *,
        workspace: Path,
        mode: str,
        session_id: str | None = None,
        on_event: EventCallback | None = None,
        on_interaction: InteractionCallback | None = None,
        step_id: str = "",
    ) -> AgentRunResult:
        interaction_handler = on_interaction or self._interaction_handler
        configured_candidates: tuple[str | None, ...] = self.settings.models or (
            (self.settings.model,) if self.settings.model else (None,)
        )
        with self._model_lock:
            candidates = tuple(
                candidate
                for candidate in configured_candidates
                if candidate is None or candidate not in self._incompatible_models
            )
            disabled = dict(self._incompatible_models)
        if not candidates:
            model = configured_candidates[0] if configured_candidates else None
            raise AgentModelCompatibilityError(
                self.display_name,
                model,
                disabled.get(str(model), "本次服务中已确认该模型不兼容"),
            )
        multi_model = not self.session_resume_enabled
        compatibility_failures: list[AgentModelCompatibilityError] = []
        for index, model in enumerate(candidates):
            try:
                result = self._run_once(
                    prompt,
                    workspace=workspace,
                    mode=mode,
                    session_id=None if multi_model else session_id,
                    on_event=on_event,
                    on_interaction=interaction_handler,
                    step_id=step_id,
                    model=model,
                )
            except AgentTimeoutError:
                has_fallback = (
                    self.settings.fallback_on_timeout and index + 1 < len(candidates)
                )
                if not has_fallback:
                    raise
                next_model = candidates[index + 1]
                if on_event is not None:
                    on_event(
                        AgentEvent(
                            self.display_name,
                            "warning",
                            f"模型 {model or 'CLI 默认'} 响应超时，切换到 {next_model or 'CLI 默认'}",
                            step_id=step_id,
                            safe_summary=f"{self.display_name} · 模型超时，正在切换备用模型",
                            metadata={
                                "attempt": index + 1,
                                "timeout_seconds": self.settings.timeout,
                            },
                        )
                )
                continue
            except AgentModelCompatibilityError as exc:
                compatibility_failures.append(exc)
                if model is not None:
                    with self._model_lock:
                        self._incompatible_models[model] = exc.reason
                has_fallback = (
                    self.settings.fallback_on_timeout and index + 1 < len(candidates)
                )
                if not has_fallback:
                    raise
                next_model = candidates[index + 1]
                if on_event is not None:
                    on_event(
                        AgentEvent(
                            self.display_name,
                            "warning",
                            f"模型 {model or 'CLI 默认'} 与当前接口不兼容，切换到 {next_model or 'CLI 默认'}",
                            step_id=step_id,
                            safe_summary=f"{self.display_name} · 模型协议不兼容，正在切换备用模型",
                            metadata={"attempt": index + 1},
                        )
                )
                continue
            return replace(result, session_id=None) if multi_model else result
        if len(compatibility_failures) == len(candidates):
            # Do not collapse an all-model protocol rejection into the generic
            # "no available model" error; the UI can then explain the gateway
            # shape problem and the user can change provider/model settings.
            raise compatibility_failures[-1]
        raise BridgeError(f"{self.display_name} 没有可用模型")

    def _run_once(
        self,
        prompt: str,
        *,
        workspace: Path,
        mode: str,
        session_id: str | None,
        on_event: EventCallback | None,
        on_interaction: InteractionCallback | None,
        step_id: str,
        model: str | None,
        timeout_bridge: _TimeoutBridge | None = None,
    ) -> AgentRunResult:
        if self.stop_requested:
            raise KeyboardInterrupt
        started = time.monotonic()

        def emit(event: AgentEvent) -> None:
            if on_event is None:
                return
            elapsed = (
                event.elapsed_seconds
                if event.elapsed_seconds is not None
                else time.monotonic() - started
            )
            on_event(
                replace(
                    event,
                    step_id=event.step_id or step_id,
                    elapsed_seconds=round(elapsed, 3),
                    safe_summary=event.safe_summary or _safe_event_summary(event),
                )
            )

        command = self.build_command(
            workspace=workspace,
            mode=mode,
            session_id=session_id,
            model=model,
            interactive=on_interaction is not None,
        )
        parser = self.new_parser()
        timed_out = threading.Event()
        emit(
            AgentEvent(
                self.display_name,
                "lifecycle",
                "starting",
                status="starting",
                safe_summary=f"{self.display_name} · 正在启动模型",
            )
        )
        try:
            process = subprocess.Popen(
                command,
                cwd=str(workspace),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=self._subprocess_environment(),
                **isolated_process_kwargs(),
            )
        except OSError as exc:
            emit(
                AgentEvent(
                    self.display_name,
                    "lifecycle",
                    "failed_to_start",
                    status="failed",
                    safe_summary=f"{self.display_name} · 启动失败",
                )
            )
            raise BridgeError(f"无法启动 {self.display_name} CLI：{exc}") from exc

        with self._process_lock:
            self._active_processes.add(process)
        if self.stop_requested:
            signal_process_tree(process)

        def stop_on_timeout() -> None:
            timed_out.set()
            signal_process_tree(process, force=True)

        inactivity_timeout = _InactivityTimeout(self.settings.timeout, stop_on_timeout)
        if timeout_bridge is not None:
            timeout_bridge.bind(inactivity_timeout)
        inactivity_timeout.start(
            startup_grace_seconds=STARTUP_TIMEOUT_GRACE_SECONDS
        )
        stdin_lock = threading.Lock()
        writer: threading.Thread | None = None
        writer_errors: list[BaseException] = []

        try:
            if process.stdin is None or process.stdout is None:
                raise BridgeError(f"{self.display_name} CLI 管道初始化失败")

            native_claude = (
                isinstance(self, ClaudeAdapter)
                and (
                    on_interaction is not None
                    or Path(self.settings.command[0]).name.lower()
                    in {"claude", "claude-code", "claude.cmd", "claude-code.cmd"}
                )
            )
            initial_input = (
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": prompt}],
                        },
                    },
                    ensure_ascii=False,
                )
                if native_claude
                else prompt
            )
            if native_claude:
                _write_native_message(process, initial_input, lock=stdin_lock)
                if on_interaction is None:
                    process.stdin.close()
            else:
                def write_prompt() -> None:
                    try:
                        with stdin_lock:
                            process.stdin.write(initial_input)
                            process.stdin.flush()
                    except BaseException as exc:
                        if not self.stop_requested and not timed_out.is_set():
                            writer_errors.append(exc)
                    finally:
                        try:
                            process.stdin.close()
                        except BaseException as exc:
                            if not self.stop_requested and not timed_out.is_set():
                                writer_errors.append(exc)

                writer = threading.Thread(
                    target=write_prompt,
                    name=f"multiagent-{self.display_name.lower()}-stdin",
                    daemon=True,
                )
                writer.start()
            emit(
                AgentEvent(
                    self.display_name,
                    "lifecycle",
                    "waiting_model",
                    status="waiting_model",
                    safe_summary=f"{self.display_name} · 等待模型响应",
                )
            )
            for line in process.stdout:
                inactivity_timeout.reset()
                data = _json_line(line)
                native_request = (
                    _claude_interaction_request(data, self.display_name)
                    if data is not None and native_claude
                    else None
                )
                if native_request is not None:
                    inactivity_timeout.pause()
                    try:
                        response = _resolve_native_interaction(
                            native_request,
                            on_interaction=on_interaction,
                            emit=emit,
                        )
                    finally:
                        if not self.stop_requested:
                            inactivity_timeout.resume()
                    payload = _claude_interaction_response(data, response)
                    try:
                        _write_native_message(
                            process,
                            json.dumps(payload, ensure_ascii=False),
                            lock=stdin_lock,
                        )
                    except (BrokenPipeError, OSError):
                        if self.stop_requested:
                            raise KeyboardInterrupt
                        raise
                    continue
                for event in parser.feed(line):
                    emit(event)
                if (
                    native_claude
                    and data is not None
                    and data.get("type") == "result"
                    and not process.stdin.closed
                ):
                    # Claude's stream-json input stays open so approval replies
                    # can be sent during the turn. Once the terminal result is
                    # received, leaving it open makes newer Claude CLIs wait for
                    # another user message instead of exiting.
                    with stdin_lock:
                        process.stdin.close()
            process.stdout.close()
            exit_code = process.wait()
            if writer is not None:
                writer.join()
            if writer_errors:
                raise writer_errors[0]
        except KeyboardInterrupt:
            stop_process_tree(process)
            raise
        finally:
            inactivity_timeout.close()
            if timeout_bridge is not None:
                timeout_bridge.unbind(inactivity_timeout)
            if writer is not None and writer.is_alive():
                signal_process_tree(process, force=True)
                writer.join(timeout=1)
            with self._process_lock:
                self._active_processes.discard(process)
            for stream in (process.stdin, process.stdout):
                if stream is not None and not stream.closed:
                    stream.close()

        if self.stop_requested:
            emit(
                AgentEvent(
                    self.display_name,
                    "lifecycle",
                    "interrupted",
                    status="interrupted",
                    safe_summary=f"{self.display_name} · 已停止当前响应",
                )
            )
            raise KeyboardInterrupt

        if timed_out.is_set():
            emit(
                AgentEvent(
                    self.display_name,
                    "lifecycle",
                    "timed_out",
                    status="failed",
                    safe_summary=f"{self.display_name} · 响应超时",
                    metadata={"timeout_seconds": self.settings.timeout},
                )
            )
            raise AgentTimeoutError(
                f"{self.display_name} CLI 连续超过 {self.settings.timeout:g} 秒无活动，已终止"
            )

        if parser.errors or exit_code != 0:
            detail = parser.errors[-1] if parser.errors else "进程未返回可识别错误"
            compatibility_reason = _model_compatibility_reason(detail)
            emit(
                AgentEvent(
                    self.display_name,
                    "lifecycle",
                    "failed",
                    status="failed",
                    safe_summary=f"{self.display_name} · 本轮执行失败",
                    metadata={"exit_code": exit_code},
                )
            )
            if compatibility_reason:
                raise AgentModelCompatibilityError(
                    self.display_name,
                    model,
                    compatibility_reason,
                )
            raise BridgeError(f"{self.display_name} CLI 失败（退出码 {exit_code}）：{detail}")
        if not parser.final_text.strip():
            raise BridgeError(f"{self.display_name} CLI 没有返回最终文本")
        final_text = parser.final_text.strip()
        duration = time.monotonic() - started
        metric = {
            "duration_seconds": duration,
            "input_tokens": parser.input_tokens,
            "output_tokens": parser.output_tokens,
            "session_id": parser.session_id,
        }
        emit(
            AgentEvent(
                self.display_name,
                "lifecycle",
                "completed",
                status="completed",
                elapsed_seconds=duration,
                safe_summary=f"{self.display_name} · 已完成本轮响应",
                metadata={"exit_code": exit_code},
            )
        )
        emit(
            AgentEvent(
                self.display_name,
                "text",
                final_text,
                status="completed",
                elapsed_seconds=duration,
                safe_summary=f"{self.display_name} · 已生成本轮最终输出",
            )
        )
        emit(
            AgentEvent(
                self.display_name,
                "metric",
                json.dumps(metric, ensure_ascii=False),
                status="completed",
                elapsed_seconds=duration,
                safe_summary=f"{self.display_name} · 已记录本轮耗时与 Token",
                metadata=metric,
            )
        )
        return AgentRunResult(
            agent=self.display_name,
            final_text=final_text,
            session_id=parser.session_id,
            exit_code=exit_code,
            duration_seconds=duration,
            input_tokens=parser.input_tokens,
            output_tokens=parser.output_tokens,
        )

    def _subprocess_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(self.environment)
        return environment

class ClaudeAdapter(BaseCLIAdapter):
    display_name = "Claude"

    def __init__(
        self,
        settings: AgentCommandSettings,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(settings, environment=environment)
        self._permission_bridge = threading.local()

    def build_command(
        self,
        *,
        workspace: Path,
        mode: str,
        session_id: str | None,
        model: str | None = None,
        interactive: bool = False,
    ) -> list[str]:
        del workspace
        # Writable turns keep Claude's native workspace editing behavior. Any
        # protected operation outside that baseline is routed through the MCP
        # permission prompt tool when the UI interaction bridge is attached.
        permission_mode = self.settings.permission_mode or (
            "acceptEdits" if mode == "write" else "plan"
        )
        command = [
            *self.settings.command,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--permission-mode",
            permission_mode,
        ]
        bridge_args = getattr(self._permission_bridge, "args", ()) if interactive else ()
        command.extend(bridge_args)
        selected_model = model or self.settings.model
        if selected_model:
            command.extend(["--model", selected_model])
        if session_id:
            command.extend(["--resume", session_id])
        command.extend(self.settings.extra_args)
        return command

    def new_parser(self) -> ClaudeEventParser:
        return ClaudeEventParser()

    def _run_once(
        self,
        prompt: str,
        *,
        workspace: Path,
        mode: str,
        session_id: str | None,
        on_event: EventCallback | None,
        on_interaction: InteractionCallback | None,
        step_id: str,
        model: str | None,
    ) -> AgentRunResult:
        if on_interaction is None:
            return super()._run_once(
                prompt,
                workspace=workspace,
                mode=mode,
                session_id=session_id,
                on_event=on_event,
                on_interaction=None,
                step_id=step_id,
                model=model,
            )

        started = time.monotonic()

        def emit(event: AgentEvent) -> None:
            if on_event is None:
                return
            on_event(
                replace(
                    event,
                    step_id=event.step_id or step_id,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                    safe_summary=event.safe_summary or _safe_event_summary(event),
                )
            )

        def resolve(request: NativeInteractionRequest) -> NativeInteractionResponse:
            timeout_bridge.begin_wait()
            try:
                return _resolve_native_interaction(
                    request,
                    on_interaction=on_interaction,
                    emit=emit,
                )
            finally:
                timeout_bridge.end_wait()

        timeout_bridge = _TimeoutBridge()
        with _ClaudePermissionBroker(resolve) as broker:
            self._permission_bridge.args = broker.command_args()
            try:
                return super()._run_once(
                    prompt,
                    workspace=workspace,
                    mode=mode,
                    session_id=session_id,
                    on_event=on_event,
                    on_interaction=on_interaction,
                    step_id=step_id,
                    model=model,
                    timeout_bridge=timeout_bridge,
                )
            finally:
                self._permission_bridge.args = ()


class CodexAdapter(BaseCLIAdapter):
    display_name = "Codex"

    def __init__(
        self,
        settings: AgentCommandSettings,
        *,
        environment: Mapping[str, str] | None = None,
        token_api_base_url: str | None = None,
    ) -> None:
        super().__init__(settings, environment=environment)
        self.token_api_base_url = token_api_base_url.rstrip("/") if token_api_base_url else None

    @property
    def session_resume_enabled(self) -> bool:
        # MultiAgent owns the shared conversation history. Native Codex threads
        # stay ephemeral so they never appear in the ChatGPT/Codex client.
        return False

    def build_command(
        self,
        *,
        workspace: Path,
        mode: str,
        session_id: str | None,
        model: str | None = None,
        interactive: bool = False,
    ) -> list[str]:
        del interactive
        sandbox = "workspace-write" if mode == "write" else "read-only"
        provider_args = self._token_api_provider_args()
        selected_model = model or self.settings.model
        if session_id:
            command = [
                *self.settings.command,
                *provider_args,
                "-C",
                str(workspace),
                "--sandbox",
                sandbox,
                "--ask-for-approval",
                "never",
            ]
            if self.settings.reasoning_effort:
                command.extend([
                    "-c",
                    f'model_reasoning_effort="{self.settings.reasoning_effort}"',
                ])
            if selected_model:
                command.extend(["--model", selected_model])
            command.extend(["exec", "resume", "--json", session_id])
            command.extend(self.settings.extra_args)
            command.append("-")
            return command

        command = [
            *self.settings.command,
            *provider_args,
            "-C",
            str(workspace),
            "--sandbox",
            sandbox,
            "--ask-for-approval",
            "never",
        ]
        if self.settings.reasoning_effort:
            command.extend([
                "-c",
                f'model_reasoning_effort="{self.settings.reasoning_effort}"',
            ])
        if selected_model:
            command.extend(["--model", selected_model])
        command.extend(["exec", "--json", "--skip-git-repo-check", "--ephemeral"])
        command.extend(self.settings.extra_args)
        command.append("-")
        return command

    def new_parser(self) -> CodexEventParser:
        return CodexEventParser()

    def run(
        self,
        prompt: str,
        *,
        workspace: Path,
        mode: str,
        session_id: str | None = None,
        on_event: EventCallback | None = None,
        on_interaction: InteractionCallback | None = None,
        step_id: str = "",
    ) -> AgentRunResult:
        interaction_handler = on_interaction or self._interaction_handler
        if interaction_handler is None:
            return super().run(
                prompt,
                workspace=workspace,
                mode=mode,
                session_id=session_id,
                on_event=on_event,
                on_interaction=None,
                step_id=step_id,
            )
        return self._run_app_server(
            prompt,
            workspace=workspace,
            mode=mode,
            session_id=session_id,
            on_event=on_event,
            on_interaction=interaction_handler,
            step_id=step_id,
        )

    def _run_app_server(
        self,
        prompt: str,
        *,
        workspace: Path,
        mode: str,
        session_id: str | None,
        on_event: EventCallback | None,
        on_interaction: InteractionCallback,
        step_id: str,
    ) -> AgentRunResult:
        candidates: tuple[str | None, ...] = self.settings.models or (
            (self.settings.model,) if self.settings.model else (None,)
        )
        multi_model = not self.session_resume_enabled
        for index, model in enumerate(candidates):
            try:
                result = self._run_app_server_once(
                    prompt,
                    workspace=workspace,
                    mode=mode,
                    session_id=None if multi_model else session_id,
                    on_event=on_event,
                    on_interaction=on_interaction,
                    step_id=step_id,
                    model=model,
                )
            except AgentTimeoutError:
                has_fallback = self.settings.fallback_on_timeout and index + 1 < len(candidates)
                if not has_fallback:
                    raise
                next_model = candidates[index + 1]
                if on_event is not None:
                    on_event(
                        AgentEvent(
                            self.display_name,
                            "warning",
                            f"模型 {model or 'CLI 默认'} 响应超时，切换到 {next_model or 'CLI 默认'}",
                            step_id=step_id,
                            safe_summary=f"{self.display_name} · 模型超时，正在切换备用模型",
                            metadata={"attempt": index + 1, "timeout_seconds": self.settings.timeout},
                        )
                    )
                continue
            return replace(result, session_id=None) if multi_model else result
        raise BridgeError(f"{self.display_name} 没有可用模型")

    def _run_app_server_once(
        self,
        prompt: str,
        *,
        workspace: Path,
        mode: str,
        session_id: str | None,
        on_event: EventCallback | None,
        on_interaction: InteractionCallback,
        step_id: str,
        model: str | None,
    ) -> AgentRunResult:
        if self.stop_requested:
            raise KeyboardInterrupt
        started = time.monotonic()

        def emit(event: AgentEvent) -> None:
            if on_event is None:
                return
            on_event(
                replace(
                    event,
                    step_id=event.step_id or step_id,
                    elapsed_seconds=round(event.elapsed_seconds if event.elapsed_seconds is not None else time.monotonic() - started, 3),
                    safe_summary=event.safe_summary or _safe_event_summary(event),
                )
            )

        command = self.build_app_server_command()
        try:
            process = subprocess.Popen(
                command,
                cwd=str(workspace),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=self._subprocess_environment(),
                **isolated_process_kwargs(),
            )
        except OSError as exc:
            raise BridgeError(f"无法启动 {self.display_name} app-server：{exc}") from exc
        with self._process_lock:
            self._active_processes.add(process)
        timed_out = threading.Event()

        def stop_on_timeout() -> None:
            timed_out.set()
            signal_process_tree(process, force=True)

        inactivity_timeout = _InactivityTimeout(self.settings.timeout, stop_on_timeout)
        inactivity_timeout.start(
            startup_grace_seconds=STARTUP_TIMEOUT_GRACE_SECONDS
        )
        rpc_id = itertools.count(1)
        stdin_lock = threading.Lock()
        messages = _CodexMessageCollector()
        input_tokens = 0
        output_tokens = 0
        thread_id = session_id
        turn_complete = False

        def send_request(method: str, params: dict[str, Any]) -> int:
            request_id = next(rpc_id)
            _write_native_message(
                process,
                json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, ensure_ascii=False),
                lock=stdin_lock,
            )
            return request_id

        try:
            if process.stdin is None or process.stdout is None:
                raise BridgeError("Codex app-server 管道初始化失败")
            emit(AgentEvent(self.display_name, "lifecycle", "starting", status="starting", safe_summary="Codex · 正在启动模型"))
            initialize_id = send_request(
                "initialize",
                {"clientInfo": {"name": "multiagent", "version": "2"}, "capabilities": {"experimentalApi": True}},
            )
            pending_thread_request: int | None = None
            pending_turn_request: int | None = None
            emit(AgentEvent(self.display_name, "lifecycle", "waiting_model", status="waiting_model", safe_summary="Codex · 等待模型响应"))
            for line in process.stdout:
                # app-server emits tool and delta notifications while a turn is
                # active. The timeout is for inactivity, not total turn time.
                inactivity_timeout.reset()
                data = _json_line(line)
                if data is None:
                    continue
                request_id = data.get("id")
                # JSON-RPC server requests also carry an ``id``.  They must
                # not be mistaken for the response to one of our requests if
                # the server happens to reuse that numeric id.
                if request_id == initialize_id and "method" not in data:
                    _write_native_message(
                        process,
                        json.dumps({"jsonrpc": "2.0", "method": "initialized"}),
                        lock=stdin_lock,
                    )
                    if thread_id:
                        pending_thread_request = send_request(
                            "thread/resume",
                            _codex_thread_params(workspace, mode, model, thread_id=thread_id),
                        )
                    else:
                        pending_thread_request = send_request(
                            "thread/start",
                            _codex_thread_params(workspace, mode, model),
                        )
                    continue
                if (
                    pending_thread_request is not None
                    and request_id == pending_thread_request
                    and "method" not in data
                ):
                    pending_thread_request = None
                    if data.get("error") is not None:
                        raise BridgeError(
                            "Codex app-server thread 启动失败："
                            f"{_rpc_error_detail(data.get('error'))}"
                        )
                    result = data.get("result")
                    if not isinstance(result, dict):
                        raise BridgeError(
                            "Codex app-server 未返回会话信息："
                            f"{_rpc_error_detail(data)}"
                        )
                    thread = result.get("thread")
                    if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
                        raise BridgeError("Codex app-server 会话 ID 无效")
                    thread_id = thread["id"]
                    pending_turn_request = send_request(
                        "turn/start",
                        _codex_turn_params(
                            thread_id=thread_id,
                            prompt=prompt,
                            workspace=workspace,
                            mode=mode,
                            model=model,
                            reasoning_effort=self.settings.reasoning_effort,
                        ),
                    )
                    continue
                if (
                    pending_turn_request is not None
                    and request_id == pending_turn_request
                    and "method" not in data
                ):
                    pending_turn_request = None
                    if data.get("error") is not None:
                        raise BridgeError(
                            "Codex app-server turn 启动失败："
                            f"{_rpc_error_detail(data.get('error'))}"
                        )
                    continue
                if "method" in data and "id" in data:
                    native_request = _codex_interaction_request(data, self.display_name)
                    if native_request is None:
                        _write_jsonrpc_error(process, data["id"], "不支持的原生交互请求", lock=stdin_lock)
                        continue
                    inactivity_timeout.pause()
                    response = _resolve_native_interaction(native_request, on_interaction=on_interaction, emit=emit)
                    try:
                        _write_native_message(
                            process,
                            json.dumps({"jsonrpc": "2.0", "id": data["id"], "result": _codex_interaction_response(data, response)}, ensure_ascii=False),
                            lock=stdin_lock,
                        )
                    except (BrokenPipeError, OSError):
                        if self.stop_requested:
                            raise KeyboardInterrupt
                        raise
                    if not self.stop_requested:
                        inactivity_timeout.resume()
                    continue
                method = str(data.get("method") or "")
                params = data.get("params") if isinstance(data.get("params"), dict) else {}
                if method == "item/agentMessage/delta":
                    progress = messages.delta(params)
                    if progress:
                        emit(AgentEvent(self.display_name, "progress", progress))
                elif method == "item/started":
                    item = params.get("item") if isinstance(params.get("item"), dict) else {}
                    item_type = str(item.get("type") or "")
                    if item_type == "agentMessage":
                        messages.start(item)
                    elif item_type in {"commandExecution", "fileChange", "mcpToolCall"}:
                        emit(
                            AgentEvent(
                                self.display_name,
                                "tool",
                                _codex_item_detail(item) or item_type,
                                metadata=_tool_event_metadata(item_type, item),
                            )
                        )
                elif method == "item/completed":
                    item = params.get("item") if isinstance(params.get("item"), dict) else {}
                    if str(item.get("type") or "") == "agentMessage":
                        progress = messages.complete(item)
                        if progress and _codex_message_phase(item) != "commentary":
                            emit(AgentEvent(self.display_name, "progress", progress))
                    elif item:
                        emit(
                            AgentEvent(
                                self.display_name,
                                "tool_result",
                                str(item.get("status") or "completed"),
                                status=_tool_result_event_status(item),
                                metadata=_tool_event_metadata(
                                    str(item.get("type") or "tool"),
                                    item,
                                ),
                            )
                        )
                elif method == "thread/tokenUsage/updated":
                    usage = params.get("tokenUsage") if isinstance(params.get("tokenUsage"), dict) else params.get("usage")
                    if isinstance(usage, dict):
                        input_tokens = _token_value(usage, "inputTokens", "input_tokens")
                        output_tokens = _token_value(usage, "outputTokens", "output_tokens")
                elif method == "turn/completed":
                    turn = params.get("turn") if isinstance(params.get("turn"), dict) else params
                    status = str(turn.get("status") or "") if isinstance(turn, dict) else ""
                    error = turn.get("error") if isinstance(turn, dict) else None
                    if status.lower() in {"failed", "error", "cancelled"}:
                        raise BridgeError(f"Codex app-server 本轮失败：{error or status}")
                    turn_complete = True
                    break
                elif method == "error":
                    raise BridgeError(f"Codex app-server 错误：{params.get('message') or params}")
            if not turn_complete:
                # On Windows the child may have already closed stdout while
                # its process handle is still being reaped. Give it a short
                # bounded wait before reporting a premature exit; otherwise
                # ``poll()`` can transiently return None and turn a successful
                # fake/real app-server completion into a false failure.
                try:
                    exit_code = process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    exit_code = process.poll()
                if timed_out.is_set():
                    raise AgentTimeoutError(f"Codex CLI 超过 {self.settings.timeout:g} 秒未完成，已终止")
                raise BridgeError(f"Codex app-server 提前退出（退出码 {exit_code}）")
            final_text = messages.final_text
            if not final_text:
                raise BridgeError("Codex app-server 没有返回最终文本")
            duration = time.monotonic() - started
            emit(AgentEvent(self.display_name, "lifecycle", "completed", status="completed", elapsed_seconds=duration, safe_summary="Codex · 已完成本轮响应"))
            emit(AgentEvent(self.display_name, "text", final_text, status="completed", elapsed_seconds=duration, safe_summary="Codex · 已生成本轮最终输出"))
            return AgentRunResult(self.display_name, final_text, session_id=thread_id, duration_seconds=duration, input_tokens=input_tokens, output_tokens=output_tokens)
        except KeyboardInterrupt:
            stop_process_tree(process)
            raise
        finally:
            inactivity_timeout.close()
            signal_process_tree(process, force=True)
            with self._process_lock:
                self._active_processes.discard(process)
            for stream in (process.stdin, process.stdout):
                if stream is not None and not stream.closed:
                    stream.close()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                stop_process_tree(process)

    def build_app_server_command(self) -> list[str]:
        return [
            *self.settings.command,
            *self._token_api_provider_args(),
            "app-server",
            *self._app_server_extra_args(),
            "--stdio",
        ]

    def _app_server_extra_args(self) -> list[str]:
        args = list(self.settings.extra_args)
        supported_with_value = {"-c", "--config", "--enable", "--disable"}
        supported_flags = {"--strict-config"}
        normalized: list[str] = []
        index = 0
        while index < len(args):
            value = args[index]
            if value in supported_flags:
                normalized.append(value)
                index += 1
                continue
            if value in supported_with_value:
                if index + 1 >= len(args):
                    raise BridgeError(f"Codex 附加参数 {value} 缺少值")
                normalized.extend((value, args[index + 1]))
                index += 2
                continue
            if any(
                value.startswith(f"{option}=")
                for option in ("--config", "--enable", "--disable")
            ):
                normalized.append(value)
                index += 1
                continue
            raise BridgeError(
                "Codex 在网页审批模式下使用 app-server；附加参数仅支持 "
                "-c/--config、--enable、--disable 和 --strict-config。"
                f"不支持：{value}"
            )
        return normalized

    def _token_api_provider_args(self) -> list[str]:
        if not self.token_api_base_url:
            return []
        base_url = self.token_api_base_url
        if not base_url.endswith("/v1"):
            base_url = f"{base_url}/v1"
        values = (
            'model_provider="OpenAI"',
            'model_providers.OpenAI.name="OpenAI"',
            f"model_providers.OpenAI.base_url={json.dumps(base_url)}",
            'model_providers.OpenAI.wire_api="responses"',
            "model_providers.OpenAI.requires_openai_auth=true",
            "disable_response_storage=true",
            "model_context_window=1000000",
            "model_auto_compact_token_limit=900000",
        )
        args: list[str] = []
        for value in values:
            args.extend(["-c", value])
        return args


def _json_line(line: str) -> dict[str, Any] | None:
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _rpc_error_detail(value: object) -> str:
    """Return a compact, useful description for a JSON-RPC error/response."""

    if isinstance(value, dict):
        message = value.get("message") or value.get("detail") or value.get("reason")
        code = value.get("code")
        if message:
            return f"{message}（code={code}）" if code is not None else str(message)
        if code is not None:
            return f"code={code}"
        return _compact_json(value, 800)
    if isinstance(value, str) and value:
        return value
    if value is None:
        return "响应为空"
    return _compact_json(value, 800)


def _stream_block_index(data: dict[str, Any]) -> int | None:
    value = data.get("index")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _write_native_message(
    process: subprocess.Popen,
    text: str,
    *,
    lock: threading.Lock,
) -> None:
    if process.stdin is None:
        raise BridgeError("原生 Agent 输入管道不可用")
    payload = text if text.endswith("\n") else f"{text}\n"
    with lock:
        process.stdin.write(payload)
        process.stdin.flush()


def _resolve_native_interaction(
    request: NativeInteractionRequest,
    *,
    on_interaction: InteractionCallback | None,
    emit: EventCallback,
) -> NativeInteractionResponse:
    if on_interaction is None:
        raise NativeInteractionUnavailable(
            f"{request.source} 正在等待原生交互，但当前运行方式无法显示审批窗口"
        )
    emit(
        AgentEvent(
            request.source,
            "interaction_request",
            request.title,
            status="waiting_user",
            safe_summary=f"{request.source} · 等待你的确认或输入",
        )
    )
    response = on_interaction(request)
    if not isinstance(response, NativeInteractionResponse):
        raise BridgeError("原生交互处理器返回了无效响应")
    emit(
        AgentEvent(
            request.source,
            "interaction_response",
            response.action,
            status="working",
            safe_summary=f"{request.source} · 已收到你的决定，继续处理",
        )
    )
    return response


def _interaction_options(*values: tuple[str, str, str]) -> tuple[NativeInteractionOption, ...]:
    return tuple(NativeInteractionOption(value, label, description) for value, label, description in values)


def _claude_permission_prompt_request(
    data: dict[str, Any],
    source: str,
) -> NativeInteractionRequest:
    tool_name = str(
        data.get("tool_name")
        or data.get("toolName")
        or data.get("name")
        or "操作"
    )
    tool_input = next(
        (
            value
            for value in (data.get("input"), data.get("tool_input"), data.get("toolInput"))
            if isinstance(value, dict)
        ),
        {},
    )
    command = str(tool_input.get("command") or tool_input.get("cmd") or "")
    reason = str(
        data.get("reason")
        or data.get("description")
        or data.get("message")
        or ""
    )
    request_id = str(
        data.get("tool_use_id")
        or data.get("toolUseId")
        or data.get("request_id")
        or data.get("requestId")
        or secrets.token_urlsafe(12)
    )
    normalized_tool = tool_name.replace("_", "").lower()
    kind = (
        "command_approval"
        if command or normalized_tool in {"bash", "shell", "terminal"}
        else "network_approval"
        if normalized_tool in {"webfetch", "websearch"}
        else "file_approval"
    )
    cwd = str(tool_input.get("cwd") or "")
    if not cwd:
        path = tool_input.get("file_path") or tool_input.get("path")
        if isinstance(path, str):
            cwd = _path_parent_for_display(path)
    return NativeInteractionRequest(
        id=request_id,
        source=source,
        kind=kind,
        title=f"{source} 请求使用 {tool_name}",
        message=reason or _compact_json(tool_input, 1200),
        command=command,
        cwd=cwd,
        options=_interaction_options(
            ("approve", "允许一次", "仅允许当前操作"),
            ("deny", "拒绝", "拒绝操作，但让 Agent 继续思考其他办法"),
            ("cancel", "拒绝并停止", "拒绝操作并终止当前回复"),
        ),
        metadata={"provider": "claude", "tool_name": tool_name},
    )


def _path_parent_for_display(path: str) -> str:
    """Preserve provider path spelling in cross-platform UI payloads."""

    value = str(path or "")
    # Provider payloads can contain POSIX paths even when the bridge itself is
    # running on Windows (for example in protocol compatibility tests or WSL).
    if value.startswith("/") and not value.startswith("//"):
        return posixpath.dirname(value) or "/"
    return str(Path(value).parent)


def _claude_permission_prompt_response(
    data: dict[str, Any],
    response: NativeInteractionResponse,
) -> dict[str, Any]:
    if response.action == "approve":
        tool_input = next(
            (
                value
                for value in (
                    data.get("input"),
                    data.get("tool_input"),
                    data.get("toolInput"),
                )
                if isinstance(value, dict)
            ),
            {},
        )
        return {"behavior": "allow", "updatedInput": tool_input}
    return {
        "behavior": "deny",
        "message": response.text
        or (
            "用户拒绝并要求停止"
            if response.action == "cancel"
            else "用户拒绝了此操作"
        ),
        "interrupt": response.action == "cancel",
    }


def _claude_interaction_request(
    data: dict[str, Any],
    source: str,
) -> NativeInteractionRequest | None:
    if data.get("type") != "control_request":
        return None
    request = data.get("request") if isinstance(data.get("request"), dict) else data
    subtype = str(request.get("subtype") or data.get("subtype") or "")
    if subtype != "can_use_tool":
        return None
    request_id = str(data.get("request_id") or data.get("requestId") or request.get("request_id") or "")
    if not request_id:
        return None
    tool_name = str(request.get("tool_name") or request.get("toolName") or "操作")
    tool_input = request.get("input") if isinstance(request.get("input"), dict) else {}
    command = str(tool_input.get("command") or tool_input.get("cmd") or "")
    reason = str(request.get("reason") or request.get("description") or "")
    kind = "command_approval" if command or tool_name.lower() in {"bash", "shell"} else "file_approval"
    return NativeInteractionRequest(
        id=request_id,
        source=source,
        kind=kind,
        title=f"{source} 请求使用 {tool_name}",
        message=reason or _compact_json(tool_input, 1200),
        command=command,
        cwd=str(tool_input.get("cwd") or ""),
        options=_interaction_options(
            ("approve", "允许一次", "仅允许当前操作"),
            ("deny", "拒绝", "拒绝操作，但让 Agent 继续思考其他办法"),
            ("cancel", "拒绝并停止", "拒绝操作并终止当前回复"),
        ),
        metadata={"provider": "claude", "tool_name": tool_name},
    )


def _claude_interaction_response(
    data: dict[str, Any],
    response: NativeInteractionResponse,
) -> dict[str, Any]:
    request = data.get("request") if isinstance(data.get("request"), dict) else {}
    request_id = str(
        data.get("request_id")
        or data.get("requestId")
        or request.get("request_id")
        or request.get("requestId")
        or ""
    )
    if response.action == "approve":
        result: dict[str, Any] = {"behavior": "allow"}
    else:
        result = {
            "behavior": "deny",
            "message": response.text or ("用户拒绝并要求停止" if response.action == "cancel" else "用户拒绝了此操作"),
            "interrupt": response.action == "cancel",
        }
    return {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": request_id,
            "response": result,
        },
    }


def _codex_thread_params(
    workspace: Path,
    mode: str,
    model: str | None,
    *,
    thread_id: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "cwd": str(workspace),
        **_codex_approval_params(),
        "sandbox": "workspace-write" if mode == "write" else "read-only",
    }
    if thread_id:
        params["threadId"] = thread_id
    else:
        params["ephemeral"] = True
    if model:
        params["model"] = model
    return params


def _codex_turn_params(
    *,
    thread_id: str,
    prompt: str,
    workspace: Path,
    mode: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "threadId": thread_id,
        "input": [{"type": "text", "text": prompt}],
        "cwd": str(workspace),
        **_codex_approval_params(),
        "sandboxPolicy": _codex_sandbox_policy(mode),
    }
    if reasoning_effort:
        params["effort"] = reasoning_effort
    if model:
        params["model"] = model
    return params


def _codex_approval_params() -> dict[str, str]:
    return {"approvalPolicy": "on-request", "approvalsReviewer": "user"}


def _codex_sandbox_policy(mode: str) -> dict[str, Any]:
    if mode == "write":
        return {
            "type": "workspaceWrite",
            "writableRoots": [],
            "networkAccess": False,
            "excludeTmpdirEnvVar": False,
            "excludeSlashTmp": False,
        }
    return {"type": "readOnly"}


def _codex_interaction_request(
    data: dict[str, Any],
    source: str,
) -> NativeInteractionRequest | None:
    method = str(data.get("method") or "")
    params = data.get("params") if isinstance(data.get("params"), dict) else {}
    request_id = str(data.get("id") or "")
    metadata = {"provider": "codex", "method": method}
    if method in {
        "item/commandExecution/requestApproval",
        "execCommandApproval",
    }:
        command_value = params.get("command")
        if isinstance(command_value, list):
            command_value = " ".join(str(item) for item in command_value)
        return NativeInteractionRequest(
            id=request_id,
            source=source,
            kind="command_approval",
            title=f"{source} 请求执行命令",
            message=str(params.get("reason") or ""),
            command=str(command_value or ""),
            cwd=str(params.get("cwd") or ""),
            options=_interaction_options(
                ("approve", "允许一次", "仅允许当前命令"),
                ("approve_session", "本次会话允许", "同类命令在本次会话中不再询问"),
                ("deny", "拒绝", "拒绝命令，让 Agent 继续寻找其他方案"),
                ("cancel", "拒绝并停止", "拒绝命令并终止当前回复"),
            ),
            metadata={
                **metadata,
                "available_decisions": params.get("availableDecisions"),
                "call_id": params.get("callId"),
            },
        )
    if method in {
        "item/fileChange/requestApproval",
        "applyPatchApproval",
    }:
        return NativeInteractionRequest(
            id=request_id,
            source=source,
            kind="file_approval",
            title=f"{source} 请求修改文件",
            message=str(params.get("reason") or ""),
            cwd=str(params.get("grantRoot") or params.get("cwd") or ""),
            options=_interaction_options(
                ("approve", "允许一次", "仅允许当前文件修改"),
                ("approve_session", "本次会话允许", "本次会话后续修改不再询问"),
                ("deny", "拒绝", "拒绝修改，让 Agent 继续处理"),
                ("cancel", "拒绝并停止", "拒绝修改并终止当前回复"),
            ),
            metadata=metadata,
        )
    if method == "item/permissions/requestApproval":
        return NativeInteractionRequest(
            id=request_id,
            source=source,
            kind="permission_approval",
            title=f"{source} 请求扩展权限",
            message=str(params.get("reason") or ""),
            cwd=str(params.get("cwd") or ""),
            options=_interaction_options(
                ("approve", "允许本轮", "仅为当前回复授予请求的权限"),
                ("approve_session", "本次会话允许", "为本次会话授予请求的权限"),
                ("deny", "拒绝", "不授予额外权限"),
                ("cancel", "拒绝并停止", "拒绝权限并终止当前回复"),
            ),
            metadata={**metadata, "permissions": params.get("permissions")},
        )
    if method == "item/tool/requestUserInput":
        questions: list[NativeInteractionQuestion] = []
        for index, raw in enumerate(params.get("questions") or []):
            if not isinstance(raw, dict):
                continue
            options = tuple(
                NativeInteractionOption(
                    str(option.get("label") or ""),
                    str(option.get("label") or ""),
                    str(option.get("description") or ""),
                )
                for option in raw.get("options") or []
                if isinstance(option, dict) and str(option.get("label") or "")
            )
            questions.append(
                NativeInteractionQuestion(
                    id=str(raw.get("id") or f"question_{index + 1}"),
                    question=str(raw.get("question") or "请提供信息"),
                    header=str(raw.get("header") or ""),
                    options=options,
                    allow_other=bool(raw.get("isOther")),
                    secret=bool(raw.get("isSecret")),
                )
            )
        return NativeInteractionRequest(
            id=request_id,
            source=source,
            kind="user_input",
            title=f"{source} 需要你的补充信息",
            questions=tuple(questions),
            options=_interaction_options(
                ("submit", "提交回答", "把回答发送给 Agent"),
                ("cancel", "取消回复", "终止当前回复"),
            ),
            metadata={**metadata, "blocking": bool(params.get("isBlocking", True))},
        )
    if method == "mcpServer/elicitation/request":
        return NativeInteractionRequest(
            id=request_id,
            source=source,
            kind="user_input",
            title=f"{source} 需要 MCP 补充信息",
            message=str(params.get("message") or "请提供信息"),
            options=_interaction_options(
                ("submit", "提交回答", "把回答发送给 Agent"),
                ("cancel", "取消回复", "终止当前回复"),
            ),
            questions=(
                NativeInteractionQuestion(
                    id="response",
                    question="请填写 MCP 服务所需的信息",
                    options=(),
                    allow_other=True,
                ),
            ),
            metadata={**metadata, "server_name": params.get("serverName")},
        )
    return None


def _codex_interaction_response(
    data: dict[str, Any],
    response: NativeInteractionResponse,
) -> dict[str, Any]:
    method = str(data.get("method") or "")
    if method in {"item/tool/requestUserInput", "mcpServer/elicitation/request"}:
        if method == "mcpServer/elicitation/request":
            if response.action == "cancel":
                return {"action": "cancel"}
            if response.action == "deny":
                return {"action": "decline"}
            return {
                "action": "accept",
                "content": {
                    key: values[0] if len(values) == 1 else list(values)
                    for key, values in response.normalized_answers().items()
                },
            }
        return {
            "answers": {
                key: {"answers": list(values)}
                for key, values in response.normalized_answers().items()
            }
        }
    if method in {"applyPatchApproval", "item/fileChange/requestApproval"}:
        decision = {
            "approve": "accept",
            "approve_session": "acceptForSession",
            "deny": "decline",
            "cancel": "cancel",
        }.get(response.action, "decline")
        return {"decision": decision}
    if method == "item/permissions/requestApproval":
        params = data.get("params") if isinstance(data.get("params"), dict) else {}
        permissions = params.get("permissions") if response.action in {"approve", "approve_session"} else {}
        return {
            "permissions": permissions if isinstance(permissions, dict) else {},
            "scope": "session" if response.action == "approve_session" else "turn",
        }
    decision = {
        "approve": "accept",
        "approve_session": "acceptForSession",
        "deny": "decline",
        "cancel": "cancel",
    }.get(response.action, "decline")
    return {"decision": decision}


def _write_jsonrpc_error(
    process: subprocess.Popen,
    request_id: object,
    message: str,
    *,
    lock: threading.Lock,
) -> None:
    _write_native_message(
        process,
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": message},
            },
            ensure_ascii=False,
        ),
        lock=lock,
    )


def _codex_item_detail(item: dict[str, Any]) -> str:
    return _first_text(item, "command", "path", "name", "tool")


def _codex_message_phase(item: dict[str, Any]) -> str:
    phase = str(item.get("phase") or "").strip().lower()
    return phase if phase in {"commentary", "final_answer"} else ""


def _tool_event_metadata(
    item_type: str,
    item: dict[str, Any],
    *,
    tool_name: str = "",
    activity_id: object = None,
) -> dict[str, Any]:
    """Normalize provider-specific tool data before public redaction."""

    normalized_type = item_type.replace("_", "").replace("-", "").lower()
    resolved_tool = tool_name or _first_text(item, "name", "tool")
    normalized_tool = resolved_tool.replace("_", "").replace("-", "").lower()
    command = _first_text(item, "command", "cmd")
    path = _first_text(item, "file_path", "path", "file")
    output_value = next(
        (
            item.get(key)
            for key in ("aggregated_output", "output", "result", "content")
            if item.get(key) not in (None, "")
        ),
        None,
    )
    output = (
        output_value.strip()
        if isinstance(output_value, str)
        else _compact_json(output_value, 6_000)
    )
    command_tools = {"bash", "shell", "terminal", "execute", "runcommand"}
    read_tools = {"read", "ls", "list", "glob", "webfetch"}
    search_tools = {"grep", "search", "find", "websearch"}
    write_tools = {"edit", "write", "multiedit", "notebookedit", "applypatch"}
    if "command" in normalized_type or normalized_tool in command_tools or command:
        activity_type = "command"
    elif "filechange" in normalized_type or normalized_tool in write_tools:
        activity_type = "file_change"
    elif normalized_tool in search_tools:
        activity_type = "search"
    elif normalized_tool in read_tools or path:
        activity_type = "read"
    else:
        activity_type = "tool"
    identifier = activity_id if activity_id is not None else item.get("id")
    metadata: dict[str, Any] = {
        "activity_type": activity_type,
        "tool_name": resolved_tool,
        "command": command,
        "path": path,
        "output": output,
        "detail": _compact_json(item, 6_000),
    }
    if identifier is not None:
        metadata["activity_id"] = str(identifier)
    exit_code = item.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        metadata["exit_code"] = exit_code
    return metadata


def _tool_result_event_status(item: dict[str, Any]) -> str:
    raw_status = str(item.get("status") or "").lower()
    exit_code = item.get("exit_code")
    if raw_status in {"failed", "error", "cancelled", "canceled"}:
        return "failed"
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
        return "failed"
    return "completed"


def _first_text(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            texts = [
                block.get("text", "")
                for block in value
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            ]
            if texts:
                return "".join(texts).strip()
    return ""


def _error_text(data: dict[str, Any]) -> str:
    error = data.get("error") or data.get("message") or data
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error)


def _model_compatibility_reason(detail: str) -> str:
    """Return a bounded public reason for known CLI/provider shape failures."""

    normalized = detail.lower()
    validation_markers = (
        "request validation",
        "extra inputs are not permitted",
        "input should be a valid string",
        "validation error",
    )
    if "cache_control" in normalized and any(
        marker in normalized for marker in validation_markers
    ):
        return "模型接口拒绝了 Claude Code 的缓存内容块（cache_control）"
    unsupported_model_markers = (
        "model not found",
        "model does not exist",
        "unknown model",
        "unsupported model",
        "invalid model",
        "model is not supported",
    )
    if any(marker in normalized for marker in unsupported_model_markers):
        return "当前服务不支持该模型名称或原生 Agent 调用方式"
    return ""


def _compact_json(value: Any, limit: int = 400) -> str:
    if value is None:
        return ""
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        text = str(value)
    return text if len(text) <= limit else f"{text[:limit]}…"


def _token_value(data: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def _safe_event_summary(event: AgentEvent) -> str:
    if event.kind == "progress":
        return f"{event.source} · 正在处理模型输出"
    if event.kind == "tool":
        activity = str(event.metadata.get("activity_type") or "tool")
        label = {
            "command": "正在执行命令",
            "file_change": "正在修改文件",
            "read": "正在读取文件",
            "search": "正在搜索内容",
        }.get(activity, "正在调用工具")
        return f"{event.source} · {label}"
    if event.kind == "tool_result":
        activity = str(event.metadata.get("activity_type") or "tool")
        label = {
            "command": "命令执行完成",
            "file_change": "文件操作完成",
            "read": "读取完成",
            "search": "搜索完成",
        }.get(activity, "工具调用完成")
        return f"{event.source} · {label}"
    if event.kind == "error":
        return f"{event.source} · 本轮发生错误"
    if event.kind == "log":
        return f"{event.source} · 原生运行日志"
    return ""
