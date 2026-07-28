from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .models import Agent, Settings


class ConfigError(ValueError):
    """Invalid or incomplete CLI configuration."""


def load_json_config(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    config_path = Path(path)
    try:
        with config_path.open(encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError as exc:
        raise ConfigError(f"找不到配置文件：{config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是有效 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("配置文件顶层必须是 JSON 对象")
    return data


def resolve_settings(
    data: dict[str, Any],
    *,
    base_url: str | None = None,
    endpoint: str | None = None,
    models: list[str] | None = None,
    timeout: float | None = None,
) -> Settings:
    protocol = data.get("protocol", "openai")
    if protocol not in {"openai", "anthropic"}:
        raise ConfigError("protocol 必须是 openai 或 anthropic")

    base_url_env = "ANTHROPIC_BASE_URL" if protocol == "anthropic" else "OPENAI_BASE_URL"
    resolved_base_url = base_url or os.getenv(base_url_env) or data.get("base_url")
    if not isinstance(resolved_base_url, str) or not resolved_base_url.strip():
        raise ConfigError("缺少 API 地址，请使用 --base-url 或在配置中填写 base_url")

    default_endpoint = "v1/messages" if protocol == "anthropic" else "chat/completions"
    resolved_endpoint = endpoint or data.get("endpoint", default_endpoint)
    if not isinstance(resolved_endpoint, str) or not resolved_endpoint.strip():
        raise ConfigError("endpoint 必须是非空字符串")

    resolved_timeout = timeout if timeout is not None else data.get("timeout", 120)
    if not isinstance(resolved_timeout, (int, float)) or resolved_timeout <= 0:
        raise ConfigError("timeout 必须是正数")

    default_key_env = "ANTHROPIC_AUTH_TOKEN" if protocol == "anthropic" else "OPENAI_API_KEY"
    api_key_env = data.get("api_key_env", default_key_env)
    if not isinstance(api_key_env, str) or not api_key_env:
        raise ConfigError("api_key_env 必须是非空字符串")

    api_key = data.get("api_key")
    if api_key is not None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ConfigError("api_key 必须是非空字符串；不想写入配置时请删除该字段")
        api_key = api_key.strip()

    anthropic_version = data.get("anthropic_version", "2023-06-01")
    if not isinstance(anthropic_version, str) or not anthropic_version.strip():
        raise ConfigError("anthropic_version 必须是非空字符串")

    agents = _resolve_agents(data, models)
    return Settings(
        base_url=resolved_base_url.strip(),
        protocol=protocol,
        endpoint=resolved_endpoint.strip(),
        api_key=api_key,
        api_key_env=api_key_env,
        anthropic_version=anthropic_version.strip(),
        timeout=float(resolved_timeout),
        agents=tuple(agents),
    )


def _resolve_agents(data: dict[str, Any], models: list[str] | None) -> list[Agent]:
    common_parameters = data.get("parameters", {})
    if not isinstance(common_parameters, dict):
        raise ConfigError("parameters 必须是 JSON 对象")

    if models:
        raw_agents: list[Any] = [
            {"name": _agent_name(index), "model": model}
            for index, model in enumerate(models)
        ]
    else:
        raw_agents = data.get("agents", [])

    if not isinstance(raw_agents, list) or not raw_agents:
        raise ConfigError("至少需要两个 Agent；重复使用 --model，或在配置中填写 agents")

    agents: list[Agent] = []
    for index, raw in enumerate(raw_agents):
        if not isinstance(raw, dict):
            raise ConfigError(f"第 {index + 1} 个 Agent 配置必须是 JSON 对象")
        model = raw.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ConfigError(f"第 {index + 1} 个 Agent 缺少 model")
        name = raw.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ConfigError(f"第 {index + 1} 个 Agent 的 name 无效")
        system_prompt = raw.get("system_prompt")
        if system_prompt is not None and not isinstance(system_prompt, str):
            raise ConfigError(f"Agent {name} 的 system_prompt 必须是字符串")
        agent_parameters = raw.get("parameters", {})
        if not isinstance(agent_parameters, dict):
            raise ConfigError(f"第 {index + 1} 个 Agent 的 parameters 必须是 JSON 对象")

        count = raw.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ConfigError(f"第 {index + 1} 个 Agent 的 count 必须是正整数")

        configured_type = raw.get("type")
        if configured_type is not None and configured_type not in {"draft", "review", "final"}:
            raise ConfigError(
                f"第 {index + 1} 个 Agent 的 type 必须是 draft、review 或 final"
            )

        parameters = {**common_parameters, **agent_parameters}
        for copy_index in range(count):
            expanded_index = len(agents)
            agent_type = configured_type or ("draft" if expanded_index == 0 else "review")
            if name is None:
                expanded_name = _agent_name(expanded_index)
            elif count == 1:
                expanded_name = name.strip()
            else:
                expanded_name = f"{name.strip()} {copy_index + 1}"
            agents.append(
                Agent(
                    name=expanded_name,
                    model=model.strip(),
                    type=agent_type,
                    system_prompt=system_prompt,
                    parameters=parameters.copy(),
                )
            )

    if len(agents) < 2:
        raise ConfigError("展开 count 后仍至少需要两个 Agent")
    if agents[0].type != "draft":
        raise ConfigError("第一个 Agent 的 type 必须是 draft")
    if any(agent.type == "draft" for agent in agents[1:]):
        raise ConfigError("只有第一个 Agent 可以使用 draft 类型")
    if any(agent.type == "final" for agent in agents[:-1]):
        raise ConfigError("final 类型只能用于最后一个 Agent")
    return agents


def _agent_name(index: int) -> str:
    if index < 26:
        return f"Agent {chr(ord('A') + index)}"
    return f"Agent {index + 1}"
