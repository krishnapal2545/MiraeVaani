"""Smallest.ai Lightning text-to-speech.

Lightning is the only engine in the catalog that returns 8kHz mu-law directly,
which is exactly the format Twilio's media stream wants — so its audio reaches
the caller with no WAV parse, no MP3 decode and no resample. Sarvam still costs
a `wav_to_mulaw8k()` pass per sentence; this costs nothing.

Synthesis goes through the *live* endpoint over SSE rather than the plain
`get_speech` POST. The plain POST does not return until the whole clip is
rendered, which measured 605-1000ms of dead air for a 54-character line; the
live endpoint delivers its first frame in about 100ms. Note the model id there
is spelled with underscores (`lightning_v3.1`) while the REST URL path uses
hyphens — the two are not interchangeable.

Two limits shape the code below: a request is capped at 250 characters (the
docs put best throughput at 140), and Lightning has no pitch control, so the
agent's pitch setting is deliberately not forwarded.
"""

import base64
import json
import logging
from typing import AsyncGenerator

import httpx

from app.providers.base import (
    TTSProvider,
    normalize_pauses,
    split_sentences,
    split_text,
    strip_pauses,
)

logger = logging.getLogger(__name__)

# Underscores, not hyphens: this is the id the live endpoint's payload takes.
MODEL = "lightning_v3.1"
API_BASE = "https://api.smallest.ai/waves/v1"
LIVE_URL = f"{API_BASE}/tts/live"

# Lightning takes 2-letter ISO 639-1 codes; this app speaks BCP-47 throughout.
# Odia is the one language where the app's Sarvam-flavoured code ("od-IN") does
# not simply truncate to the ISO code.
_ISO_OVERRIDES = {"od": "or"}
SUPPORTED_LANGUAGES = {
    "hi", "en", "ta", "te", "kn", "ml", "mr", "gu", "bn", "pa", "or", "es",
}

# The hard cap is 250 characters. Staying near the documented sweet spot also
# gets the first chunk to the caller sooner, which is what the ear notices.
MAX_CHARS = 140


def _chunks(text: str) -> list[str]:
    """Request-sized pieces, smallest first.

    The first piece alone decides when the caller hears anything at all, so it
    is a single sentence however short. Everything after it is packed up to the
    request limit, because by then audio is already playing and only throughput
    matters.
    """
    sentences = split_sentences(text)
    if len(sentences) <= 1:
        return split_text(text, MAX_CHARS)
    return [sentences[0], *split_text(" ".join(sentences[1:]), MAX_CHARS)]

# Fallback only — /api/catalog replaces this with the account's own list, which
# is where cloned and Pro-tier voices show up. 217 voices ship with the model.
VOICES = [
    {"id": v, "name": v.title(), "language": "multi"}
    for v in ["mishka", "meher", "sophia", "devansh", "kartik", "maithili", "liam", "avery"]
]


def _mulaw_payload(audio: bytes) -> bytes:
    """The bytes Twilio can play, container or not.

    `output_format: ulaw` is already 8kHz mu-law. When the response wraps it in
    a RIFF header the header has to come off, or the caller hears the metadata
    as a burst of noise ahead of the speech.
    """
    if not audio.startswith(b"RIFF"):
        return audio
    marker = audio.find(b"data")
    return audio[marker + 8:] if marker != -1 else b""


class SmallestTTS(TTSProvider):
    name = "smallest"

    def __init__(
        self,
        api_key: str,
        voice: str = "mishka",
        model: str = MODEL,
        fallback_language: str = "hi-IN",
        speaking_rate: float = 1.0,
    ) -> None:
        self._voice = voice
        self._model = model
        self._fallback_language = fallback_language
        # Lightning clamps outside 0.5-2.0.
        self._speed = min(max(float(speaking_rate), 0.5), 2.0)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "text/event-stream",
            },
        )

    async def list_voices(self) -> list[dict]:
        return VOICES

    def _language_code(self, language: str | None) -> str:
        for candidate in (language, self._fallback_language):
            code = (candidate or "").split("-")[0].lower()
            code = _ISO_OVERRIDES.get(code, code)
            if code in SUPPORTED_LANGUAGES:
                return code
        return "hi"

    async def synthesize_streaming(
        self, text: str, language: str | None = None
    ) -> AsyncGenerator[bytes, None]:
        # Lightning has no SSML, so pause marks become commas — which it pauses
        # on, and which never get read aloud as "dot dot dot".
        text = strip_pauses(normalize_pauses(text.strip()))
        if not text:
            return

        lang = self._language_code(language)
        for chunk in _chunks(text):
            async for audio in self._stream_api(chunk, lang):
                yield audio

    async def _stream_api(
        self, text: str, language: str
    ) -> AsyncGenerator[bytes, None]:
        """Yield mu-law frames as the live endpoint produces them.

        Each SSE event carries `{"audio": <base64>, "done": false}`; the final
        one carries `done: true` and no audio.
        """
        payload = {
            "text": text,
            "voice_id": self._voice,
            "model": self._model,
            "language": language,
            "sample_rate": 8000,
            "output_format": "ulaw",
            "speed": self._speed,
        }
        first = True
        try:
            async with self._client.stream(
                "POST", LIVE_URL, json=payload
            ) as response:
                if response.status_code >= 400:
                    # The body has to be pulled in before it can be read at all
                    # on a streamed response, and it is the only thing that says
                    # what the endpoint actually objected to.
                    await response.aread()
                    logger.error(
                        "Smallest TTS %s: %s",
                        response.status_code,
                        response.text[:300],
                    )
                    return

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    frame = json.loads(line[5:].strip())
                    if frame.get("done"):
                        return
                    encoded = frame.get("audio")
                    if not encoded:
                        continue
                    audio = base64.b64decode(encoded)
                    # Only the opening frame could carry a container header.
                    if first:
                        audio = _mulaw_payload(audio)
                        first = False
                    if audio:
                        yield audio
        except (httpx.HTTPError, ValueError):
            logger.exception("Smallest TTS failed for: %s...", text[:50])

    async def close(self) -> None:
        await self._client.aclose()
