from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


DEFAULT_TOKEN_API_BASE_URL = "https://tokencheap.io"
TOKEN_API_KEY_ENV_VARS = ("MULTIAGENT_TOKEN_API_KEY", "TOKENCHEAP_API_KEY")


@dataclass(frozen=True)
class TokenAPISettings:
    """Project-safe Token API configuration; credentials are stored separately."""

    enabled: bool = False
    base_url: str = DEFAULT_TOKEN_API_BASE_URL


@dataclass(frozen=True)
class ModelSpec:
    id: str
    family: str
    note: str = ""
    temporary: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "family": self.family,
            "note": self.note,
            "temporary": self.temporary,
        }


def _models(family: str, *names: str, note: str = "") -> tuple[ModelSpec, ...]:
    return tuple(ModelSpec(name, family, note) for name in names)


CLAUDE_MODELS = (
    *_models(
        "Claude",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-opus-4-5",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
        "claude-sonnet-4",
        "claude-haiku-4-5",
    ),
    *_models(
        "Claude · 1M 上下文",
        "claude-opus-5[1M]",
        "claude-opus-4-8[1M]",
        "claude-opus-4-7[1M]",
        "claude-opus-4-6[1M]",
        "claude-sonnet-5[1M]",
        "claude-sonnet-4-6[1M]",
    ),
    *_models(
        "GPT · Claude Code 网关别名",
        "openai-gpt-5.6-sol",
        "openai-gpt-5.6-terra",
        "openai-gpt-5.6-luna",
        "openai-gpt-5.5",
        "openai-gpt-5.4",
        "openai-gpt-5.4-mini",
        "claude-gpt-5.6-sol",
        "claude-gpt-5.6-terra",
        "claude-gpt-5.6-luna",
        note="文档声明可在 Claude Code 中使用的网关模型名。",
    ),
    *_models(
        "Gemini · Claude Code",
        "gemini-3.5-flash",
        "gemini-3.1-pro-preview",
        "gemini-3-flash-preview",
    ),
    ModelSpec(
        "glm-5.2-s",
        "GLM · Claude Code",
        "文档标注为限时两个月免费；服务端当前是否仍开放需以实际响应为准。",
        True,
    ),
    ModelSpec(
        "claude-glm-5.2-s",
        "GLM · Claude Code",
        "文档标注为限时两个月免费；服务端当前是否仍开放需以实际响应为准。",
        True,
    ),
    *_models(
        "国内模型 · Claude Code",
        "qwen3.8-max",
        "deepseek-v4-flash-0731",
        "kimi-k3",
        "glm-5.2",
        "glm-5.2[1M]",
        "kimi-k2.6",
        "kimi-k2.7-code",
        "MiniMax-M2.5",
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "qwen3.7-max",
        "qwen3.7-plus",
        "qwen3.6-flash",
        "qwen3.6-plus",
        "qwen3.5-plus",
        "qwen3.5-flash",
        "glm-5",
        "deepseek-3.2",
        "qwen3-coder-next",
        "minimax-m2.5",
        "minimax-m2.1",
    ),
)

CODEX_MODELS = _models(
    "GPT · Codex Responses",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex-spark",
)

UNAVAILABLE_MODELS = (
    {
        "ids": ["gpt-image-2", "gpt-image-2-2k", "gpt-image-2-4k"],
        "kind": "图像生成",
        "reason": "只提供 /v1/images/generations 图像接口，不返回 Claude/Codex 对话与工具调用协议。",
    },
    {
        "ids": ["text-embedding-3-small", "text-embedding-3-large"],
        "kind": "Embedding",
        "reason": "只生成向量，不能作为会话式编码智能体回复消息或执行工具。",
    },
)

DEFAULT_MODEL_ORDERS = {
    "claude": [
        "claude-opus-5[1M]",
        "claude-sonnet-5[1M]",
        "openai-gpt-5.6-sol",
        "gemini-3.5-flash",
        "glm-5.2[1M]",
    ],
    "codex": [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
    ],
}


def public_model_catalog() -> dict[str, Any]:
    """Return the document-backed model catalog without credentials."""

    return {
        "claude": [model.to_dict() for model in CLAUDE_MODELS],
        "codex": [model.to_dict() for model in CODEX_MODELS],
        "defaults": {name: list(models) for name, models in DEFAULT_MODEL_ORDERS.items()},
    }


def known_incompatible_reason(agent: str, model: str) -> str:
    """Explain known incompatible selections while allowing future custom names."""

    for group in UNAVAILABLE_MODELS:
        if model in group["ids"]:
            return str(group["reason"])
    if agent == "codex" and any(item.id == model for item in CLAUDE_MODELS):
        return "该模型只在文档的 Claude Code 接入方式中出现，未声明支持 Codex Responses 与工具调用协议。"
    if agent == "claude" and any(item.id == model for item in CODEX_MODELS):
        return "该名称是 Codex 模型名；Claude Code 请使用文档给出的 openai-gpt-* 或 claude-gpt-* 网关别名。"
    return ""


class TokenAPICredentials:
    """Private API-key storage under the local MultiAgent state directory."""

    def __init__(
        self,
        state_root: str | Path,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.state_root = Path(state_root).expanduser()
        self.directory = self.state_root / "_credentials"
        self.path = self.directory / "token_api.json"
        self.environ = os.environ if environ is None else environ

    def load(self) -> str | None:
        for name in TOKEN_API_KEY_ENV_VARS:
            value = self.environ.get(name, "").strip()
            if value:
                return value
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            return None
        value = data.get("api_key") if isinstance(data, dict) else None
        return value.strip() if isinstance(value, str) and value.strip() else None

    def source(self) -> str:
        for name in TOKEN_API_KEY_ENV_VARS:
            if self.environ.get(name, "").strip():
                return name
        return "private_file" if self.load() else ""

    def status(self) -> dict[str, Any]:
        value = self.load()
        source = next(
            (
                name
                for name in TOKEN_API_KEY_ENV_VARS
                if self.environ.get(name, "").strip()
            ),
            "private_file" if value else "",
        )
        return {
            "configured": bool(value),
            "source": source,
            "masked": f"••••{value[-4:]}" if value else "",
        }

    @staticmethod
    def validate(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Token API Key 不能为空")
        normalized = value.strip()
        if any(char.isspace() for char in normalized):
            raise ValueError("Token API Key 不能包含空白字符")
        if len(normalized) < 8:
            raise ValueError("Token API Key 格式过短")
        return normalized

    def save(self, value: object) -> None:
        normalized = self.validate(value)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.state_root.chmod(0o700)
            self.directory.chmod(0o700)
        except OSError:
            pass
        temporary = self.path.with_suffix(".json.tmp")
        try:
            temporary.write_text(
                json.dumps({"api_key": normalized}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            temporary.replace(self.path)
            self.path.chmod(0o600)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise
