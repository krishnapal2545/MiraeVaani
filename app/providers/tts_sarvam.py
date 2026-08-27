"""Sarvam AI Bulbul text-to-speech (ported from v4, with sentence streaming added).

Sarvam is asked for 8kHz WAV directly, so its output maps straight to mu-law
with no MP3 decode — which makes it the lowest-latency option in the catalog.
"""

import base64
import logging
from typing import AsyncGenerator

import httpx

from app.audio import wav_to_mulaw8k
from app.providers.base import TTSProvider, split_sentences

logger = logging.getLogger(__name__)

SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"

SUPPORTED_LANGUAGES = {
    "hi-IN", "bn-IN", "ta-IN", "te-IN", "gu-IN", "kn-IN",
    "ml-IN", "mr-IN", "od-IN", "pa-IN", "en-IN",
}

VOICES = [
    {"id": s, "name": s.title(), "language": "multi"}
    for s in ["anushka", "abhilash", "manisha", "vidya", "arya", "karun", "hitesh"]
]


class SarvamTTS(TTSProvider):
    name = "sarvam"

    def __init__(
        self,
        api_key: str,
        voice: str = "anushka",
        model: str = "bulbul:v2",
        fallback_language: str = "hi-IN",
    ) -> None:
        self._voice = voice
        self._model = model
        self._fallback_language = fallback_language
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"api-subscription-key": api_key},
        )

    async def list_voices(self) -> list[dict]:
        return VOICES

    async def synthesize_streaming(
        self, text: str, language: str | None = None
    ) -> AsyncGenerator[bytes, None]:
        text = text.strip()
        if not text:
            return

        lang = language if language in SUPPORTED_LANGUAGES else self._fallback_language
        if lang not in SUPPORTED_LANGUAGES:
            lang = "hi-IN"

        for sentence in split_sentences(text):
            audio = await self._call_api(sentence, lang)
            if audio:
                yield audio

    async def _call_api(self, text: str, language: str) -> bytes | None:
        payload = {
            "text": text,
            "target_language_code": language,
            "speaker": self._voice,
            "model": self._model,
            "speech_sample_rate": 8000,
            "enable_preprocessing": True,
        }
        try:
            response = await self._client.post(SARVAM_TTS_URL, json=payload)
            response.raise_for_status()
        except httpx.HTTPError:
            logger.exception("Sarvam TTS failed for: %s...", text[:50])
            return None

        audio = bytearray()
        for encoded in response.json().get("audios", []):
            audio.extend(wav_to_mulaw8k(base64.b64decode(encoded)))
        return bytes(audio)

    async def close(self) -> None:
        await self._client.aclose()
