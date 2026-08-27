"""Google Gemini — translates the OpenAI-shaped message list into Gemini Contents.

v5 declared Gemini's tools as Python closures capturing `self`, which meant they
could not be built from a config row and had to be duplicated by hand for Groq.
Here the canonical JSON-Schema tools in `base.TOOLS` are converted to Gemini
function declarations, and automatic function calling is disabled so tool calls
come back as data for `DialogEngine` to dispatch.

Thinking is disabled (`thinking_budget=0`) — on a phone call, latency beats depth.
"""

import logging
from typing import Any

from app.providers.base import LLMProvider, LLMReply, ToolCall

logger = logging.getLogger(__name__)


class GeminiLLM(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash-lite") -> None:
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model

    def _to_contents(self, messages: list[dict[str, Any]]) -> list[Any]:
        from google.genai import types

        contents = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content") or ""

            if role == "tool":
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_function_response(
                                name=msg.get("name", "tool"),
                                response={"result": content},
                            )
                        ],
                    )
                )
            elif role == "assistant":
                if content:
                    contents.append(
                        types.Content(
                            role="model", parts=[types.Part.from_text(text=content)]
                        )
                    )
            else:
                contents.append(
                    types.Content(
                        role="user", parts=[types.Part.from_text(text=content)]
                    )
                )
        return contents

    def _to_config(
        self,
        system: str,
        tools: list[dict[str, Any]] | None,
        temperature: float,
        max_output_tokens: int,
    ) -> Any:
        from google.genai import types

        kwargs: dict[str, Any] = {
            "system_instruction": system,
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
            "thinking_config": types.ThinkingConfig(thinking_budget=0),
        }
        if tools:
            kwargs["tools"] = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t["name"],
                            description=t["description"],
                            parameters=t["parameters"],
                        )
                        for t in tools
                    ]
                )
            ]
            # Tool calls must arrive as data, not be auto-executed by the SDK.
            kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(
                disable=True
            )
        return types.GenerateContentConfig(**kwargs)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.4,
        max_output_tokens: int = 150,
    ) -> LLMReply:
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=self._to_contents(messages),
                config=self._to_config(system, tools, temperature, max_output_tokens),
            )
        except Exception:
            logger.exception("Gemini request failed")
            return LLMReply()

        text_parts: list[str] = []
        calls: list[ToolCall] = []

        for candidate in response.candidates or []:
            for part in (getattr(candidate.content, "parts", None) or []):
                if getattr(part, "function_call", None):
                    fc = part.function_call
                    calls.append(
                        ToolCall(
                            id=fc.name,
                            name=fc.name,
                            arguments=dict(fc.args or {}),
                        )
                    )
                elif getattr(part, "text", None):
                    text_parts.append(part.text)

        return LLMReply(text="".join(text_parts).strip(), tool_calls=calls)
