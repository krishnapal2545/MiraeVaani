"""Application settings — loaded from .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Twilio ---
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_PHONE_NUMBER: str = ""

    # --- Colab GPU Services (set after starting Colab notebook) ---
    STT_BASE_URL: str = ""   # e.g. https://xxxx.ngrok-free.app
    LLM_BASE_URL: str = ""   # e.g. https://yyyy.ngrok-free.app
    TTS_BASE_URL: str = ""   # e.g. https://zzzz.ngrok-free.app

    # --- LLM ---
    LLM_MODEL: str = "gemma2:9b"
    LLM_TEMPERATURE: float = 0.7
    LLM_MAX_TOKENS: int = 60

    # --- STT ---
    STT_LANGUAGE: str = "auto"        # "auto" = auto-detect, or "hi", "ta", "te", etc.
    STT_WHISPER_MODEL: str = "large-v3"

    # --- TTS ---
    TTS_DEFAULT_LANGUAGE: str = "hi"  # fallback language if detection fails
    TTS_SPEAKER: str = "Ana Florence" # default XTTS speaker

    # --- App ---
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    BASE_URL: str = ""                # Public URL for Twilio webhooks (ngrok/deployment)
    LOG_LEVEL: str = "INFO"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()
