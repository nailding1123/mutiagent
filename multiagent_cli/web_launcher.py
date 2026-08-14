from __future__ import annotations

import argparse
import os
import socket
import sys
import webbrowser
from collections.abc import Mapping
from pathlib import Path

from .run_store import RunStore
from .ui_server import select_ui_workspace, serve_ui, ui_is_running


DEFAULT_UI_PORT = 8765
FALLBACK_PORT_COUNT = 32


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="multiagent",
        description="启动或打开 MultiAgent 本地 Web 工作台。",
    )
    parser.add_argument("-C", "--workspace", help="默认工作区")
    parser.add_argument("--port", type=int, default=_environment_port())
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    return parser


def main(argv: list[str] | None = None) -> int:
    argument_list = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argument_list)
    store = RunStore()
    try:
        workspace = _resolve_launcher_workspace(
            store,
            explicit=args.workspace,
        )
    except ValueError as exc:
        _show_error(str(exc))
        return 2
    url = f"http://127.0.0.1:{args.port}/"
    if _ui_is_running(url):
        select_ui_workspace(url, workspace)
        if not args.no_open:
            webbrowser.open(url)
        return 0

    selected_port = args.port
    if not _port_was_explicit(argument_list) and not _port_is_available(args.port):
        if _wait_for_ui(url):
            select_ui_workspace(url, workspace)
            if not args.no_open:
                webbrowser.open(url)
            return 0
        fallback = _find_available_port(args.port + 1)
        if fallback is not None:
            selected_port = fallback
            url = f"http://127.0.0.1:{selected_port}/"

    result = serve_ui(
        workspace=workspace,
        store=store,
        port=selected_port,
        open_browser=not args.no_open,
        quiet=True,
    )
    if result != 0:
        if _ui_is_running(url):
            select_ui_workspace(url, workspace)
            if not args.no_open:
                webbrowser.open(url)
            return 0
        _show_error(
            f"无法启动 MultiAgent Web 工作台。请确认端口 {selected_port} 未被占用，"
            "且当前用户可以读取工作区与状态目录。"
        )
    return result


def _resolve_launcher_workspace(
    store: RunStore,
    *,
    explicit: str | None = None,
    cwd: Path | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    values = environ if environ is not None else os.environ
    requested = explicit or values.get("MULTIAGENT_WORKSPACE")
    if requested:
        path = Path(requested).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"工作区不是有效目录：{path}")
        return path

    current = (cwd or Path.cwd()).expanduser().resolve()
    if _looks_like_project(current):
        return current

    latest = store.latest()
    if latest is not None:
        value = latest.get("workspace")
        if isinstance(value, str) and value:
            candidate = Path(value).expanduser().resolve()
            if candidate.is_dir():
                return candidate

    if current.is_dir():
        return current
    fallback = (home or Path.home()).expanduser().resolve()
    if fallback.is_dir():
        return fallback
    raise ValueError("找不到可用的默认工作区")


def _looks_like_project(path: Path) -> bool:
    return path.is_dir() and any(
        (path / name).exists()
        for name in (".git", ".multiagent.json", ".mutiagent.json")
    )


def _ui_is_running(url: str) -> bool:
    return ui_is_running(url)


def _wait_for_ui(url: str, *, attempts: int = 5, delay: float = 0.1) -> bool:
    import time

    for attempt in range(attempts):
        if _ui_is_running(url):
            return True
        if attempt + 1 < attempts:
            time.sleep(delay)
    return False


def _port_is_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


def _find_available_port(start: int) -> int | None:
    stop = min(65536, start + FALLBACK_PORT_COUNT)
    for port in range(max(1, start), stop):
        if _port_is_available(port):
            return port
    return None


def _port_was_explicit(arguments: list[str]) -> bool:
    return any(
        argument == "--port" or argument.startswith("--port=")
        for argument in arguments
    )


def _environment_port(environ: Mapping[str, str] | None = None) -> int:
    values = environ if environ is not None else os.environ
    raw = values.get("MULTIAGENT_UI_PORT", "")
    try:
        port = int(raw) if raw else DEFAULT_UI_PORT
    except ValueError:
        return DEFAULT_UI_PORT
    return port if 1 <= port <= 65535 else DEFAULT_UI_PORT


def _show_error(message: str) -> None:
    try:
        from tkinter import Tk, messagebox

        root = Tk()
        root.withdraw()
        messagebox.showerror("MultiAgent Web", message, parent=root)
        root.destroy()
    except Exception:
        print(f"MultiAgent Web：{message}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
