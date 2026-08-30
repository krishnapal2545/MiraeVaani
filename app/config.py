"""Infrastructure settings only.

Unlike v5, this holds NO provider credentials — those live in the `credentials`
table, entered through the UI and encrypted at rest. Everything an agent needs
comes from its row in the `agents` table.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Public HTTPS base URL Twilio calls back on (ngrok/cloudflared tunnel).
    BASE_URL: str = "http://localhost:8000"

    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    LOGS_DIR: str = "logs"
    DB_PATH: str = "miraevaani.db"

    # Passphrase used to derive the Fernet key that encrypts stored API keys.
    # Change this and every saved credential becomes unreadable.
    APP_SECRET: str = "change-me-in-dot-env"

    # Ceiling on simultaneous calls across every agent on this machine, and the
    # last word over any per-agent or per-campaign setting. Twilio sends one
    # websocket message per 20ms per call, so a handful of calls is real work
    # for one Python process — this default is sized for a laptop behind a
    # tunnel, not for a server.
    GLOBAL_MAX_CONCURRENT_CALLS: int = 5
    # How often a worker asks whether its campaigns are owed another call.
    DISPATCHER_TICK_SECONDS: float = 5.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
