from __future__ import annotations

import json
import socket
from collections.abc import Iterator, Sequence
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class APIError(RuntimeError):
    """A readable error returned by the compatible API."""


class OpenAICompatibleClient:
    """Minimal Chat Completions client implemented with the Python standard library."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        endpoint: str = "chat/completions",
        timeout: float = 120,
    ) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.url = self._make_url(base_url, endpoint)

    @staticmethod
    def _make_url(base_url: str, endpoint: str) -> str:
        if endpoint.startswith(("http://", "https://")):
            return endpoint
        return f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"

    def iter_chat(
        self,
        *,
        model: str,
        messages: Sequence[dict[str, str]],
        parameters: dict[str, Any] | None = None,
        stream: bool = True,
    ) -> Iterator[str]:
        payload = dict(parameters or {})
        # These fields define the protocol and cannot be overridden by config.
        payload.update(
            {
                "model": model,
                "messages": list(messages),
                "stream": stream,
            }
        )
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if stream else "application/json",
                "User-Agent": "relay-multiagent-cli/0.1",
            },
        )

        try:
            with urlopen(request, timeout=self.timeout) as response:
                if stream:
                    yield from self._read_stream(response)
                else:
                    data = json.load(response)
                    content = self._message_content(data)
                    if content:
                        yield content
        except HTTPError as exc:
            detail = self._http_error_detail(exc)
            raise APIError(f"API 请求失败（HTTP {exc.code}）：{detail}") from exc
        except (URLError, TimeoutError, socket.timeout) as exc:
            reason = getattr(exc, "reason", exc)
            raise APIError(f"无法连接 API：{reason}") from exc
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise APIError(f"API 返回了无法识别的数据：{exc}") from exc

    def _read_stream(self, response: Any) -> Iterator[str]:
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue

            data_text = line[5:].strip()
            if data_text == "[DONE]":
                return

            data = json.loads(data_text)
            choices = data.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            content = self._content_text(delta.get("content"))
            if content:
                yield content

    @classmethod
    def _message_content(cls, data: dict[str, Any]) -> str:
        choices = data.get("choices") or []
        if not choices:
            raise APIError("API 响应中没有 choices")
        message = choices[0].get("message") or {}
        return cls._content_text(message.get("content"))

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            return "".join(parts)
        return ""

    @staticmethod
    def _http_error_detail(exc: HTTPError) -> str:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            error = data.get("error", data)
            if isinstance(error, dict):
                return str(error.get("message") or error)
            return str(error)
        except (json.JSONDecodeError, AttributeError):
            return raw[:500] if "raw" in locals() else str(exc.reason)


class AnthropicCompatibleClient(OpenAICompatibleClient):
    """Minimal Anthropic Messages client, including its SSE event format."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        endpoint: str = "v1/messages",
        timeout: float = 120,
        anthropic_version: str = "2023-06-01",
    ) -> None:
        super().__init__(base_url, api_key, endpoint=endpoint, timeout=timeout)
        self.anthropic_version = anthropic_version

    def iter_chat(
        self,
        *,
        model: str,
        messages: Sequence[dict[str, str]],
        parameters: dict[str, Any] | None = None,
        stream: bool = True,
    ) -> Iterator[str]:
        system_parts = [
            message["content"] for message in messages if message.get("role") == "system"
        ]
        conversation = [
            message for message in messages if message.get("role") in {"user", "assistant"}
        ]

        payload = dict(parameters or {})
        payload.setdefault("max_tokens", 4096)
        payload.update(
            {
                "model": model,
                "messages": conversation,
                "stream": stream,
            }
        )
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        else:
            payload.pop("system", None)

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            self.url,
            data=body,
            method="POST",
            headers={
                # Claude Code sends ANTHROPIC_AUTH_TOKEN as a Bearer token.
                "Authorization": f"Bearer {self.api_key}",
                "Anthropic-Version": self.anthropic_version,
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if stream else "application/json",
                "User-Agent": "relay-multiagent-cli/0.1",
            },
        )

        try:
            with urlopen(request, timeout=self.timeout) as response:
                if stream:
                    yield from self._read_anthropic_stream(response)
                else:
                    data = json.load(response)
                    content = self._anthropic_message_content(data)
                    if content:
                        yield content
        except HTTPError as exc:
            detail = self._http_error_detail(exc)
            raise APIError(f"API 请求失败（HTTP {exc.code}）：{detail}") from exc
        except (URLError, TimeoutError, socket.timeout) as exc:
            reason = getattr(exc, "reason", exc)
            raise APIError(f"无法连接 API：{reason}") from exc
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise APIError(f"API 返回了无法识别的数据：{exc}") from exc

    def _read_anthropic_stream(self, response: Any) -> Iterator[str]:
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue

            data = json.loads(line[5:].strip())
            event_type = data.get("type")
            if event_type == "error":
                error = data.get("error") or data
                if isinstance(error, dict):
                    detail = error.get("message") or error
                else:
                    detail = error
                raise APIError(f"Anthropic 流式响应错误：{detail}")
            if event_type == "message_stop":
                return
            if event_type != "content_block_delta":
                continue

            delta = data.get("delta") or {}
            if delta.get("type") != "text_delta":
                continue
            text = delta.get("text")
            if isinstance(text, str) and text:
                yield text

    @classmethod
    def _anthropic_message_content(cls, data: dict[str, Any]) -> str:
        content = data.get("content")
        if not isinstance(content, list):
            raise APIError("Anthropic 响应中没有 content")
        return cls._content_text(content)
