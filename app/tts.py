"""
TTS — Text-to-Speech via remote XTTS v2 / IndicTTS service on Colab GPU.

Sends text + language to the Colab TTS endpoint and receives
base64-encoded mulaw 8kHz audio ready to stream to Twilio.

For languages not supported by XTTS v2 natively, the Colab service
falls back to gTTS (AI4Bharat IndicTTS in v2 of the Colab notebook).
"""

import base64
import logging
from typing import AsyncIterator

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# Languages natively supported by XTTS v2 (high quality)
XTTS_NATIVE_LANGUAGES = {
    "en", "es", "fr", "de", "it", "pt", "pl", "tr",
    "ru", "nl", "cs", "ar", "zh-cn", "ja", "hu", "ko",
}

# Indian languages that fall back to IndicTTS/gTTS endpoint
INDIC_LANGUAGES = {"hi", "ta", "te", "mr", "bn", "gu", "kn", "ml", "pa", "ur", "or", "as"}
# Chunk size for Twilio: 640 bytes = 80ms of 8kHz mulaw audio
TWILIO_CHUNK_SIZE = 640


async def synthesize_speech(
    text: str,
    language: str = "hi",
) -> AsyncIterator[str]:
    """
    Convert text to Twilio-ready mulaw audio chunks.

    Args:
        text: Text to synthesize.
        language: Language code detected by ASR (e.g. "hi", "ta", "en").

    Yields:
        Base64-encoded mulaw audio chunks (640 bytes each = 80ms).
    """
    settings = get_settings()

    if not settings.TTS_BASE_URL:
        logger.error("TTS_BASE_URL not configured — cannot synthesize speech")
        return

    # Route to the appropriate endpoint based on language support
    if language in INDIC_LANGUAGES:
        endpoint = "/synthesize_regional"
    else:
        endpoint = "/synthesize"
        if language not in XTTS_NATIVE_LANGUAGES:
            language = settings.TTS_DEFAULT_LANGUAGE  # fallback to Hindi

    url = f"{settings.TTS_BASE_URL.rstrip('/')}{endpoint}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                json={
                    "text": text,
                    "language": language,
                    "speaker": settings.TTS_SPEAKER,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        # Colab service returns base64-encoded mulaw audio
        audio_bytes = base64.b64decode(data["audio_base64"])

        # Chunk into 80ms pieces for Twilio media streams
        for i in range(0, len(audio_bytes), TWILIO_CHUNK_SIZE):
            chunk = audio_bytes[i : i + TWILIO_CHUNK_SIZE]
            yield base64.b64encode(chunk).decode("ascii")

        logger.debug(
            "TTS: synthesized %d bytes in language '%s'",
            len(audio_bytes),
            language,
        )

    except httpx.TimeoutException:
        logger.error("TTS: request timed out to %s", url)
    except Exception:
        logger.exception("TTS: synthesis failed")
