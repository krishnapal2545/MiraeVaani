"""xAI Grok — OpenAI-shaped chat completions over REST.

Not to be confused with `llm_groq.py`: Groq is an inference company running
Llama, xAI is Grok. Their keys are not interchangeable (`gsk_...` vs `xai-...`),
which is a mistake the credential page now catches.

xAI's API is OpenAI-compatible, so this adapter talks to it with httpx directly
rather than pulling in another SDK — the message list and tool-call shapes are
already what `DialogEngine` produces.
"""

import json
import logging
from typing import Any

import httpx

from app.providers.base import LLMProvider, LLMReply, ToolCall, to_openai_messages

logger = logging.getLogger(__name__)

XAI_CHAT_URL = "https://api.x.ai/v1/chat/completions"


class XaiLLM(LLMProvider):
    name = "xai"

    def __init__(self, api_key: str, model: str = "grok-4.6") -> None:
        self._model = model
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.4,
        max_output_tokens: int = 150,
    ) -> LLMReply:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                *to_openai_messages(messages),
            ],
            "temperature": temperature,
            "max_tokens": max_output_tokens,
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": t} for t in tools]
            payload["tool_choice"] = "auto"

        try:
            response = await self._client.post(XAI_CHAT_URL, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # xAI reports "no credits" and "wrong model" the same way the
            # credential page does — as a body, not a traceback.
            detail = exc.response.text[:300]
            logger.error("xAI %s: %s", exc.response.status_code, detail)
            return LLMReply(error=f"HTTP {exc.response.status_code}: {detail}")
        except httpx.HTTPError as exc:
            logger.error("xAI request failed: %s", exc)
            return LLMReply(error=str(exc))

        choices = response.json().get("choices") or []
        if not choices:
            return LLMReply()

        message = choices[0].get("message") or {}
        calls: list[ToolCall] = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                logger.warning("xAI returned unparseable tool arguments: %s", function)
                arguments = {}
            calls.append(
                ToolCall(
                    id=call.get("id") or function.get("name", ""),
                    name=function.get("name", ""),
                    arguments=arguments,
                )
            )

        return LLMReply(
            text=(message.get("content") or "").strip(),
            tool_calls=calls,
            truncated=choices[0].get("finish_reason") == "length",
        )

    async def close(self) -> None:
        await self._client.aclose()
