"""Sarvam AI Saarika speech-to-text with automatic Indian language detection."""

import logging

import httpx

from app.providers.base import STTProvider

logger = logging.getLogger(__name__)

SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"


class SarvamSTT(STTProvider):
    """Transcribes a single utterance (16kHz WAV) via Sarvam's REST STT API.

    `language_code="unknown"` enables per-utterance auto language detection
    across 22 Indian languages + English, including code-mixed speech.
    """

    name = "sarvam"

    def __init__(self, api_key: str, model: str = "saarika:v2.5") -> None:
        self._model = model
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"api-subscription-key": api_key},
        )

    async def transcribe(
        self, wav16k: bytes, language: str | None = None
    ) -> tuple[str, str | None]:
        files = {"file": ("utterance.wav", wav16k, "audio/wav")}
        data = {"model": self._model, "language_code": language or "unknown"}

        try:
            response = await self._client.post(SARVAM_STT_URL, files=files, data=data)
            response.raise_for_status()
        except httpx.HTTPError:
            logger.exception("Sarvam STT request failed")
            return "", None

        body = response.json()
        transcript = (body.get("transcript") or "").strip()
        detected = body.get("language_code")
        logger.info("STT [sarvam/%s]: %s", detected, transcript)
        return transcript, detected

    async def close(self) -> None:
        await self._client.aclose()
