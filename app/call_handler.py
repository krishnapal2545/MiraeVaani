"""Per-call session over Twilio Media Streams: STT -> LLM -> TTS with barge-in.

Turn detection is energy-based: caller audio is buffered while speech is
detected, and the utterance is finalized after SILENCE_END_SECONDS of quiet.
The full utterance is then sent to Sarvam STT (with language auto-detection),
Gemini generates a reply, and Bhashini TTS speaks it back sentence-by-sentence.

Key improvements over v4:
- Sentence-streaming TTS: first sentence plays while remaining synthesize
- Barge-in discards old pipeline, answers ONLY the latest question
- Call hangup: after goodbye the call is disconnected automatically
- Clear per-service latency logging (STT hit→response, LLM hit→response, TTS hit→first-byte)
"""

import asyncio
import base64
import json
import logging
import time
from typing import Callable

from fastapi import WebSocket
from twilio.rest import Client as TwilioClient

from app.asr import SarvamSTT
from app.audio import mulaw8k_to_wav16k, mulaw_frame_rms, mulaw_to_pcm, pcm_to_wav_bytes
from app.call_logger import CallLogger
from app.config import get_settings
from app.llm import DialogEngine
from app.prompts import get_prompt
from app.tts import BhashiniTTS

logger = logging.getLogger(__name__)

# Sustained voiced audio needed to treat caller speech as an interruption.
BARGE_IN_SECONDS = 0.15
# Outbound audio is sent to Twilio in chunks of this many bytes (1s @ 8kHz mu-law).
OUTBOUND_CHUNK_BYTES = 8000
# Delay before hanging up after goodbye (let final audio play).
HANGUP_DELAY_SECONDS = 3.0


class CallSession:
    def __init__(
        self,
        websocket: WebSocket,
        stt: SarvamSTT,
        tts: BhashiniTTS,
        context_resolver: Callable[[str | None], dict],
    ) -> None:
        self.ws = websocket
        self.stt = stt
        self.tts = tts
        self._resolve_context = context_resolver
        self._settings = get_settings()

        self.stream_sid: str | None = None
        self.call_sid: str | None = None
        self.dialog: DialogEngine | None = None
        self.language: str | None = None
        self.call_log: CallLogger | None = None
        self._turn = 0

        # Turn-detection state
        self._buffer = bytearray()
        self._capturing = False
        self._speech_secs = 0.0
        self._silence_secs = 0.0

        # Agent playback / barge-in state
        self._is_speaking = False
        self._mark_counter = 0
        self._pending_mark: str | None = None
        self._barge_secs = 0.0
        self._barge_buffer = bytearray()

        self._response_task: asyncio.Task | None = None
        self._hangup_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    async def handle(self) -> None:
        try:
            async for raw in self.ws.iter_text():
                message = json.loads(raw)
                event = message.get("event")

                if event == "start":
                    await self._on_start(message["start"])
                elif event == "media":
                    await self._on_media(base64.b64decode(message["media"]["payload"]))
                elif event == "mark":
                    if message.get("mark", {}).get("name") == self._pending_mark:
                        self._is_speaking = False
                elif event == "stop":
                    logger.info("Media stream stopped: call=%s", self.call_sid)
                    break
        except Exception:
            logger.exception("Error in call session")
        finally:
            await self._cleanup()

    async def _on_start(self, start: dict) -> None:
        self.stream_sid = start["streamSid"]
        self.call_sid = start.get("callSid")
        call_id = (start.get("customParameters") or {}).get("call_id")

        context = self._resolve_context(call_id)
        system_prompt = get_prompt(context.get("scenario", "inbound"), **context)

        self.call_log = CallLogger(
            self._settings.LOGS_DIR, self.call_sid or call_id or "unknown"
        )
        self.call_log.event(
            "call_start",
            call_sid=self.call_sid,
            call_id=call_id,
            scenario=context.get("scenario"),
            context=context,
        )

        def _on_payment_agreed(summary: str) -> None:
            if self.call_log:
                self.call_log.event("payment_agreed", summary=summary)

        def _on_call_end() -> None:
            if self.call_log:
                self.call_log.event("call_end_triggered")

        self.dialog = DialogEngine(
            system_prompt=system_prompt,
            on_payment_agreed=_on_payment_agreed,
            on_call_end=_on_call_end,
        )

        logger.info(
            "Stream started: call=%s call_id=%s scenario=%s",
            self.call_sid, call_id, context.get("scenario"),
        )
        self._response_task = asyncio.create_task(self._greet())

    # ------------------------------------------------------------------
    # Inbound audio: turn detection + barge-in
    # ------------------------------------------------------------------
    async def _on_media(self, chunk: bytes) -> None:
        duration = len(chunk) / 8000.0  # mu-law: 1 byte per sample @ 8kHz
        voiced = mulaw_frame_rms(chunk) >= self._settings.SILENCE_THRESHOLD_RMS

        if self._is_speaking:
            # Agent is talking — watch for a sustained interruption.
            if voiced:
                self._barge_secs += duration
                self._barge_buffer.extend(chunk)
                if self._barge_secs >= BARGE_IN_SECONDS:
                    logger.info("Barge-in detected — cancelling current response")
                    if self.call_log:
                        self.call_log.event("barge_in", turn=self._turn)
                    await self._interrupt_playback()
                    self._buffer = bytearray(self._barge_buffer)
                    self._speech_secs = self._barge_secs
                    self._silence_secs = 0.0
                    self._capturing = True
                    self._barge_secs = 0.0
                    self._barge_buffer.clear()
            else:
                self._barge_secs = 0.0
                self._barge_buffer.clear()
            return

        if voiced:
            self._capturing = True
            self._buffer.extend(chunk)
            self._speech_secs += duration
            self._silence_secs = 0.0
        elif self._capturing:
            self._buffer.extend(chunk)
            self._silence_secs += duration
            if self._silence_secs >= self._settings.SILENCE_END_SECONDS:
                utterance = bytes(self._buffer)
                speech_secs = self._speech_secs
                self._reset_capture()
                if speech_secs >= self._settings.MIN_UTTERANCE_SECONDS:
                    # Cancel any old in-flight response — only latest question matters
                    if self._response_task and not self._response_task.done():
                        self._response_task.cancel()
                        try:
                            await self._response_task
                        except (asyncio.CancelledError, Exception):
                            pass
                    self._response_task = asyncio.create_task(
                        self._process_utterance(utterance)
                    )

    def _reset_capture(self) -> None:
        self._buffer = bytearray()
        self._capturing = False
        self._speech_secs = 0.0
        self._silence_secs = 0.0

    # ------------------------------------------------------------------
    # STT -> LLM -> TTS pipeline
    # ------------------------------------------------------------------
    async def _process_utterance(self, mulaw_utterance: bytes) -> None:
        try:
            self._turn += 1
            turn = self._turn
            turn_start = time.perf_counter()

            # --- STT ---
            wav_bytes = mulaw8k_to_wav16k(mulaw_utterance)
            if self.call_log:
                self.call_log.save_audio(f"turn_{turn:03d}_user.wav", wav_bytes)

            stt_hit = time.perf_counter()
            transcript, detected_language = await self.stt.transcribe(wav_bytes)
            stt_done = time.perf_counter()
            stt_ms = (stt_done - stt_hit) * 1000

            if self.call_log:
                self.call_log.event(
                    "stt",
                    turn=turn,
                    hit_at_ms=round((stt_hit - turn_start) * 1000),
                    latency_ms=round(stt_ms),
                    language=detected_language,
                    customer_said=transcript,
                    audio_secs=round(len(mulaw_utterance) / 8000, 2),
                )

            if not transcript or not self.dialog:
                return
            if detected_language:
                self.language = detected_language

            # --- LLM ---
            llm_hit = time.perf_counter()
            reply = await self.dialog.generate_response(transcript, self.language)
            llm_done = time.perf_counter()
            llm_ms = (llm_done - llm_hit) * 1000

            if self.call_log:
                self.call_log.event(
                    "llm",
                    turn=turn,
                    hit_at_ms=round((llm_hit - turn_start) * 1000),
                    latency_ms=round(llm_ms),
                    agent_reply=reply,
                    payment_agreed=self.dialog.payment_agreed,
                    call_ended=self.dialog.call_ended,
                )
            logger.info("Agent [%s]: %s", self.language, reply)

            # --- TTS + streaming playback ---
            tts_hit = time.perf_counter()
            await self._speak_streaming(reply, turn=turn, tts_hit=tts_hit)
            tts_done = time.perf_counter()
            tts_ms = (tts_done - tts_hit) * 1000

            total_ms = (time.perf_counter() - turn_start) * 1000
            if self.call_log:
                self.call_log.event(
                    "turn_total",
                    turn=turn,
                    stt_ms=round(stt_ms),
                    llm_ms=round(llm_ms),
                    tts_ms=round(tts_ms),
                    total_ms=round(total_ms),
                )

            # --- Hangup if call ended ---
            if self.dialog.call_ended:
                self._hangup_task = asyncio.create_task(self._hangup_after_delay())

        except asyncio.CancelledError:
            logger.info("Turn %d cancelled (barge-in or newer utterance)", self._turn)
        except Exception:
            logger.exception("Error processing utterance")

    async def _greet(self) -> None:
        try:
            await asyncio.sleep(0.5)
            if not self.dialog:
                return
            t0 = time.perf_counter()
            greeting = await self.dialog.generate_greeting()
            llm_ms = (time.perf_counter() - t0) * 1000
            if self.call_log:
                self.call_log.event(
                    "llm", turn=0, latency_ms=round(llm_ms), agent_reply=greeting
                )
            logger.info("Agent greeting: %s", greeting)
            tts_hit = time.perf_counter()
            await self._speak_streaming(greeting, turn=0, tts_hit=tts_hit)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Error sending greeting")

    async def _speak_streaming(self, text: str, turn: int = 0, tts_hit: float = 0) -> None:
        """Synthesize sentence-by-sentence and stream each to Twilio immediately."""
        if not self.stream_sid:
            return

        self._is_speaking = True
        self._barge_secs = 0.0
        self._barge_buffer.clear()

        total_audio = bytearray()
        first_byte_logged = False

        async for mulaw_chunk in self.tts.synthesize_streaming(text, self.language):
            if not self._is_speaking:
                return  # interrupted

            if not first_byte_logged and self.call_log:
                ttfb_ms = (time.perf_counter() - tts_hit) * 1000
                self.call_log.event("tts_first_byte", turn=turn, ttfb_ms=round(ttfb_ms))
                first_byte_logged = True

            total_audio.extend(mulaw_chunk)

            # Send this sentence's audio to Twilio immediately
            for offset in range(0, len(mulaw_chunk), OUTBOUND_CHUNK_BYTES):
                if not self._is_speaking:
                    return
                await self.ws.send_json({
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {
                        "payload": base64.b64encode(
                            mulaw_chunk[offset:offset + OUTBOUND_CHUNK_BYTES]
                        ).decode("ascii")
                    },
                })

        if not self._is_speaking:
            return

        # Log total TTS audio
        if self.call_log:
            tts_total_ms = (time.perf_counter() - tts_hit) * 1000
            self.call_log.event(
                "tts",
                turn=turn,
                total_ms=round(tts_total_ms),
                language=self.language or self._settings.DEFAULT_LANGUAGE,
                chars=len(text),
                audio_secs=round(len(total_audio) / 8000, 2),
            )
            if total_audio:
                agent_wav = pcm_to_wav_bytes(mulaw_to_pcm(bytes(total_audio)), 8000)
                self.call_log.save_audio(f"turn_{turn:03d}_agent.wav", agent_wav)

        # Twilio mark to track when playback finishes
        self._mark_counter += 1
        self._pending_mark = f"utterance-{self._mark_counter}"
        await self.ws.send_json({
            "event": "mark",
            "streamSid": self.stream_sid,
            "mark": {"name": self._pending_mark},
        })

    async def _interrupt_playback(self) -> None:
        """Stop agent speech: cancel generation and flush Twilio's audio buffer."""
        self._is_speaking = False
        if self._response_task and not self._response_task.done():
            self._response_task.cancel()
        if self.stream_sid:
            await self.ws.send_json({"event": "clear", "streamSid": self.stream_sid})

    async def _hangup_after_delay(self) -> None:
        """Wait for final audio to play, then hang up the call via Twilio REST."""
        try:
            await asyncio.sleep(HANGUP_DELAY_SECONDS)
            if self.call_sid:
                logger.info("Hanging up call: %s", self.call_sid)
                if self.call_log:
                    self.call_log.event("hangup", call_sid=self.call_sid)
                settings = self._settings
                client = TwilioClient(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
                client.calls(self.call_sid).update(status="completed")
        except Exception:
            logger.exception("Failed to hang up call")

    async def _cleanup(self) -> None:
        if self._response_task and not self._response_task.done():
            self._response_task.cancel()
        if self._hangup_task and not self._hangup_task.done():
            self._hangup_task.cancel()
        if self.call_log:
            self.call_log.event(
                "call_end",
                call_sid=self.call_sid,
                turns=self._turn,
                payment_agreed=self.dialog.payment_agreed if self.dialog else False,
            )
            self.call_log.close()
        logger.info("Call session cleaned up: call=%s", self.call_sid)
