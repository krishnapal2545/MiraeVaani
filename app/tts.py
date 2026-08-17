"""Bhashini AI TTS — low-latency REST synthesis with sentence-level streaming.

Returns Twilio-ready 8kHz mu-law audio. Splits text into sentences and
synthesizes each independently so the first sentence can start playing
before the full response is ready (low time-to-first-byte).
"""

import logging
from typing import AsyncGenerator

import httpx

from app.audio import mp3_to_mulaw8k
from app.config import get_settings

logger = logging.getLogger(__name__)

# BCP-47 -> Bhashini language name
LANGUAGE_MAP = {
    "hi-IN": "Hindi",
    "en-IN": "English",
    "bn-IN": "Bengali",
    "ta-IN": "Tamil",
    "te-IN": "Telugu",
    "gu-IN": "Gujarati",
    "kn-IN": "Kannada",
    "ml-IN": "Malayalam",
    "mr-IN": "Marathi",
    "od-IN": "Odia",
    "pa-IN": "Punjabi",
    "ur-IN": "Urdu",
    "as-IN": "Assamese",
}

MAX_CHARS_PER_REQUEST = 500


class BhashiniTTS:
    """Synthesizes agent replies via Bhashini REST TTS, matching caller's language."""

    def __init__(self) -> None:
        settings = get_settings()
        self._voice_en = settings.BHASHINI_TTS_VOICE_EN
        self._voice_hi = settings.BHASHINI_TTS_VOICE_HI
        self._style = settings.BHASHINI_TTS_STYLE
        self._base_url = settings.BHASHINI_TTS_BASE_URL.rstrip("/")
        self._fallback_language = settings.DEFAULT_LANGUAGE
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"X-API-KEY": settings.BHASHINI_API_KEY},
        )

    def _resolve_voice(self, language_code: str | None) -> tuple[str, str]:
        """Returns (bhashini_language_name, voice_name) for the given BCP-47 code."""
        lang = language_code or self._fallback_language
        bhashini_lang = LANGUAGE_MAP.get(lang, "Hindi")
        if bhashini_lang == "English":
            return bhashini_lang, self._voice_en
        return bhashini_lang, self._voice_hi

    async def synthesize(self, text: str, language_code: str | None) -> bytes:
        """Full synthesis: returns complete 8kHz mu-law bytes for the text."""
        text = text.strip()
        if not text:
            return b""

        bhashini_lang, voice = self._resolve_voice(language_code)
        audio = bytearray()

        for chunk in _split_text(text, MAX_CHARS_PER_REQUEST):
            mp3_bytes = await self._call_api(chunk, bhashini_lang, voice)
            if mp3_bytes:
                audio.extend(mp3_to_mulaw8k(mp3_bytes))

        logger.info("TTS [%s/%s]: %d chars -> %.1fs audio", bhashini_lang, voice, len(text), len(audio) / 8000)
        return bytes(audio)

    async def synthesize_streaming(self, text: str, language_code: str | None) -> AsyncGenerator[bytes, None]:
        """Yields mulaw audio sentence-by-sentence for low TTFB streaming to Twilio."""
        text = text.strip()
        if not text:
            return

        bhashini_lang, voice = self._resolve_voice(language_code)

        for chunk in _split_sentences(text):
            mp3_bytes = await self._call_api(chunk, bhashini_lang, voice)
            if mp3_bytes:
                yield mp3_to_mulaw8k(mp3_bytes)

    async def _call_api(self, text: str, language: str, voice: str) -> bytes | None:
        """Call Bhashini REST TTS and return raw MP3 bytes."""
        payload = {
            "text": text,
            "language": language,
            "voiceName": voice,
            "voiceStyle": self._style,
        }
        try:
            response = await self._client.post(
                f"{self._base_url}/v2/synthesize", json=payload
            )
            response.raise_for_status()
            return response.content
        except httpx.HTTPError:
            logger.exception("Bhashini TTS request failed for: %s...", text[:50])
            return None

    async def close(self) -> None:
        await self._client.aclose()


def _split_sentences(text: str) -> list[str]:
    """Split text into individual sentences for streaming synthesis."""
    separators = ["। ", ". ", "? ", "! ", "।\n", ".\n"]
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


def _split_text(text: str, max_chars: int) -> list[str]:
    """Split long text into sentence-aligned chunks under the API limit."""
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current = ""
    for sentence in _split_sentences(text):
        if len(current) + len(sentence) + 1 > max_chars and current:
            chunks.append(current.strip())
            current = ""
        current += " " + sentence if current else sentence
    if current.strip():
        chunks.append(current.strip())
    return chunks
