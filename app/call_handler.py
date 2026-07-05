"""
WebSocket handler for Twilio Media Streams — the core real-time call loop.

Flow:
    Twilio mulaw audio
        → Silero VAD (local CPU)     — detects speech end
        → Faster-Whisper on Colab    — transcribes + detects language
        → Ollama Gemma 2 on Colab    — generates response in detected language
        → XTTS / IndicTTS on Colab   — synthesizes speech
        → Twilio mulaw audio
"""

import asyncio
import base64
import json
import logging

from fastapi import WebSocket

from app.asr import ASRClient
from app.llm import DialogEngine
from app.tts import synthesize_speech
from app.vad import VADBuffer

logger = logging.getLogger(__name__)


class CallHandler:
    """Manages a single active call session over Twilio Media Streams WebSocket."""

    def __init__(self, websocket: WebSocket, dialog_engine: DialogEngine):
        self.ws = websocket
        self.dialog = dialog_engine
        self.asr = ASRClient()

        self.stream_sid: str | None = None
        self.call_sid: str | None = None

        # VAD buffer — fires _on_speech_end when utterance is complete
        self.vad = VADBuffer(on_speech_end=self._on_speech_end)

        # Turn management
        self._is_speaking: bool = False       # True while agent TTS is streaming
        self._response_task: asyncio.Task | None = None

    async def handle(self) -> None:
        """Main loop: receive Twilio WS messages and dispatch."""
        try:
            # Send greeting after a brief pause
            asyncio.create_task(self._send_greeting())

            async for raw in self.ws.iter_text():
                msg = json.loads(raw)
                event = msg.get("event")

                if event == "start":
                    self.stream_sid = msg["start"]["streamSid"]
                    self.call_sid = msg["start"].get("callSid")
                    logger.info(
                        "Media stream started: stream=%s call=%s",
                        self.stream_sid,
                        self.call_sid,
                    )

                # elif event == "media":
                #     payload = msg["media"]["payload"]
                #     audio_bytes = base64.b64decode(payload)

                #     # Barge-in: if customer speaks while agent is talking, stop TTS
                #     if self._is_speaking:
                #         logger.info("Barge-in detected — stopping TTS")
                #         self._is_speaking = False
                #         await self._clear_audio()
                #         self.vad.reset()

                #     # Feed audio to local VAD
                #     self.vad.feed(audio_bytes)
                elif event == "media":
                    payload = msg["media"]["payload"]
                    audio_bytes = base64.b64decode(payload)

                    # Skip VAD processing while agent is speaking
                    if not self._is_speaking:
                        self.vad.feed(audio_bytes)

                elif event == "stop":
                    logger.info("Media stream stopped")
                    break

        except Exception:
            logger.exception("Error in call handler")
        finally:
            await self._cleanup()

    async def _on_speech_end(self, pcm_16k_bytes: bytes) -> None:
        """
        Called by VADBuffer when a complete customer utterance is detected.
        Runs: ASR → LLM → TTS pipeline.
        """
        logger.debug("VAD: utterance end, %d bytes of PCM audio", len(pcm_16k_bytes))

        # Cancel any previous in-flight response
        if self._response_task and not self._response_task.done():
            self._response_task.cancel()

        self._response_task = asyncio.create_task(
            self._process_utterance(pcm_16k_bytes)
        )

    async def _process_utterance(self, pcm_16k_bytes: bytes) -> None:
        """ASR → LLM → TTS for a single customer utterance."""
        try:
            # 1. Transcribe with language detection
            asr_result = await self.asr.transcribe(pcm_16k_bytes)
            transcript = asr_result.get("transcript", "").strip()
            language = asr_result.get("language", "hi")

            if not transcript:
                logger.info("ASR returned empty transcript — ignoring")
                return

            logger.info("Customer [%s]: %s", language, transcript)

            # 2. Pass detected language to dialog engine for context
            self.dialog.detected_language = language

            # 3. Generate LLM response
            response_text = await self.dialog.generate_response(transcript)
            logger.info("Agent [%s]: %s", language, response_text)

            # 4. Synthesize and stream TTS
            await self._stream_tts(response_text, language)

        except asyncio.CancelledError:
            logger.debug("Utterance processing cancelled (barge-in)")
            self._is_speaking = False
        except Exception:
            logger.exception("Error processing utterance")
            self._is_speaking = False

    async def _stream_tts(self, text: str, language: str) -> None:
        """Synthesize speech and stream audio chunks to Twilio."""
        self._is_speaking = True
        try:
            async for audio_chunk in synthesize_speech(text, language):
                if not self._is_speaking:
                    break  # Barge-in cancelled streaming
                await self._send_audio(audio_chunk)
        finally:
            self._is_speaking = False

    async def _send_greeting(self) -> None:
        """Send the opening agent greeting after the call connects."""
        await asyncio.sleep(1.0)
        greeting = await self.dialog.generate_greeting()
        logger.info("Agent greeting: %s", greeting)
        await self._stream_tts(greeting, self.dialog.detected_language)

    async def _send_audio(self, audio_payload: str) -> None:
        """Send a base64-encoded mulaw audio chunk to Twilio via WebSocket."""
        if not self.stream_sid:
            return
        await self.ws.send_json({
            "event": "media",
            "streamSid": self.stream_sid,
            "media": {"payload": audio_payload},
        })

    async def _clear_audio(self) -> None:
        """Tell Twilio to discard any queued audio (barge-in support)."""
        if self.stream_sid:
            await self.ws.send_json({
                "event": "clear",
                "streamSid": self.stream_sid,
            })

    async def _cleanup(self) -> None:
        """Clean up resources when the call ends."""
        if self._response_task and not self._response_task.done():
            self._response_task.cancel()
        logger.info("Call handler cleaned up: call=%s", self.call_sid)
