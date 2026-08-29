"""Provider interfaces.

Two rules make UI-supplied credentials work, and both are load-bearing:

1. Every adapter takes its API key as a constructor argument. Nothing under
   `providers/` may import `get_settings()`.
2. Message history is OpenAI-shaped (`[{"role", "content"}]`) everywhere. Groq
   consumes it natively; the Gemini adapter translates. Conversation state and
   the tool loop live in `dialog.py`, so providers stay stateless.
"""

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # True when generation stopped at the token limit rather than because the
    # model finished. The tail is then a fragment, and TTS reads fragments
    # aloud — the caller hears the agent stop mid-word.
    truncated: bool = False
    # Why the reply is empty, when it is empty. A silent fallback line on every
    # turn (a retired model id, an unfunded account) is otherwise invisible to
    # anyone watching the call in the UI.
    error: str = ""


# Canonical tool definitions (JSON Schema). v5 kept a hand-maintained Groq copy
# alongside Gemini closures that captured `self` — those could never be built
# from a config row, so both providers now translate from this single source.
TOOLS: list[dict[str, Any]] = [
    {
        "name": "record_outcome",
        "description": (
            "Record that the caller has clearly committed to the call's objective "
            "(for example agreeing to pay, to add funds, or to renew a document). "
            "Call only on a clear, unambiguous commitment — never for vague "
            "answers like 'maybe' or 'I'll see'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "outcome": {
                    "type": "string",
                    "description": "Short machine-friendly label, e.g. 'agreed_to_pay'.",
                },
                "summary": {
                    "type": "string",
                    "description": "One short sentence describing what the caller agreed to.",
                },
            },
            "required": ["outcome", "summary"],
        },
    },
    {
        "name": "end_call",
        "description": (
            "End the phone call. Call this ONLY after you have already said your "
            "final goodbye."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]


def to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render DialogEngine history in the wire shape OpenAI-style APIs expect.

    Tool calls travel through history as plain dicts so the engine stays
    provider-agnostic; on the wire they become `tool_calls`, whose arguments are
    a JSON *string*, not an object. Sending the assistant turn without them
    orphans the `role: tool` message that answers it — which the model then
    reads as a free-floating instruction rather than as a tool result.
    """
    wire: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        calls = msg.get("tool_calls")
        if role == "assistant" and calls:
            wire.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(
                                call.get("arguments") or {}, ensure_ascii=False
                            ),
                        },
                    }
                    for call in calls
                ],
            })
        elif role == "tool":
            wire.append({
                "role": "tool",
                "tool_call_id": msg.get("tool_call_id", ""),
                "content": msg.get("content") or "",
            })
        else:
            wire.append({"role": role, "content": msg.get("content") or ""})
    return wire


class STTProvider(ABC):
    """Transcribes one complete utterance."""

    name: str = ""

    @abstractmethod
    async def transcribe(
        self, wav16k: bytes, language: str | None = None
    ) -> tuple[str, str | None]:
        """Returns (transcript, detected BCP-47 language or None).

        `language=None` asks the provider to auto-detect where it can.
        """

    async def close(self) -> None:
        return None


class TTSProvider(ABC):
    """Synthesizes speech as Twilio-ready 8kHz mu-law."""

    name: str = ""

    @abstractmethod
    def synthesize_streaming(
        self, text: str, language: str | None = None
    ) -> AsyncGenerator[bytes, None]:
        """Yields 8kHz mu-law, sentence by sentence, for low time-to-first-byte."""

    async def synthesize(self, text: str, language: str | None = None) -> bytes:
        """Whole utterance at once — used to pre-render filler clips."""
        audio = bytearray()
        async for chunk in self.synthesize_streaming(text, language):
            audio.extend(chunk)
        return bytes(audio)

    async def close(self) -> None:
        return None


class LLMProvider(ABC):
    """Stateless completion. History and tool dispatch belong to DialogEngine."""

    name: str = ""

    @abstractmethod
    async def complete(
        self,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.4,
        max_output_tokens: int = 150,
    ) -> LLMReply:
        ...

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Pauses
#
# A model asked to sound human writes "जी बिलकुल... एक मिनट". Every TTS engine
# here would otherwise read that as a sentence break (or, worse, say "dot dot
# dot"), so the marker is normalised to a single character on the way in and
# each provider decides what to do with it: Google turns it into an SSML break,
# engines without SSML get a comma, which they already pause on.
# ---------------------------------------------------------------------------
PAUSE_MARK = "…"

_ELLIPSIS_RE = re.compile(r"\s*(?:\.\s*){2,}\.?\s*|\s*…\s*")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")


def normalize_pauses(text: str) -> str:
    """Collapse '...' / '. . .' / '…' into a single PAUSE_MARK with one space."""
    return _MULTI_SPACE_RE.sub(" ", _ELLIPSIS_RE.sub(f" {PAUSE_MARK} ", text)).strip()


def strip_pauses(text: str, replacement: str = ", ") -> str:
    """Render pause marks for an engine with no SSML — a comma is a real pause."""
    cleaned = text.replace(f" {PAUSE_MARK} ", replacement).replace(PAUSE_MARK, replacement)
    # A pause mark that landed next to punctuation must not become ", ," or " ,".
    cleaned = re.sub(r"\s+,", ",", cleaned)
    cleaned = re.sub(r",\s*([,.।?!])", r"\1", cleaned)
    return _MULTI_SPACE_RE.sub(" ", cleaned).strip()


def to_ssml(text: str, pause_ms: int = 350, comma_ms: int = 0) -> str:
    """Wrap text as SSML, turning pause marks into explicit breaks.

    `comma_ms` adds a small extra beat after commas. Engines already pause
    there; a little more is what makes a read sound spoken rather than
    recited, but too much sounds hesitant, so it is off unless asked for.
    """
    escaped = (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    escaped = escaped.replace(
        PAUSE_MARK, f'<break time="{max(int(pause_ms), 0)}ms"/>'
    )
    if comma_ms > 0:
        escaped = escaped.replace(",", f',<break time="{int(comma_ms)}ms"/>')
    return f"<speak>{escaped}</speak>"


def split_sentences(text: str) -> list[str]:
    """Split into sentences for streaming synthesis (handles Devanagari danda)."""
    separators = ["। ", ". ", "? ", "! ", "।\n", ".\n", "।"]
    sentences: list[str] = []
    current = ""
    i = 0
    while i < len(text):
        matched = False
        for sep in separators:
            if text[i:i + len(sep)] == sep:
                current += sep
                if current.strip():
                    sentences.append(current.strip())
                current = ""
                i += len(sep)
                matched = True
                break
        if not matched:
            current += text[i]
            i += 1
    if current.strip():
        sentences.append(current.strip())
    return sentences


def split_text(text: str, max_chars: int) -> list[str]:
    """Sentence-aligned chunks under a provider's per-request character limit."""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    current = ""
    for sentence in split_sentences(text):
        if len(current) + len(sentence) + 1 > max_chars and current:
            chunks.append(current.strip())
            current = ""
        current += (" " + sentence) if current else sentence
    if current.strip():
        chunks.append(current.strip())
    return chunks
