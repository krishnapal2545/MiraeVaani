"""Provider interfaces.

Two rules make UI-supplied credentials work, and both are load-bearing:

1. Every adapter takes its API key as a constructor argument. Nothing under
   `providers/` may import `get_settings()`.
2. Message history is OpenAI-shaped (`[{"role", "content"}]`) everywhere. Groq
   consumes it natively; the Gemini adapter translates. Conversation state and
   the tool loop live in `dialog.py`, so providers stay stateless.
"""

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
