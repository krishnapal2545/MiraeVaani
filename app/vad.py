"""
Silero VAD — Voice Activity Detection running locally on CPU.

Buffers raw mulaw audio from Twilio, detects speech end,
and emits complete utterance audio chunks for STT processing.
"""

import asyncio
import audioop
import logging
from typing import Callable, Optional

import numpy as np
import torch

logger = logging.getLogger(__name__)

# Silero VAD model (downloaded once on first use, ~2MB)
_vad_model: Optional[torch.jit.ScriptModule] = None
_vad_utils = None


def _load_vad_model():
    """Load Silero VAD model (cached after first load)."""
    global _vad_model, _vad_utils
    if _vad_model is None:
        logger.info("Loading Silero VAD model...")
        model, utils = torch.hub.load(
            repo_or_dir="snakers4/silero-vad",
            model="silero_vad",
            force_reload=False,
            onnx=False,
        )
        _vad_model = model
        _vad_utils = utils
        logger.info("Silero VAD model loaded")
    return _vad_model, _vad_utils


class VADBuffer:
    """
    Accumulates Twilio mulaw audio, runs Silero VAD on 30ms frames,
    and fires on_speech_end(pcm_16khz_bytes) when silence is detected.

    Twilio sends 8kHz mulaw. Silero VAD needs 16kHz PCM float32.
    """

    # VAD hyperparameters
    SPEECH_THRESHOLD = 0.5          # Probability threshold to count as speech
    SILENCE_FRAMES_TO_END = 15      # ~480ms of silence (15 × 32ms) = end of utterance
    MIN_SPEECH_FRAMES = 5           # At least 160ms of speech to count as valid
    FRAME_MS = 32                   # VAD frame size in milliseconds
    SAMPLE_RATE = 16000             # Silero VAD requires 16kHz
    FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_MS / 1000)  # 512 samples per frame

    def __init__(self, on_speech_end: Callable[[bytes], asyncio.Future]):
        """
        Args:
            on_speech_end: async callback(pcm_16khz_bytes) called when
                           a complete utterance is detected.
        """
        self.on_speech_end = on_speech_end
        self._model, _ = _load_vad_model()
        self._model.eval()

        # Internal state
        self._pcm_buffer: bytearray = bytearray()   # 16kHz PCM accumulation
        self._speech_buffer: bytearray = bytearray() # current utterance audio
        self._is_speaking: bool = False
        self._silence_frame_count: int = 0
        self._speech_frame_count: int = 0
        self._frame_buffer: bytearray = bytearray()  # partial frame accumulator

    def feed(self, mulaw_chunk: bytes) -> None:
        """
        Feed a chunk of raw mulaw audio (8kHz) from Twilio.
        This is synchronous — VAD runs inline.
        """
        # 1. Decode mulaw → 16-bit PCM at 8kHz
        pcm_8k = audioop.ulaw2lin(mulaw_chunk, 2)

        # 2. Upsample 8kHz → 16kHz (simple 2× repeat, good enough for VAD)
        pcm_8k_array = np.frombuffer(pcm_8k, dtype=np.int16)
        pcm_16k_array = np.repeat(pcm_8k_array, 2)
        pcm_16k = pcm_16k_array.astype(np.int16).tobytes()

        # 3. Accumulate into frame buffer
        self._frame_buffer.extend(pcm_16k)

        # 4. Process complete VAD frames
        frame_bytes = self.FRAME_SAMPLES * 2  # 2 bytes per int16 sample
        while len(self._frame_buffer) >= frame_bytes:
            frame = bytes(self._frame_buffer[:frame_bytes])
            self._frame_buffer = self._frame_buffer[frame_bytes:]
            self._process_frame(frame)

    def _process_frame(self, frame_bytes: bytes) -> None:
        """Run VAD on a single 30ms frame."""
        # Convert to float32 tensor in [-1, 1] range
        samples = np.frombuffer(frame_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        tensor = torch.from_numpy(samples)

        with torch.no_grad():
            speech_prob = self._model(tensor, self.SAMPLE_RATE).item()

        is_speech = speech_prob >= self.SPEECH_THRESHOLD

        if is_speech:
            self._speech_frame_count += 1
            self._silence_frame_count = 0
            self._is_speaking = True
            self._speech_buffer.extend(frame_bytes)
        else:
            if self._is_speaking:
                self._silence_frame_count += 1
                self._speech_buffer.extend(frame_bytes)  # include trailing silence

                if self._silence_frame_count >= self.SILENCE_FRAMES_TO_END:
                    if self._speech_frame_count >= self.MIN_SPEECH_FRAMES:
                        # Valid utterance detected — fire callback
                        utterance = bytes(self._speech_buffer)
                        asyncio.create_task(self.on_speech_end(utterance))
                        logger.debug(
                            "VAD: speech end — %d frames speech, %d frames silence",
                            self._speech_frame_count,
                            self._silence_frame_count,
                        )

                    # Reset state
                    self._speech_buffer.clear()
                    self._is_speaking = False
                    self._speech_frame_count = 0
                    self._silence_frame_count = 0

    def reset(self) -> None:
        """Reset all buffers (e.g. on barge-in)."""
        self._frame_buffer.clear()
        self._speech_buffer.clear()
        self._is_speaking = False
        self._silence_frame_count = 0
        self._speech_frame_count = 0
