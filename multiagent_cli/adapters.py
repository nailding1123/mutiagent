from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .bridge_models import (
    AgentCommandSettings,
    AgentEvent,
    AgentRunResult,
    BridgeError,
)


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

    def __init__(self, settings: AgentCommandSettings) -> None:
        self.settings = settings

    def version(self) -> str:
        try:
            completed = subprocess.run(
                [*self.settings.command, "--version"],
                check=False,
                capture_output=True,
                text=True,
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
    ) -> list[str]:
        raise NotImplementedError

    def new_parser(self) -> CodexEventParser | ClaudeEventParser:
        raise NotImplementedError

    def run(
        self,
        prompt: str,
        *,
        workspace: Path,
        mode: str,
        session_id: str | None = None,
        on_event: EventCallback | None = None,
    ) -> AgentRunResult:
        started = time.monotonic()
        command = self.build_command(
            workspace=workspace,
            mode=mode,
            session_id=session_id,
        )
        parser = self.new_parser()
        timed_out = threading.Event()
        try:
            process = subprocess.Popen(
                command,
                cwd=str(workspace),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise BridgeError(f"无法启动 {self.display_name} CLI：{exc}") from exc

        def stop_on_timeout() -> None:
            timed_out.set()
            if process.poll() is None:
                process.kill()

        timer = threading.Timer(self.settings.timeout, stop_on_timeout)
        timer.daemon = True
        timer.start()

        try:
            if process.stdin is None or process.stdout is None:
                raise BridgeError(f"{self.display_name} CLI 管道初始化失败")
            try:
                process.stdin.write(prompt)
            except BrokenPipeError:
                pass
            finally:
                process.stdin.close()
            for line in process.stdout:
                for event in parser.feed(line):
                    if on_event:
                        on_event(event)
            process.stdout.close()
            exit_code = process.wait()
        except KeyboardInterrupt:
            process.terminate()
            process.wait()
            raise
        finally:
            timer.cancel()

        if timed_out.is_set():
            raise BridgeError(
                f"{self.display_name} CLI 超过 {self.settings.timeout:g} 秒未完成，已终止"
            )

        if exit_code != 0:
            detail = parser.errors[-1] if parser.errors else "进程未返回可识别错误"
            raise BridgeError(f"{self.display_name} CLI 失败（退出码 {exit_code}）：{detail}")
        if not parser.final_text.strip():
            raise BridgeError(f"{self.display_name} CLI 没有返回最终文本")
        final_text = parser.final_text.strip()
        duration = time.monotonic() - started
        if on_event:
            on_event(AgentEvent(self.display_name, "text", final_text))
            on_event(
                AgentEvent(
                    self.display_name,
                    "metric",
                    json.dumps(
                        {
                            "duration_seconds": duration,
                            "input_tokens": parser.input_tokens,
                            "output_tokens": parser.output_tokens,
                            "session_id": parser.session_id,
                        },
                        ensure_ascii=False,
                    ),
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


class ClaudeAdapter(BaseCLIAdapter):
    display_name = "Claude"

    def build_command(
        self,
        *,
        workspace: Path,
        mode: str,
        session_id: str | None,
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
        if self.settings.model:
            command.extend(["--model", self.settings.model])
        if session_id:
            command.extend(["--resume", session_id])
        command.extend(self.settings.extra_args)
        return command

    def new_parser(self) -> ClaudeEventParser:
        return ClaudeEventParser()


class CodexAdapter(BaseCLIAdapter):
    display_name = "Codex"

    def build_command(
        self,
        *,
        workspace: Path,
        mode: str,
        session_id: str | None,
    ) -> list[str]:
        if session_id:
            command = [
                *self.settings.command,
                "--ask-for-approval",
                "never",
            ]
            if self.settings.model:
                command.extend(["--model", self.settings.model])
            command.extend(["exec", "resume", "--json", session_id])
            command.extend(self.settings.extra_args)
            command.append("-")
            return command

        sandbox = "workspace-write" if mode == "write" else "read-only"
        command = [
            *self.settings.command,
            "-C",
            str(workspace),
            "--sandbox",
            sandbox,
            "--ask-for-approval",
            "never",
        ]
        if self.settings.model:
            command.extend(["--model", self.settings.model])
        command.extend(["exec", "--json", "--skip-git-repo-check"])
        command.extend(self.settings.extra_args)
        command.append("-")
        return command

    def new_parser(self) -> CodexEventParser:
        return CodexEventParser()


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
