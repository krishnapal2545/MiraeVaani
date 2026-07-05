"""
LLM — Dialog generation via remote Ollama service on Colab GPU.

Uses Ollama's OpenAI-compatible /v1/chat/completions endpoint.
Model: Gemma 2 9B (excellent multilingual Indian language support).
"""

import asyncio
import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class DialogEngine:
    """Manages conversation state and generates responses via remote Ollama."""

    def __init__(self, system_prompt: str):
        self.settings = get_settings()
        self.system_prompt = system_prompt
        self.conversation_history: list[dict] = []
        self._detected_language: str = "hi"  # updated by ASR on each turn

    @property
    def detected_language(self) -> str:
        return self._detected_language

    @detected_language.setter
    def detected_language(self, lang: str) -> None:
        self._detected_language = lang

    async def generate_response(self, user_message: str) -> str:
        """
        Generate a conversational response for the customer's utterance.

        Args:
            user_message: Transcribed customer speech.

        Returns:
            Agent response text (in the detected language).
        """
        self.conversation_history.append({"role": "user", "content": user_message})

        for attempt in range(3):
            try:
                response_text = await self._call_ollama(self.conversation_history)
                self.conversation_history.append(
                    {"role": "assistant", "content": response_text}
                )
                logger.info("LLM response: %s", response_text)
                return response_text

            except Exception as e:
                if attempt < 2:
                    logger.warning(
                        "LLM attempt %d failed: %s — retrying...", attempt + 1, e
                    )
                    await asyncio.sleep((attempt + 1) * 1.5)
                    continue
                logger.exception("LLM: all retries exhausted")
                return "I apologize, I'm having a brief technical issue. Could you please repeat that?"

    async def generate_greeting(self) -> str:
        """Generate the opening greeting for an outbound call."""
        messages = [
            {
                "role": "user",
                "content": (
                    "Generate your opening greeting for this call. "
                    "Introduce yourself as Vaani from Mirae Asset Sharekhan. "
                    "Say you're calling regarding their account and ask to confirm "
                    "you are speaking with the right person. "
                    "Do NOT reveal the actual issue yet — wait for identity confirmation. "
                    "Keep it to 2 sentences max."
                ),
            }
        ]
        try:
            greeting = await self._call_ollama(messages)
            # Seed history so the model knows it already greeted
            self.conversation_history.append({"role": "assistant", "content": greeting})
            logger.info("LLM greeting: %s", greeting)
            return greeting
        except Exception:
            logger.exception("LLM: greeting generation failed")
            fallback = "Hello, I'm Vaani calling from Mirae Asset Sharekhan. Am I speaking with the account holder?"
            self.conversation_history.append({"role": "assistant", "content": fallback})
            return fallback

    async def _call_ollama(self, messages: list[dict]) -> str:
        """POST to Ollama's OpenAI-compatible chat completions endpoint."""
        if not self.settings.LLM_BASE_URL:
            raise RuntimeError("LLM_BASE_URL not configured")

        url = f"{self.settings.LLM_BASE_URL.rstrip('/')}/v1/chat/completions"

        payload = {
            "model": self.settings.LLM_MODEL,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                *messages,
            ],
            "temperature": self.settings.LLM_TEMPERATURE,
            "max_tokens": self.settings.LLM_MAX_TOKENS,
            "stream": False,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

        return data["choices"][0]["message"]["content"].strip()

    def get_history(self) -> list[dict]:
        return self.conversation_history.copy()

    def reset(self) -> None:
        self.conversation_history = []
