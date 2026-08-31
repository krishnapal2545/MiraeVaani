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
    # "auto": follow STT's detection, but only once it is corroborated — see
    #         `app/language.py`. A single bad guess on a 1.5s clip no longer
    #         flips the whole call into another language.
    # "fixed": always use `language`, ignoring detection entirely.
    language_mode: str = "auto"
    language: str = "hi-IN"
    # Comma-separated BCP-47 allow-list for auto mode; empty means "anything
    # the STT returns". Setting it is the hard stop against a Marathi call
    # being dragged into Punjabi by one mis-detection.
    allowed_languages: str = ""
    # Consecutive detections of a new language before the agent follows it.
    language_switch_turns: int = 2
    # Utterances shorter than this never vote on the language at all.
    language_switch_min_seconds: float = 1.0

    # --- LLM ---
    llm_provider: str = "gemini"
    # gemini-2.5-flash-lite was retired and 404s for accounts that had not
    # already used it, so every new agent built on the default failed on every
    # turn. Groq is measurably faster than any Gemini here (see the catalog
    # note) but needs its own key, which a fresh install may not have.
    llm_model: str = "gemini-3.5-flash"
    temperature: float = 0.4
    # Devanagari costs roughly a token per character on every tokenizer here,
    # and a reasoning model spends part of the budget before it writes a word.
    # 150 truncated ordinary two-sentence Hindi replies mid-word; the cap is
    # only a ceiling, so raising it costs nothing when the reply is short.
    max_output_tokens: int = 220

    # --- Text to speech ---
    tts_provider: str = "bhashini"
    tts_voice: str = "hi-f3"
    # Slightly under 1.0 reads as considered rather than rushed on a phone line.
    tts_speaking_rate: float = 0.95
    # Authored in Google's semitones (-20..20); other engines rescale.
    tts_pitch: float = 0.0
    # How long a "..." in the reply text becomes, in milliseconds.
    tts_pause_ms: int = 350

    # --- Conversation ---
    system_prompt: str = ""
    greeting_mode: str = "llm"       # "llm" | "static"
    greeting_text: str = ""

    # --- Smart fillers ---
    fillers_enabled: bool = True
    filler_delay_ms: int = 350

    # --- Turn detection (energy based, with an adaptive floor — see app/vad.py) ---
    # Absolute minimum threshold. The live threshold rides above the measured
    # room noise, so this is a floor, not the whole story.
    silence_threshold_rms: int = 300
    silence_end_seconds: float = 0.8
    min_utterance_seconds: float = 0.4
    # How far above the room's noise floor speech must sit. Raise it on a noisy
    # line (a TV, an office, a second conversation); lower it for a soft talker.
    noise_margin: float = 2.0
    # Sustained caller speech needed to cut the agent off. 0.15s was a cough.
    barge_in_seconds: float = 0.5
    # Barge-in is ignored for this long after the agent starts speaking, which
    # covers line echo and the caller's own trailing "haan" from the last turn.
    barge_in_grace_seconds: float = 0.7

    # --- When the caller's answer never arrives ---
    # Silence after the agent stops speaking before it checks whether it is
    # being heard at all, the way a person does. 0 disables the check entirely.
    no_reply_seconds: float = 6.0
    # Unanswered check-ins before the agent gives up and ends the call, rather
    # than holding a dead line open until Twilio times it out.
    no_reply_prompts: int = 2

    # --- Escalation ---
    redirect_number: str = ""

    # --- Campaign behaviour ---
    # Ceiling on this agent's simultaneous calls. The worker also honours the
    # campaign's own cap and the global one, and dials to the lowest of the three.
    max_concurrent_calls: int = 20
    # POSTed to when the agent records an outcome, so a decision on the call can
    # trigger something outside it (a payment link, a CRM update). Empty disables.
    outcome_webhook_url: str = ""

    def resolve_language(self, detected: str | None) -> str:
        """Language to speak in, honouring language_mode."""
        if self.language_mode == "fixed":
            return self.language
        return detected or self.language


class CallRequest(BaseModel):
    agent_id: str
    to: str
    variables: dict[str, str] = Field(default_factory=dict)
