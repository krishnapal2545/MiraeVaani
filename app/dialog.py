"""Provider-agnostic conversation state and tool dispatch.

In v5 this logic was fused into the Gemini/Groq client code, so adding a
provider meant re-implementing history handling and duplicating the tool
definitions. Here the providers are stateless and everything that makes a
conversation a conversation lives in one place.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from app.providers.base import TOOLS, LLMProvider

logger = logging.getLogger(__name__)

MAX_HISTORY_TURNS = 20
MAX_TOOL_ROUNDS = 2

FALLBACK_REPLY = "Sorry, kya aap dobara bol sakte hain?"


@dataclass
class DialogSignals:
    """Side effects the LLM asked for, read by CallSession after each turn."""

    end_call: bool = False
    outcome: str | None = None
    outcome_summary: str | None = None
    transfer_to: str | None = None
    tool_names: list[str] = field(default_factory=list)


class DialogEngine:
    def __init__(
        self,
        llm: LLMProvider,
        system_prompt: str,
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.4,
        max_output_tokens: int = 150,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self._llm = llm
        self._system_prompt = system_prompt
        self._tools = TOOLS if tools is None else tools
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._on_event = on_event
        self._history: list[dict[str, Any]] = []
        self.signals = DialogSignals()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------
    async def generate_greeting(self, static_text: str = "") -> str:
        """Opening line when the call connects."""
        if static_text.strip():
            self._history.append({"role": "assistant", "content": static_text.strip()})
            return static_text.strip()
        return await self._generate(
            "[The call has just connected. Greet the caller now exactly as your "
            "instructions describe. One or two short sentences only.]"
        )

    async def respond(self, user_text: str, language: str | None = None) -> str:
        """Reply to a caller utterance, in the caller's detected language."""
        if language:
            user_text = f"[detected_language={language}] {user_text}"
        return await self._generate(user_text)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    async def _generate(self, user_text: str) -> str:
        self._history.append({"role": "user", "content": user_text})

        reply = ""
        error = ""
        for _ in range(MAX_TOOL_ROUNDS):
            response = await self._llm.complete(
                self._history,
                system=self._system_prompt,
                tools=self._tools,
                temperature=self._temperature,
                max_output_tokens=self._max_output_tokens,
            )
            reply = response.text
            error = response.error

            if not response.tool_calls:
                break

            # Record the assistant turn that requested the tools, then their results.
            if reply:
                self._history.append({"role": "assistant", "content": reply})

            for call in response.tool_calls:
                result = self._dispatch(call.name, call.arguments)
                self._history.append({
                    "role": "tool",
                    "name": call.name,
                    "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })

            # Loop once more so the model can speak after acting.

        if not reply:
            # The caller hears the fallback either way; the UI and the log
            # should still say whether the model failed or just said nothing.
            logger.warning("Falling back — LLM returned nothing. %s", error or "(no error)")
            if self._on_event:
                self._on_event("llm_error", {"error": error or "empty response"})

        reply = reply or FALLBACK_REPLY
        self._history.append({"role": "assistant", "content": reply})
        self._trim()
        return reply

    def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.signals.tool_names.append(name)
        logger.info("TOOL CALL %s(%s)", name, args)

        if name == "record_outcome":
            self.signals.outcome = args.get("outcome") or "recorded"
            self.signals.outcome_summary = args.get("summary", "")
            if self._on_event:
                self._on_event("outcome", {
                    "outcome": self.signals.outcome,
                    "summary": self.signals.outcome_summary,
                })
            return {
                "status": "recorded",
                "next_step": "Confirm back to the caller and close the call politely.",
            }

        if name == "end_call":
            self.signals.end_call = True
            if self._on_event:
                self._on_event("end_call_triggered", {})
            return {"status": "call_ending"}

        if name == "transfer_to_human":
            self.signals.transfer_to = args.get("number") or ""
            if self._on_event:
                self._on_event("transfer_requested", {"number": self.signals.transfer_to})
            return {"status": "transferring"}

        logger.warning("Unknown tool requested: %s", name)
        return {"error": "unknown tool"}

    def _trim(self) -> None:
        limit = MAX_HISTORY_TURNS * 2
        if len(self._history) > limit:
            self._history = self._history[-limit:]

    @property
    def history(self) -> list[dict[str, Any]]:
        return self._history
