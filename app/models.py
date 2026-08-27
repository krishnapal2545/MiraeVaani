"""AgentConfig — the object that replaces v5's global Settings.

In v5 every provider choice, VAD threshold and prompt came from .env and was
baked in at import time. Here they are fields on a row loaded per call, which is
the entire point of v6: an agent is data, not code.
"""

from pydantic import BaseModel, Field


class AgentConfig(BaseModel):
    id: str = ""
    name: str = "New Agent"

    # --- Speech to text ---
    stt_provider: str = "sarvam"
    stt_model: str = "saarika:v2.5"

    # --- Language ---
    # "auto": trust STT's per-utterance detection and reply/speak in that language.
    # "fixed": always use `language`, ignoring detection (stops one bad detection
    #          from switching a Tamil agent to a Hindi voice mid-call).
    language_mode: str = "auto"
    language: str = "hi-IN"

    # --- LLM ---
    llm_provider: str = "gemini"
    llm_model: str = "gemini-2.5-flash-lite"
    temperature: float = 0.4
    max_output_tokens: int = 150

    # --- Text to speech ---
    tts_provider: str = "bhashini"
    tts_voice: str = "hi-f3"

    # --- Conversation ---
    system_prompt: str = ""
    greeting_mode: str = "llm"       # "llm" | "static"
    greeting_text: str = ""

    # --- Smart fillers ---
    fillers_enabled: bool = True
    filler_delay_ms: int = 350

    # --- Turn detection (energy based, as in v5) ---
    silence_threshold_rms: int = 300
    silence_end_seconds: float = 0.8
    min_utterance_seconds: float = 0.3

    # --- Escalation ---
    redirect_number: str = ""

    def resolve_language(self, detected: str | None) -> str:
        """Language to speak in, honouring language_mode."""
        if self.language_mode == "fixed":
            return self.language
        return detected or self.language


class CallRequest(BaseModel):
    agent_id: str
    to: str
    variables: dict[str, str] = Field(default_factory=dict)
