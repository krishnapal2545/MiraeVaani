"""Energy-based voice activity detection with an adaptive noise floor.

Up to v6.0 turn detection was one line: any 20 ms frame whose RMS cleared a
fixed number counted as speech. On a real phone line that number is not what
separates the caller from the room — a television, a second conversation, road
noise and handset rustle all clear 300 RMS comfortably. Every such frame opened
a capture and shipped whatever followed to STT, and 150 ms of it was enough to
cancel the agent mid-sentence.

Three mechanisms replace it, and all three are load-bearing:

- **An adaptive floor.** What matters is not an absolute level, it is "louder
  than this room". The floor tracks quiet frames with a slow EMA and the speech
  threshold rides on top of it, so a noisy line self-calibrates within a second
  or two instead of needing the RMS knob retuned per call.
- **Hysteresis.** Speech must persist for several consecutive frames before a
  capture opens, and the threshold to *stay* open is lower than the one to open
  it, so ordinary pauses inside a sentence do not chop the utterance in two.
- **A whole-utterance gate.** Even after capture, an utterance whose peak never
  rose clearly above the floor is background and is dropped before STT. This is
  what stops a distant voice from becoming a turn.

The detector is deliberately stateful per call and knows nothing about buffers
or WebSockets; `call_handler` owns those.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# The floor is a low percentile of a rolling window rather than an average of
# "quiet" frames. An average has a fatal circularity: on a line noisy enough to
# clear the threshold, every frame is classified as speech, no frame is ever
# fed to the average, and the floor stays at whatever it was seeded with —
# which is exactly the deployment that needs the measurement most. A percentile
# does not care how the frames were classified.
FLOOR_WINDOW_FRAMES = 500  # 10s at 20ms per frame
FLOOR_PERCENTILE = 0.15
# Recomputing on every frame would sort a 500-element list 50 times a second
# per call for no benefit; the room does not change that fast.
FLOOR_RECOMPUTE_FRAMES = 25
# The floor is never allowed below this (a digitally silent line would drive
# every threshold to zero) nor above this (one loud burst must not deafen us).
FLOOR_MIN = 20.0
FLOOR_MAX = 900.0
# Staying in speech is easier than entering it — classic Schmitt trigger.
RELEASE_RATIO = 0.65
# An accepted utterance is measured against the live open threshold, not the
# raw floor. Background that gets captured at all is background that just
# cleared the threshold, so it sits *at* it; the person holding the phone sits
# far above it. Mean does the work — one loud frame can spike any peak — and
# the peak check only rejects the pathological case of a single clipped bang.
ACCEPT_MEAN_MARGIN = 1.5
ACCEPT_PEAK_MARGIN = 2.0

# A short utterance is not automatically noise. "Hello", "हाँ", "yes" are two or
# three tenths of a second of speech, and a caller says them constantly — most
# often at exactly the moment they are checking whether the agent can hear them
# at all. Discarding those is how a call goes quiet while the caller repeats
# "hello" into a line that is working perfectly. So a clip under
# `min_speech_secs` gets a second look, and is kept when it is unmistakably a
# person speaking into the handset rather than a room behind one: loud on
# average *and* with a peak far above the threshold. Below SHORT_WORD_MIN_SECS
# there are too few frames for either number to mean anything.
# The peak margin is the one doing the work — a handset held to a mouth peaks
# several times higher than anything in the room behind it — so the mean margin
# only has to stay above the full-length gate's 1.5, not duplicate the test. Set
# equal to 2.0 it rejected a real "Hello" that measured 1.9.
SHORT_WORD_MIN_SECS = 0.15
SHORT_WORD_MEAN_MARGIN = 1.75
SHORT_WORD_PEAK_MARGIN = 4.0


@dataclass
class UtteranceGate:
    """Verdict on a captured utterance, before it costs an STT call."""

    accept: bool
    # Why it was rejected, or — on an accept — the exception that saved it.
    reason: str = ""
    peak_rms: int = 0
    mean_rms: int = 0
    noise_floor: int = 0
    threshold: int = 0
    speech_secs: float = 0.0


class VoiceActivityDetector:
    """Frame-by-frame speech/silence classification for one call.

    `base_threshold_rms` stays as the absolute floor of the floor: the adaptive
    threshold can rise above it on a noisy line but never sinks below it, so an
    existing agent's tuning is still honoured as a minimum.
    """

    def __init__(
        self,
        base_threshold_rms: int = 300,
        noise_margin: float = 2.0,
        attack_frames: int = 3,
    ) -> None:
        self._base = max(int(base_threshold_rms), 1)
        self._margin = max(float(noise_margin), 1.1)
        self._attack_frames = max(int(attack_frames), 1)

        self._floor = min(max(self._base / 3.0, FLOOR_MIN), FLOOR_MAX)
        self._window: deque[int] = deque(maxlen=FLOOR_WINDOW_FRAMES)
        self._since_recompute = 0
        self._in_speech = False
        self._attack = 0
        self._peak = 0
        self._voiced_sum = 0
        self._voiced_frames = 0

    # ------------------------------------------------------------------
    # Thresholds
    # ------------------------------------------------------------------
    @property
    def noise_floor(self) -> int:
        return int(self._floor)

    @property
    def open_threshold(self) -> int:
        """Level a frame must clear to start counting towards speech."""
        return int(max(self._base, self._floor * self._margin))

    @property
    def close_threshold(self) -> int:
        """Level speech must fall below to end. Lower, so pauses don't cut."""
        return int(max(self._floor * 1.25, self.open_threshold * RELEASE_RATIO))

    # ------------------------------------------------------------------
    # Frame classification
    # ------------------------------------------------------------------
    def observe(self, rms: int, learn: bool = True) -> bool:
        """Classify one frame, updating the floor. Returns True if voiced.

        Pass `learn=False` while the agent is speaking: that audio is the
        agent's own voice echoing back down the line, and letting it into the
        window would teach the detector that the room is as loud as the agent.
        """
        if learn:
            self._window.append(rms)
            self._since_recompute += 1
            if self._since_recompute >= FLOOR_RECOMPUTE_FRAMES:
                self._since_recompute = 0
                self._recompute_floor()

        threshold = self.close_threshold if self._in_speech else self.open_threshold

        if rms >= threshold:
            self._attack += 1
            self._peak = max(self._peak, rms)
            self._voiced_sum += rms
            self._voiced_frames += 1
            if self._attack >= self._attack_frames:
                self._in_speech = True
            return self._in_speech

        self._attack = 0
        self._in_speech = False
        return False

    def _recompute_floor(self) -> None:
        if len(self._window) < FLOOR_RECOMPUTE_FRAMES:
            return
        ordered = sorted(self._window)
        index = min(int(len(ordered) * FLOOR_PERCENTILE), len(ordered) - 1)
        self._floor = min(max(float(ordered[index]), FLOOR_MIN), FLOOR_MAX)

    # ------------------------------------------------------------------
    # Utterance-level gate
    # ------------------------------------------------------------------
    def judge(self, speech_secs: float, min_speech_secs: float) -> UtteranceGate:
        """Decide whether a captured utterance is worth an STT call.

        Resets the measurement either way: this ends the utterance from the
        detector's point of view.
        """
        peak = self._peak
        mean = self._voiced_sum // self._voiced_frames if self._voiced_frames else 0
        floor = int(self._floor)
        threshold = self.open_threshold
        self.reset_peak()

        def gate(accept: bool, reason: str = "") -> UtteranceGate:
            return UtteranceGate(
                accept, reason, peak, mean, floor, threshold, round(speech_secs, 2)
            )

        if speech_secs < min_speech_secs:
            if (
                speech_secs >= SHORT_WORD_MIN_SECS
                and mean >= threshold * SHORT_WORD_MEAN_MARGIN
                and peak >= threshold * SHORT_WORD_PEAK_MARGIN
            ):
                # Short, but far too loud to be the room: a one-word answer.
                return gate(True, "short_word")
            return gate(False, "too_short")

        # Loud enough to trip frames, never loud enough to be the person
        # holding the phone. This is the background-voice case.
        if mean < threshold * ACCEPT_MEAN_MARGIN:
            return gate(False, "below_noise_margin")
        if peak < threshold * ACCEPT_PEAK_MARGIN:
            return gate(False, "no_speech_peak")

        return gate(True)

    def reset_peak(self) -> None:
        self._peak = 0
        self._voiced_sum = 0
        self._voiced_frames = 0
