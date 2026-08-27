"""Google Cloud Text-to-Speech v1 over REST, authenticated with an API key.

Requests LINEAR16 at 8kHz so the returned WAV converts straight to mu-law with
no MP3 decode step.
"""

import base64
import logging
from typing import AsyncGenerator

import httpx

from app.audio import wav_to_mulaw8k
from app.providers.base import TTSProvider, split_sentences

logger = logging.getLogger(__name__)

GOOGLE_TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"

VOICES = [
    {"id": "hi-IN-Wavenet-A", "name": "Hindi Wavenet A (female)", "language": "hi-IN"},
    {"id": "hi-IN-Wavenet-B", "name": "Hindi Wavenet B (male)", "language": "hi-IN"},
    {"id": "hi-IN-Neural2-A", "name": "Hindi Neural2 A (female)", "language": "hi-IN"},
    {"id": "en-IN-Wavenet-A", "name": "English (India) Wavenet A", "language": "en-IN"},
    {"id": "en-IN-Neural2-A", "name": "English (India) Neural2 A", "language": "en-IN"},
    {"id": "ta-IN-Wavenet-A", "name": "Tamil Wavenet A", "language": "ta-IN"},
    {"id": "bn-IN-Wavenet-A", "name": "Bengali Wavenet A", "language": "bn-IN"},
    {"id": "mr-IN-Wavenet-A", "name": "Marathi Wavenet A", "language": "mr-IN"},
    {"id": "gu-IN-Wavenet-A", "name": "Gujarati Wavenet A", "language": "gu-IN"},
    {"id": "kn-IN-Wavenet-A", "name": "Kannada Wavenet A", "language": "kn-IN"},
    {"id": "ml-IN-Wavenet-A", "name": "Malayalam Wavenet A", "language": "ml-IN"},
    {"id": "te-IN-Standard-A", "name": "Telugu Standard A", "language": "te-IN"},
]


class GoogleTTS(TTSProvider):
    name = "google"

    def __init__(
        self,
        api_key: str,
        voice: str = "hi-IN-Wavenet-A",
        fallback_language: str = "hi-IN",
        speaking_rate: float = 1.0,
    ) -> None:
        self._api_key = api_key
        self._voice = voice
        self._fallback_language = fallback_language
        self._speaking_rate = speaking_rate
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))

    async def list_voices(self) -> list[dict]:
        return VOICES

    def _language_for_voice(self, language: str | None) -> str:
        """Google requires the voice name and languageCode to agree."""
        parts = self._voice.split("-")
        if len(parts) >= 2:
            return f"{parts[0]}-{parts[1]}"
        return language or self._fallback_language

    async def synthesize_streaming(
        self, text: str, language: str | None = None
    ) -> AsyncGenerator[bytes, None]:
        text = text.strip()
        if not text:
            return

        lang = self._language_for_voice(language)

        for sentence in split_sentences(text):
            audio = await self._call_api(sentence, lang)
            if audio:
                yield audio

    async def _call_api(self, text: str, language: str) -> bytes | None:
        payload = {
            "input": {"text": text},
            "voice": {"languageCode": language, "name": self._voice},
            "audioConfig": {
                "audioEncoding": "LINEAR16",
                "sampleRateHertz": 8000,
                "speakingRate": self._speaking_rate,
            },
        }
        try:
            response = await self._client.post(
                GOOGLE_TTS_URL, params={"key": self._api_key}, json=payload
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Google's message names the real problem (wrong key type, API not
            # enabled, quota); the traceback alone never does.
            logger.error(
                "Google TTS %s for %s...: %s",
                exc.response.status_code,
                text[:50],
                exc.response.text[:500],
            )
            return None
        except httpx.HTTPError:
            logger.exception("Google TTS failed for: %s...", text[:50])
            return None

        encoded = response.json().get("audioContent")
        if not encoded:
            return None
        return wav_to_mulaw8k(base64.b64decode(encoded))

    async def close(self) -> None:
        await self._client.aclose()
