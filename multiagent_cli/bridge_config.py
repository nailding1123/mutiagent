from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .bridge_models import (
    DEFAULT_GROUP_CHAT_AGENT_A_IDENTITY,
    DEFAULT_GROUP_CHAT_AGENT_B_IDENTITY,
    AgentCommandSettings,
    BridgeSettings,
    ContextCompactionSettings,
    normalize_group_chat_identity,
)
from .token_api import (
    DEFAULT_TOKEN_API_BASE_URL,
    TokenAPISettings,
    known_incompatible_reason,
)


CODEX_REASONING_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}
CLAUDE_PERMISSION_MODES = {"auto", "acceptEdits", "plan", "manual", "dontAsk"}


class ConfigError(ValueError):
    """Invalid or incomplete bridge configuration."""


def ensure_local_config_ignored(workspace: str | Path) -> bool:
    """Keep the project-local MultiAgent config out of Git status.

    The config belongs to the user's machine (it can contain executable paths,
    model choices and local service settings), so add an exact path to the
    repository's local ``.git/info/exclude`` instead of changing the project's
    shareable ``.gitignore``. Tracked files are left untouched.
    """

    workspace_path = Path(workspace).expanduser().resolve()
    if not workspace_path.is_dir():
        return False
    config_path = workspace_path / ".multiagent.json"
    try:
        git_dir_result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=workspace_path,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if git_dir_result.returncode != 0 or not git_dir_result.stdout.strip():
            return False
        git_root_result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=workspace_path,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if git_root_result.returncode != 0 or not git_root_result.stdout.strip():
            return False
        repository = Path(git_root_result.stdout.strip()).expanduser().resolve()
        relative_name = config_path.relative_to(repository).as_posix()
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", relative_name],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if tracked.returncode == 0:
            return False
        ignored = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", "--", relative_name],
            cwd=repository,
            check=False,
            timeout=5,
        )
        if ignored.returncode == 0:
            return True
        raw_git_dir = Path(git_dir_result.stdout.strip())
        git_dir = raw_git_dir if raw_git_dir.is_absolute() else repository / raw_git_dir
        exclude = git_dir.resolve() / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        lines = existing.splitlines()
        if relative_name in lines:
            return True
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        exclude.write_text(
            existing
            + prefix
            + "# MultiAgent local project configuration\n"
            + relative_name
            + "\n",
            encoding="utf-8",
        )
        return True
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        return False


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
) -> BridgeSettings:
    workspace_path = Path(workspace).expanduser().resolve()
    if not workspace_path.is_dir():
        raise ConfigError(f"工作区不是有效目录：{workspace_path}")

    group_chat_default_agent = data.get("group_chat_default_agent", "both")
    if group_chat_default_agent not in {"both", "claude", "codex"}:
        raise ConfigError(
            "group_chat_default_agent 必须是 both、claude 或 codex"
        )
    group_chat_agent_a_identity, group_chat_agent_b_identity = _resolve_identities(
        data.get("group_chat_identities", {}),
        section="group_chat_identities",
        default_a=DEFAULT_GROUP_CHAT_AGENT_A_IDENTITY,
        default_b=DEFAULT_GROUP_CHAT_AGENT_B_IDENTITY,
    )
    # Isolated Git Worktrees are the safe default for concurrent Agent writes.
    # Existing config files that explicitly set ``false`` remain opt-out.
    worktree = data.get("worktree", True)
    if not isinstance(worktree, bool):
        raise ConfigError("worktree 必须是布尔值")
    claude = _resolve_agent_settings("claude", data.get("claude", {}))
    codex = _resolve_agent_settings("codex", data.get("codex", {}))
    context_compaction = _resolve_context_compaction_settings(
        data.get("context_compaction", {})
    )
    token_api = _resolve_token_api_settings(data.get("token_api", {}))
    return BridgeSettings(
        workspace=workspace_path,
        claude=claude,
        codex=codex,
        config_path=config_path,
        group_chat_agent_a_identity=group_chat_agent_a_identity,
        group_chat_agent_b_identity=group_chat_agent_b_identity,
        group_chat_default_agent=group_chat_default_agent,
        worktree=worktree,
        context_compaction=context_compaction,
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
    return (
        normalize_group_chat_identity(agent_a),
        normalize_group_chat_identity(agent_b),
    )


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

    reasoning_effort = raw.get("reasoning_effort") if name == "codex" else None
    if reasoning_effort is None or reasoning_effort == "" or reasoning_effort == "auto":
        reasoning_effort = None
    if reasoning_effort is not None:
        if not isinstance(reasoning_effort, str) or reasoning_effort not in CODEX_REASONING_EFFORTS:
            values = "、".join(sorted(CODEX_REASONING_EFFORTS))
            raise ConfigError(
                f"codex.reasoning_effort 必须是 auto、{values} 之一"
            )

    permission_mode = raw.get("permission_mode") if name == "claude" else None
    if permission_mode is None or (
        isinstance(permission_mode, str)
        and permission_mode in {"", "task", "default"}
    ):
        permission_mode = None
    elif not isinstance(permission_mode, str) or permission_mode not in CLAUDE_PERMISSION_MODES:
        values = "、".join(sorted(CLAUDE_PERMISSION_MODES))
        raise ConfigError(f"claude.permission_mode 必须是默认、{values} 之一")

    return AgentCommandSettings(
        command=command,
        model=models[0] if models else None,
        models=models,
        fallback_on_timeout=fallback_on_timeout,
        extra_args=tuple(extra_args),
        timeout=float(timeout),
        reasoning_effort=reasoning_effort,
        permission_mode=permission_mode,
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


def _resolve_context_compaction_settings(raw: Any) -> ContextCompactionSettings:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("context_compaction 必须是 JSON 对象")
    defaults = ContextCompactionSettings()
    enabled = raw.get("enabled", defaults.enabled)
    threshold_tokens = raw.get("threshold_tokens", defaults.threshold_tokens)
    target_tokens = raw.get("target_tokens", defaults.target_tokens)
    recent_messages = raw.get("recent_messages", defaults.recent_messages)
    if not isinstance(enabled, bool):
        raise ConfigError("context_compaction.enabled 必须是布尔值")
    for name, value in (
        ("threshold_tokens", threshold_tokens),
        ("target_tokens", target_tokens),
        ("recent_messages", recent_messages),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigError(f"context_compaction.{name} 必须是正整数")
    if target_tokens >= threshold_tokens:
        raise ConfigError(
            "context_compaction.target_tokens 必须小于 threshold_tokens"
        )
    if recent_messages > 100:
        raise ConfigError("context_compaction.recent_messages 不能超过 100")
    return ContextCompactionSettings(
        enabled=enabled,
        threshold_tokens=threshold_tokens,
        target_tokens=target_tokens,
        recent_messages=recent_messages,
    )


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
