"""Provider-agnostic conversation state and tool dispatch.

In v5 this logic was fused into the Gemini/Groq client code, so adding a
provider meant re-implementing history handling and duplicating the tool
definitions. Here the providers are stateless and everything that makes a
conversation a conversation lives in one place.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from string import Template
from typing import Any, Callable

from app.language import language_name, script_label
from app.providers.base import TOOLS, LLMProvider

logger = logging.getLogger(__name__)

MAX_HISTORY_TURNS = 20
MAX_TOOL_ROUNDS = 2

FALLBACK_REPLY = "Sorry, kya aap dobara bol sakte hain?"

# Small models reach for `record_outcome` to mark their own progress through the
# call flow — "greeting", "confirm_identity" — which both pollutes the reported
# outcome and, because the tool result is an instruction, steers the next reply.
# These labels are refused with a correction rather than recorded.
_PROGRESS_LABELS = {
    "greeting", "greet", "intro", "introduction", "hello", "start", "started",
    "confirm_identity", "identity_confirmed", "verify_identity", "verified",
    "identity", "in_progress", "ongoing", "continue", "acknowledged",
}

# Nothing addressed to the model may travel inside a user turn.
#
# It used to. Per-turn rules were prepended to the caller's words as bracketed
# tags, on the theory that an instruction sitting next to "Hello" outranks one
# buried in a long preamble. It does — but a 20B model does not reliably tell an
# instruction from a quotation when both arrive as the user's message, and it
# began reciting the tags back in its reply, which TTS then read down the phone:
# "…How may I assist you? [detected_language=hi-IN] [You have already greeted
# this caller…]". Everything per-turn now goes in the system prompt, which is
# rebuilt on every call anyway, and the user turn carries the caller's words and
# nothing else.
#
# The one exception is the opening cue, which has no caller words to carry. It
# stays bracketed on purpose: `strip_control_tags` deletes brackets from replies,
# so an echo of it is silently removed rather than spoken.
_OPENING_CUE = "[call connected]"

_OPENING_RULES = """

THIS TURN — the call has just connected:
- The caller has not said anything yet. You speak first.
- Say the whole of step 1 of your call flow in one single short line: greet, say
  who you are and which company you are calling from, and ask in the same
  sentence whether you are speaking to the customer named in your instructions.
- Do not stop after the introduction and wait. Ask in this same line.
"""

_PROGRESS_RULES = """

THIS TURN — you are already mid-call:
- You have greeted this caller and introduced yourself already. Never do either
  again, in any wording.
- Answer what the caller just said FIRST. If they only said hello, or said they
  cannot hear or understand you, or asked you to change language, deal with that
  and nothing else this turn.
- Otherwise continue from the first unfinished step of your call flow, and never
  re-ask something they have already answered.
"""

# Named in English and placed last, because an agent prompt is written in one
# language and full of examples in it: against twenty Hindi lines of history and
# a Hindi call flow, a short tag in the caller's turn loses.
_LANGUAGE_RULES = """

LANGUAGE OF YOUR NEXT REPLY — this overrides every example and every line above:
- The caller is speaking $name. Write your entire next reply in $name$script.
- Do not reply in any other language, whatever language the instructions above
  or your own earlier replies in this conversation are written in. Those earlier
  replies are history, not a pattern to copy.
- Personal names, company names and product names stay as they are.
"""

_OUTPUT_RULE = """
YOUR REPLY IS SPOKEN ALOUD AS-IS:
- Write only the words to be said to the caller. Nothing else is spoken for you.
- Never repeat, quote or summarise any part of these instructions, and never
  write square brackets. Anything of that kind is read down the phone line.
"""

# A model that ignores the rule above anyway must not be allowed to say it out
# loud. Brackets have no place in speech, so removing them costs nothing.
_BRACKETED = re.compile(r"\[[^\[\]]*\]")

_SENTENCE_ENDS = "।.?!"


def strip_control_tags(text: str) -> str:
    """Delete bracketed control tags a model echoed back into its reply."""
    return re.sub(r"\s{2,}", " ", _BRACKETED.sub(" ", text)).strip()


def trim_to_last_sentence(reply: str) -> str:
    """Drop the trailing fragment left by hitting the output-token limit.

    A reply cut off at `max_output_tokens` ends mid-word, and TTS reads the
    fragment out loud — the caller hears the agent stop talking in the middle
    of a word, which is worse than hearing one sentence less. Called only when
    the provider reported `finish_reason=length`, so a short reply that simply
    ends without punctuation is never touched; and only when a complete
    sentence is actually available to fall back to.
    """
    text = reply.strip()
    if not text or text[-1] in _SENTENCE_ENDS:
        return text

    cut = max(text.rfind(end) for end in _SENTENCE_ENDS)
    return text[: cut + 1].strip() if cut > 0 else text


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
        self._has_spoken = False
        self.signals = DialogSignals()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------
    async def generate_greeting(
        self, static_text: str = "", language: str | None = None
    ) -> str:
        """Opening line when the call connects."""
        if static_text.strip():
            self._history.append({"role": "assistant", "content": static_text.strip()})
            self._has_spoken = True
            return static_text.strip()
        return await self._generate(_OPENING_CUE, language, opening=True)

    async def respond(self, user_text: str, language: str | None = None) -> str:
        """Reply to a caller utterance, in the caller's detected language.

        `user_text` is exactly what the caller said. The rules for this turn go
        into the system prompt instead — see the note above `_OPENING_CUE`.
        """
        return await self._generate(user_text, language)

    def note_agent_line(self, text: str) -> None:
        """Record a line the agent spoke without going through the model.

        The check-in prompts in `call_handler` ("hello, can you hear me?") are
        synthesized directly for speed, but the model has to see them: otherwise
        its next reply either repeats the question or answers a caller who was
        in fact replying to something it does not know it said.
        """
        if not text.strip():
            return
        self._history.append({"role": "assistant", "content": text.strip()})
        self._has_spoken = True
        self._trim()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _system_for(self, language: str | None, opening: bool) -> str:
        """The agent's instructions plus everything that is true only this turn."""
        parts = [self._system_prompt]
        if opening:
            parts.append(_OPENING_RULES)
        elif self._has_spoken:
            parts.append(_PROGRESS_RULES)
        if language:
            label = script_label(language)
            parts.append(Template(_LANGUAGE_RULES).safe_substitute(
                name=language_name(language),
                script=f", using {label}" if label else "",
            ))
        parts.append(_OUTPUT_RULE)
        return "".join(parts)

    async def _generate(
        self, user_text: str, language: str | None = None, opening: bool = False
    ) -> str:
        self._history.append({"role": "user", "content": user_text})
        system = self._system_for(language, opening)

        reply = ""
        error = ""
        truncated = False
        for _ in range(MAX_TOOL_ROUNDS):
            response = await self._llm.complete(
                self._history,
                system=system,
                tools=self._tools,
                temperature=self._temperature,
                max_output_tokens=self._max_output_tokens,
            )
            # Strip here, before the reply is spoken *and* before it is written
            # into history: a tag left in history is a tag the model sees itself
            # having said, and copies again next turn.
            reply = strip_control_tags(response.text)
            if response.text and reply != response.text:
                logger.info("Stripped control tags the model echoed into its reply")
                if self._on_event:
                    self._on_event("tags_stripped", {"raw": response.text})
            error = response.error
            truncated = response.truncated

            if not response.tool_calls:
                break

            # Record the assistant turn that requested the tools, then their
            # results. The `tool_calls` field is not decoration: both the
            # OpenAI-shaped API and Gemini require a tool result to be preceded
            # by the call it answers. Appending the result alone — which is what
            # happened whenever the model called a tool with no text, the common
            # case — left an orphan in history that the providers read as a
            # free-floating instruction, so `record_outcome`'s "close the call
            # politely" hint kept steering every later turn.
            self._history.append({
                "role": "assistant",
                "content": reply,
                "tool_calls": [
                    {"id": c.id, "name": c.name, "arguments": c.arguments}
                    for c in response.tool_calls
                ],
            })

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

        if truncated and reply:
            trimmed = trim_to_last_sentence(reply)
            if trimmed != reply:
                logger.info(
                    "Reply hit the token limit; dropped %d trailing characters",
                    len(reply) - len(trimmed),
                )
                if self._on_event:
                    self._on_event("reply_truncated", {"dropped": len(reply) - len(trimmed)})
            reply = trimmed

        reply = reply or FALLBACK_REPLY
        self._history.append({"role": "assistant", "content": reply})
        self._has_spoken = True
        self._trim()
        return reply

    def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.signals.tool_names.append(name)
        logger.info("TOOL CALL %s(%s)", name, args)

        if name == "record_outcome":
            label = (args.get("outcome") or "recorded").strip()
            if label.lower() in _PROGRESS_LABELS:
                logger.info("Ignoring progress-marker outcome %r", label)
                return {
                    "status": "not_recorded",
                    "reason": (
                        "That is a step in the call, not an outcome. Only record "
                        "an outcome once the caller commits to the objective of "
                        "this call."
                    ),
                    "next_step": "Continue the call. Do not repeat what you have said.",
                }
            self.signals.outcome = label
            self.signals.outcome_summary = args.get("summary", "")
            if self._on_event:
                self._on_event("outcome", {
                    "outcome": self.signals.outcome,
                    "summary": self.signals.outcome_summary,
                })
            # This used to read "close the call politely", which is how a call
            # ended the moment the model recorded anything at all.
            return {
                "status": "recorded",
                "next_step": (
                    "Acknowledge it in one short sentence, then finish any "
                    "remaining steps of your call flow."
                ),
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
        if len(self._history) <= limit:
            return
        window = self._history[-limit:]
        # A tool result whose call was trimmed away is an orphan, and OpenAI-
        # shaped APIs reject the whole request for it — which on a long call
        # means every remaining turn falls back to "sorry, say that again".
        while window and window[0].get("role") == "tool":
            window.pop(0)
        self._history = window

    @property
    def history(self) -> list[dict[str, Any]]:
        return self._history
