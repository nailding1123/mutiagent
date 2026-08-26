from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .bridge_models import ContextCompactionSettings


CONTEXT_PROJECTION_VERSION = 1
_CRITICAL_LINE_RE = re.compile(
    r"(?:必须|不要|不得|需要|要求|决定|结论|风险|错误|失败|通过|测试|验证|"
    r"修改|删除|新增|文件|路径|权限|待办|下一步|TODO|FIXME|error|failed|"
    r"decision|constraint|requirement|test|verify|risk|file|path)",
    re.IGNORECASE,
)
_TECHNICAL_LINE_RE = re.compile(
    r"(?:`[^`]+`|(?:^|\s)(?:[A-Za-z]:\\|/|\./|\.\./)[^\s]+|"
    r"\b[A-Za-z0-9_.-]+\.(?:py|js|ts|tsx|jsx|json|md|css|html|sh|swift|rs|go|java)\b)"
)


@dataclass(frozen=True)
class ContextProjection:
    text: str
    compacted: bool
    record: dict[str, Any] | None = None


def estimate_tokens(text: str) -> int:
    """Return a conservative tokenizer-free estimate for mixed CJK/code text."""

    if not text:
        return 0
    ascii_count = sum(1 for character in text if ord(character) < 128)
    non_ascii_count = len(text) - ascii_count
    return non_ascii_count + math.ceil(ascii_count / 4)


def build_context_projection(
    messages: list[dict[str, Any]],
    settings: ContextCompactionSettings,
    formatter: Callable[[dict[str, Any]], str],
    *,
    force: bool = False,
) -> ContextProjection:
    """Build a deterministic projection of shared messages.

    ``force`` is used when a resumable native session has crossed its
    cumulative budget even though the raw message list alone is below the
    standalone threshold.
    """

    formatted = [formatter(message) for message in messages]
    full_text = "\n\n".join(formatted)
    before_tokens = estimate_tokens(full_text)
    if (
        not settings.enabled
        or (not force and before_tokens <= settings.threshold_tokens)
        or len(messages) <= 1
    ):
        return ContextProjection(full_text, False)

    recent_start = max(0, len(messages) - settings.recent_messages)
    summary_budget = min(2_000, max(384, settings.target_tokens // 4))
    recent_budget = max(512, settings.target_tokens - summary_budget)
    while recent_start < len(messages) - 1:
        recent_text = "\n\n".join(formatted[recent_start:])
        if estimate_tokens(recent_text) <= recent_budget:
            break
        recent_start += 1

    historical = messages[:recent_start]
    if not historical:
        return ContextProjection(full_text, False)

    summary = _extractive_summary(historical, summary_budget)
    recent_text = "\n\n".join(formatted[recent_start:])
    projected_text = (
        '<group_chat_history_summary mode="extractive">\n'
        f"{summary}\n"
        "</group_chat_history_summary>\n\n"
        "<group_chat_recent_messages>\n"
        f"{recent_text}\n"
        "</group_chat_recent_messages>"
    )
    after_tokens = estimate_tokens(projected_text)
    if after_tokens >= before_tokens:
        return ContextProjection(full_text, False)

    source_hash = hashlib.sha256()
    for message in historical:
        source_hash.update(str(message.get("id", "")).encode("utf-8"))
        source_hash.update(b"\0")
        source_hash.update(str(message.get("content", "")).encode("utf-8"))
        source_hash.update(b"\0")
    record = {
        "version": CONTEXT_PROJECTION_VERSION,
        "mode": "extractive",
        "through_message_id": str(historical[-1].get("id", "")),
        "source_hash": source_hash.hexdigest(),
        "source_message_count": len(historical),
        "recent_message_count": len(messages) - recent_start,
        "estimated_tokens_before": before_tokens,
        "estimated_tokens_after": after_tokens,
        "summary": summary,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return ContextProjection(projected_text, True, record)


def _extractive_summary(
    messages: list[dict[str, Any]],
    token_budget: int,
) -> str:
    heading = (
        "以下内容由 MultiAgent 从较早的群聊记录中提取。它只保留原文片段，"
        "不代表新的 Agent 结论；消息 ID 可用于回查完整记录。"
    )
    remaining = max(64, token_budget - estimate_tokens(heading) - 24)
    candidates: list[tuple[int, int, str]] = []
    last_index = len(messages) - 1
    for index, message in enumerate(messages):
        is_user = message.get("role") == "user"
        entry_limit = 240 if is_user else 180
        entry = _summary_entry(message, entry_limit)
        score = 100 if is_user else 20
        content = str(message.get("content", ""))
        if _CRITICAL_LINE_RE.search(content):
            score += 40
        if _TECHNICAL_LINE_RE.search(content):
            score += 25
        if message.get("changes") or message.get("status") == "failed":
            score += 30
        score += round(20 * index / max(1, last_index))
        if index in {0, last_index}:
            score += 80
        candidates.append((score, index, entry))

    selected: list[tuple[int, str]] = []
    for _score, index, entry in sorted(
        candidates,
        key=lambda item: (-item[0], -item[1]),
    ):
        if remaining < 32:
            break
        entry_tokens = estimate_tokens(entry)
        if entry_tokens > remaining:
            entry = _truncate_to_tokens(entry, remaining)
            entry_tokens = estimate_tokens(entry)
        if entry_tokens < 8:
            continue
        selected.append((index, entry))
        remaining -= entry_tokens + 2

    selected.sort(key=lambda item: item[0])
    omitted = len(messages) - len(selected)
    lines = [heading, *(entry for _index, entry in selected)]
    if omitted > 0:
        lines.append(f"[压缩说明] 另有 {omitted} 条低优先级历史消息未展开。")
    return "\n\n".join(lines)


def _summary_entry(message: dict[str, Any], token_limit: int) -> str:
    sender = {
        "user": "用户",
        "claude": "Claude Code",
        "codex": "Codex",
    }.get(str(message.get("sender", "")), str(message.get("sender", "未知")))
    action = "执行" if message.get("action") == "execute" else "讨论"
    message_id = str(message.get("id", "?"))
    header = f"[{message_id}] [{action}] {sender}"
    content_budget = max(16, token_limit - estimate_tokens(header) - 2)
    content = _extract_content(str(message.get("content", "")), content_budget)
    return f"{header}\n{content}"


def _extract_content(content: str, token_limit: int) -> str:
    value = content.strip()
    if estimate_tokens(value) <= token_limit:
        return value
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        return _truncate_to_tokens(value, token_limit)

    selected: list[str] = []
    indexes = {0, min(1, len(lines) - 1), max(0, len(lines) - 1)}
    indexes.update(
        index
        for index, line in enumerate(lines)
        if _CRITICAL_LINE_RE.search(line) or _TECHNICAL_LINE_RE.search(line)
    )
    for index in sorted(indexes):
        line = lines[index]
        if line not in selected:
            selected.append(line)
    return _truncate_to_tokens("\n".join(selected), token_limit)


def _truncate_to_tokens(text: str, token_limit: int) -> str:
    value = text.strip()
    if token_limit <= 0:
        return ""
    if estimate_tokens(value) <= token_limit:
        return value
    marker = "\n…\n"
    low = 1
    high = len(value)
    best = value[:1]
    while low <= high:
        kept = (low + high) // 2
        head = max(1, round(kept * 0.7))
        tail = max(0, kept - head)
        candidate = value[:head] + marker + (value[-tail:] if tail else "")
        if estimate_tokens(candidate) <= token_limit:
            best = candidate
            low = kept + 1
        else:
            high = kept - 1
    return best.strip()
