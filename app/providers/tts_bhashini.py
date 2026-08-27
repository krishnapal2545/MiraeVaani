"""Bhashini AI TTS — REST synthesis with sentence-level streaming.

Two changes from v5:
- The voice is passed in from the agent row. v5 hardcoded exactly two voices and
  collapsed every non-English language to `hi-f3`, which made a Tamil or Bengali
  agent impossible.
- MP3 decoding runs in a worker thread. `AudioSegment.from_mp3` forks ffmpeg
  synchronously, so calling it directly inside async code blocks the event loop
  for every other concurrent call.
"""

import asyncio
import logging
from typing import AsyncGenerator

import httpx

from app.audio import mp3_to_mulaw8k
from app.providers.base import TTSProvider, split_sentences

logger = logging.getLogger(__name__)

# BCP-47 -> Bhashini language name
LANGUAGE_MAP = {
    "hi-IN": "Hindi", "en-IN": "English", "bn-IN": "Bengali", "ta-IN": "Tamil",
    "te-IN": "Telugu", "gu-IN": "Gujarati", "kn-IN": "Kannada",
    "ml-IN": "Malayalam", "mr-IN": "Marathi", "od-IN": "Odia",
    "pa-IN": "Punjabi", "ur-IN": "Urdu", "as-IN": "Assamese",
}

VOICES = [
    {"id": "hi-f3", "name": "Hindi Female 3", "language": "hi-IN"},
    {"id": "hi-m1", "name": "Hindi Male 1", "language": "hi-IN"},
    {"id": "Female3", "name": "English Female 3", "language": "en-IN"},
    {"id": "Male1", "name": "English Male 1", "language": "en-IN"},
    {"id": "ta-f1", "name": "Tamil Female 1", "language": "ta-IN"},
    {"id": "bn-f1", "name": "Bengali Female 1", "language": "bn-IN"},
    {"id": "mr-f1", "name": "Marathi Female 1", "language": "mr-IN"},
    {"id": "te-f1", "name": "Telugu Female 1", "language": "te-IN"},
]


class BhashiniTTS(TTSProvider):
    name = "bhashini"

    def __init__(
        self,
        api_key: str,
        voice: str = "hi-f3",
        style: str = "Neutral",
        base_url: str = "https://tts.bhashini.ai",
        fallback_language: str = "hi-IN",
    ) -> None:
        self._voice = voice
        self._style = style
        self._base_url = base_url.rstrip("/")
        self._fallback_language = fallback_language
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"X-API-KEY": api_key},
        )

    async def list_voices(self) -> list[dict]:
        return VOICES

    async def synthesize_streaming(
        self, text: str, language: str | None = None
    ) -> AsyncGenerator[bytes, None]:
        text = text.strip()
        if not text:
            return

        lang_name = LANGUAGE_MAP.get(language or self._fallback_language, "Hindi")

        for sentence in split_sentences(text):
            mp3_bytes = await self._call_api(sentence, lang_name)
            if mp3_bytes:
                # ffmpeg fork off the event loop
                yield await asyncio.to_thread(mp3_to_mulaw8k, mp3_bytes)

    async def _call_api(self, text: str, language: str) -> bytes | None:
        payload = {
            "text": text,
            "language": language,
            "voiceName": self._voice,
            "voiceStyle": self._style,
        }
        try:
            response = await self._client.post(
                f"{self._base_url}/v2/synthesize", json=payload
            )
            response.raise_for_status()
            return response.content
        except httpx.HTTPError:
            logger.exception("Bhashini TTS failed for: %s...", text[:50])
            return None

    async def close(self) -> None:
        await self._client.aclose()
