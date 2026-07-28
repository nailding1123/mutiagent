from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Agent:
    """One model and its role in the revision chain."""

    name: str
    model: str
    type: str = "auto"
    system_prompt: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Turn:
    """The completed answer produced by one agent."""

    agent: Agent
    answer: str


@dataclass(frozen=True)
class Settings:
    """Resolved application settings."""

    base_url: str
    protocol: str
    endpoint: str
    api_key: str | None = field(repr=False)
    api_key_env: str
    anthropic_version: str
    timeout: float
    agents: tuple[Agent, ...]
