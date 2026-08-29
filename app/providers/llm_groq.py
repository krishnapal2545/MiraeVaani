"""Groq — OpenAI-style chat completions with tool calling.

Groq's catalog is now reasoning models (gpt-oss, qwen) rather than the plain
llama-3.x chat models this app was written against, and reasoning costs a phone
call twice: the thinking eats the short `max_tokens` budget until `content` comes
back empty, and it doubles time-to-first-token. So gpt-oss is asked for the least
reasoning it will do, and any think block that leaks into `content` is stripped
before it can be spoken aloud.
"""

import json
import logging
import re
from typing import Any

from app.providers.base import LLMProvider, LLMReply, ToolCall, to_openai_messages

logger = logging.getLogger(__name__)

# Models that accept Groq's reasoning controls. Sending these to a model that
# does not support them is a 400, so the list stays explicit.
REASONING_MODELS = ("gpt-oss",)

THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


class GroqLLM(LLMProvider):
    name = "groq"

    def __init__(self, api_key: str, model: str = "openai/gpt-oss-20b") -> None:
        from groq import AsyncGroq

        self._client = AsyncGroq(api_key=api_key)
        self._model = model
        self._supports_tools = True

    def _payload(
        self,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None,
        temperature: float,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                *to_openai_messages(messages),
            ],
            "temperature": temperature,
            "max_tokens": max_output_tokens,
        }
        if any(marker in self._model for marker in REASONING_MODELS):
            # Measured on gpt-oss-20b: 245-513ms with 'low' against 520-768ms
            # without, and it stops the reply being crowded out by thinking.
            payload["reasoning_effort"] = "low"
        if tools and self._supports_tools:
            payload["tools"] = [{"type": "function", "function": t} for t in tools]
            payload["tool_choice"] = "auto"
        return payload

    async def complete(
        self,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.4,
        max_output_tokens: int = 150,
    ) -> LLMReply:
        payload = self._payload(messages, system, tools, temperature, max_output_tokens)

        try:
            response = await self._client.chat.completions.create(**payload)
        except Exception as exc:
            detail = str(exc)
            # Some Groq models (groq/compound-mini, allam-2-7b) reject tools
            # outright. Losing end_call beats losing every turn of the call.
            if tools and self._supports_tools and "tool calling` is not supported" in detail:
                logger.warning("%s does not support tools — retrying without them", self._model)
                self._supports_tools = False
                return await self.complete(
                    messages, system, None, temperature, max_output_tokens
                )
            # No traceback: on a live call this fires once per turn, and the
            # provider's own message ("model does not exist") is the useful part.
            logger.error("Groq request failed: %s", detail)
            return LLMReply(error=detail)

        message = response.choices[0].message
        calls = [
            ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=json.loads(tc.function.arguments or "{}"),
            )
            for tc in (message.tool_calls or [])
        ]

        text = THINK_BLOCK.sub("", message.content or "").strip()
        finish = response.choices[0].finish_reason
        error = ""
        if not text and not calls:
            # An empty reply is normally a reasoning model spending the whole
            # budget on thinking; say so rather than letting the fallback line
            # look like the caller was misheard.
            error = f"{self._model} returned no text (finish_reason={finish})"
        return LLMReply(
            text=text, tool_calls=calls, error=error, truncated=(finish == "length")
        )
