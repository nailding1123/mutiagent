from __future__ import annotations

import unittest

from multiagent_cli.bridge_models import ContextCompactionSettings
from multiagent_cli.context_compaction import (
    build_context_projection,
    estimate_tokens,
)


def _message(index: int, sender: str, content: str) -> dict[str, object]:
    return {
        "id": f"message-{index}",
        "sender": sender,
        "role": "user" if sender == "user" else "assistant",
        "content": content,
        "recipients": ["claude", "codex"],
        "action": "discuss",
    }


def _format(message: dict[str, object]) -> str:
    return f"[{message['id']}] {message['sender']}\n{message['content']}"


class ContextCompactionTests(unittest.TestCase):
    def test_short_context_is_not_compacted(self) -> None:
        messages = [_message(1, "user", "检查这个问题")]
        projection = build_context_projection(
            messages,
            ContextCompactionSettings(threshold_tokens=100, target_tokens=50),
            _format,
        )

        self.assertFalse(projection.compacted)
        self.assertIsNone(projection.record)
        self.assertIn("检查这个问题", projection.text)

    def test_long_context_keeps_recent_original_and_extracts_critical_history(
        self,
    ) -> None:
        messages = [
            _message(
                1,
                "user",
                "必须保留这个约束：不要删除 src/state.py。" + "背景说明" * 120,
            ),
            *[
                _message(
                    index,
                    "codex" if index % 2 else "user",
                    f"第 {index} 轮普通讨论 " + "重复内容 " * 100,
                )
                for index in range(2, 9)
            ],
            _message(9, "user", "CURRENT_MESSAGE 必须原样保留"),
        ]
        projection = build_context_projection(
            messages,
            ContextCompactionSettings(
                threshold_tokens=300,
                target_tokens=180,
                recent_messages=2,
            ),
            _format,
        )

        self.assertTrue(projection.compacted)
        self.assertIn("group_chat_history_summary", projection.text)
        self.assertIn("不要删除 src/state.py", projection.text)
        self.assertIn("CURRENT_MESSAGE 必须原样保留", projection.text)
        self.assertIsNotNone(projection.record)
        record = projection.record or {}
        self.assertEqual(record["mode"], "extractive")
        self.assertLess(
            record["estimated_tokens_after"],
            record["estimated_tokens_before"],
        )
        self.assertEqual(len(messages), 9)

    def test_disabled_compaction_keeps_full_context(self) -> None:
        messages = [
            _message(index, "user", "很长的消息" * 200)
            for index in range(1, 5)
        ]
        projection = build_context_projection(
            messages,
            ContextCompactionSettings(
                enabled=False,
                threshold_tokens=10,
                target_tokens=5,
            ),
            _format,
        )

        self.assertFalse(projection.compacted)
        self.assertNotIn("group_chat_history_summary", projection.text)
        self.assertGreater(estimate_tokens(projection.text), 10)

    def test_force_compacts_history_below_the_standalone_threshold(self) -> None:
        messages = [
            _message(1, "user", "必须保留：不要删除 src/state.py。" + "背景" * 180),
            _message(2, "codex", "已经检查过相关实现。"),
            _message(3, "user", "CURRENT_MESSAGE 请继续"),
        ]
        settings = ContextCompactionSettings(
            threshold_tokens=2_000,
            target_tokens=500,
            recent_messages=1,
        )

        ordinary = build_context_projection(messages, settings, _format)
        forced = build_context_projection(messages, settings, _format, force=True)

        self.assertFalse(ordinary.compacted)
        self.assertTrue(forced.compacted)
        self.assertIn("不要删除 src/state.py", forced.text)
        self.assertIn("CURRENT_MESSAGE 请继续", forced.text)


if __name__ == "__main__":
    unittest.main()
