"""Audio helpers: Twilio 8kHz mu-law <-> PCM WAV conversions and energy detection.

Twilio Media Streams carry 8kHz 8-bit mu-law mono audio.
Sarvam STT works best with 16kHz 16-bit PCM WAV.
Sarvam TTS is asked for 8kHz WAV so output maps straight back to mu-law.
"""

import audioop
import io
import wave


def mulaw_to_pcm(mulaw_bytes: bytes) -> bytes:
    """8kHz mu-law -> 8kHz 16-bit linear PCM."""
    return audioop.ulaw2lin(mulaw_bytes, 2)


def pcm_to_mulaw(pcm_bytes: bytes) -> bytes:
    """16-bit linear PCM -> mu-law (same sample rate)."""
    return audioop.lin2ulaw(pcm_bytes, 2)


def resample_pcm(pcm_bytes: bytes, from_rate: int, to_rate: int) -> bytes:
    """Resample 16-bit mono PCM between sample rates."""
    if from_rate == to_rate:
        return pcm_bytes
    converted, _ = audioop.ratecv(pcm_bytes, 2, 1, from_rate, to_rate, None)
    return converted


def pcm_to_wav_bytes(pcm_bytes: bytes, sample_rate: int = 16000) -> bytes:
    """Wrap 16-bit mono PCM in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm_bytes)
    return buf.getvalue()


def wav_bytes_to_pcm(wav_bytes: bytes) -> tuple[bytes, int]:
    """Extract 16-bit mono PCM and sample rate from a WAV container."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        rate = wav.getframerate()
        pcm = wav.readframes(wav.getnframes())
        if wav.getsampwidth() != 2:
            pcm = audioop.lin2lin(pcm, wav.getsampwidth(), 2)
        if wav.getnchannels() == 2:
            pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
    return pcm, rate


def mulaw_frame_rms(mulaw_bytes: bytes) -> int:
    """RMS energy of a mu-law frame (used for speech/silence detection)."""
    return audioop.rms(mulaw_to_pcm(mulaw_bytes), 2)


def mulaw8k_to_wav16k(mulaw_bytes: bytes) -> bytes:
    """Full inbound pipeline: Twilio mu-law 8kHz -> 16kHz PCM WAV for STT."""
    pcm8k = mulaw_to_pcm(mulaw_bytes)
    pcm16k = resample_pcm(pcm8k, 8000, 16000)
    return pcm_to_wav_bytes(pcm16k, 16000)


def wav_to_mulaw8k(wav_bytes: bytes) -> bytes:
    """Full outbound pipeline: TTS WAV -> 8kHz mu-law for Twilio."""
    pcm, rate = wav_bytes_to_pcm(wav_bytes)
    pcm8k = resample_pcm(pcm, rate, 8000)
    return pcm_to_mulaw(pcm8k)
