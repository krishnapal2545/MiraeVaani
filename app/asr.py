"""Sarvam AI Saarika speech-to-text with automatic Indian language detection."""

import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"


class SarvamSTT:
    """Transcribes a single utterance (16kHz WAV) via Sarvam's REST STT API.

    `language_code="unknown"` enables per-utterance auto language detection
    across 22 Indian languages + English, including code-mixed speech.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._model = settings.SARVAM_STT_MODEL
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"api-subscription-key": settings.SARVAM_API_KEY},
        )

    async def transcribe(self, wav_bytes: bytes) -> tuple[str, str | None]:
        """Returns (transcript, detected_language_code e.g. 'ta-IN')."""
        files = {"file": ("utterance.wav", wav_bytes, "audio/wav")}
        data = {"model": self._model, "language_code": "unknown"}

        try:
            response = await self._client.post(SARVAM_STT_URL, files=files, data=data)
            response.raise_for_status()
        except httpx.HTTPError:
            logger.exception("Sarvam STT request failed")
            return "", None

        body = response.json()
        transcript = (body.get("transcript") or "").strip()
        language_code = body.get("language_code")
        logger.info("STT [%s]: %s", language_code, transcript)
        return transcript, language_code

    async def close(self) -> None:
        await self._client.aclose()
