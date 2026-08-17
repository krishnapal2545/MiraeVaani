"""Multi-provider dialog engine supporting Gemini and Groq.

Supports:
- Gemini 2.5 Flash-Lite (thinking disabled for low latency)
- Groq Llama 3.1 8B (ultra-fast inference)

Switch between them via LLM_PROVIDER in .env ("gemini" or "groq").

Includes a `customer_agreed_to_pay` tool: when the caller clearly commits to
paying, the LLM calls it automatically.
"""

import json
import logging
from typing import Callable

from app.config import get_settings

logger = logging.getLogger(__name__)

MAX_HISTORY_TURNS = 20


# ---------------------------------------------------------------------------
# Groq tool definitions (OpenAI-style function calling)
# ---------------------------------------------------------------------------
GROQ_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "customer_agreed_to_pay",
            "description": (
                "Record that the customer has CLEARLY agreed to pay. "
                "Call only when the customer gives a clear, unambiguous commitment "
                "(not for vague answers like 'maybe' or 'I'll see')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "One short sentence describing what the customer agreed to.",
                    }
                },
                "required": ["summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_call",
            "description": (
                "End the phone call. Call ONLY after you have said your final goodbye."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]


class DialogEngine:
    """Per-call conversation state + LLM response generation."""

    def __init__(
        self,
        system_prompt: str,
        on_payment_agreed: Callable[[str], None] | None = None,
        on_call_end: Callable[[], None] | None = None,
    ) -> None:
        settings = get_settings()
        self._provider = settings.LLM_PROVIDER.lower()
        self._on_payment_agreed = on_payment_agreed
        self._on_call_end = on_call_end
        self.payment_agreed = False
        self.call_ended = False
        self._system_prompt = system_prompt

        if self._provider == "groq":
            self._init_groq(settings)
        else:
            self._init_gemini(settings)

    # ------------------------------------------------------------------
    # Provider initialization
    # ------------------------------------------------------------------

    def _init_gemini(self, settings) -> None:
        from google import genai
        from google.genai import types

        self._client = genai.Client(api_key=settings.GEMINI_API_KEY)
        self._model = settings.GEMINI_MODEL

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

        def end_call() -> dict:
            """End the phone call. Call this ONLY after you have said your final goodbye.

            Call this when:
            - The customer says goodbye/bye/thank you and the conversation is clearly over
            - You have already confirmed and said your goodbye
            - The call objective is complete and farewell has been exchanged
            """
            self.call_ended = True
            logger.info("FUNCTION CALL end_call triggered")
            if self._on_call_end:
                self._on_call_end()
            return {"status": "call_ending"}

        self._gemini_config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.4,
            max_output_tokens=150,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            tools=[customer_agreed_to_pay, end_call],
        )
        self._gemini_history: list[types.Content] = []

    def _init_groq(self, settings) -> None:
        from groq import AsyncGroq

        self._groq_client = AsyncGroq(api_key=settings.GROQ_API_KEY)
        self._model = settings.GROQ_MODEL
        self._groq_history: list[dict] = []

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Internal dispatch
    # ------------------------------------------------------------------

    async def _generate(self, user_text: str) -> str:
        if self._provider == "groq":
            return await self._generate_groq(user_text)
        return await self._generate_gemini(user_text)

    # ------------------------------------------------------------------
    # Gemini implementation
    # ------------------------------------------------------------------

    async def _generate_gemini(self, user_text: str) -> str:
        from google.genai import types

        self._gemini_history.append(
            types.Content(role="user", parts=[types.Part.from_text(text=user_text)])
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=self._gemini_history,
                config=self._gemini_config,
            )
            reply = (response.text or "").strip()
        except Exception:
            logger.exception("Gemini request failed")
            reply = ""

        if not reply:
            reply = "Sorry, kya aap dobara bol sakte hain?"

        self._gemini_history.append(
            types.Content(role="model", parts=[types.Part.from_text(text=reply)])
        )
        if len(self._gemini_history) > MAX_HISTORY_TURNS * 2:
            self._gemini_history = self._gemini_history[-MAX_HISTORY_TURNS * 2:]
        return reply

    # ------------------------------------------------------------------
    # Groq implementation
    # ------------------------------------------------------------------

    async def _generate_groq(self, user_text: str) -> str:
        self._groq_history.append({"role": "user", "content": user_text})

        messages = [{"role": "system", "content": self._system_prompt}] + self._groq_history

        try:
            response = await self._groq_client.chat.completions.create(
                model=self._model,
                messages=messages,
                tools=GROQ_TOOLS,
                tool_choice="auto",
                temperature=0.4,
                max_tokens=150,
            )

            message = response.choices[0].message

            # Handle tool calls if any
            if message.tool_calls:
                self._groq_history.append(message.model_dump())

                for tool_call in message.tool_calls:
                    result = self._handle_groq_tool_call(tool_call)
                    self._groq_history.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result),
                    })

                # Get follow-up response after tool execution
                messages = [{"role": "system", "content": self._system_prompt}] + self._groq_history
                follow_up = await self._groq_client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    temperature=0.4,
                    max_tokens=150,
                )
                reply = (follow_up.choices[0].message.content or "").strip()
            else:
                reply = (message.content or "").strip()

        except Exception:
            logger.exception("Groq request failed")
            reply = ""

        if not reply:
            reply = "Sorry, kya aap dobara bol sakte hain?"

        self._groq_history.append({"role": "assistant", "content": reply})

        if len(self._groq_history) > MAX_HISTORY_TURNS * 2:
            self._groq_history = self._groq_history[-MAX_HISTORY_TURNS * 2:]
        return reply

    def _handle_groq_tool_call(self, tool_call) -> dict:
        """Execute a tool call from Groq and return the result."""
        name = tool_call.function.name
        args = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}

        if name == "customer_agreed_to_pay":
            summary = args.get("summary", "")
            self.payment_agreed = True
            print(f"\n{'=' * 60}\n*** CUSTOMER AGREED TO PAY ***\n{summary}\n{'=' * 60}\n")
            logger.info("FUNCTION CALL customer_agreed_to_pay: %s", summary)
            if self._on_payment_agreed:
                self._on_payment_agreed(summary)
            return {"status": "recorded", "next_step": "Confirm back to the customer and close the call politely."}

        elif name == "end_call":
            self.call_ended = True
            logger.info("FUNCTION CALL end_call triggered")
            if self._on_call_end:
                self._on_call_end()
            return {"status": "call_ending"}

        return {"error": "unknown tool"}
