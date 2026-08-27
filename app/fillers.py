"""Smart conversational fillers.

The silence between a caller finishing their sentence and the agent starting to
speak is `STT (0.3-0.8s) + LLM (0.4-0.9s) + TTS TTFB (0.3-0.7s)` — roughly one
to two and a half seconds. Callers read that as a dropped line and start talking
again, which trips barge-in and makes the turn worse.

The filler covers that gap. Two constraints shape the design:

- You cannot classify anything until STT returns, but once it does the whole
  LLM + TTS window is still ahead. So the filler is chosen *after* STT and raced
  against the real response.
- A filler that needs its own TTS round-trip is not a filler. Every clip is
  synthesized once at session start and cached as mu-law bytes.
"""

import asyncio
import logging
import random
import re

from app.providers.base import TTSProvider

logger = logging.getLogger(__name__)

# Category -> language -> candidate phrases.
FILLER_BANK: dict[str, dict[str, list[str]]] = {
    "lookup": {
        "hi-IN": ["एक सेकंड, मैं चेक कर रही हूँ।", "जी, अभी देखती हूँ।"],
        "en-IN": ["One second, let me check that.", "Sure, just looking that up."],
    },
    "thinking": {
        "hi-IN": ["जी, एक मिनट।", "हाँ जी।"],
        "en-IN": ["Right, one moment.", "Okay."],
    },
    "empathy": {
        "hi-IN": ["जी, मैं समझ सकती हूँ।", "जी, बिलकुल समझती हूँ।"],
        "en-IN": ["I completely understand.", "I see, I understand."],
    },
    "confirm": {
        "hi-IN": ["जी बिलकुल।", "ठीक है जी।"],
        "en-IN": ["Absolutely.", "Understood."],
    },
}

_LOOKUP_PATTERNS = re.compile(
    r"kitna|kitne|kitni|balance|amount|paisa|paise|rupay|rupaye|account|"
    r"statement|shortfall|margin|date|kab|kaunsa|status|detail|"
    r"how much|how many|when|what is my|my account|my balance",
    re.IGNORECASE,
)
_EMPATHY_PATTERNS = re.compile(
    r"pareshan|problem|dikkat|galat|shikayat|naraz|gussa|bekar|kharab|nahi hua|"
    r"complaint|issue|wrong|angry|upset|frustrat|not working|worst|bad",
    re.IGNORECASE,
)
_CONFIRM_PATTERNS = re.compile(
    r"^(haan|han|ha|ji|ok|okay|theek|thik|sahi|yes|yeah|sure|done|kar dunga|"
    r"kar dungi|karta hoon|karti hoon)\b",
    re.IGNORECASE,
)
_QUESTION_PATTERNS = re.compile(
    r"kaise|kyun|kyu|kya|matlab|samajh|batao|bataiye|"
    r"how do|why|what|explain|tell me|can you",
    re.IGNORECASE,
)


def classify(transcript: str) -> str | None:
    """Pick a filler category from the transcript. Keywords only — deliberately.

    Calling an LLM to choose a filler would reintroduce the very latency the
    filler exists to hide.
    """
    text = (transcript or "").strip()
    if not text:
        return None

    # Very short replies ("haan", "ok") get answered fast anyway; a filler in
    # front of them sounds robotic.
    if len(text.split()) <= 2:
        return None

    if _EMPATHY_PATTERNS.search(text):
        return "empathy"
    if _LOOKUP_PATTERNS.search(text):
        return "lookup"
    if _CONFIRM_PATTERNS.search(text):
        return "confirm"
    if _QUESTION_PATTERNS.search(text) or text.endswith("?"):
        return "thinking"
    return None


class FillerBank:
    """Pre-synthesized filler audio for one call, keyed by (category, language)."""

    def __init__(self, tts: TTSProvider, languages: list[str]) -> None:
        self._tts = tts
        self._languages = languages or ["hi-IN"]
        self._clips: dict[tuple[str, str], list[bytes]] = {}
        self._ready = asyncio.Event()

    async def warm(self) -> None:
        """Synthesize every clip once, in the background while the greeting plays.

        Fired concurrently: this is a dozen-odd short requests, and doing them
        serially would take seconds and risk the bank not being ready by the time
        the caller's first question needs it.
        """

        async def render(category: str, language: str, phrase: str):
            try:
                audio = await self._tts.synthesize(phrase, language)
                return category, language, audio
            except Exception:
                logger.warning("Filler synthesis failed: %s [%s]", phrase, language)
                return category, language, b""

        jobs = [
            render(category, language, phrase)
            for category, by_language in FILLER_BANK.items()
            for language in self._languages
            for phrase in by_language.get(language, [])
        ]
        for category, language, audio in await asyncio.gather(*jobs):
            if audio:
                self._clips.setdefault((category, language), []).append(audio)

        self._ready.set()
        logger.info("Filler bank warmed: %d entries", len(self._clips))

    def get(self, category: str, language: str | None) -> bytes | None:
        if not self._ready.is_set():
            return None
        lang = language or self._languages[0]
        clips = self._clips.get((category, lang))
        if not clips:
            # Fall back to the agent's primary language rather than staying silent.
            clips = self._clips.get((category, self._languages[0]))
        return random.choice(clips) if clips else None


class FillerController:
    """Decides whether a filler plays this turn, and which one."""

    def __init__(self, bank: FillerBank, *, enabled: bool = True) -> None:
        self._bank = bank
        self._enabled = enabled
        self._last_turn_played = False
        self._turns = 0
        self._played = 0

    def choose(self, transcript: str, language: str | None) -> tuple[str, bytes] | None:
        """Returns (category, mulaw_audio) or None if no filler should play."""
        self._turns += 1

        if not self._enabled:
            return None

        # Never two turns in a row — that is what makes a bot sound like a bot.
        if self._last_turn_played:
            self._last_turn_played = False
            return None

        # Cap at roughly a third of turns.
        if self._turns > 3 and self._played / self._turns > 0.34:
            return None

        category = classify(transcript)
        if not category:
            return None

        audio = self._bank.get(category, language)
        if not audio:
            return None

        self._last_turn_played = True
        self._played += 1
        return category, audio
