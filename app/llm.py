"""Gemini 2.5 Flash-Lite dialog engine (thinking disabled for low latency).

Includes a `customer_agreed_to_pay` tool: when the caller clearly commits to
paying, Gemini calls it automatically (google-genai automatic function calling).
"""

import logging
from typing import Callable

from google import genai
from google.genai import types

from app.config import get_settings

logger = logging.getLogger(__name__)

MAX_HISTORY_TURNS = 20


class DialogEngine:
    """Per-call conversation state + Gemini response generation."""

    def __init__(
        self,
        system_prompt: str,
        on_payment_agreed: Callable[[str], None] | None = None,
    ) -> None:
        settings = get_settings()
        self._client = genai.Client(api_key=settings.GEMINI_API_KEY)
        self._model = settings.GEMINI_MODEL
        self._on_payment_agreed = on_payment_agreed
        self.payment_agreed = False

        def customer_agreed_to_pay(summary: str) -> dict:
            """Record that the customer has CLEARLY agreed to pay / add the required funds.

            Call this only when the customer gives a clear, unambiguous commitment
            to pay (not for vague answers like "maybe" or "I'll see").

            Args:
                summary: One short sentence describing what the customer agreed to.
            """
            self.payment_agreed = True
            print(f"\n{'=' * 60}\n*** CUSTOMER AGREED TO PAY ***\n{summary}\n{'=' * 60}\n")
            logger.info("FUNCTION CALL customer_agreed_to_pay: %s", summary)
            if self._on_payment_agreed:
                self._on_payment_agreed(summary)
            return {"status": "recorded", "next_step": "Confirm back to the customer and close the call politely."}

        self._config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.4,
            max_output_tokens=150,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            tools=[customer_agreed_to_pay],
        )
        self._history: list[types.Content] = []

    async def generate_greeting(self) -> str:
        """Opening line when the call connects."""
        return await self._generate(
            "[The call has just connected. Greet the caller now exactly as your "
            "instructions describe. One or two short sentences only.]"
        )

    async def generate_response(self, user_text: str, language_code: str | None = None) -> str:
        """Reply to a caller utterance, in the caller's detected language."""
        if language_code:
            user_text = f"[detected_language={language_code}] {user_text}"
        return await self._generate(user_text)

    async def _generate(self, user_text: str) -> str:
        self._history.append(
            types.Content(role="user", parts=[types.Part.from_text(text=user_text)])
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=self._history,
                config=self._config,
            )
            reply = (response.text or "").strip()
        except Exception:
            logger.exception("Gemini request failed")
            reply = ""

        if not reply:
            reply = "Sorry, kya aap dobara bol sakte hain?"

        self._history.append(
            types.Content(role="model", parts=[types.Part.from_text(text=reply)])
        )
        # Trim history to bound latency and cost on long calls.
        if len(self._history) > MAX_HISTORY_TURNS * 2:
            self._history = self._history[-MAX_HISTORY_TURNS * 2:]
        return reply
