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
  main.py          FastAPI: UI, CRUD, campaign API, Twilio webhooks, WS proxy, SSE
  models.py        AgentConfig — the object that replaced v5's global Settings
  db.py            SQLite: agents, credentials, calls, turns, contacts, campaigns
  dialer.py        placing a call and running the conversation — shared by app and workers
  worker.py        Vaani: one agent's calling process (a pod, later)
  runner.py        starts/stops workers; LocalProcessRunner now, KubernetesRunner later
  contacts.py      CSV parse, phone normalisation, column→$variable mapping
  schedule.py      calling windows, timezones, and the legal hours clamp
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
COMPARISON.md      provider accuracy/latency/cost research (Aug 2026)
```

Adding a provider means one adapter under `providers/` and one entry in
`registry.CATALOG`. Nothing else changes.

---

## Outbound campaigns

The system is two halves. **MiraeVaani** is the management app: one UI and API for
agents, contacts, campaigns and monitoring, and it owns the database. **Vaani** is
the worker — one process per agent, which reads that agent's config once at startup,
dials its campaign list, holds the conversations, and writes each contact's status back.

Using it:

1. **Contacts** — upload a CSV. The agent's prompt already declares what each call
   needs (`$customer_name`, `$shortfall_amount`, …), so the importer proposes a
   column mapping and you correct it before anything is stored. Numbers are
   normalised to E.164 and deduplicated; unusable rows are reported, not dropped.
2. **Campaigns** — pick an agent and a list, set the calling window, timezone,
   simultaneous calls and retry policy.
3. **Start** — the worker comes up and begins dialing when the window is open.
   Pause stops it claiming new contacts; Stop also cancels what is still queued.
   Both let calls already in progress finish.

Things worth knowing:

- **Editing an agent restarts its worker.** Config is read once at startup, so an
  edit drains the old worker — calls in progress finish on the config they began
  with — and starts a replacement. This is why there is no "did my change take
  effect?" ambiguity.
- **Calling hours are clamped to 09:00–21:00** in the campaign's own timezone,
  whatever the UI is set to. TRAI restricts when commercial calls may be placed.
- **Three concurrency caps apply** and the lowest wins: the agent's, the campaign's,
  and `GLOBAL_MAX_CONCURRENT_CALLS` (default 5, sized for a laptop).
- **Answering-machine detection is on for campaign calls.** Without it a large share
  of a run holds full conversations with voicemail and logs them as successes.
- **Unanswered is not failed.** `no-answer`, `busy` and `failed` are retried after
  the campaign's interval until its attempt budget is spent.
- **Suppression is checked at dial time**, so someone who asks not to be called
  during a running campaign is not called again.
- **Outcome webhook** — set a URL on the agent and it is POSTed the moment an
  outcome is recorded: the caller's decision, their number, and the contact's
  variables. This is how a call triggers a payment link or a CRM update while the
  customer is still on the phone. Fire-and-forget: a slow endpoint cannot stall a call.

### Going to Kubernetes

The laptop build was written so this is a deployment change, not a rewrite. Each
row is the same worker code either way:

| Concern | Now | Kubernetes |
|---|---|---|
| Worker | child process on a local port | pod |
| Launch / stop | `runner.LocalProcessRunner` | `KubernetesRunner` |
| Reaching the worker | the WS proxy in `main.py` | Service + Ingress |
| Event fan-out to the UI | worker POSTs `/api/internal/events` | Redis pub/sub |
| Store | SQLite in WAL mode | Postgres |
| Public URL | ngrok | real domain + TLS (`BASE_URL` only) |

What must be dealt with before production:

- **ngrok is development only.** It exists because Twilio has to reach your machine
  inbound and a laptop has no public IP or certificate. In production `BASE_URL`
  becomes a real domain; no code depends on the tunnel.
- **Horizontal scaling is blocked** by `dialer.CALL_CONTEXTS` and the in-process
  `EventBroker`. Both need Redis before a second management process can run. Live
  sessions can stay process-local — the WebSocket *is* the session — so sticky
  sessions are not required.
- **SQLite → Postgres** before concurrent writers get serious; target claiming then
  becomes `SELECT ... FOR UPDATE SKIP LOCKED` instead of the single-worker claim.
- **The WS proxy is a stand-in.** At 50 messages/sec per call per direction it will
  not carry production traffic; a Service does that job in a cluster.
- **A `KubernetesRunner` needs cluster write permission** to create and delete pods.
  That is a real security surface — scope it to one namespace with a minimal Role.
- **Graceful stop needs `terminationGracePeriodSeconds` ≈ 600** (calls run minutes;
  the default 30 is far too short) plus a `preStop` hook calling `/shutdown`.
- **Pods scale with agent count; load scales with call count.** If idle agents get
  expensive, the same worker binary can run as a fungible autoscaled pool — a
  deployment change only, since config is read at worker start rather than baked in.
- **Contacts are claimed in batches**, not loaded wholesale; keep it that way for
  large lists.
- **Per-org rate limiting** will be needed if the platform holds the provider keys,
  or one organisation's campaign will starve the rest. At 600 concurrent calls
  expect roughly 120 req/s each to STT, LLM and TTS — quota increases have lead time.
- **Twilio at volume** needs a DID pool with rotation and a CPS increase. Per
  `COMPARISON.md`, Exotel/Plivo cut telephony cost 40–60% for India-only traffic;
  telephony should move behind an interface like `app/providers/`.

### Not taken from `miraevaani-enhancements`

That repo (MiraeVaani 4.0) has two things worth folding in later, deliberately kept
out of this build so that voice quality and campaign logic are not debugged together:

- **Silero VAD** — real speech detection instead of RMS energy, which fixes false
  barge-ins. Use the ONNX build; the torch build costs ~300MB RSS *per process*,
  which multiplies badly when every agent is its own process. Worth putting behind a
  per-agent `energy | silero` toggle so it can be compared on real calls.
- **Call-centre ambience** under the agent's speech. Do it *after* Silero: that
  repo's VAD docstring records that the ambience bed echoing back down the line was
  itself causing the false barge-ins.

Its tone classifier is a prototype (~71% accuracy, 322 rows, three languages) and is
not ready to drive behaviour, though its shadow-mode pattern — run the new model
alongside the live one, log both, compare before trusting it — is worth copying.

---

## Known gaps

These are deliberate for a demo build, not oversights:

- **ffmpeg is required for Bhashini TTS** (it returns MP3). Sarvam and Google
  return WAV and work without it. If `ffmpeg` is not on PATH, Bhashini will fail —
  `check_providers.py` says so explicitly.
- **Call context is an in-memory dict**, so the management app is single-process.
  It is no longer *popped* on read, so a Twilio reconnect no longer kills the call,
  but horizontal scale needs Redis. Workers are separate processes and do scale.
- **No org login.** Every table carries `org_id` and credentials resolve org-first
  then platform, so the multi-tenant split is a change to queries rather than a
  migration — but there is no authentication yet, so everything is one default org.
- **No authentication** on any endpoint, including the Twilio webhooks. Anyone who
  can reach the tunnel can place calls on your account.
- **Batch STT** sets a latency floor of `silence_end_seconds + STT round-trip`.
  Streaming STT is the next real win.
- **No billing or usage metering.** Provider spend is invisible to the app.
- **Outbound calling in India** needs TRAI DLT registration, DND scrubbing and
  recording disclosure before this touches real customers.
