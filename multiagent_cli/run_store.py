from __future__ import annotations

import json
import os
import re
import secrets
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class RunStore:
    """Small JSON run history stored outside target workspaces."""

    def __init__(self, root: str | Path | None = None) -> None:
        self._lock = threading.RLock()
        configured = (
            root
            or os.getenv("MULTIAGENT_STATE_DIR")
            or os.getenv("MUTIAGENT_STATE_DIR")
        )
        if configured:
            self.root = Path(configured).expanduser()
            return
        self.root = _default_run_root()

    def start(
        self,
        *,
        task: str,
        workspace: Path,
        executor: str,
        consensus: bool,
        collaboration_mode: str = "workflow",
        run_id: str | None = None,
        settings_snapshot: dict[str, Any] | None = None,
        display_task: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if run_id is not None and not _valid_run_id(run_id):
                raise ValueError("任务 ID 格式无效")
            now = _timestamp()
            existing = self.get(run_id) if run_id else None
            record = existing or {
                "id": run_id or _new_run_id(),
                "created_at": now,
                "attempts": 0,
            }
            record.update(
                {
                    "updated_at": now,
                    "status": "running",
                    "task": task,
                    "workspace": str(workspace),
                    "executor": executor,
                    "consensus": consensus,
                    "collaboration_mode": collaboration_mode,
                    "attempts": int(record.get("attempts", 0)) + 1,
                    "error": "",
                    "archived": False,
                    "archived_at": "",
                }
            )
            if settings_snapshot is not None:
                record["settings"] = settings_snapshot
            if display_task is not None:
                record["display_task"] = display_task
            if attachments is not None:
                record["attachments"] = attachments
            self.save(record)
            return record

    def update(self, run_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            record = self.get(run_id)
            if record is None:
                raise KeyError(f"找不到运行记录：{run_id}")
            record.update(changes)
            record["updated_at"] = _timestamp()
            self.save(record)
            return record

    def mutate(
        self,
        run_id: str,
        callback: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        """Atomically update one record within this process."""

        with self._lock:
            record = self.get(run_id)
            if record is None:
                raise KeyError(f"找不到运行记录：{run_id}")
            callback(record)
            record["updated_at"] = _timestamp()
            self.save(record)
            return record

    def save(self, record: dict[str, Any]) -> None:
        with self._lock:
            run_id = record.get("id")
            if not _valid_run_id(run_id):
                raise ValueError("任务记录缺少有效 ID")
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                self.root.chmod(0o700)
            except OSError:
                pass
            path = self.root / f"{run_id}.json"
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            temporary.replace(path)

    def get(self, run_id: str | None) -> dict[str, Any] | None:
        with self._lock:
            if not _valid_run_id(run_id):
                return None
            path = self.root / f"{run_id}.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                return None
            return data if isinstance(data, dict) else None

    def delete(self, run_id: str) -> dict[str, Any]:
        """Permanently delete one validated run record."""

        with self._lock:
            if not _valid_run_id(run_id):
                raise ValueError("任务 ID 格式无效")
            record = self.get(run_id)
            if record is None:
                raise KeyError(f"找不到运行记录：{run_id}")
            (self.root / f"{run_id}.json").unlink()
            return record

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            records: list[dict[str, Any]] = []
            if limit <= 0 or not self.root.is_dir():
                return records
            paths = list(self.root.glob("*.json"))
            paths.sort(key=_modified_time, reverse=True)
            for path in paths:
                if not _valid_run_id(path.stem):
                    continue
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if isinstance(data, dict) and _valid_run_id(data.get("id")):
                    records.append(data)
                    if len(records) >= limit:
                        break
            records.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
            return records

    def latest(self) -> dict[str, Any] | None:
        records = self.list(limit=1)
        return records[0] if records else None


def _new_run_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


def _default_run_root(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    os_name: str | None = None,
) -> Path:
    """Return the platform default while preserving the legacy misspelling."""

    resolved_home = home or Path.home()
    resolved_environ = environ if environ is not None else os.environ
    resolved_os_name = os_name or os.name
    if resolved_os_name == "nt":
        configured_base = (
            resolved_environ.get("LOCALAPPDATA")
            or resolved_environ.get("APPDATA")
        )
        base = (
            Path(configured_base).expanduser()
            if configured_base
            else resolved_home / "AppData" / "Local"
        )
    else:
        base = resolved_home / ".local" / "state"
    preferred = base / "multiagent" / "runs"
    legacy = base / "mutiagent" / "runs"
    return legacy if legacy.is_dir() and not preferred.exists() else preferred


def _valid_run_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and RUN_ID_RE.fullmatch(value) is not None
    )


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _modified_time(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0
