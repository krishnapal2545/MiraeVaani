"""Per-call session over Twilio Media Streams: STT -> LLM -> TTS with barge-in.

Configuration arrives on an `AgentConfig` loaded from the database, so one
process serves many differently-configured agents.

Smart fillers: after STT returns we know what the caller said but the LLM and
TTS are still ahead, so a pre-synthesized filler is raced against the real
response and plays only if the response is slow.

Turn detection was rewritten after call recordings showed the two failure modes
that dominate a real line, both of which came from treating "RMS over 300" as
"the caller is speaking":

- **Background voices became turns.** A television or a second conversation in
  the room cleared the threshold, opened a capture, and was transcribed and
  answered. Frames are now classified against a floor that tracks the room
  (`app/vad.py`), and a captured utterance whose peak never rose clearly above
  that floor is dropped before it costs an STT call.
- **Anything cancelled the agent.** 150 ms above threshold ended the agent's
  sentence, so a cough, a door, or line echo of the agent's own voice cut it
  off mid-word. Barge-in now needs sustained speech and is deaf for a short
  grace period after the agent starts talking.

The third artefact — the agent changing language mid-call — was not a turn
detection problem but a consequence of one: short noise captures got a language
guess, and the guess was believed. `app/language.py` now debounces that.

Suppressing noise created a failure of its own, and it is the one callers
actually notice: an answer that gets discarded leaves *both* sides silent. The
caller says "hello" into a working line, hears nothing, says it again, and the
agent — which knows only that no turn arrived — waits. So the agent now behaves
the way a person does when a line goes quiet: after `no_reply_seconds` it asks
whether it can be heard, and if audio was heard and thrown away during the wait
it asks the caller to speak up instead. Those lines are synthesized directly
rather than generated, because a check-in that takes two seconds to think is not
a check-in, but they are written into the dialog history so the model knows it
said them.
"""

import asyncio
import base64
import json
import logging
import time
from collections import deque
from typing import Any

from fastapi import WebSocket
from twilio.rest import Client as TwilioClient

from app import db
from app.audio import mulaw8k_to_wav16k, mulaw_frame_rms, mulaw_to_pcm, pcm_to_wav_bytes
from app.call_logger import CallLogger
from app.dialog import DialogEngine
from app.events import broker
from app.fillers import FillerController
from app.language import (
    LanguageTracker,
    conflicting_script,
    language_for_script,
    parse_allowed,
)
from app.models import AgentConfig
from app.prompts import check_in_line, no_reply_goodbye
from app.providers.base import STTProvider, TTSProvider
from app.vad import VoiceActivityDetector

logger = logging.getLogger(__name__)

# Outbound audio is sent to Twilio in chunks of this many bytes (1s @ 8kHz mu-law).
OUTBOUND_CHUNK_BYTES = 8000
# Delay before hanging up after goodbye (let final audio play).
HANGUP_DELAY_SECONDS = 3.0
# Frames kept before a capture opens, so the hysteresis that suppresses noise
# does not also clip the first consonant off every utterance.
PREROLL_FRAMES = 8

# What a transcript of background noise looks like coming back from STT: an
# ellipsis, a lone punctuation mark, or a single vocalisation. These are not
# turns, and answering them is how the agent ends up talking to an empty room.
_NOISE_TRANSCRIPTS = {
    "...", "…", ".", ",", "?", "!", "।", "-", "--",
    "hmm", "hm", "mm", "mmm", "uh", "uhh", "um", "ah", "aah", "eh",
    "हम्म", "हूँ", "हूं", "अं", "अँ", "हं", "म्म",
    # Caption-trained models emit these for near-silence.
    "thanks for watching", "thank you for watching", "subscribe",
}


def is_noise_transcript(transcript: str) -> bool:
    """True when STT returned something no caller actually said.

    The `thanks for watching` entries are not a joke: STT models trained on
    video captions emit them for near-silence, and they arrive looking exactly
    like a real turn.
    """
    text = transcript.strip().strip(".,!?।-–— ").lower()
    if not text:
        return True
    return text in _NOISE_TRANSCRIPTS


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
        self._vad = VoiceActivityDetector(
            base_threshold_rms=agent.silence_threshold_rms,
            noise_margin=agent.noise_margin,
        )
        self._buffer = bytearray()
        self._preroll: deque[bytes] = deque(maxlen=PREROLL_FRAMES)
        self._capturing = False
        self._speech_secs = 0.0
        self._silence_secs = 0.0

        # Language stability across the call
        self._language_tracker = LanguageTracker(
            agent.language,
            mode=agent.language_mode,
            allowed=parse_allowed(agent.allowed_languages),
            confirmations=agent.language_switch_turns,
            min_seconds=agent.language_switch_min_seconds,
        )

        # Agent playback / barge-in state
        self._is_speaking = False
        self._speaking_since = 0.0
        self._mark_counter = 0
        self._pending_mark: str | None = None
        self._barge_secs = 0.0
        self._barge_buffer = bytearray()

        self._response_task: asyncio.Task | None = None
        self._hangup_task: asyncio.Task | None = None
        self._warm_task: asyncio.Task | None = None

        # "Can you hear me?" state. `_rejected_since_reply` is what tells the
        # two silences apart: a line with nobody on it, and a caller whose voice
        # is being thrown away by the energy gate.
        self._watchdog_task: asyncio.Task | None = None
        self._rejected_since_reply = 0
        self._no_reply_prompts = 0
        self._closing = False

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
                        # Drop the peak accumulated from the agent's own echo,
                        # so the next utterance is judged on the caller alone —
                        # unless a capture is open, in which case those numbers
                        # belong to the caller and wiping them judges their
                        # utterance as mean=0, peak=0 and throws it away.
                        if not self._capturing:
                            self._vad.reset_peak()
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

        # Start on the agent's own language rather than None, so the greeting is
        # synthesized with the right voice instead of a provider fallback.
        self.language = self._language_tracker.current

        logger.info(
            "Stream started: call=%s agent=%s", self.call_sid, self.agent.name
        )

        # Warm the filler bank in the background while the greeting plays, so the
        # first filler is already cached by the time it might be needed.
        if self.fillers:
            self._warm_task = asyncio.create_task(self.fillers.bank.warm())

        self._response_task = asyncio.create_task(self._greet())

    # ------------------------------------------------------------------
    # Inbound audio: turn detection + barge-in  (unchanged from v5)
    # ------------------------------------------------------------------
    async def _on_media(self, chunk: bytes) -> None:
        duration = len(chunk) / 8000.0  # mu-law: 1 byte per sample @ 8kHz
        # The agent's own audio echoes back while it speaks; it must not teach
        # the detector how loud the room is.
        voiced = self._vad.observe(mulaw_frame_rms(chunk), learn=not self._is_speaking)

        if self._is_speaking:
            await self._on_media_while_speaking(chunk, duration, voiced)
            return

        if voiced:
            if not self._capturing:
                # Replay the pre-roll so the attack frames the VAD spent
                # deciding are still in the audio sent to STT.
                self._capturing = True
                for frame in self._preroll:
                    self._buffer.extend(frame)
                self._preroll.clear()
            self._buffer.extend(chunk)
            self._speech_secs += duration
            self._silence_secs = 0.0
            return

        if not self._capturing:
            self._preroll.append(chunk)
            return

        self._buffer.extend(chunk)
        self._silence_secs += duration
        if self._silence_secs < self.agent.silence_end_seconds:
            return

        utterance = bytes(self._buffer)
        speech_secs = self._speech_secs
        self._reset_capture()

        verdict = self._vad.judge(speech_secs, self.agent.min_utterance_seconds)
        if not verdict.accept:
            # Evidence that someone is talking into a line we are not letting
            # through. The watchdog reads this to decide what to say.
            self._rejected_since_reply += 1
            # The single highest-value log line for tuning a noisy deployment:
            # it says what was heard, how loud the room is, and why it lost.
            logger.info(
                "Discarded capture (%s): mean=%d peak=%d threshold=%d floor=%d "
                "speech=%.2fs",
                verdict.reason, verdict.mean_rms, verdict.peak_rms,
                verdict.threshold, verdict.noise_floor, verdict.speech_secs,
            )
            self._emit(
                "noise_rejected",
                turn=self._turn,
                reason=verdict.reason,
                peak_rms=verdict.peak_rms,
                mean_rms=verdict.mean_rms,
                threshold=verdict.threshold,
                noise_floor=verdict.noise_floor,
                speech_secs=verdict.speech_secs,
            )
            return

        if verdict.reason:
            logger.info(
                "Kept a short utterance (%s): mean=%d peak=%d threshold=%d "
                "speech=%.2fs",
                verdict.reason, verdict.mean_rms, verdict.peak_rms,
                verdict.threshold, verdict.speech_secs,
            )

        # The caller is being heard; nothing to check in about.
        self._cancel_watchdog()
        self._rejected_since_reply = 0
        self._no_reply_prompts = 0

        # Cancel any old in-flight response — only the latest question matters.
        if self._response_task and not self._response_task.done():
            self._response_task.cancel()
            try:
                await self._response_task
            except (asyncio.CancelledError, Exception):
                pass
        self._response_task = asyncio.create_task(
            self._process_utterance(utterance, verdict.speech_secs)
        )

    async def _on_media_while_speaking(
        self, chunk: bytes, duration: float, voiced: bool
    ) -> None:
        """Watch for a genuine interruption while the agent is talking.

        Two guards that v6.0 lacked. The grace period covers the line echoing
        the agent's own first words back, and the caller's trailing "haan" from
        the previous turn. The longer sustained-speech requirement is what
        separates a person deciding to interrupt from a cough or a door.
        """
        if not voiced:
            self._barge_secs = 0.0
            self._barge_buffer.clear()
            return

        if time.perf_counter() - self._speaking_since < self.agent.barge_in_grace_seconds:
            return

        self._barge_secs += duration
        self._barge_buffer.extend(chunk)
        if self._barge_secs < self.agent.barge_in_seconds:
            return

        logger.info("Barge-in detected — cancelling current response")
        self._emit("barge_in", turn=self._turn, after_secs=round(self._barge_secs, 2))
        await self._interrupt_playback()
        # Carry the interrupting audio straight into the capture: it is the
        # start of what the caller is saying.
        self._buffer = bytearray(self._barge_buffer)
        self._speech_secs = self._barge_secs
        self._silence_secs = 0.0
        self._capturing = True
        self._barge_secs = 0.0
        self._barge_buffer.clear()

    def _reset_capture(self) -> None:
        self._buffer = bytearray()
        self._preroll.clear()
        self._capturing = False
        self._speech_secs = 0.0
        self._silence_secs = 0.0

    # ------------------------------------------------------------------
    # STT -> (filler race) -> LLM -> TTS pipeline
    # ------------------------------------------------------------------
    async def _process_utterance(
        self, mulaw_utterance: bytes, voiced_secs: float = 0.0
    ) -> None:
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
            # Voiced seconds, not clip length: the trailing silence that ends a
            # turn must not make a one-word clip look like a confident sample.
            speech_secs = voiced_secs or len(mulaw_utterance) / 8000

            self._emit(
                "stt",
                turn=turn,
                hit_at_ms=round((stt_hit - turn_start) * 1000),
                latency_ms=round(stt_ms),
                language=detected_language,
                customer_said=transcript,
                audio_secs=round(speech_secs, 2),
            )

            if not transcript or is_noise_transcript(transcript):
                # Room noise that survived the energy gate but transcribed to
                # nothing a person actually said. Answering it is what makes an
                # agent talk over an empty room. Nobody is going to speak next
                # unless the agent does, so the silence watch goes back on —
                # and this counts as a sound we heard and threw away, which is
                # what makes the check-in ask them to speak up.
                if transcript:
                    logger.info("Discarded noise transcript: %r", transcript)
                    self._emit("noise_rejected", turn=turn,
                               reason="empty_transcript", transcript=transcript)
                self._rejected_since_reply += 1
                self._arm_watchdog()
                return

            # --- Language: follow detection, but only once corroborated ---
            decision = self._language_tracker.update(
                detected_language, speech_secs, transcript
            )
            self.language = decision.language
            if decision.ignored:
                logger.info(
                    "Keeping %s this turn — ignored %s", self.language, decision.ignored
                )
                self._emit("language_held", turn=turn, language=self.language,
                           detected=detected_language, reason=decision.ignored)
            if decision.switched:
                self._emit("language_switched", turn=turn, language=self.language)
                if self.fillers:
                    # Warm the new language in the background so later turns get
                    # a filler in the language the agent is now speaking.
                    self.fillers.bank.ensure_language(self.language)

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
                self.agent.greeting_text if self.agent.greeting_mode == "static" else "",
                self.language,
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
    # "Can you hear me?" — the caller's answer never arrived
    # ------------------------------------------------------------------
    def _arm_watchdog(self) -> None:
        """Start counting silence once the agent has finished a line."""
        self._cancel_watchdog()
        if (
            self.agent.no_reply_seconds <= 0
            or self.dialog.signals.end_call
            or self._closing
        ):
            return
        self._watchdog_task = asyncio.create_task(self._watch_for_reply())

    def _cancel_watchdog(self) -> None:
        task = self._watchdog_task
        self._watchdog_task = None
        # The watchdog speaks, and speaking re-arms: it must not cancel itself
        # mid-sentence on the way through.
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _watch_for_reply(self) -> None:
        """Speak up when nothing comes back, instead of waiting in silence.

        A person who gets no answer says "hello, are you there?" — they do not
        stand mute and then carry on with the next point of their script. This
        is the same behaviour, and it is what covers the failure the recordings
        show: the caller answers, the energy gate discards the answer as too
        short or too quiet, and the call goes dead in both directions while the
        caller repeats "hello" into a working line.

        Rejected captures during the wait say the caller *is* talking, so the
        prompt asks them to speak up rather than asking whether they are there.
        After `no_reply_prompts` unanswered check-ins the call is closed
        properly rather than left open until Twilio times it out.
        """
        try:
            # Playback is only really over when Twilio returns the mark.
            while self._is_speaking:
                await asyncio.sleep(0.2)
            await asyncio.sleep(self.agent.no_reply_seconds)

            # A turn that arrived in the meantime is the answer; nothing to do.
            if self._is_speaking or (
                self._response_task and not self._response_task.done()
            ):
                return

            faint = self._rejected_since_reply > 0
            self._no_reply_prompts += 1
            giving_up = self._no_reply_prompts > self.agent.no_reply_prompts
            line = (
                no_reply_goodbye(self.language)
                if giving_up
                else check_in_line(self.language, faint=faint)
            )

            logger.info(
                "No reply for %.1fs (%d rejected captures) — %s",
                self.agent.no_reply_seconds, self._rejected_since_reply,
                "closing the call" if giving_up else f"asking: {line}",
            )
            self._emit(
                "no_reply",
                turn=self._turn,
                waited_secs=self.agent.no_reply_seconds,
                rejected=self._rejected_since_reply,
                attempt=self._no_reply_prompts,
                reason="faint" if faint else "silence",
                spoken=line,
                closing=giving_up,
            )
            self._rejected_since_reply = 0
            self._closing = giving_up

            # The model has to know it said this, or its next reply answers a
            # question it does not know was asked.
            self.dialog.note_agent_line(line)
            await self._speak_streaming(line, turn=self._turn, tts_hit=time.perf_counter())

            if giving_up:
                self._hangup_task = asyncio.create_task(self._hangup_after_delay())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("No-reply check failed")

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
            self._speaking_since = time.perf_counter()
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

    def _voice_language_for(self, text: str) -> str | None:
        """The language to synthesize in, when the reply's script disagrees.

        The model is told which language to answer in and mostly obeys, but
        "mostly" is not good enough here: a Hindi sentence handed to an English
        voice is not a wrong accent, it is unintelligible noise on the line —
        which is exactly what the caller heard for six turns after the call
        switched to English. Where the text and the label disagree, the text is
        the thing that has to be pronounced, so the text wins.
        """
        target = self.language or self.agent.language
        script = conflicting_script(text, target)
        # Latin is left alone on purpose. The correction is asymmetric because
        # the engines are: an Indian-language voice reads romanised text with an
        # accent, which is fine, while an English voice given Devanagari reads
        # nothing recognisable at all. Only the second case is worth overriding,
        # and skipping the first keeps a romanised Hindi line — the fallback
        # reply among them — on the voice the agent was configured with.
        if not script or script == "latin":
            return target

        spoken = language_for_script(script, self.agent.language, target)
        if not spoken or spoken == target:
            return target
        logger.info(
            "Reply is %s though the call is in %s — speaking it as %s",
            script, target, spoken,
        )
        self._emit("voice_language_corrected", turn=self._turn,
                   call_language=target, spoken_language=spoken, script=script)
        return spoken

    async def _speak_streaming(self, text: str, turn: int = 0, tts_hit: float = 0) -> None:
        """Synthesize sentence-by-sentence and stream each to Twilio immediately."""
        if not self.stream_sid or not text.strip():
            return

        speak_language = self._voice_language_for(text)
        self._is_speaking = True
        self._speaking_since = time.perf_counter()
        self._barge_secs = 0.0
        self._barge_buffer.clear()
        # A capture still open when the agent starts talking is stale — it was
        # opened before this reply existed, and the caller has now been talked
        # over. Dropping it here is not the loss it looks like: leaving it open
        # was worse, because `reset_peak` below wipes the levels it will later
        # be judged on, and it then fails the gate as silence it never was.
        self._reset_capture()
        self._vad.reset_peak()

        total_audio = bytearray()
        first_byte_logged = False

        async for mulaw_chunk in self.tts.synthesize_streaming(text, speak_language):
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
                language=speak_language or self.agent.language,
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

        # The agent has said its piece; from here the caller owes an answer.
        self._arm_watchdog()

    async def _interrupt_playback(self) -> None:
        """Stop agent speech: cancel generation and flush Twilio's audio buffer."""
        self._is_speaking = False
        self._cancel_watchdog()
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
                     self._warm_task, self._watchdog_task):
            if task and not task.done():
                task.cancel()
        if self.fillers:
            # Languages warmed on demand mid-call have their own tasks, which
            # would otherwise outlive the session and synthesize into nothing.
            self.fillers.bank.cancel_warming()

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
