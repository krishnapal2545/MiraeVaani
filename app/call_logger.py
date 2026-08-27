"""Per-call file logging: every API call (STT/LLM/TTS) with latency, transcripts,
turn totals, and all audio saved to disk.

Each call gets its own folder:

    logs/20260803_213500_CAxxxx/
        call.log        # human-readable timeline with latencies
        events.jsonl    # same events, machine-readable (one JSON per line)
        audio/
            turn_001_user.wav    # what the caller said (16kHz)
            turn_001_agent.wav   # what the agent spoke (8kHz)
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class CallLogger:
    def __init__(self, base_dir: str, call_ref: str) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_ref = "".join(c for c in call_ref if c.isalnum() or c in "-_") or "call"
        self.dir = Path(base_dir) / f"{stamp}_{safe_ref}"
        self.audio_dir = self.dir / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)

        self._log_file = open(self.dir / "call.log", "a", encoding="utf-8")
        self._jsonl_file = open(self.dir / "events.jsonl", "a", encoding="utf-8")
        self._start = time.perf_counter()
        self._closed = False

    def event(self, event_type: str, **data) -> None:
        """Log one event to call.log, events.jsonl, and the console logger."""
        if self._closed:
            return
        now = datetime.now().isoformat(timespec="milliseconds")
        elapsed = time.perf_counter() - self._start

        details = " | ".join(f"{k}={v}" for k, v in data.items())
        line = f"[{now}] (+{elapsed:7.2f}s) {event_type.upper():<16} {details}"
        self._log_file.write(line + "\n")
        self._log_file.flush()

        self._jsonl_file.write(
            json.dumps(
                {"ts": now, "elapsed_s": round(elapsed, 3), "event": event_type, **data},
                ensure_ascii=False,
                default=str,
            )
            + "\n"
        )
        self._jsonl_file.flush()

        logger.info("%s %s", event_type.upper(), details)

    def save_audio(self, filename: str, wav_bytes: bytes) -> str:
        """Persist a WAV file under the call's audio folder."""
        path = self.audio_dir / filename
        path.write_bytes(wav_bytes)
        return str(path)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._log_file.close()
            self._jsonl_file.close()
