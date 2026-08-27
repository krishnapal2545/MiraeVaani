"""Per-call session over Twilio Media Streams: STT -> LLM -> TTS with barge-in.

Ported from v5. The pipeline, the energy-based turn detection and the barge-in
handling are unchanged — that logic took five versions to tune and is copied
verbatim. What changed is where configuration comes from: every threshold,
provider and prompt now arrives on an `AgentConfig` loaded from the database,
so one process serves many differently-configured agents.

New in v6: smart fillers. After STT returns we know what the caller said but the
LLM and TTS are still ahead, so a pre-synthesized filler is raced against the
real response and plays only if the response is slow.
"""

import asyncio
import base64
import json
import logging
import time
from typing import Any

from fastapi import WebSocket
from twilio.rest import Client as TwilioClient

from app import db
from app.audio import mulaw8k_to_wav16k, mulaw_frame_rms, mulaw_to_pcm, pcm_to_wav_bytes
from app.call_logger import CallLogger
from app.dialog import DialogEngine
from app.events import broker
from app.fillers import FillerController
from app.models import AgentConfig
from app.providers.base import STTProvider, TTSProvider

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
        *,
        agent: AgentConfig,
        stt: STTProvider,
        tts: TTSProvider,
        dialog: DialogEngine,
        fillers: FillerController | None,
        call_id: str,
        logs_dir: str,
        twilio_creds: dict[str, Any] | None = None,
    ) -> None:
        self.ws = websocket
        self.agent = agent
        self.stt = stt
        self.tts = tts
        self.dialog = dialog
        self.fillers = fillers
        self.call_id = call_id
        self._logs_dir = logs_dir
        self._twilio_creds = twilio_creds or {}

        self.stream_sid: str | None = None
        self.call_sid: str | None = None
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
        self._warm_task: asyncio.Task | None = None

        # Filler state
        self._filler_task: asyncio.Task | None = None
        self._filler_started = False
        self._filler_this_turn: str | None = None

    # ------------------------------------------------------------------
    # Event emission: disk log + live SSE + database
    # ------------------------------------------------------------------
    def _emit(self, event_type: str, **data: Any) -> None:
        if self.call_log:
            self.call_log.event(event_type, **data)
        broker.publish(self.call_id, event_type, data)

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

        self.call_log = CallLogger(self._logs_dir, self.call_sid or self.call_id)
        self._emit(
            "call_start",
            call_sid=self.call_sid,
            call_id=self.call_id,
            agent=self.agent.name,
            stt=f"{self.agent.stt_provider}/{self.agent.stt_model}",
            llm=f"{self.agent.llm_provider}/{self.agent.llm_model}",
            tts=f"{self.agent.tts_provider}/{self.agent.tts_voice}",
        )
        db.update_call(
            self.call_id,
            call_sid=self.call_sid,
            status="in_progress",
            started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )

        if self.agent.language_mode == "fixed":
            self.language = self.agent.language

        logger.info(
            "Stream started: call=%s agent=%s", self.call_sid, self.agent.name
        )

        # Warm the filler bank in the background while the greeting plays, so the
        # first filler is already cached by the time it might be needed.
        if self.fillers:
            self._warm_task = asyncio.create_task(self.fillers._bank.warm())

        self._response_task = asyncio.create_task(self._greet())

    # ------------------------------------------------------------------
    # Inbound audio: turn detection + barge-in  (unchanged from v5)
    # ------------------------------------------------------------------
    async def _on_media(self, chunk: bytes) -> None:
        duration = len(chunk) / 8000.0  # mu-law: 1 byte per sample @ 8kHz
        voiced = mulaw_frame_rms(chunk) >= self.agent.silence_threshold_rms

        if self._is_speaking:
            # Agent is talking — watch for a sustained interruption.
            if voiced:
                self._barge_secs += duration
                self._barge_buffer.extend(chunk)
                if self._barge_secs >= BARGE_IN_SECONDS:
                    logger.info("Barge-in detected — cancelling current response")
                    self._emit("barge_in", turn=self._turn)
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
            if self._silence_secs >= self.agent.silence_end_seconds:
                utterance = bytes(self._buffer)
                speech_secs = self._speech_secs
                self._reset_capture()
                if speech_secs >= self.agent.min_utterance_seconds:
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
    # STT -> (filler race) -> LLM -> TTS pipeline
    # ------------------------------------------------------------------
    async def _process_utterance(self, mulaw_utterance: bytes) -> None:
        try:
            self._turn += 1
            turn = self._turn
            turn_start = time.perf_counter()
            self._filler_this_turn = None

            # --- STT ---
            wav_bytes = mulaw8k_to_wav16k(mulaw_utterance)
            if self.call_log:
                self.call_log.save_audio(f"turn_{turn:03d}_user.wav", wav_bytes)

            stt_hit = time.perf_counter()
            stt_language = self.agent.language if self.agent.language_mode == "fixed" else None
            transcript, detected_language = await self.stt.transcribe(
                wav_bytes, stt_language
            )
            stt_done = time.perf_counter()
            stt_ms = (stt_done - stt_hit) * 1000

            self._emit(
                "stt",
                turn=turn,
                hit_at_ms=round((stt_hit - turn_start) * 1000),
                latency_ms=round(stt_ms),
                language=detected_language,
                customer_said=transcript,
                audio_secs=round(len(mulaw_utterance) / 8000, 2),
            )

            if not transcript:
                return

            self.language = self.agent.resolve_language(detected_language)

            # --- Smart filler: start the race against the real response ---
            self._start_filler(transcript, turn)

            # --- LLM ---
            llm_hit = time.perf_counter()
            reply = await self.dialog.respond(transcript, self.language)
            llm_done = time.perf_counter()
            llm_ms = (llm_done - llm_hit) * 1000

            self._emit(
                "llm",
                turn=turn,
                hit_at_ms=round((llm_hit - turn_start) * 1000),
                latency_ms=round(llm_ms),
                agent_reply=reply,
                outcome=self.dialog.signals.outcome,
                call_ended=self.dialog.signals.end_call,
            )
            logger.info("Agent [%s]: %s", self.language, reply)

            # Cancel the filler if it never started; let it finish if it did, so
            # the two never overlap.
            await self._settle_filler()

            # --- TTS + streaming playback ---
            tts_hit = time.perf_counter()
            await self._speak_streaming(reply, turn=turn, tts_hit=tts_hit)
            tts_done = time.perf_counter()
            tts_ms = (tts_done - tts_hit) * 1000

            total_ms = (time.perf_counter() - turn_start) * 1000
            self._emit(
                "turn_total",
                turn=turn,
                stt_ms=round(stt_ms),
                llm_ms=round(llm_ms),
                tts_ms=round(tts_ms),
                total_ms=round(total_ms),
                filler=self._filler_this_turn,
            )

            db.add_turn(
                self.call_id, turn, "user", transcript,
                language=detected_language, stt_ms=round(stt_ms),
            )
            db.add_turn(
                self.call_id, turn, "agent", reply,
                language=self.language, llm_ms=round(llm_ms),
                tts_ms=round(tts_ms), total_ms=round(total_ms),
                filler_played=self._filler_this_turn,
            )
            db.update_call(self.call_id, turns=turn)

            # --- Hangup if call ended ---
            if self.dialog.signals.end_call:
                self._hangup_task = asyncio.create_task(self._hangup_after_delay())

        except asyncio.CancelledError:
            logger.info("Turn %d cancelled (barge-in or newer utterance)", self._turn)
        except Exception:
            logger.exception("Error processing utterance")

    async def _greet(self) -> None:
        try:
            await asyncio.sleep(0.5)
            t0 = time.perf_counter()
            greeting = await self.dialog.generate_greeting(
                self.agent.greeting_text if self.agent.greeting_mode == "static" else ""
            )
            llm_ms = (time.perf_counter() - t0) * 1000
            self._emit("llm", turn=0, latency_ms=round(llm_ms), agent_reply=greeting)
            logger.info("Agent greeting: %s", greeting)

            tts_hit = time.perf_counter()
            await self._speak_streaming(greeting, turn=0, tts_hit=tts_hit)
            db.add_turn(self.call_id, 0, "agent", greeting,
                        language=self.language, llm_ms=round(llm_ms))
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Error sending greeting")

    # ------------------------------------------------------------------
    # Smart fillers
    # ------------------------------------------------------------------
    def _start_filler(self, transcript: str, turn: int) -> None:
        self._filler_started = False
        self._filler_task = None
        if not self.fillers:
            return
        choice = self.fillers.choose(transcript, self.language)
        if not choice:
            return
        category, audio = choice
        self._filler_task = asyncio.create_task(
            self._play_filler_after_delay(category, audio, turn)
        )

    async def _play_filler_after_delay(
        self, category: str, audio: bytes, turn: int
    ) -> None:
        """Play a cached filler only if the real response has not arrived yet."""
        try:
            await asyncio.sleep(self.agent.filler_delay_ms / 1000.0)
            # No await between here and the flag: the settle path cannot race it.
            self._filler_started = True
            self._filler_this_turn = category
            self._emit("filler", turn=turn, category=category,
                       audio_secs=round(len(audio) / 8000, 2))
            self._is_speaking = True
            await self._send_mulaw(audio)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Filler playback failed")

    async def _settle_filler(self) -> None:
        """Cancel a filler that has not started; wait for one that has."""
        task = self._filler_task
        self._filler_task = None
        if task is None or task.done():
            return
        if self._filler_started:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        else:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------
    # Outbound audio
    # ------------------------------------------------------------------
    async def _send_mulaw(self, audio: bytes) -> None:
        """Stream mu-law bytes to Twilio in 1-second chunks."""
        if not self.stream_sid:
            return
        for offset in range(0, len(audio), OUTBOUND_CHUNK_BYTES):
            if not self._is_speaking:
                return
            await self.ws.send_json({
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {
                    "payload": base64.b64encode(
                        audio[offset:offset + OUTBOUND_CHUNK_BYTES]
                    ).decode("ascii")
                },
            })

    async def _speak_streaming(self, text: str, turn: int = 0, tts_hit: float = 0) -> None:
        """Synthesize sentence-by-sentence and stream each to Twilio immediately."""
        if not self.stream_sid or not text.strip():
            return

        self._is_speaking = True
        self._barge_secs = 0.0
        self._barge_buffer.clear()

        total_audio = bytearray()
        first_byte_logged = False

        async for mulaw_chunk in self.tts.synthesize_streaming(text, self.language):
            if not self._is_speaking:
                return  # interrupted

            if not first_byte_logged:
                ttfb_ms = (time.perf_counter() - tts_hit) * 1000
                self._emit("tts_first_byte", turn=turn, ttfb_ms=round(ttfb_ms))
                first_byte_logged = True

            total_audio.extend(mulaw_chunk)
            await self._send_mulaw(mulaw_chunk)

        if not self._is_speaking:
            return

        if self.call_log:
            tts_total_ms = (time.perf_counter() - tts_hit) * 1000
            self._emit(
                "tts",
                turn=turn,
                total_ms=round(tts_total_ms),
                language=self.language or self.agent.language,
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
        if self._filler_task and not self._filler_task.done():
            self._filler_task.cancel()
        if self._response_task and not self._response_task.done():
            self._response_task.cancel()
        if self.stream_sid:
            await self.ws.send_json({"event": "clear", "streamSid": self.stream_sid})

    async def _hangup_after_delay(self) -> None:
        """Wait for final audio to play, then hang up the call via Twilio REST."""
        try:
            await asyncio.sleep(HANGUP_DELAY_SECONDS)
            if self.call_sid and self._twilio_creds:
                logger.info("Hanging up call: %s", self.call_sid)
                self._emit("hangup", call_sid=self.call_sid)
                client = TwilioClient(
                    self._twilio_creds.get("account_sid"),
                    self._twilio_creds.get("auth_token"),
                )
                client.calls(self.call_sid).update(status="completed")
        except Exception:
            logger.exception("Failed to hang up call")

    async def _cleanup(self) -> None:
        for task in (self._response_task, self._hangup_task, self._filler_task,
                     self._warm_task):
            if task and not task.done():
                task.cancel()

        signals = self.dialog.signals
        self._emit(
            "call_end",
            call_sid=self.call_sid,
            turns=self._turn,
            outcome=signals.outcome,
        )
        db.update_call(
            self.call_id,
            status="completed",
            ended_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            turns=self._turn,
            outcome=signals.outcome,
            outcome_summary=signals.outcome_summary,
            log_dir=str(self.call_log.dir) if self.call_log else None,
        )

        if self.call_log:
            self.call_log.close()

        for provider in (self.stt, self.tts):
            try:
                await provider.close()
            except Exception:
                pass

        logger.info("Call session cleaned up: call=%s", self.call_sid)
