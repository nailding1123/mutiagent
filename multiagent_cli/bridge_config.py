from __future__ import annotations

import json
import os
import shlex
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .bridge_models import (
    DEFAULT_AGENT_A_IDENTITY,
    DEFAULT_AGENT_B_IDENTITY,
    DEFAULT_GROUP_CHAT_AGENT_A_IDENTITY,
    DEFAULT_GROUP_CHAT_AGENT_B_IDENTITY,
    COLLABORATION_MODES,
    AgentCommandSettings,
    BridgeSettings,
    VerificationCommand,
)
from .token_api import (
    DEFAULT_TOKEN_API_BASE_URL,
    TokenAPISettings,
    known_incompatible_reason,
)


class ConfigError(ValueError):
    """Invalid or incomplete bridge configuration."""


def find_config_path(
    explicit: str | None,
    workspace: str | Path | None = None,
) -> Path | None:
    """Find an optional bridge config without tying it to the target workspace."""

    if explicit:
        return Path(explicit).expanduser().resolve()
    from_env = os.getenv("MULTIAGENT_CONFIG") or os.getenv("MUTIAGENT_CONFIG")
    if from_env:
        return Path(from_env).expanduser().resolve()

    workspace_path = Path(workspace or ".").expanduser().resolve()
    candidates = (
        workspace_path / ".multiagent.json",
        workspace_path / ".mutiagent.json",
        *_user_config_candidates(),
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
    executor: str | None = None,
    review_rounds: int | None = None,
    consensus: bool | None = None,
    collaboration_mode: str | None = None,
) -> BridgeSettings:
    workspace_path = Path(workspace).expanduser().resolve()
    if not workspace_path.is_dir():
        raise ConfigError(f"工作区不是有效目录：{workspace_path}")

    resolved_executor = executor or data.get("executor", "claude")
    if resolved_executor not in {"claude", "codex"}:
        raise ConfigError("executor 必须是 claude 或 codex")

    raw_mode = collaboration_mode or data.get("collaboration_mode", "workflow")
    resolved_mode = (
        raw_mode.strip().lower().replace("-", "_")
        if isinstance(raw_mode, str)
        else ""
    )
    if resolved_mode not in COLLABORATION_MODES:
        raise ConfigError("collaboration_mode 必须是 workflow 或 group_chat")

    group_chat_default_agent = data.get("group_chat_default_agent", "both")
    if group_chat_default_agent not in {"both", "claude", "codex"}:
        raise ConfigError(
            "group_chat_default_agent 必须是 both、claude 或 codex"
        )
    group_chat_execution = data.get("group_chat_execution", True)
    if not isinstance(group_chat_execution, bool):
        raise ConfigError("group_chat_execution 必须是布尔值")

    rounds = review_rounds if review_rounds is not None else data.get("review_rounds", 1)
    if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 0:
        raise ConfigError("review_rounds 必须是大于或等于 0 的整数")

    final_review = data.get("final_review", True)
    if not isinstance(final_review, bool):
        raise ConfigError("final_review 必须是布尔值")

    planning_collaboration = data.get("planning_collaboration", True)
    if not isinstance(planning_collaboration, bool):
        raise ConfigError("planning_collaboration 必须是布尔值")

    resolved_consensus = data.get("consensus", False) if consensus is None else consensus
    if not isinstance(resolved_consensus, bool):
        raise ConfigError("consensus 必须是布尔值")
    if resolved_consensus and not planning_collaboration:
        raise ConfigError("consensus 需要启用 planning_collaboration")

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
    agent_a_identity, agent_b_identity = _resolve_identities(
        data.get("identities", {}),
        section="identities",
        default_a=DEFAULT_AGENT_A_IDENTITY,
        default_b=DEFAULT_AGENT_B_IDENTITY,
    )
    group_chat_agent_a_identity, group_chat_agent_b_identity = _resolve_identities(
        data.get("group_chat_identities", {}),
        section="group_chat_identities",
        default_a=DEFAULT_GROUP_CHAT_AGENT_A_IDENTITY,
        default_b=DEFAULT_GROUP_CHAT_AGENT_B_IDENTITY,
    )
    claude = _resolve_agent_settings("claude", data.get("claude", {}))
    codex = _resolve_agent_settings("codex", data.get("codex", {}))
    token_api = _resolve_token_api_settings(data.get("token_api", {}))
    return BridgeSettings(
        workspace=workspace_path,
        executor=resolved_executor,
        review_rounds=rounds,
        planning_collaboration=planning_collaboration,
        consensus=resolved_consensus,
        max_consensus_rounds=max_consensus_rounds,
        plan_approval=plan_approval,
        max_plan_revisions=max_plan_revisions,
        final_review=final_review,
        verification_commands=verification_commands,
        claude=claude,
        codex=codex,
        config_path=config_path,
        agent_a_identity=agent_a_identity,
        agent_b_identity=agent_b_identity,
        group_chat_agent_a_identity=group_chat_agent_a_identity,
        group_chat_agent_b_identity=group_chat_agent_b_identity,
        collaboration_mode=resolved_mode,
        group_chat_default_agent=group_chat_default_agent,
        group_chat_execution=group_chat_execution,
        token_api=token_api,
    )


def _resolve_identities(
    raw: Any,
    *,
    section: str,
    default_a: str,
    default_b: str,
) -> tuple[str, str]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{section} 必须是 JSON 对象")
    agent_a = raw.get("agent_a", default_a)
    agent_b = raw.get("agent_b", default_b)
    if not isinstance(agent_a, str) or not agent_a.strip():
        raise ConfigError(f"{section}.agent_a 必须是非空字符串")
    if not isinstance(agent_b, str) or not agent_b.strip():
        raise ConfigError(f"{section}.agent_b 必须是非空字符串")
    return agent_a.strip(), agent_b.strip()


def _resolve_agent_settings(name: str, raw: Any) -> AgentCommandSettings:
    if not isinstance(raw, dict):
        raise ConfigError(f"{name} 配置必须是 JSON 对象")

    command = _parse_command(raw.get("command"))
    if command is None:
        command = (_discover_executable(name),)

    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ConfigError(f"{name}.model 必须是非空字符串或 null")

    models_raw = raw.get("models")
    if models_raw is None:
        models = (model.strip(),) if isinstance(model, str) else ()
    else:
        if not isinstance(models_raw, list) or not all(
            isinstance(value, str) and value.strip() for value in models_raw
        ):
            raise ConfigError(f"{name}.models 必须是非空字符串数组")
        models = tuple(value.strip() for value in models_raw)
        if len(set(models)) != len(models):
            raise ConfigError(f"{name}.models 不能包含重复模型")
    for candidate in models:
        reason = known_incompatible_reason(name, candidate)
        if reason:
            raise ConfigError(f"{name}.models 中的 {candidate} 不兼容：{reason}")

    fallback_on_timeout = raw.get("fallback_on_timeout", True)
    if not isinstance(fallback_on_timeout, bool):
        raise ConfigError(f"{name}.fallback_on_timeout 必须是布尔值")

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
        model=models[0] if models else None,
        models=models,
        fallback_on_timeout=fallback_on_timeout,
        extra_args=tuple(extra_args),
        timeout=float(timeout),
    )


def _resolve_token_api_settings(raw: Any) -> TokenAPISettings:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("token_api 必须是 JSON 对象")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("token_api.enabled 必须是布尔值")
    base_url = raw.get("base_url", DEFAULT_TOKEN_API_BASE_URL)
    if not isinstance(base_url, str) or not base_url.strip():
        raise ConfigError("token_api.base_url 必须是有效 URL")
    normalized = base_url.strip().rstrip("/")
    parsed = urlparse(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError("token_api.base_url 必须是无账号、查询参数和片段的 HTTP(S) URL")
    return TokenAPISettings(enabled=enabled, base_url=normalized)


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
        values = _split_command_text(raw)
    elif isinstance(raw, list) and raw and all(isinstance(value, str) for value in raw):
        values = tuple(raw)
    else:
        raise ConfigError("command 必须是非空字符串或字符串数组")
    return values


def _split_command_text(raw: str, *, os_name: str | None = None) -> tuple[str, ...]:
    """Split a command without treating Windows path separators as escapes."""

    if (os_name or os.name) != "nt":
        return tuple(shlex.split(raw))
    values = shlex.split(raw, posix=False)
    return tuple(_strip_matching_quotes(value) for value in values)


def _strip_matching_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


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


def _user_config_candidates(
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    os_name: str | None = None,
) -> tuple[Path, Path]:
    """Return correctly and historically spelled per-user config paths."""

    resolved_home = home or Path.home()
    resolved_environ = environ if environ is not None else os.environ
    resolved_os_name = os_name or os.name
    if resolved_os_name == "nt":
        configured_base = (
            resolved_environ.get("APPDATA")
            or resolved_environ.get("LOCALAPPDATA")
        )
        base = (
            Path(configured_base).expanduser()
            if configured_base
            else resolved_home / "AppData" / "Roaming"
        )
    else:
        base = resolved_home / ".config"
    return (
        base / "multiagent" / "config.json",
        base / "mutiagent" / "config.json",
    )
