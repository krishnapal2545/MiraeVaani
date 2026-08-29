"""Google Cloud Text-to-Speech v1 over REST, authenticated with an API key.

Requests LINEAR16 at 8kHz so the returned WAV converts straight to mu-law with
no MP3 decode step.

Two things here are less obvious than they look:

**The voice must match the language being spoken, not the language configured.**
Until now this adapter derived `languageCode` from the *voice name* and pinned
that voice for the whole call, so an agent set to `hi-IN-Wavenet-A` read Marathi
and Punjabi replies with a Hindi voice. Devanagari is shared, so it does not
error — it just mispronounces every word, which is exactly what it sounds like
on the recording. The voice is now chosen per utterance from the language the
agent decided to speak, falling back to the configured voice only when there is
no mapping.

**Pauses come from SSML.** A model told to write "जी बिलकुल... एक मिनट" needs the
ellipsis turned into a real break; sent as plain text it is either ignored or
read aloud. Chirp3-HD voices do not accept SSML or pitch, so those requests are
sent as plain text and the pause mark is rendered as a comma instead.
"""

import base64
import logging
from typing import AsyncGenerator

import httpx

from app.audio import wav_to_mulaw8k
from app.providers.base import (
    TTSProvider,
    normalize_pauses,
    split_sentences,
    strip_pauses,
    to_ssml,
)

logger = logging.getLogger(__name__)

GOOGLE_TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"

VOICES = [
    {"id": "hi-IN-Wavenet-A", "name": "Hindi Wavenet A (female)", "language": "hi-IN"},
    {"id": "hi-IN-Wavenet-B", "name": "Hindi Wavenet B (male)", "language": "hi-IN"},
    {"id": "hi-IN-Neural2-A", "name": "Hindi Neural2 A (female)", "language": "hi-IN"},
    {"id": "hi-IN-Neural2-D", "name": "Hindi Neural2 D (female, warmer)", "language": "hi-IN"},
    {"id": "hi-IN-Chirp3-HD-Achernar", "name": "Hindi Chirp3 HD (female, most natural)", "language": "hi-IN"},
    {"id": "hi-IN-Chirp3-HD-Charon", "name": "Hindi Chirp3 HD (male, most natural)", "language": "hi-IN"},
    {"id": "en-IN-Wavenet-A", "name": "English (India) Wavenet A", "language": "en-IN"},
    {"id": "en-IN-Neural2-A", "name": "English (India) Neural2 A", "language": "en-IN"},
    {"id": "en-IN-Chirp3-HD-Achernar", "name": "English (India) Chirp3 HD (female)", "language": "en-IN"},
    {"id": "ta-IN-Wavenet-A", "name": "Tamil Wavenet A", "language": "ta-IN"},
    {"id": "bn-IN-Wavenet-A", "name": "Bengali Wavenet A", "language": "bn-IN"},
    {"id": "mr-IN-Wavenet-A", "name": "Marathi Wavenet A (female)", "language": "mr-IN"},
    {"id": "mr-IN-Wavenet-B", "name": "Marathi Wavenet B (male)", "language": "mr-IN"},
    {"id": "mr-IN-Chirp3-HD-Achernar", "name": "Marathi Chirp3 HD (female)", "language": "mr-IN"},
    {"id": "gu-IN-Wavenet-A", "name": "Gujarati Wavenet A", "language": "gu-IN"},
    {"id": "kn-IN-Wavenet-A", "name": "Kannada Wavenet A", "language": "kn-IN"},
    {"id": "ml-IN-Wavenet-A", "name": "Malayalam Wavenet A", "language": "ml-IN"},
    {"id": "pa-IN-Wavenet-A", "name": "Punjabi Wavenet A", "language": "pa-IN"},
    {"id": "te-IN-Standard-A", "name": "Telugu Standard A", "language": "te-IN"},
]

# Voice used when the agent speaks a language its configured voice cannot.
# Deliberately Wavenet/Standard rather than Chirp3: these exist in every
# project without extra enablement, and a fallback that 400s is not a fallback.
AUTO_VOICE: dict[str, str] = {
    "hi-IN": "hi-IN-Wavenet-A",
    "en-IN": "en-IN-Wavenet-A",
    "mr-IN": "mr-IN-Wavenet-A",
    "ta-IN": "ta-IN-Wavenet-A",
    "te-IN": "te-IN-Standard-A",
    "bn-IN": "bn-IN-Wavenet-A",
    "gu-IN": "gu-IN-Wavenet-A",
    "kn-IN": "kn-IN-Wavenet-A",
    "ml-IN": "ml-IN-Wavenet-A",
    "pa-IN": "pa-IN-Wavenet-A",
}


def _locale_of(voice: str) -> str:
    parts = voice.split("-")
    return f"{parts[0]}-{parts[1]}" if len(parts) >= 2 else ""


class GoogleTTS(TTSProvider):
    name = "google"

    def __init__(
        self,
        api_key: str,
        voice: str = "hi-IN-Wavenet-A",
        fallback_language: str = "hi-IN",
        speaking_rate: float = 1.0,
        pitch: float = 0.0,
        pause_ms: int = 350,
    ) -> None:
        self._api_key = api_key
        self._voice = voice
        self._fallback_language = fallback_language
        self._speaking_rate = speaking_rate
        self._pitch = pitch
        self._pause_ms = pause_ms
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        # Voices that have already failed once; never retried for the rest of
        # the call, so one unavailable Chirp3 voice costs a single 400 and not
        # one per sentence.
        self._dead: set[str] = set()

    async def list_voices(self) -> list[dict]:
        return VOICES

    def _voice_for(self, language: str | None) -> tuple[str, str]:
        """Pick (voice, languageCode) for the language actually being spoken."""
        lang = language or self._fallback_language
        configured_locale = _locale_of(self._voice)

        if configured_locale == lang and self._voice not in self._dead:
            return self._voice, lang

        auto = AUTO_VOICE.get(lang)
        if auto and auto not in self._dead:
            if configured_locale and configured_locale != lang:
                logger.info(
                    "Google TTS: speaking %s, so using %s instead of configured %s",
                    lang, auto, self._voice,
                )
            return auto, lang

        # No mapping for this language — the configured voice at least produces
        # audio, which beats silence on a live call.
        return self._voice, configured_locale or self._fallback_language

    async def synthesize_streaming(
        self, text: str, language: str | None = None
    ) -> AsyncGenerator[bytes, None]:
        text = normalize_pauses(text.strip())
        if not text:
            return

        voice, lang = self._voice_for(language)

        for sentence in split_sentences(text):
            audio = await self._call_api(sentence, voice, lang)
            if audio is None and voice != AUTO_VOICE.get(lang, voice):
                # The chosen voice just died; re-pick and retry this sentence
                # once so the caller does not lose a line to a config mistake.
                voice, lang = self._voice_for(language)
                audio = await self._call_api(sentence, voice, lang)
            if audio:
                yield audio

    def _payload(self, text: str, voice: str, language: str) -> dict:
        chirp = "Chirp" in voice
        audio_config: dict = {
            "audioEncoding": "LINEAR16",
            "sampleRateHertz": 8000,
            "speakingRate": self._speaking_rate,
            # Google's own phone-line EQ. On an 8kHz mu-law channel this is the
            # single biggest intelligibility win available.
            "effectsProfileId": ["telephony-class-application"],
        }
        if not chirp:
            audio_config["pitch"] = self._pitch

        return {
            # Chirp3-HD rejects SSML outright, so pause marks become commas.
            "input": (
                {"text": strip_pauses(text)}
                if chirp
                else {"ssml": to_ssml(text, self._pause_ms)}
            ),
            "voice": {"languageCode": language, "name": voice},
            "audioConfig": audio_config,
        }

    async def _call_api(self, text: str, voice: str, language: str) -> bytes | None:
        try:
            response = await self._client.post(
                GOOGLE_TTS_URL,
                params={"key": self._api_key},
                json=self._payload(text, voice, language),
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Google's message names the real problem (wrong key type, API not
            # enabled, voice not available, quota); the traceback never does.
            if exc.response.status_code in (400, 404):
                self._dead.add(voice)
            logger.error(
                "Google TTS %s for voice=%s lang=%s text=%s...: %s",
                exc.response.status_code, voice, language, text[:50],
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
