from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from multiagent_cli.client import AnthropicCompatibleClient, OpenAICompatibleClient


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()
        return False


class ClientTests(unittest.TestCase):
    def test_non_streaming_chat_completion(self) -> None:
        response = FakeResponse(
            json.dumps({"choices": [{"message": {"content": "完整答案"}}]}).encode()
        )
        client = OpenAICompatibleClient(
            "https://relay.example/v1/", "test-key", endpoint="/chat/completions"
        )

        with patch("multiagent_cli.client.urlopen", return_value=response) as mocked:
            chunks = list(
                client.iter_chat(
                    model="model-a",
                    messages=[{"role": "user", "content": "问题"}],
                    parameters={"temperature": 0.2, "model": "cannot-override"},
                    stream=False,
                )
            )

        self.assertEqual(chunks, ["完整答案"])
        request = mocked.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://relay.example/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(payload["model"], "model-a")
        self.assertEqual(payload["temperature"], 0.2)
        self.assertFalse(payload["stream"])

    def test_streaming_chat_completion(self) -> None:
        response = FakeResponse(
            b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            b': keep-alive\n\n'
            b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
            b'data: [DONE]\n\n'
        )
        client = OpenAICompatibleClient("https://relay.example/v1", "test-key")

        with patch("multiagent_cli.client.urlopen", return_value=response):
            chunks = list(
                client.iter_chat(
                    model="model-a",
                    messages=[{"role": "user", "content": "question"}],
                )
            )

        self.assertEqual(chunks, ["hello", " world"])

    def test_anthropic_non_streaming_messages(self) -> None:
        response = FakeResponse(
            json.dumps(
                {"content": [{"type": "text", "text": "Anthropic 完整答案"}]}
            ).encode()
        )
        client = AnthropicCompatibleClient("https://tokencheap.io", "test-key")
        messages = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "问题"},
        ]

        with patch("multiagent_cli.client.urlopen", return_value=response) as mocked:
            chunks = list(
                client.iter_chat(
                    model="claude-sonnet-4-6[1M]",
                    messages=messages,
                    parameters={"temperature": 0.2},
                    stream=False,
                )
            )

        self.assertEqual(chunks, ["Anthropic 完整答案"])
        request = mocked.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://tokencheap.io/v1/messages")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(request.get_header("Anthropic-version"), "2023-06-01")
        self.assertEqual(payload["system"], "系统提示")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "问题"}])
        self.assertEqual(payload["max_tokens"], 4096)

    def test_anthropic_streaming_messages(self) -> None:
        response = FakeResponse(
            b'event: message_start\n'
            b'data: {"type":"message_start","message":{}}\n\n'
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hello"}}\n\n'
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":" anthropic"}}\n\n'
            b'event: message_stop\n'
            b'data: {"type":"message_stop"}\n\n'
        )
        client = AnthropicCompatibleClient("https://tokencheap.io", "test-key")

        with patch("multiagent_cli.client.urlopen", return_value=response):
            chunks = list(
                client.iter_chat(
                    model="claude-sonnet-4-6[1M]",
                    messages=[{"role": "user", "content": "question"}],
                )
            )

        self.assertEqual(chunks, ["hello", " anthropic"])


if __name__ == "__main__":
    unittest.main()
