"""Environment settings loaded from .env (Pydantic Settings)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Sarvam AI (STT)
    SARVAM_API_KEY: str = ""
    SARVAM_STT_MODEL: str = "saarika:v2.5"

    # Bhashini AI (TTS)
    BHASHINI_API_KEY: str = ""
    BHASHINI_TTS_VOICE_EN: str = "Female3"
    BHASHINI_TTS_VOICE_HI: str = "hi-f3"
    BHASHINI_TTS_STYLE: str = "Neutral"
    BHASHINI_TTS_BASE_URL: str = "https://tts.bhashini.ai"

    # LLM Provider: "gemini" or "groq"
    LLM_PROVIDER: str = "gemini"

    # Google Gemini (LLM)
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash-lite"

    # Groq (LLM)
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "llama-3.1-8b-instant"

    # Twilio
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_PHONE_NUMBER: str = ""

    # App
    BASE_URL: str = "http://localhost:8000"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    LOGS_DIR: str = "logs"

    # Conversation tuning
    DEFAULT_LANGUAGE: str = "hi-IN"
    SILENCE_THRESHOLD_RMS: int = 300
    SILENCE_END_SECONDS: float = 0.8
    MIN_UTTERANCE_SECONDS: float = 0.3


@lru_cache
def get_settings() -> Settings:
    return Settings()
