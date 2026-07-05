# MiraeVaani 2.0

AI Voice Agent for Mirae Asset Sharekhan — fully open-source stack.

```
Twilio (telephony)
    ↕ WebSocket (mulaw 8kHz)
FastAPI on Laptop/Docker (CPU)
    ├── Silero VAD  — local CPU, detects speech end
    ├── → Faster-Whisper large-v3 (Colab GPU)  — STT + language detection
    ├── → Ollama Gemma 2 9B (Colab GPU)        — multilingual dialog
    └── → Coqui XTTS v2 / gTTS (Colab GPU)    — Indian language TTS
```

**Supported languages:** Hindi, Tamil, Telugu, Marathi, Bengali, Gujarati, Kannada, Malayalam, Punjabi, Urdu, English (auto-detected per utterance).

---

## Prerequisites

- Python 3.11 or 3.12 (not 3.13 — `audioop` removed)
- Twilio account with a phone number
- Google Colab (free tier works, Pro recommended for longer sessions)
- ngrok account (free at https://dashboard.ngrok.com)

---

## Setup

### Step 1 — Start GPU services on Colab

Open the following notebooks in **separate Colab tabs** (all with T4 GPU runtime):

| Notebook | Service | Port |
|----------|---------|------|
| `colab/colab_1_stt.ipynb` | Faster-Whisper STT | 8001 |
| `colab/colab_2_llm.ipynb` | Ollama + Gemma 2 9B | 11434 |
| `colab/colab_3_tts.ipynb` | XTTS v2 + gTTS | 8003 |

For each notebook:
1. Set runtime to **T4 GPU** (Runtime → Change runtime type)
2. Add your ngrok auth token
3. Run all cells
4. Copy the ngrok URL printed at the end

### Step 2 — Configure laptop environment

```bash
cd d:\Tech\MiraeVaani2.0
copy .env.example .env
```

Edit `.env` and fill in:
```env
# Twilio credentials
TWILIO_ACCOUNT_SID=ACxxxx
TWILIO_AUTH_TOKEN=xxxx
TWILIO_PHONE_NUMBER=+16505823202

# Paste ngrok URLs from each Colab notebook
STT_BASE_URL=https://xxxx.ngrok-free.dev
LLM_BASE_URL=https://yyyy.ngrok-free.dev
TTS_BASE_URL=https://zzzz.ngrok-free.dev

# Your local ngrok URL for Twilio webhooks (Step 3)
BASE_URL=https://your-url.ngrok-free.dev
```

### Step 3 — Expose laptop to Twilio via ngrok

```bash
ngrok http 8000
# Copy the https URL → paste as BASE_URL in .env
```

### Step 4 — Install dependencies and run

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
pip install packaging          # Required by Silero VAD

python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Or with Docker:
```bash
docker-compose up --build
```

### Step 5 — Verify

```bash
curl http://localhost:8000/health
```

Expected response:
```json
{
  "status": "ok",
  "service": "MiraeVaani 2.0",
  "stt_url": "https://xxxx.ngrok-free.dev",
  "llm_url": "https://yyyy.ngrok-free.dev",
  "tts_url": "https://zzzz.ngrok-free.dev"
}
```

---

## Making a Test Call

```bash
curl -X POST http://localhost:8000/api/call ^
  -H "Content-Type: application/json" ^
  -d "{\"to\": \"+919876543210\", \"scenario\": \"margin_shortfall\", \"customer_name\": \"Rajesh Kumar\", \"metadata\": {\"account_id\": \"SHA123456\", \"shortfall_amount\": \"50000\", \"deadline\": \"2 July 2025, 3:30 PM\"}}"
```

### Scenarios

| Scenario | Description |
|----------|-------------|
| `margin_shortfall` | Outbound — notify customer of margin shortfall |
| `kyc_expiry` | Outbound — KYC expiry reminder |
| `inbound_support` | Inbound — general customer support |

---

## Architecture

### Laptop (CPU only)

| File | Role |
|------|------|
| `app/main.py` | FastAPI app, Twilio webhooks, WebSocket endpoint |
| `app/call_handler.py` | Real-time call loop — orchestrates VAD → ASR → LLM → TTS |
| `app/call_manager.py` | Twilio outbound call initiation |
| `app/vad.py` | Silero VAD — local speech detection, 32ms frames |
| `app/asr.py` | HTTP client → Faster-Whisper on Colab |
| `app/llm.py` | HTTP client → Ollama on Colab (OpenAI-compatible API) |
| `app/tts.py` | HTTP client → XTTS v2 / gTTS on Colab |
| `app/prompts.py` | System prompts for each call scenario |
| `app/config.py` | Pydantic settings from `.env` |

### Colab GPU services

| Service | Port | Model | VRAM |
|---------|------|-------|------|
| STT | 8001 | Faster-Whisper large-v3 | ~3 GB |
| LLM | 11434 | Ollama + Gemma 2 9B | ~6 GB |
| TTS | 8003 | Coqui XTTS v2 + gTTS | ~4 GB |
| **Total** | | | **~13 GB** (fits T4 16GB) |

### Call flow

```
1. POST /api/call → Twilio initiates outbound call
2. Twilio hits POST /api/twilio/voice → returns TwiML with <Connect><Stream>
3. Twilio opens WebSocket to /api/twilio/media-stream
4. CallHandler loop:
   a. Sends greeting (LLM → TTS → Twilio audio)
   b. Feeds incoming mulaw audio into Silero VAD (local)
   c. VAD detects speech end → fires callback
   d. ASR: PCM bytes → POST Colab Whisper → transcript + language
   e. LLM: transcript → POST Colab Ollama → response text
   f. TTS: response → POST Colab XTTS/gTTS → mulaw audio → stream to Twilio
   g. Loop back to (b)
```

---

## Latency Budget (Colab T4)

| Step | Time |
|------|------|
| VAD silence detection | ~800 ms |
| Network laptop ↔ Colab (×3 hops) | ~300 ms |
| Whisper large-v3 (3s audio) | ~400 ms |
| Gemma 2 9B (50 tokens) | ~800 ms |
| XTTS v2 / gTTS synthesis | ~600 ms |
| **Total perceived latency** | **~3–5 s** |

To reduce latency: lower `SILENCE_FRAMES_TO_END` in `vad.py`, reduce `LLM_MAX_TOKENS`, or use a smaller LLM (`gemma2:2b`).

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Twilio says "application error" | Invalid TwiML (unescaped `&` in XML) or stale ngrok URL | Ensure `BASE_URL` in `.env` matches current ngrok URL |
| WebSocket connects then crashes | Missing `packaging` module for Silero VAD | `pip install packaging` |
| `Input audio chunk is too short` | VAD frame size < 512 samples | Set `FRAME_MS = 32` in `vad.py` |
| No audio heard by caller | Barge-in triggered on every packet | Only feed VAD when agent is not speaking |
| `STT_BASE_URL not configured` | Colab not started or ngrok URL stale | Re-run Colab notebook and update `.env` |
| TTS returns empty audio | XTTS model still loading (~40s) | Wait for health check to pass |
| Hindi sounds robotic/wrong | Using English speaker voice for XTTS | Route Hindi to gTTS or use Devanagari script |
| VAD never fires | Twilio not sending audio | Check "Media stream started" appears in logs |
| Colab disconnects | Free tier timeout (~90 min idle) | Keep keep-alive cell running; use Colab Pro |

---

## Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `TWILIO_ACCOUNT_SID` | Twilio Account SID | `ACxxxx` |
| `TWILIO_AUTH_TOKEN` | Twilio Auth Token | `xxxx` |
| `TWILIO_PHONE_NUMBER` | Twilio phone number (E.164) | `+16505823202` |
| `STT_BASE_URL` | Colab STT ngrok URL | `https://xxx.ngrok-free.dev` |
| `LLM_BASE_URL` | Colab LLM ngrok URL | `https://yyy.ngrok-free.dev` |
| `TTS_BASE_URL` | Colab TTS ngrok URL | `https://zzz.ngrok-free.dev` |
| `LLM_MODEL` | Ollama model name | `gemma2:9b` |
| `LLM_TEMPERATURE` | LLM temperature | `0.7` |
| `LLM_MAX_TOKENS` | Max response tokens | `120` |
| `STT_LANGUAGE` | Force language or `auto` | `auto` |
| `TTS_DEFAULT_LANGUAGE` | Fallback TTS language | `hi` |
| `TTS_SPEAKER` | XTTS speaker name | `Ana Florence` |
| `BASE_URL` | Public URL for Twilio webhooks | `https://xxx.ngrok-free.dev` |
| `LOG_LEVEL` | Logging level | `INFO` |
