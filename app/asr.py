"""
ASR — Speech-to-Text via remote Faster-Whisper service on Colab GPU.

Sends 16kHz PCM audio to the Colab STT endpoint and returns
the transcript along with the detected language.
"""

import io
import logging
import wave

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class ASRClient:
    """Client for the remote Faster-Whisper STT service running on Colab."""

    def __init__(self):
        self.settings = get_settings()

    async def transcribe(self, pcm_16k_bytes: bytes) -> dict:
        """
        Transcribe 16kHz PCM audio.

        Args:
            pcm_16k_bytes: Raw 16kHz 16-bit mono PCM audio bytes.

        Returns:
            dict with keys:
                - transcript (str): Transcribed text.
                - language (str): Detected language code (e.g. "hi", "ta").
                - language_probability (float): Confidence 0.0–1.0.
        """
        if not self.settings.STT_BASE_URL:
            logger.error("STT_BASE_URL not configured — cannot transcribe")
            return {"transcript": "", "language": "hi", "language_probability": 0.0}

        # Wrap raw PCM in a WAV container so Whisper can read it
        wav_bytes = _pcm_to_wav(pcm_16k_bytes, sample_rate=16000)

        url = f"{self.settings.STT_BASE_URL.rstrip('/')}/transcribe"

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    url,
                    files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                    data={
                        "language": (
                            None
                            if self.settings.STT_LANGUAGE == "auto"
                            else self.settings.STT_LANGUAGE
                        )
                    },
                )
                response.raise_for_status()
                result = response.json()

            logger.info(
                "ASR: [%s %.0f%%] %s",
                result.get("language", "?"),
                result.get("language_probability", 0) * 100,
                result.get("transcript", ""),
            )
            return result

        except httpx.TimeoutException:
            logger.error("ASR: request timed out to %s", url)
            return {"transcript": "", "language": "hi", "language_probability": 0.0}
        except Exception:
            logger.exception("ASR: transcription failed")
            return {"transcript": "", "language": "hi", "language_probability": 0.0}


def _pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 16000) -> bytes:
    """Wrap raw 16-bit mono PCM bytes in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)      # 16-bit = 2 bytes
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()
