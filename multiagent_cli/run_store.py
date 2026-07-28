from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RunStore:
    """Small JSON run history stored outside target workspaces."""

    def __init__(self, root: str | Path | None = None) -> None:
        configured = root or os.getenv("MUTIAGENT_STATE_DIR")
        self.root = (
            Path(configured).expanduser()
            if configured
            else Path.home() / ".local" / "state" / "mutiagent" / "runs"
        )

    def start(
        self,
        *,
        task: str,
        workspace: Path,
        lead: str,
        consensus: bool,
        run_id: str | None = None,
        settings_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
                "lead": lead,
                "consensus": consensus,
                "attempts": int(record.get("attempts", 0)) + 1,
                "error": "",
            }
        )
        if settings_snapshot is not None:
            record["settings"] = settings_snapshot
        self.save(record)
        return record

    def update(self, run_id: str, **changes: Any) -> dict[str, Any]:
        record = self.get(run_id)
        if record is None:
            raise KeyError(f"找不到运行记录：{run_id}")
        record.update(changes)
        record["updated_at"] = _timestamp()
        self.save(record)
        return record

    def save(self, record: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        path = self.root / f"{record['id']}.json"
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
        if not run_id:
            return None
        path = self.root / f"{run_id}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if not self.root.is_dir():
            return records
        for path in self.root.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(data, dict):
                records.append(data)
        records.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
        return records[:limit]

    def latest(self) -> dict[str, Any] | None:
        records = self.list(limit=1)
        return records[0] if records else None


def _new_run_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
