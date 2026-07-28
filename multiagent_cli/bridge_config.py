from __future__ import annotations

import json
import os
import shlex
import shutil
from pathlib import Path
from typing import Any

from .bridge_models import (
    DEFAULT_LEAD_IDENTITY,
    DEFAULT_REVIEWER_IDENTITY,
    AgentCommandSettings,
    BridgeSettings,
    VerificationCommand,
)
from .config import ConfigError


def find_config_path(
    explicit: str | None,
    workspace: str | Path | None = None,
) -> Path | None:
    """Find an optional bridge config without tying it to the target workspace."""

    if explicit:
        return Path(explicit).expanduser().resolve()
    from_env = os.getenv("MUTIAGENT_CONFIG")
    if from_env:
        return Path(from_env).expanduser().resolve()

    workspace_path = Path(workspace or ".").expanduser().resolve()
    candidates = (
        workspace_path / ".mutiagent.json",
        Path.home() / ".config" / "mutiagent" / "config.json",
        Path(__file__).resolve().parent.parent / "bridge.json",
    )
    return next((path for path in candidates if path.is_file()), None)


def load_bridge_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        with path.open(encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError as exc:
        raise ConfigError(f"找不到桥接配置：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"桥接配置不是有效 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("桥接配置顶层必须是 JSON 对象")
    return data


def resolve_bridge_settings(
    data: dict[str, Any],
    *,
    workspace: str | Path,
    config_path: Path | None = None,
    lead: str | None = None,
    review_rounds: int | None = None,
    consensus: bool | None = None,
) -> BridgeSettings:
    workspace_path = Path(workspace).expanduser().resolve()
    if not workspace_path.is_dir():
        raise ConfigError(f"工作区不是有效目录：{workspace_path}")

    resolved_lead = lead or data.get("lead", "claude")
    if resolved_lead not in {"claude", "codex"}:
        raise ConfigError("lead 必须是 claude 或 codex")

    rounds = review_rounds if review_rounds is not None else data.get("review_rounds", 1)
    if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 0:
        raise ConfigError("review_rounds 必须是大于或等于 0 的整数")

    final_review = data.get("final_review", True)
    if not isinstance(final_review, bool):
        raise ConfigError("final_review 必须是布尔值")

    requirement_review = data.get("requirement_review", True)
    if not isinstance(requirement_review, bool):
        raise ConfigError("requirement_review 必须是布尔值")

    resolved_consensus = data.get("consensus", False) if consensus is None else consensus
    if not isinstance(resolved_consensus, bool):
        raise ConfigError("consensus 必须是布尔值")
    if resolved_consensus and not requirement_review:
        raise ConfigError("consensus 需要启用 requirement_review")

    max_consensus_rounds = data.get("max_consensus_rounds", 3)
    if (
        isinstance(max_consensus_rounds, bool)
        or not isinstance(max_consensus_rounds, int)
        or max_consensus_rounds < 1
    ):
        raise ConfigError("max_consensus_rounds 必须是大于或等于 1 的整数")

    plan_approval = data.get("plan_approval", True)
    if not isinstance(plan_approval, bool):
        raise ConfigError("plan_approval 必须是布尔值")

    max_plan_revisions = data.get("max_plan_revisions", 2)
    if (
        isinstance(max_plan_revisions, bool)
        or not isinstance(max_plan_revisions, int)
        or max_plan_revisions < 0
    ):
        raise ConfigError("max_plan_revisions 必须是大于或等于 0 的整数")

    verification_commands = _resolve_verification(data.get("verification", {}))
    lead_identity, reviewer_identity = _resolve_identities(data.get("identities", {}))
    worktree = data.get("worktree", False)
    if not isinstance(worktree, bool):
        raise ConfigError("worktree 必须是布尔值")

    claude = _resolve_agent_settings("claude", data.get("claude", {}))
    codex = _resolve_agent_settings("codex", data.get("codex", {}))
    return BridgeSettings(
        workspace=workspace_path,
        lead=resolved_lead,
        review_rounds=rounds,
        requirement_review=requirement_review,
        consensus=resolved_consensus,
        max_consensus_rounds=max_consensus_rounds,
        plan_approval=plan_approval,
        max_plan_revisions=max_plan_revisions,
        final_review=final_review,
        verification_commands=verification_commands,
        claude=claude,
        codex=codex,
        config_path=config_path,
        lead_identity=lead_identity,
        reviewer_identity=reviewer_identity,
        worktree=worktree,
    )


def _resolve_identities(raw: Any) -> tuple[str, str]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("identities 必须是 JSON 对象")
    lead = raw.get("lead", DEFAULT_LEAD_IDENTITY)
    reviewer = raw.get("reviewer", DEFAULT_REVIEWER_IDENTITY)
    if not isinstance(lead, str) or not lead.strip():
        raise ConfigError("identities.lead 必须是非空字符串")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ConfigError("identities.reviewer 必须是非空字符串")
    return lead.strip(), reviewer.strip()


def _resolve_agent_settings(name: str, raw: Any) -> AgentCommandSettings:
    if not isinstance(raw, dict):
        raise ConfigError(f"{name} 配置必须是 JSON 对象")

    command = _parse_command(raw.get("command"))
    if command is None:
        command = (_discover_executable(name),)

    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ConfigError(f"{name}.model 必须是非空字符串或 null")

    extra_args = raw.get("extra_args", [])
    if not isinstance(extra_args, list) or not all(
        isinstance(value, str) for value in extra_args
    ):
        raise ConfigError(f"{name}.extra_args 必须是字符串数组")

    timeout = raw.get("timeout", 900)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
    ):
        raise ConfigError(f"{name}.timeout 必须是正数")

    return AgentCommandSettings(
        command=command,
        model=model.strip() if isinstance(model, str) else None,
        extra_args=tuple(extra_args),
        timeout=float(timeout),
    )


def _resolve_verification(raw: Any) -> tuple[VerificationCommand, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ConfigError("verification 必须是 JSON 对象")
    commands = raw.get("commands", [])
    if not isinstance(commands, list):
        raise ConfigError("verification.commands 必须是数组")

    default_timeout = raw.get("timeout", 300)
    if (
        isinstance(default_timeout, bool)
        or not isinstance(default_timeout, (int, float))
        or default_timeout <= 0
    ):
        raise ConfigError("verification.timeout 必须是正数")

    resolved: list[VerificationCommand] = []
    for index, item in enumerate(commands):
        name = f"check-{index + 1}"
        timeout = float(default_timeout)
        command_raw: Any = item
        if isinstance(item, dict):
            name_raw = item.get("name", name)
            if not isinstance(name_raw, str) or not name_raw.strip():
                raise ConfigError(f"第 {index + 1} 个验证命令的 name 无效")
            name = name_raw.strip()
            command_raw = item.get("command")
            timeout_raw = item.get("timeout", default_timeout)
            if (
                isinstance(timeout_raw, bool)
                or not isinstance(timeout_raw, (int, float))
                or timeout_raw <= 0
            ):
                raise ConfigError(f"验证命令 {name} 的 timeout 必须是正数")
            timeout = float(timeout_raw)

        command = _parse_command(command_raw)
        if command is None:
            raise ConfigError(f"第 {index + 1} 个验证命令不能为空")
        if not isinstance(item, dict):
            name = command[0]
        resolved.append(VerificationCommand(name=name, command=command, timeout=timeout))
    return tuple(resolved)


def _parse_command(raw: Any) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip():
        values = tuple(shlex.split(raw))
    elif isinstance(raw, list) and raw and all(isinstance(value, str) for value in raw):
        values = tuple(raw)
    else:
        raise ConfigError("command 必须是非空字符串或字符串数组")
    return values


def _discover_executable(name: str) -> str:
    on_path = shutil.which(name)
    if on_path:
        return on_path

    home = Path.home()
    if name == "claude":
        candidates = (home / ".local" / "bin" / "claude",)
    else:
        candidates = (
            home / "Applications" / "ChatGPT.app" / "Contents" / "Resources" / "codex",
            Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
        )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise ConfigError(
        f"找不到 {name} CLI；请先安装，或在桥接配置的 {name}.command 中填写路径"
    )
