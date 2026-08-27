"""Groq (Llama et al.) — OpenAI-style chat completions with tool calling."""

import json
import logging
from typing import Any

from app.providers.base import LLMProvider, LLMReply, ToolCall

logger = logging.getLogger(__name__)


class GroqLLM(LLMProvider):
    name = "groq"

    def __init__(self, api_key: str, model: str = "llama-3.1-8b-instant") -> None:
        from groq import AsyncGroq

        self._client = AsyncGroq(api_key=api_key)
        self._model = model

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
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": temperature,
            "max_tokens": max_output_tokens,
        }
        if tools:
            payload["tools"] = [
                {"type": "function", "function": t} for t in tools
            ]
            payload["tool_choice"] = "auto"

        try:
            response = await self._client.chat.completions.create(**payload)
        except Exception:
            logger.exception("Groq request failed")
            return LLMReply()

        message = response.choices[0].message
        calls = [
            ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=json.loads(tc.function.arguments or "{}"),
            )
            for tc in (message.tool_calls or [])
        ]
        return LLMReply(text=(message.content or "").strip(), tool_calls=calls)
