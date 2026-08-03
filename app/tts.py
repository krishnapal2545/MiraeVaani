"""Sarvam AI Bulbul text-to-speech, returning Twilio-ready 8kHz mu-law audio."""

import base64
import logging

import httpx

from app.audio import wav_to_mulaw8k
from app.config import get_settings

logger = logging.getLogger(__name__)

SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"

# Languages supported by Bulbul TTS (BCP-47 codes).
TTS_SUPPORTED_LANGUAGES = {
    "hi-IN", "bn-IN", "ta-IN", "te-IN", "gu-IN", "kn-IN",
    "ml-IN", "mr-IN", "od-IN", "pa-IN", "en-IN",
}

MAX_CHARS_PER_REQUEST = 1500


class SarvamTTS:
    """Synthesizes agent replies with Bulbul, matching the caller's language."""

    def __init__(self) -> None:
        settings = get_settings()
        self._model = settings.SARVAM_TTS_MODEL
        self._speaker = settings.SARVAM_TTS_SPEAKER
        self._fallback_language = settings.DEFAULT_LANGUAGE
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"api-subscription-key": settings.SARVAM_API_KEY},
        )

    async def synthesize(self, text: str, language_code: str | None) -> bytes:
        """Returns 8kHz mu-law bytes ready for Twilio Media Streams."""
        text = text.strip()
        if not text:
            return b""

        language = language_code if language_code in TTS_SUPPORTED_LANGUAGES else self._fallback_language
        if language not in TTS_SUPPORTED_LANGUAGES:
            language = "hi-IN"

        audio = bytearray()
        for chunk in _split_text(text, MAX_CHARS_PER_REQUEST):
            payload = {
                "text": chunk,
                "target_language_code": language,
                "speaker": self._speaker,
                "model": self._model,
                "speech_sample_rate": 8000,
                "enable_preprocessing": True,
            }
            try:
                response = await self._client.post(SARVAM_TTS_URL, json=payload)
                response.raise_for_status()
            except httpx.HTTPError:
                logger.exception("Sarvam TTS request failed")
                continue

            for encoded in response.json().get("audios", []):
                wav_bytes = base64.b64decode(encoded)
                audio.extend(wav_to_mulaw8k(wav_bytes))

        logger.info("TTS [%s]: %d chars -> %.1fs audio", language, len(text), len(audio) / 8000)
        return bytes(audio)

    async def close(self) -> None:
        await self._client.aclose()


def _split_text(text: str, max_chars: int) -> list[str]:
    """Split long text into sentence-aligned chunks under the API limit."""
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current = ""
    for sentence in text.replace("।", "।|").replace(". ", ". |").split("|"):
        if len(current) + len(sentence) > max_chars and current:
            chunks.append(current.strip())
            current = ""
        current += sentence
    if current.strip():
        chunks.append(current.strip())
    return chunks
