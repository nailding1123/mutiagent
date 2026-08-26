from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .adapters import ClaudeAdapter, CodexAdapter
from .bridge_config import ConfigError, resolve_bridge_settings
from .bridge_models import BridgeSettings
from .run_store import RunStore
from .token_api import TokenAPICredentials


def make_adapters(
    settings: BridgeSettings,
    *,
    state_root: str | Path | None = None,
):
    """Build the two native adapters used by Web sessions."""

    claude_environment: dict[str, str] = {}
    codex_environment: dict[str, str] = {}
    token_api_base_url: str | None = None
    if settings.token_api.enabled:
        credential_root = Path(state_root) if state_root is not None else RunStore().root
        api_key = TokenAPICredentials(credential_root).load()
        if not api_key:
            raise ConfigError(
                "已启用 Token API，但尚未保存 API Key；请在 Web 设置的智能体页面填写"
            )
        token_api_base_url = settings.token_api.base_url
        claude_environment = {
            "ANTHROPIC_BASE_URL": token_api_base_url,
            "ANTHROPIC_AUTH_TOKEN": api_key,
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
        }
        codex_environment = {"OPENAI_API_KEY": api_key}
    return {
        "claude": ClaudeAdapter(
            settings.claude,
            environment=claude_environment,
        ),
        "codex": CodexAdapter(
            settings.codex,
            environment=codex_environment,
            token_api_base_url=token_api_base_url,
        ),
    }


def settings_snapshot(settings: BridgeSettings) -> dict[str, object]:
    """Persist only the resolved settings required to resume a Web task."""

    def agent_snapshot(agent) -> dict[str, object]:
        return {
            "command": list(agent.command),
            "model": agent.model,
            "models": list(agent.models or ((agent.model,) if agent.model else ())),
            "fallback_on_timeout": agent.fallback_on_timeout,
            "timeout": agent.timeout,
            "extra_args": list(agent.extra_args),
        }

    resolved = {
        "group_chat_default_agent": settings.group_chat_default_agent,
        "worktree": settings.worktree,
        "group_chat_identities": {
            "agent_a": settings.group_chat_agent_a_identity,
            "agent_b": settings.group_chat_agent_b_identity,
        },
        "context_compaction": {
            "enabled": settings.context_compaction.enabled,
            "threshold_tokens": settings.context_compaction.threshold_tokens,
            "target_tokens": settings.context_compaction.target_tokens,
            "recent_messages": settings.context_compaction.recent_messages,
        },
        "token_api": {
            "enabled": settings.token_api.enabled,
            "base_url": settings.token_api.base_url,
        },
        "claude": agent_snapshot(settings.claude),
        "codex": agent_snapshot(settings.codex),
    }
    return {
        "config_path": str(settings.config_path) if settings.config_path else "",
        "group_chat_default_agent": settings.group_chat_default_agent,
        "worktree": settings.worktree,
        "group_chat_agent_a_identity": settings.group_chat_agent_a_identity,
        "group_chat_agent_b_identity": settings.group_chat_agent_b_identity,
        "claude_model": settings.claude.model,
        "codex_model": settings.codex.model,
        "claude_models": list(
            settings.claude.models
            or ((settings.claude.model,) if settings.claude.model else ())
        ),
        "codex_models": list(
            settings.codex.models
            or ((settings.codex.model,) if settings.codex.model else ())
        ),
        "claude_timeout": settings.claude.timeout,
        "codex_timeout": settings.codex.timeout,
        "resolved_config": resolved,
    }


def resume_value(record: object, key: str):
    if not isinstance(record, dict):
        return None
    snapshot = record.get("settings")
    return snapshot.get(key) if isinstance(snapshot, dict) else None


def apply_resume_settings(
    settings: BridgeSettings,
    record: object,
) -> BridgeSettings:
    """Restore model/session settings saved with an existing Web task."""

    if not isinstance(record, dict):
        return settings
    snapshot = record.get("settings")
    if not isinstance(snapshot, dict):
        return settings
    resolved_config = snapshot.get("resolved_config")
    if isinstance(resolved_config, dict):
        return resolve_bridge_settings(
            resolved_config,
            workspace=settings.workspace,
            config_path=settings.config_path,
        )
    changes: dict[str, object] = {}
    for name in ("group_chat_agent_a_identity", "group_chat_agent_b_identity"):
        value = snapshot.get(name)
        if isinstance(value, str) and value.strip():
            changes[name] = value

    def restore_agent(agent, prefix: str):
        model = snapshot.get(f"{prefix}_model")
        models = snapshot.get(f"{prefix}_models")
        timeout = snapshot.get(f"{prefix}_timeout")
        return replace(
            agent,
            model=model if isinstance(model, str) else None,
            models=(
                tuple(models)
                if isinstance(models, list)
                and all(isinstance(value, str) for value in models)
                else ((model,) if isinstance(model, str) else ())
            ),
            timeout=(
                float(timeout)
                if isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
                else agent.timeout
            ),
        )

    changes["claude"] = restore_agent(settings.claude, "claude")
    changes["codex"] = restore_agent(settings.codex, "codex")
    return replace(settings, **changes)
