"""Google Cloud Speech-to-Text v1 over REST, authenticated with an API key.

Using the REST endpoint with `?key=` rather than the client library keeps the
credential a single string the user can paste into the UI — a service-account
JSON file would not fit that model.

Note Google has no true "detect anything" mode: it needs one primary language
plus up to three alternates, so auto-detection here is narrower than Sarvam's.
"""

import base64
import logging

import httpx

from app.audio import wav_bytes_to_pcm
from app.providers.base import STTProvider

logger = logging.getLogger(__name__)

GOOGLE_STT_URL = "https://speech.googleapis.com/v1/speech:recognize"

# Consulted when the agent is in auto mode: primary + up to 3 alternates.
DEFAULT_ALTERNATES = ["en-IN", "mr-IN", "ta-IN"]


class GoogleSTT(STTProvider):
    name = "google"

    def __init__(self, api_key: str, model: str = "latest_short") -> None:
        self._api_key = api_key
        self._model = model
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))

    async def transcribe(
        self, wav16k: bytes, language: str | None = None
    ) -> tuple[str, str | None]:
        pcm, rate = wav_bytes_to_pcm(wav16k)
        primary = language or "hi-IN"
        alternates = [c for c in DEFAULT_ALTERNATES if c != primary][:3]

        payload = {
            "config": {
                "encoding": "LINEAR16",
                "sampleRateHertz": rate,
                "languageCode": primary,
                "alternativeLanguageCodes": alternates,
                "enableAutomaticPunctuation": True,
                "model": self._model,
            },
            "audio": {"content": base64.b64encode(pcm).decode("ascii")},
        }

        try:
            response = await self._client.post(
                GOOGLE_STT_URL, params={"key": self._api_key}, json=payload
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Google STT %s: %s", exc.response.status_code, exc.response.text[:500]
            )
            return "", None
        except httpx.HTTPError:
            logger.exception("Google STT request failed")
            return "", None

        results = response.json().get("results") or []
        if not results:
            return "", None

        first = results[0]
        transcript = (first.get("alternatives") or [{}])[0].get("transcript", "").strip()
        detected = first.get("languageCode") or primary
        logger.info("STT [google/%s]: %s", detected, transcript)
        return transcript, detected

    async def close(self) -> None:
        await self._client.aclose()
