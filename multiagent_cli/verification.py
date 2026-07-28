from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from .bridge_models import AgentEvent, VerificationCommand, VerificationResult


EventCallback = Callable[[AgentEvent], None]
MAX_CAPTURE_CHARS = 50_000


def run_verifications(
    commands: Iterable[VerificationCommand],
    *,
    workspace: Path,
    on_event: EventCallback | None = None,
) -> tuple[VerificationResult, ...]:
    results: list[VerificationResult] = []
    for check in commands:
        if on_event:
            on_event(AgentEvent("Verifier", "verification", f"{check.name}: {' '.join(check.command)}"))
        started = time.monotonic()
        try:
            completed = subprocess.run(
                list(check.command),
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=check.timeout,
                check=False,
            )
            output = _combine_output(completed.stdout, completed.stderr)
            result = VerificationResult(
                name=check.name,
                command=check.command,
                exit_code=completed.returncode,
                output=output,
                duration_seconds=time.monotonic() - started,
            )
        except subprocess.TimeoutExpired as exc:
            output = _combine_output(exc.stdout, exc.stderr)
            result = VerificationResult(
                name=check.name,
                command=check.command,
                exit_code=None,
                output=output,
                duration_seconds=time.monotonic() - started,
                timed_out=True,
            )
        except OSError as exc:
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
    output = "\n".join(parts)
    if len(output) > MAX_CAPTURE_CHARS:
        return f"{output[:MAX_CAPTURE_CHARS]}\n… output truncated …"
    return output

