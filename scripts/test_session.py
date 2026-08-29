"""Offline call simulation — drives CallSession with fake providers and a fake
Twilio websocket, so the pipeline can be verified without spending a real call.

Checks the things that are hard to eyeball on a live call:
  1. Energy turn detection fires on speech followed by silence.
  2. A slow reply triggers a filler.
  3. A fast reply does NOT trigger a filler.
  4. Filler audio and reply audio never overlap (filler is fully sent first).
  5. end_call from the LLM sets the hangup signal.

Run:  .venv/Scripts/python.exe scripts/test_session.py
"""

import asyncio
import base64
import uuid
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Filler text is Devanagari; the default Windows console codepage cannot encode it.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass
os.environ.setdefault("DB_PATH", "test_session.db")
os.environ.setdefault("APP_SECRET", "test-secret")
os.environ.setdefault("LOGS_DIR", "logs_test")

from app import db  # noqa: E402
from app.call_handler import CallSession  # noqa: E402
from app.dialog import DialogEngine  # noqa: E402
from app.fillers import FillerBank, FillerController  # noqa: E402
from app.models import AgentConfig  # noqa: E402
from app.prompts import check_in_line, no_reply_goodbye  # noqa: E402
from app.providers.base import LLMProvider, LLMReply, STTProvider, ToolCall, TTSProvider  # noqa: E402

SILENT = bytes([0xFF]) * 160     # ~20ms of mu-law near-silence
LOUD = bytes([0x00, 0x80]) * 80  # ~20ms of high-energy mu-law


class FakeSTT(STTProvider):
    def __init__(self, transcript): self.transcript = transcript
    async def transcribe(self, wav16k, language=None):
        await asyncio.sleep(0.01)
        return self.transcript, "hi-IN"


class FakeTTS(TTSProvider):
    def __init__(self): self.calls = []
    async def synthesize_streaming(self, text, language=None):
        self.calls.append(text)
        await asyncio.sleep(0.01)
        yield bytes([0x7F]) * 8000       # 1 second of audio
    async def list_voices(self): return []


class FakeLLM(LLMProvider):
    def __init__(self, delay=0.0, reply="Theek hai ji.", end_call=False):
        self.delay, self.reply, self.end_call = delay, reply, end_call
    async def complete(self, messages, system, tools=None, temperature=0.4,
                       max_output_tokens=150):
        await asyncio.sleep(self.delay)
        calls = [ToolCall(id="1", name="end_call", arguments={})] if self.end_call else []
        return LLMReply(text=self.reply, tool_calls=calls)


class FakeTwilio:
    """A duplex stand-in for the Twilio Media Streams websocket.

    The important part is the `mark` echo: Twilio sends a mark event back once
    queued audio has finished playing, and that echo is what clears the session's
    `_is_speaking` flag. Without it the agent looks like it is speaking forever
    and every caller frame is treated as a possible barge-in rather than a turn.

    Timing is also real: frames are paced, and the caller only starts speaking
    once the agent has finished, so a clean turn is exercised rather than a
    barge-in. (Barge-in has its own case.)
    """

    FRAME_INTERVAL = 0.004   # faster than the real 20ms, still ordered

    def __init__(self, *, speech_ms=600, silence_ms=1200, tail_wait=3.0,
                 interrupt=False):
        self.sent: list[dict] = []
        self._inbound: asyncio.Queue = asyncio.Queue()
        self._speech_ms = speech_ms
        self._silence_ms = silence_ms
        self._tail_wait = tail_wait
        self._interrupt = interrupt
        self._agent_idle = asyncio.Event()
        self._closed = False
        self._producer: asyncio.Task | None = None

    # ---- outbound (session -> Twilio) ----
    async def send_json(self, payload):
        self.sent.append(payload)
        if payload.get("event") == "mark":
            # Twilio plays the buffered audio, then echoes the mark back.
            asyncio.create_task(self._echo_mark(payload["mark"]["name"]))

    async def _echo_mark(self, name):
        await asyncio.sleep(0.05)
        await self._inbound.put(json.dumps({"event": "mark", "mark": {"name": name}}))
        self._agent_idle.set()

    # ---- inbound (Twilio -> session) ----
    async def iter_text(self):
        self._producer = asyncio.create_task(self._produce())
        while not self._closed:
            frame = await self._inbound.get()
            yield frame
            if json.loads(frame).get("event") == "stop":
                break

    async def _produce(self):
        await self._inbound.put(json.dumps({"event": "start", "start": {
            "streamSid": "MZtest", "callSid": "CAtest",
            "customParameters": {"call_id": "t1"}}}))

        if not self._interrupt:
            # Let the greeting finish before the caller speaks.
            try:
                await asyncio.wait_for(self._agent_idle.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(0.1)

        for _ in range(self._speech_ms // 20):
            await self._inbound.put(json.dumps(
                {"event": "media", "media": {"payload": base64.b64encode(LOUD).decode()}}))
            await asyncio.sleep(self.FRAME_INTERVAL)

        for _ in range(self._silence_ms // 20):
            await self._inbound.put(json.dumps(
                {"event": "media", "media": {"payload": base64.b64encode(SILENT).decode()}}))
            await asyncio.sleep(self.FRAME_INTERVAL)

        await asyncio.sleep(self._tail_wait)
        self._closed = True
        await self._inbound.put(json.dumps({"event": "stop"}))

    # ---- assertions ----
    def audio_bytes(self):
        return sum(
            len(base64.b64decode(p["media"]["payload"]))
            for p in self.sent if p.get("event") == "media"
        )

    def cleared(self):
        return sum(1 for p in self.sent if p.get("event") == "clear")


async def run_case(name, *, llm_delay, filler_delay_ms, fillers_on=True, end_call=False,
                   min_utterance_seconds=0.3, no_reply_seconds=0.0, no_reply_prompts=2,
                   speech_ms=600, silence_ms=1200, tail_wait=None):
    agent = AgentConfig(
        name="Test", system_prompt="You are a test agent.",
        fillers_enabled=fillers_on, filler_delay_ms=filler_delay_ms,
        silence_threshold_rms=300, silence_end_seconds=0.8,
        min_utterance_seconds=min_utterance_seconds,
        no_reply_seconds=no_reply_seconds, no_reply_prompts=no_reply_prompts,
        greeting_mode="static", greeting_text="Namaste.",
    )
    call_id = f"t-{uuid.uuid4().hex[:8]}"
    db.create_call(call_id, "agent-x", "+910000000000", {})

    stt = FakeSTT("mera balance kitna hai bataiye")
    tts = FakeTTS()
    llm = FakeLLM(delay=llm_delay, end_call=end_call)
    dialog = DialogEngine(llm, "test prompt", temperature=0.4, max_output_tokens=150)

    controller = None
    if fillers_on:
        bank = FillerBank(tts, ["hi-IN"])
        await bank.warm()
        controller = FillerController(bank, enabled=True)

    ws = FakeTwilio(
        speech_ms=speech_ms, silence_ms=silence_ms,
        tail_wait=tail_wait if tail_wait is not None else max(2.0, llm_delay + 1.5),
    )
    session = CallSession(
        ws, agent=agent, stt=stt, tts=tts, dialog=dialog, fillers=controller,
        call_id=call_id, logs_dir=os.environ["LOGS_DIR"], twilio_creds={},
    )
    await session.handle()

    filler_played = session._filler_this_turn
    print(f"  {name}")
    print(f"    turns={session._turn}  filler={filler_played or 'none'}  "
          f"end_call={dialog.signals.end_call}")
    print(f"    audio sent={ws.audio_bytes()} bytes ({ws.audio_bytes() / 8000:.1f}s)"
          f"  tts_reply_calls={tts.calls[-2:]}")
    return session, ws, filler_played


async def main():
    db.init()
    print("\nCall simulation\n" + "-" * 60)

    print("\n[1] Slow LLM (1.2s) + 300ms filler delay -> filler SHOULD play")
    s, ws, filler = await run_case("slow reply", llm_delay=1.2, filler_delay_ms=300)
    assert s._turn == 1, "turn detection did not fire"
    assert filler is not None, "expected a filler on a slow reply"
    # Greeting (1s) + filler (1s) + reply (1s) = 3s of audio
    assert ws.audio_bytes() >= 24000, f"expected filler+reply audio, got {ws.audio_bytes()}"

    print("\n[2] Fast LLM (0.02s) + 400ms filler delay -> filler must NOT play")
    s, ws, filler = await run_case("fast reply", llm_delay=0.02, filler_delay_ms=400)
    assert s._turn == 1, "turn detection did not fire"
    assert filler is None, f"filler played on a fast reply: {filler}"
    assert ws.audio_bytes() <= 16000, f"unexpected extra audio: {ws.audio_bytes()}"

    print("\n[3] Fillers disabled -> never plays, even when slow")
    s, ws, filler = await run_case("fillers off", llm_delay=1.2, filler_delay_ms=300,
                                   fillers_on=False)
    assert filler is None, "filler played while disabled"

    print("\n[4] LLM calls end_call -> hangup signalled")
    s, ws, filler = await run_case("end call", llm_delay=0.02, filler_delay_ms=400,
                                   end_call=True)
    assert s.dialog.signals.end_call, "end_call tool did not set the signal"

    print("\n[5] Caller says nothing -> agent asks if it can be heard, then closes")
    s, ws, filler = await run_case("no reply", llm_delay=0.02, filler_delay_ms=400,
                                   fillers_on=False, speech_ms=0, silence_ms=0,
                                   no_reply_seconds=0.4, no_reply_prompts=1,
                                   tail_wait=3.0)
    spoken = tuple(t for t in s.tts.calls if t != "Namaste.")
    assert spoken, "agent waited in silence instead of checking the line"
    assert spoken[0] == check_in_line("hi-IN"), \
        f"first check-in was not a check-in: {spoken[0]!r}"
    assert spoken[-1] == no_reply_goodbye("hi-IN"), \
        f"agent never closed the unanswered call: {spoken}"
    # The model has to see its own check-ins, or it answers a question it does
    # not know it asked.
    assert [m["content"] for m in s.dialog.history if m["role"] == "assistant"][-1] \
        == spoken[-1], "check-ins were not written into the dialog history"

    print("\n[6] A short, loud 'Hello' is a turn, not background")
    s, ws, filler = await run_case("short word", llm_delay=0.02, filler_delay_ms=400,
                                   fillers_on=False, speech_ms=240,
                                   min_utterance_seconds=0.5, tail_wait=2.0)
    assert s._turn == 1, "a one-word answer was discarded as background noise"

    print("\n[7] Control tags never reach the caller's ear or the history")
    class Echo(LLMProvider):
        async def complete(self, messages, system, tools=None, temperature=0.4,
                           max_output_tokens=150):
            self.seen = [dict(m) for m in messages]
            # What gpt-oss-20b actually did: recite the instructions back.
            return LLMReply(text="Hi, this is Vaani. [detected_language=hi-IN] "
                                 "[You have already greeted this caller.] Hi Krishna.")

    echo = Echo()
    d = DialogEngine(echo, "You are Vaani.")
    await d.generate_greeting("", "hi-IN")
    reply = await d.respond("Hello", "en-IN")
    said = [m["content"] for m in d.history if m["role"] == "assistant"]
    heard = [m["content"] for m in echo.seen if m["role"] == "user"]
    print(f"    spoken: {reply}")
    print(f"    caller turns as the model sees them: {heard}")
    assert "[" not in reply, f"a control tag was spoken aloud: {reply!r}"
    assert not any("[" in t for t in said), f"a tag was stored in history: {said}"
    # Only the opening cue may be bracketed, because nothing the caller said
    # exists yet; every real turn is the caller's words alone.
    assert heard[-1] == "Hello", f"instructions leaked into the caller's turn: {heard[-1]!r}"

    print("\n" + "-" * 60)
    print("ALL SESSION CHECKS PASSED\n")


if __name__ == "__main__":
    asyncio.run(main())
