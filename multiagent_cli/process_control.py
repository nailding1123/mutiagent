from __future__ import annotations

import os
import signal
import subprocess


IS_POSIX = os.name == "posix"
IS_WINDOWS = os.name == "nt"
WINDOWS_NEW_PROCESS_GROUP = 0x00000200
WINDOWS_CTRL_BREAK_EVENT = getattr(signal, "CTRL_BREAK_EVENT", None)


def isolated_process_kwargs() -> dict[str, object]:
    """Launch a command in a group that can be stopped without killing the bridge."""

    if IS_POSIX:
        return {"start_new_session": True}
    if IS_WINDOWS:
        return {"creationflags": WINDOWS_NEW_PROCESS_GROUP}
    return {}


def signal_process_tree(
    process: subprocess.Popen,
    *,
    force: bool = False,
) -> None:
    """Best-effort termination of a command and the children it spawned."""

    if process.poll() is not None:
        return
    try:
        if IS_POSIX:
            os.killpg(
                process.pid,
                signal.SIGKILL if force else signal.SIGTERM,
            )
        elif IS_WINDOWS:
            if force:
                if not _taskkill_process_tree(process.pid):
                    process.kill()
            elif WINDOWS_CTRL_BREAK_EVENT is not None:
                try:
                    os.kill(process.pid, WINDOWS_CTRL_BREAK_EVENT)
                except OSError:
                    process.terminate()
            else:
                process.terminate()
        elif force:
            process.kill()
        else:
            process.terminate()
    except (OSError, ProcessLookupError):
        return


def stop_process_tree(
    process: subprocess.Popen,
    *,
    grace_seconds: float = 1.0,
) -> None:
    """Terminate a process group and escalate if it ignores the first signal."""

    signal_process_tree(process)
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        signal_process_tree(process, force=True)
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            pass


def _taskkill_process_tree(pid: int) -> bool:
    """Force-stop a Windows process tree, falling back to Popen.kill on failure."""

    try:
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0
