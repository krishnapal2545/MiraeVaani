# MiraeVaani 6.0 — Configurable Voice Agents

A multilingual AI voice agent platform. Build an agent in the browser — pick the
speech, language and voice models, write the scenario — then call a real phone
number and watch the conversation stream in live.

Where v5 was **one agent hardcoded in `.env`**, v6 makes an agent **a row in a
database that the UI edits**. The call pipeline underneath is v5's, unchanged.

---

## What's new since v5

| | v5 | v6 |
|---|---|---|
| Agent config | `.env` + `prompts.py`, restart to change | Database row, edited in the UI |
| STT | Sarvam only | Sarvam · Google |
| LLM | Gemini or Groq via env switch | Gemini · Groq, per agent |
| TTS | Bhashini only, 2 hardcoded voices | Sarvam · Bhashini · Google, voice per agent |
| API keys | plaintext `.env` | entered in the UI, Fernet-encrypted in SQLite |
| Dead air while thinking | silence | **smart fillers** |
| Transcript | files on disk | live in the browser (SSE) + SQLite + files |
| Prompt rendering | `str.format` (crashes on `{`) | `string.Template`, brace-safe |

---

## Smart fillers

The gap between a caller finishing their sentence and the agent replying is
`STT + LLM + TTS TTFB` — roughly 1 to 2.5 seconds. Callers read that as a dropped
line and start talking again, which trips barge-in and makes the turn worse.

After STT returns we know what the caller said but the reply is still being
generated, so a filler is **chosen from the transcript and raced against the real
response**:

```
caller stops speaking
  └─ STT ──► transcript
              ├─► LLM ──► TTS ──► first audio byte ──┐
              └─► filler timer (default 350ms)       │
                     └─ classify → play CACHED clip  │
                                          settled ◄──┘
```

Four rules keep it human rather than robotic:

1. **Race, don't always play.** If the reply beats the timer, no filler is heard.
2. **Pre-synthesized.** Every clip is rendered once when the call starts, concurrently,
   and cached as mu-law. A filler that needs its own TTS round-trip is not a filler.
3. **Rate-limited.** Never two turns in a row; capped near a third of turns.
4. **Interruptible**, through the same barge-in path as normal speech.

Categories are chosen by keyword — `lookup`, `thinking`, `empathy`, `confirm` —
because calling an LLM to pick a filler would reintroduce the latency it exists to hide.

---

## Setup

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
cp .env.example .env          # set BASE_URL and APP_SECRET
.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
```

Open <http://localhost:8000>.

**Expose it to Twilio** (required for real calls):

```bash
ngrok http 8000
# put the https URL in .env as BASE_URL, then restart the server
```

### Credentials

Everything is entered on the **Credentials** page — nothing goes in `.env`. Keys
are encrypted with `APP_SECRET` before being written to SQLite and are never sent
back to the browser in full.

Save probes every key against its provider first: a key the provider rejects is
reported inline and **not stored**, so a bad key fails on the Credentials page
instead of as dead air mid-call. Anything the check cannot settle (provider
unreachable, an API not enabled, a from-number that is not on the Twilio account)
is saved with an amber warning. **Test saved keys** re-runs the same checks
against what is already stored.

The agent builder's LLM model dropdown is filled from the provider's own model
list once its key is saved (cached for five minutes), because hardcoded model ids
rot — Groq retired the `llama-3.x` ids this app shipped with. Placing a call also
refuses to dial if the agent's saved model is no longer available, naming the ones
that are.

| Provider | Used for | Where to get it |
|---|---|---|
| Sarvam AI | Saarika STT **and** Bulbul TTS | dashboard.sarvam.ai |
| Bhashini AI | TTS | bhashini.ai |
| Google Cloud | Speech-to-Text and Text-to-Speech | GCP API key, both APIs enabled + billing on |
| Google Gemini | LLM | aistudio.google.com |
| Groq (Llama) | LLM | console.groq.com — key starts `gsk_` |
| xAI Grok | LLM | console.x.ai — key starts `xai-`, team needs credits |
| Twilio | Telephony | Account SID, auth token, and a voice-capable number |

---

## Demo runbook

1. `ngrok http 8000`, put the HTTPS URL in `.env` as `BASE_URL`, start the server.
2. **Credentials** → paste keys → Save.
3. `.venv/Scripts/python.exe scripts/check_providers.py` — proves every key works
   and writes a sample WAV per voice into `preflight/` so you can choose one.
4. **Agents** → New agent → pick models, choose a starter prompt, preview the voice → Save.
5. **Call** → enter your number, fill the `$variables`, hit Call.
6. Watch the transcript appear turn by turn with per-turn STT/LLM/TTS latencies and
   a marker wherever a filler fired.

The convincing move: **make two agents with different providers and prompts, and
call both.** That is what proves the configuration is real and not a skin.

---

## Verification

```bash
.venv/Scripts/python.exe scripts/test_session.py     # offline call simulation
.venv/Scripts/python.exe scripts/check_providers.py  # live credential pre-flight
```

`test_session.py` drives a full call through fake providers and a duplex fake
Twilio socket — including the `mark` echo that clears the speaking flag — and
asserts that a slow reply produces a filler, a fast one does not, disabling
fillers suppresses them, and `end_call` signals a hangup.

---

## Layout

```
app/
  main.py          FastAPI: UI, agent/credential CRUD, call API, Twilio webhooks, WS, SSE
  models.py        AgentConfig — the object that replaced v5's global Settings
  db.py            SQLite: agents, credentials, calls, turns
  verify.py        live credential probes run before a key is stored
  crypto.py        Fernet encryption for stored keys
  registry.py      CATALOG + builds providers from an agent row
  dialog.py        provider-agnostic history and tool dispatch
  fillers.py       classification, pre-synthesis, rate limiting
  call_handler.py  the call session (v5's pipeline, now config-driven)
  events.py        in-process pub/sub feeding the live transcript
  providers/       one adapter per vendor; none may import app.config
static/            plain HTML/CSS/JS, no build step
scripts/           test_session.py, check_providers.py
```

Adding a provider means one adapter under `providers/` and one entry in
`registry.CATALOG`. Nothing else changes.

---

## Known gaps

These are deliberate for a demo build, not oversights:

- **ffmpeg is required for Bhashini TTS** (it returns MP3). Sarvam and Google
  return WAV and work without it. If `ffmpeg` is not on PATH, Bhashini will fail —
  `check_providers.py` says so explicitly.
- **Call context is an in-memory dict**, so this is single-process. It is no longer
  *popped* on read, so a Twilio reconnect no longer kills the call, but horizontal
  scale needs Redis.
- **No authentication** on any endpoint, including the Twilio webhooks. Anyone who
  can reach the tunnel can place calls on your account.
- **Batch STT** sets a latency floor of `silence_end_seconds + STT round-trip`.
  Streaming STT is the next real win.
- **No billing or usage metering.** Provider spend is invisible to the app.
- **Outbound calling in India** needs TRAI DLT registration, DND scrubbing and
  recording disclosure before this touches real customers.
