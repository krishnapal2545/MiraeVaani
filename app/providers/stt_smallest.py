"""Smallest.ai Pulse speech-to-text.

Pulse takes the audio as **raw bytes** in the request body with `model` and
`language` as query parameters — not as the multipart upload Sarvam expects.
Sending a form is a 400 on every single request, which in a call log looks
exactly like a caller who never said anything.

The language list is the second trap, and it is the one worth remembering when
picking providers: of the languages this app offers, Pulse's pre-recorded models
transcribe Hindi and English and nothing else. Every other language has to go to
the `multi-asian` aggregator rather than to a code Pulse will reject. Lightning
TTS, from the same vendor, speaks all eleven — so a Smallest-only agent set to
Tamil will talk fluently and never understand a word.
"""

import logging

import httpx

from app.providers.base import STTProvider

logger = logging.getLogger(__name__)

SMALLEST_STT_URL = "https://api.smallest.ai/waves/v1/stt/"

# Pulse takes 2-letter ISO 639-1 codes. These are the only two of this app's
# languages that can be pinned; `multi-asian` covers the rest.
PINNABLE = {"hi", "en"}
AUTO_LANGUAGE = "multi-asian"


class SmallestSTT(STTProvider):
    name = "smallest"

    def __init__(self, api_key: str, model: str = "pulse") -> None:
        self._model = model
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/octet-stream",
            },
        )

    async def transcribe(
        self, wav16k: bytes, language: str | None = None
    ) -> tuple[str, str | None]:
        code = (language or "").split("-")[0].lower()
        params = {
            "model": self._model,
            "language": code if code in PINNABLE else AUTO_LANGUAGE,
        }

        try:
            response = await self._client.post(
                SMALLEST_STT_URL, params=params, content=wav16k
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # The status code on its own says nothing useful; Pulse puts the
            # actual complaint in the body. Logging only the traceback is the
            # difference between a one-line fix and a documentation hunt.
            logger.error(
                "Smallest STT %s: %s",
                exc.response.status_code,
                exc.response.text[:300],
            )
            return "", None
        except httpx.HTTPError:
            logger.exception("Smallest STT request failed")
            return "", None

        body = response.json()
        if not isinstance(body, dict):
            return "", None

        transcript = (body.get("transcription") or body.get("text") or "").strip()
        # Pulse returns no detected-language field, so auto mode gets no vote
        # from this provider and the call stays on the language it is already
        # speaking. Returning a guess here would be worse than returning none.
        logger.info("STT [smallest]: %s", transcript)
        return transcript, None

    async def close(self) -> None:
        await self._client.aclose()
