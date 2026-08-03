# MiraeVaani 4.0 — Multilingual AI Voice Agent

AI voice agent for Indian customer calls (outbound alerts + inbound support) built on a fully API-based stack, optimized for Indian regional languages and Indian-accented English.

## Stack

| Component | Provider | Why |
|-----------|----------|-----|
| STT | Sarvam Saarika (`saarika:v2.5`) | 22 Indian languages, per-utterance auto language detection, code-mixing (Hinglish/Tanglish) |
| LLM | Gemini 2.5 Flash-Lite (thinking disabled) | Fast TTFT, cheap, strong Indian-language quality |
| TTS | Sarvam Bulbul (`bulbul:v2`) | Natural Indian voices in 11 languages |
| Telephony | Twilio Media Streams | Bidirectional 8kHz mu-law WebSocket |

## Architecture

```
Caller ↔ Twilio (PSTN) ↔ FastAPI WebSocket (8kHz mu-law)
        → energy-based turn detection (utterance buffering)
        → Sarvam STT (auto language detect, 16kHz WAV)
        → Gemini 2.5 Flash-Lite (replies in caller's language)
        → Sarvam Bulbul TTS (same language, 8kHz)
        → Twilio → Caller
```

The agent detects the caller's language on every utterance and replies in that language — Hindi, Tamil, Telugu, Bengali, Marathi, Gujarati, Kannada, Malayalam, Punjabi, Odia, or English.

## Quick Start

### 1. Prerequisites
- Python 3.11+
- [Sarvam AI API key](https://dashboard.sarvam.ai) (STT + TTS)
- [Gemini API key](https://aistudio.google.com/apikey)
- [Twilio account](https://console.twilio.com) with a voice-enabled phone number
- [ngrok](https://ngrok.com/) for local development

### 2. Setup

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# Fill in API keys in .env
```

### 3. Run locally

```powershell
# Terminal 1: expose the server
ngrok http 8000
# Copy the https URL into BASE_URL in .env

# Terminal 2: start the server
python -m app.main
```

For **inbound calls**, set your Twilio number's Voice webhook to:
`POST {BASE_URL}/api/twilio/voice`

### 4. Make a test outbound call

```powershell
curl -X POST http://localhost:8000/api/call `
  -H "Content-Type: application/json" `
  -d '{
    "to": "+919876543210",
    "scenario": "margin_shortfall",
    "customer_name": "Rajesh Kumar",
    "metadata": {
      "account_id": "SHA123456",
      "shortfall_amount": "fifty thousand rupees",
      "deadline": "30 June, 3:30 PM"
    }
  }'
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET`  | `/health` | Health check |
| `POST` | `/api/call` | Initiate an outbound call |
| `POST` | `/api/twilio/voice` | Twilio voice webhook (TwiML) |
| `POST` | `/api/twilio/status` | Twilio call status callback |
| `WS`   | `/api/twilio/media-stream` | Bidirectional audio WebSocket |

## Scenarios

- **`margin_shortfall`** — outbound margin shortfall alert
- **`kyc_expiry`** — outbound KYC renewal reminder
- **`inbound`** — general inbound support (default for incoming calls)

Add new scenarios in `app/prompts.py`.

## Configuration notes

- **TTS voice/model:** set `SARVAM_TTS_MODEL=bulbul:v3` and a v3 speaker (e.g. `SARVAM_TTS_SPEAKER=meera`) in `.env` for the newest voices. Default is `bulbul:v2` + `anushka`.
- **Turn detection:** tune `SILENCE_THRESHOLD_RMS` (noise floor) and `SILENCE_END_SECONDS` (pause that ends a turn) in `.env`.
- **Default language:** `DEFAULT_LANGUAGE` is used for the greeting before the caller's language is detected.

## Cost (approx., per 5-minute call, excluding Twilio minutes)

- STT: ₹2.5–7 · LLM: ~₹1 · TTS: ~₹13 → **≈ ₹17–21/call**

## Production roadmap

- Move `CALL_CONTEXTS` to Redis; add auth on `/api/call`
- Switch STT/TTS to Sarvam **WebSocket streaming** APIs to cut per-turn latency further
- Call recording + transcript storage (PostgreSQL)
- Rate limiting, concurrent-call caps, monitoring/alerting
- If concurrency outgrows Python: port the media orchestration hot path to Go, keep this service as the LLM "brain"
