"""Provider catalog and construction.

CATALOG is the single source of truth for what the UI dropdowns offer and what
the backend can build. Adding a provider means adding one entry here and one
adapter under `providers/` — nothing else in the app changes.
"""

import logging
from typing import Any

from app import db
from app.models import AgentConfig
from app.providers.base import LLMProvider, STTProvider, TTSProvider
from app.providers.llm_gemini import GeminiLLM
from app.providers.llm_groq import GroqLLM
from app.providers.llm_xai import XaiLLM
from app.providers.stt_google import GoogleSTT
from app.providers.stt_sarvam import SarvamSTT
from app.providers.stt_smallest import SmallestSTT
from app.providers.tts_bhashini import VOICES as BHASHINI_VOICES, BhashiniTTS
from app.providers.tts_google import VOICES as GOOGLE_VOICES, GoogleTTS
from app.providers.tts_sarvam import VOICES as SARVAM_VOICES, SarvamTTS
from app.providers.tts_smallest import VOICES as SMALLEST_VOICES, SmallestTTS

logger = logging.getLogger(__name__)


class MissingCredential(Exception):
    """Raised when an agent names a provider whose API key has not been saved."""


LANGUAGES = [
    {"code": "hi-IN", "name": "Hindi"},
    {"code": "en-IN", "name": "English (India)"},
    {"code": "mr-IN", "name": "Marathi"},
    {"code": "ta-IN", "name": "Tamil"},
    {"code": "te-IN", "name": "Telugu"},
    {"code": "bn-IN", "name": "Bengali"},
    {"code": "gu-IN", "name": "Gujarati"},
    {"code": "kn-IN", "name": "Kannada"},
    {"code": "ml-IN", "name": "Malayalam"},
    {"code": "pa-IN", "name": "Punjabi"},
    {"code": "od-IN", "name": "Odia"},
]

ALL_LANGUAGES = [lang["code"] for lang in LANGUAGES]

CATALOG: dict[str, list[dict[str, Any]]] = {
    "stt": [
        {
            "provider": "sarvam",
            "label": "Sarvam Saarika",
            "credential": "sarvam",
            "models": ["saarika:v2.5", "saarika:v2", "saarika:v1"],
            "auto_detect": True,
            "languages": ALL_LANGUAGES,
            "note": "Auto-detects 22 Indian languages, handles code-mixing.",
        },
        {
            "provider": "smallest",
            "label": "Smallest.ai Pulse",
            "credential": "smallest",
            "models": ["pulse"],
            # Pulse reports no detected language, so auto mode never switches
            # on it — see `providers/stt_smallest.py`.
            "auto_detect": False,
            "languages": ["hi-IN", "en-IN"],
            "note": (
                "Hindi and English only. Other languages go to the 'multi-asian' "
                "aggregator, and it never reports which one it heard."
            ),
        },
        {
            "provider": "google",
            "label": "Google Speech-to-Text",
            "credential": "google",
            "models": ["latest_short", "latest_long", "default"],
            "auto_detect": False,
            "languages": ALL_LANGUAGES,
            "note": "Needs a GCP project with Speech-to-Text enabled and billing on.",
        },
    ],
    "llm": [
        {
            "provider": "gemini",
            "label": "Google Gemini",
            "credential": "gemini",
            # Fallback only — /api/catalog replaces this with the account's own
            # list. The 2.5-lite and 2.0 ids that used to be here are retired.
            "models": ["gemini-3.5-flash", "gemini-2.5-flash"],
            "note": (
                "Thinking disabled for call latency. Measured 1.2-1.5s per turn "
                "on Hindi replies — the slowest option in this catalog."
            ),
        },
        {
            "provider": "groq",
            "label": "Groq (Llama)",
            "credential": "groq",
            # Fallback only — /api/catalog replaces this with the account's own
            # list. The llama-3.x ids that used to be here have been retired.
            "models": ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "groq/compound-mini"],
            "note": (
                "Fastest in the catalog — gpt-oss-20b measured ~470ms per Hindi "
                "turn against 1.5s for Gemini. Key starts 'gsk_'. Note "
                "compound-mini has no tool support, so outcomes never record."
            ),
        },
        {
            "provider": "xai",
            "label": "xAI Grok",
            "credential": "xai",
            "models": ["grok-4.6", "grok-4-fast", "grok-3-mini"],
            "note": (
                "Different company from Groq — key starts 'xai-' and needs credits "
                "on console.x.ai. GET https://api.x.ai/v1/models lists what your "
                "team can actually run."
            ),
        },
    ],
    "tts": [
        {
            "provider": "smallest",
            "label": "Smallest.ai Lightning",
            "credential": "smallest",
            # Fallback only — /api/catalog replaces this with the account's own
            # voices, which is where Pro-tier and cloned voices appear.
            "voices": SMALLEST_VOICES,
            "languages": ALL_LANGUAGES,
            "note": "Streams 8kHz mu-law — nothing to decode, ~100ms to first audio. No pitch control.",
        },
        {
            "provider": "sarvam",
            "label": "Sarvam Bulbul",
            "credential": "sarvam",
            "voices": SARVAM_VOICES,
            "languages": ALL_LANGUAGES,
            "note": "Returns 8kHz WAV directly — no MP3 decode, lowest latency.",
        },
        {
            "provider": "bhashini",
            "label": "Bhashini AI",
            "credential": "bhashini",
            "voices": BHASHINI_VOICES,
            "languages": ALL_LANGUAGES,
            "note": "Returns MP3; decoded off the event loop.",
        },
        {
            "provider": "google",
            "label": "Google Text-to-Speech",
            "credential": "google",
            "voices": GOOGLE_VOICES,
            # No Odia voice ships in the catalog, so the language cannot be
            # spoken even though Google's STT can hear it.
            "languages": [c for c in ALL_LANGUAGES if c != "od-IN"],
            "note": "Wavenet/Neural2 voices. Needs Text-to-Speech enabled in GCP. No Odia voice.",
        },
    ],
}


def credential_key(kind: str, provider: str) -> str:
    """Which credential slot a provider draws from ('' if unknown)."""
    entry = next((e for e in CATALOG[kind] if e["provider"] == provider), None)
    return entry["credential"] if entry else ""


def _credential_for(kind: str, provider: str) -> str:
    entry = next(
        (e for e in CATALOG[kind] if e["provider"] == provider), None
    )
    if entry is None:
        raise MissingCredential(f"Unknown {kind} provider: {provider}")

    key = db.get_credential(entry["credential"])
    if not key:
        raise MissingCredential(
            f"No API key saved for '{entry['credential']}'. "
            f"Add it on the Credentials page."
        )
    return key


def build_stt(agent: AgentConfig) -> STTProvider:
    key = _credential_for("stt", agent.stt_provider)
    if agent.stt_provider == "google":
        return GoogleSTT(api_key=key, model=agent.stt_model)
    if agent.stt_provider == "smallest":
        return SmallestSTT(api_key=key, model=agent.stt_model)
    return SarvamSTT(api_key=key, model=agent.stt_model)


def build_tts(agent: AgentConfig) -> TTSProvider:
    key = _credential_for("tts", agent.tts_provider)
    fallback = agent.language
    if agent.tts_provider == "smallest":
        # Lightning has no pitch parameter, so tts_pitch is not passed on.
        return SmallestTTS(
            api_key=key, voice=agent.tts_voice, fallback_language=fallback,
            speaking_rate=agent.tts_speaking_rate,
        )
    if agent.tts_provider == "sarvam":
        return SarvamTTS(
            api_key=key, voice=agent.tts_voice, fallback_language=fallback,
            speaking_rate=agent.tts_speaking_rate, pitch=agent.tts_pitch,
        )
    if agent.tts_provider == "google":
        return GoogleTTS(
            api_key=key, voice=agent.tts_voice, fallback_language=fallback,
            speaking_rate=agent.tts_speaking_rate, pitch=agent.tts_pitch,
            pause_ms=agent.tts_pause_ms,
        )
    return BhashiniTTS(api_key=key, voice=agent.tts_voice, fallback_language=fallback)


def build_llm(agent: AgentConfig) -> LLMProvider:
    key = _credential_for("llm", agent.llm_provider)
    if agent.llm_provider == "groq":
        return GroqLLM(api_key=key, model=agent.llm_model)
    if agent.llm_provider == "xai":
        return XaiLLM(api_key=key, model=agent.llm_model)
    return GeminiLLM(api_key=key, model=agent.llm_model)


def supported_languages(stt_provider: str, tts_provider: str) -> list[str]:
    """Languages both halves of the pipeline can actually handle.

    An agent has one language setting driving both, so anything only one side
    supports is unusable: Smallest speaks all eleven but its STT hears two, and
    an agent configured that way talks fluently and understands nothing.
    """
    def codes(kind: str, provider: str) -> set[str]:
        entry = next((e for e in CATALOG[kind] if e["provider"] == provider), None)
        return set(entry["languages"]) if entry else set()

    usable = codes("stt", stt_provider) & codes("tts", tts_provider)
    return [code for code in ALL_LANGUAGES if code in usable]


def voices_for(provider: str) -> list[dict[str, Any]]:
    entry = next((e for e in CATALOG["tts"] if e["provider"] == provider), None)
    return entry["voices"] if entry else []


def credential_slots() -> list[dict[str, str]]:
    """Every credential the UI should offer a field for."""
    return [
        {"key": "smallest", "label": "Smallest.ai", "hint": "One key for Pulse STT and Lightning TTS"},
        {"key": "sarvam", "label": "Sarvam AI", "hint": "Used for both Saarika STT and Bulbul TTS"},
        {"key": "bhashini", "label": "Bhashini AI", "hint": "TTS only"},
        {"key": "google", "label": "Google Cloud", "hint": "API key for Speech-to-Text and Text-to-Speech"},
        {"key": "gemini", "label": "Google Gemini", "hint": "AI Studio API key"},
        {"key": "groq", "label": "Groq (Llama)", "hint": "console.groq.com key — starts 'gsk_'"},
        {"key": "xai", "label": "xAI Grok", "hint": "console.x.ai key — starts 'xai-'"},
    ]
