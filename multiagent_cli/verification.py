from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from .bridge_models import AgentEvent, VerificationCommand, VerificationResult
from .process_control import isolated_process_kwargs, stop_process_tree


EventCallback = Callable[[AgentEvent], None]
MAX_CAPTURE_CHARS = 50_000


def run_verifications(
    commands: Iterable[VerificationCommand],
    *,
    workspace: Path,
    on_event: EventCallback | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[VerificationResult, ...]:
    results: list[VerificationResult] = []
    for check in commands:
        if should_stop is not None and should_stop():
            raise KeyboardInterrupt
        if on_event:
            on_event(
                AgentEvent(
                    "Verifier",
                    "verification",
                    f"{check.name}: {' '.join(check.command)}",
                    status="working",
                    step_id="verification",
                    elapsed_seconds=0.0,
                    safe_summary=f"正在运行验证：{check.name}",
                    metadata={"check": check.name},
                )
            )
        started = time.monotonic()
        process: subprocess.Popen[str] | None = None
        capture_threads: tuple[threading.Thread, ...] = ()
        try:
            process = subprocess.Popen(
                list(check.command),
                cwd=str(workspace),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                **isolated_process_kwargs(),
            )
            if process.stdout is None or process.stderr is None:
                raise OSError("验证进程管道初始化失败")
            stdout = _BoundedTextCapture(MAX_CAPTURE_CHARS)
            stderr = _BoundedTextCapture(MAX_CAPTURE_CHARS)
            capture_threads = (
                _start_capture(process.stdout, stdout, f"{check.name}-stdout"),
                _start_capture(process.stderr, stderr, f"{check.name}-stderr"),
            )
            deadline = started + check.timeout
            timed_out = False
            while True:
                if should_stop is not None and should_stop():
                    stop_process_tree(process)
                    _join_capture_threads(capture_threads)
                    raise KeyboardInterrupt
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    stop_process_tree(process, grace_seconds=0.1)
                    break
                try:
                    process.wait(timeout=min(0.2, remaining))
                except subprocess.TimeoutExpired:
                    continue
                break
            _join_capture_threads(capture_threads)
            output = _combine_output(stdout.render(), stderr.render())
            result = VerificationResult(
                name=check.name,
                command=check.command,
                exit_code=None if timed_out else process.returncode,
                output=output,
                duration_seconds=time.monotonic() - started,
                timed_out=timed_out,
            )
        except KeyboardInterrupt:
            if process is not None and process.poll() is None:
                stop_process_tree(process)
            _join_capture_threads(capture_threads)
            raise
        except OSError as exc:
            if process is not None and process.poll() is None:
                stop_process_tree(process)
            _join_capture_threads(capture_threads)
            result = VerificationResult(
                name=check.name,
                command=check.command,
                exit_code=None,
                output=str(exc),
                duration_seconds=time.monotonic() - started,
            )
        results.append(result)
        if on_event:
            status = "PASS" if result.passed else "FAIL"
            on_event(
                AgentEvent(
                    "Verifier",
                    "verification_result",
                    f"{result.name}: {status} ({result.duration_seconds:.1f}s)",
                    status="completed" if result.passed else "failed",
                    step_id="verification",
                    elapsed_seconds=result.duration_seconds,
                    safe_summary=(
                        f"验证 {result.name}：{status} "
                        f"({_format_event_duration(result.duration_seconds)})"
                    ),
                    metadata={
                        "check": result.name,
                        "passed": result.passed,
                        "timed_out": result.timed_out,
                        "exit_code": result.exit_code,
                    },
                )
            )
    return tuple(results)


def format_verification_results(results: Iterable[VerificationResult]) -> str:
    values = list(results)
    if not values:
        return "未配置桥接器独立验证命令。"
    sections: list[str] = []
    for result in values:
        status = "PASS" if result.passed else "TIMEOUT" if result.timed_out else "FAIL"
        output = result.output.strip() or "(no output)"
        sections.append(
            f"[{result.name}] {status}\n"
            f"command: {' '.join(result.command)}\n"
            f"exit_code: {result.exit_code}\n"
            f"output:\n{output}"
        )
    return "\n\n".join(sections)


def verifications_passed(results: Iterable[VerificationResult]) -> bool:
    values = list(results)
    return all(result.passed for result in values)


def _combine_output(stdout: object, stderr: object) -> str:
    parts = []
    for value in (stdout, stderr):
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return _truncate_output("\n".join(parts), MAX_CAPTURE_CHARS)


class _BoundedTextCapture:
    """Keep a fixed-size head and tail while a pipe is being drained."""

    def __init__(self, limit: int) -> None:
        self.limit = max(1, limit)
        self.head_limit = self.limit // 2
        self.tail_limit = self.limit - self.head_limit
        self.head = ""
        self.tail = ""
        self.total_chars = 0
        self._lock = threading.Lock()

    def append(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self.total_chars += len(text)
            if len(self.head) < self.head_limit:
                needed = self.head_limit - len(self.head)
                self.head += text[:needed]
                text = text[needed:]
            if text:
                self.tail = (self.tail + text)[-self.tail_limit :]

    def render(self) -> str:
        with self._lock:
            captured = self.head + self.tail
            if self.total_chars <= self.limit:
                return captured
            omitted = self.total_chars - len(captured)
            return f"{self.head}\n… {omitted} chars truncated …\n{self.tail}"


def _start_capture(
    stream: object,
    capture: _BoundedTextCapture,
    name: str,
) -> threading.Thread:
    def read_stream() -> None:
        try:
            read = getattr(stream, "read")
            while True:
                chunk = read(8192)
                if not chunk:
                    return
                capture.append(chunk)
        finally:
            getattr(stream, "close")()

    thread = threading.Thread(
        target=read_stream,
        name=f"multiagent-verification-{name}",
        daemon=True,
    )
    thread.start()
    return thread


def _join_capture_threads(threads: tuple[threading.Thread, ...]) -> None:
    for thread in threads:
        thread.join(timeout=2)


def _truncate_output(output: str, limit: int) -> str:
    if len(output) <= limit:
        return output
    head_limit = limit // 2
    tail_limit = limit - head_limit
    omitted = len(output) - limit
    return (
        f"{output[:head_limit]}\n… {omitted} chars truncated …\n"
        f"{output[-tail_limit:]}"
    )


def _format_event_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes}m{remainder:02d}s"
