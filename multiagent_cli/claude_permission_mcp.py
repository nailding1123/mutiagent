from __future__ import annotations

import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any


TOOL_NAME = "approve"


def _write_message(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _tool_result(arguments: object) -> dict[str, Any]:
    directory_value = os.environ.get("MULTIAGENT_CLAUDE_PERMISSION_DIR", "")
    if not directory_value:
        raise RuntimeError("MultiAgent Claude permission bridge is not configured")
    directory = Path(directory_value)
    request_id = secrets.token_urlsafe(18)
    request_path = directory / f"request-{request_id}.json"
    pending_path = directory / f".request-{request_id}.tmp"
    response_path = directory / f"response-{request_id}.json"
    pending_path.write_text(
        json.dumps(
            arguments if isinstance(arguments, dict) else {},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.replace(pending_path, request_path)
    deadline = time.monotonic() + 3600
    while not response_path.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("MultiAgent permission request timed out")
        if not directory.exists():
            raise RuntimeError("MultiAgent permission bridge stopped")
        time.sleep(0.05)
    try:
        payload = json.loads(response_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"MultiAgent permission request failed: {exc}") from exc
    finally:
        response_path.unlink(missing_ok=True)
    if not isinstance(payload, dict):
        raise RuntimeError("MultiAgent permission response was invalid")
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False),
            }
        ]
    }


def _handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = message.get("method")
    if request_id is None:
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "multiagent-permission", "version": "1"},
            },
        }
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": [
                    {
                        "name": TOOL_NAME,
                        "description": "Ask the MultiAgent user to approve a Claude tool call.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": True,
                        },
                    }
                ]
            },
        }
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict) or params.get("name") != TOOL_NAME:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "Unknown permission tool"},
            }
        try:
            result = _tool_result(params.get("arguments"))
        except RuntimeError as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": str(exc)}],
                },
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Unsupported method: {method}"},
    }


def main() -> int:
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                continue
            response = _handle_request(message)
        except json.JSONDecodeError:
            continue
        if response is not None:
            _write_message(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
