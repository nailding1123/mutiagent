from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from .bridge_models import (
    AgentCommandSettings,
    AgentEvent,
    AgentRunResult,
    AgentTimeoutError,
    BridgeError,
)
from .process_control import isolated_process_kwargs, signal_process_tree, stop_process_tree


EventCallback = Callable[[AgentEvent], None]


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
                return [AgentEvent("Codex", "tool", detail)]
            if event_type == "item.completed":
                status = item.get("status") or item.get("exit_code") or "completed"
                return [AgentEvent("Codex", "tool_result", str(status))]
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
                    text = block["text"].strip()
                    if text:
                        self.final_text = text
                        events.append(AgentEvent("Claude", "progress", text))
                elif block_type == "tool_use":
                    name = str(block.get("name") or "tool")
                    tool_input = block.get("input")
                    detail = _compact_json(tool_input)
                    events.append(
                        AgentEvent("Claude", "tool", f"{name}: {detail}" if detail else name)
                    )
        elif event_type == "user":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    if block.get("is_error"):
                        events.append(AgentEvent("Claude", "tool_result", "failed"))
        return events

    def _capture_usage(self, usage: object) -> None:
        if not isinstance(usage, dict):
            return
        self.input_tokens = _token_value(usage, "input_tokens", "inputTokens")
        self.output_tokens = _token_value(usage, "output_tokens", "outputTokens")


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
        step_id: str = "",
    ) -> AgentRunResult:
        candidates: tuple[str | None, ...] = self.settings.models or (
            (self.settings.model,) if self.settings.model else (None,)
        )
        multi_model = not self.session_resume_enabled
        for index, model in enumerate(candidates):
            try:
                result = self._run_once(
                    prompt,
                    workspace=workspace,
                    mode=mode,
                    session_id=None if multi_model else session_id,
                    on_event=on_event,
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
            return replace(result, session_id=None) if multi_model else result
        raise BridgeError(f"{self.display_name} 没有可用模型")

    def _run_once(
        self,
        prompt: str,
        *,
        workspace: Path,
        mode: str,
        session_id: str | None,
        on_event: EventCallback | None,
        step_id: str,
        model: str | None,
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

        timer = threading.Timer(self.settings.timeout, stop_on_timeout)
        timer.daemon = True
        timer.start()
        writer: threading.Thread | None = None
        writer_errors: list[BaseException] = []

        try:
            if process.stdin is None or process.stdout is None:
                raise BridgeError(f"{self.display_name} CLI 管道初始化失败")

            def write_prompt() -> None:
                try:
                    process.stdin.write(prompt)
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
                for event in parser.feed(line):
                    emit(event)
            process.stdout.close()
            exit_code = process.wait()
            writer.join()
            if writer_errors:
                raise writer_errors[0]
        except KeyboardInterrupt:
            stop_process_tree(process)
            raise
        finally:
            timer.cancel()
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
                f"{self.display_name} CLI 超过 {self.settings.timeout:g} 秒未完成，已终止"
            )

        if exit_code != 0:
            detail = parser.errors[-1] if parser.errors else "进程未返回可识别错误"
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

    def build_command(
        self,
        *,
        workspace: Path,
        mode: str,
        session_id: str | None,
        model: str | None = None,
    ) -> list[str]:
        del workspace
        permission_mode = "acceptEdits" if mode == "write" else "plan"
        command = [
            *self.settings.command,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            permission_mode,
        ]
        selected_model = model or self.settings.model
        if selected_model:
            command.extend(["--model", selected_model])
        if session_id:
            command.extend(["--resume", session_id])
        command.extend(self.settings.extra_args)
        return command

    def new_parser(self) -> ClaudeEventParser:
        return ClaudeEventParser()


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

    def build_command(
        self,
        *,
        workspace: Path,
        mode: str,
        session_id: str | None,
        model: str | None = None,
    ) -> list[str]:
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
        if selected_model:
            command.extend(["--model", selected_model])
        command.extend(["exec", "--json", "--skip-git-repo-check"])
        command.extend(self.settings.extra_args)
        command.append("-")
        return command

    def new_parser(self) -> CodexEventParser:
        return CodexEventParser()

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
        return f"{event.source} · 正在执行内部操作"
    if event.kind == "tool_result":
        return f"{event.source} · 已完成内部操作"
    if event.kind == "error":
        return f"{event.source} · 本轮发生错误"
    if event.kind == "log":
        return f"{event.source} · 原生运行日志"
    return ""
