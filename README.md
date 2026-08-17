# MiraeVaani 5.0 — Multilingual AI Voice Agent

AI voice agent for Indian customer calls (outbound alerts + inbound support) built on a fully API-based stack, optimized for Indian regional languages and Indian-accented English.

## Stack

| Component | Provider | Why |
|-----------|----------|-----|
| STT | Sarvam Saarika (`saarika:v2.5`) | 22 Indian languages, per-utterance auto language detection, code-mixing (Hinglish/Tanglish) |
| LLM | Gemini 2.5 Flash-Lite (thinking disabled) | Fast TTFT, cheap, strong Indian-language quality |
| TTS | Bhashini AI (REST `/v2/synthesize`) | 22+ Indian languages, low-latency, sentence streaming |
| Telephony | Twilio Media Streams | Bidirectional 8kHz mu-law WebSocket |

## Architecture

```
Caller ↔ Twilio (PSTN) ↔ FastAPI WebSocket (8kHz mu-law)
        → energy-based turn detection (utterance buffering)
        → Sarvam STT (auto language detect, 16kHz WAV)
        → Gemini 2.5 Flash-Lite (replies in caller's language)
        → Bhashini TTS (sentence-streaming, MP3 → mu-law)
        → Twilio → Caller
```

## v5 Improvements over v4

- **Bhashini TTS** replaces Sarvam Bulbul — 22+ Indian languages via REST
- **Sentence-streaming TTS** — first sentence starts playing before full response is synthesized (low TTFB)
- **Clear latency logging** — per-service hit/response timestamps (STT, LLM, TTS first-byte, turn total)
- **Fixed barge-in** — old in-flight response is fully cancelled; only the latest question gets answered
- **Auto call hangup** — LLM calls `end_call` tool after goodbye, Twilio disconnects automatically
- **Requires ffmpeg** — for pydub MP3 decoding (install via `choco install ffmpeg` or `apt install ffmpeg`)

The agent detects the caller's language on every utterance and replies in that language — Hindi, Tamil, Telugu, Bengali, Marathi, Gujarati, Kannada, Malayalam, Punjabi, Odia, or English.

## Quick Start

### 1. Prerequisites
- Python 3.11+
- **ffmpeg** installed and on PATH (for MP3 decoding: `choco install ffmpeg` / `apt install ffmpeg`)
- [Sarvam AI API key](https://dashboard.sarvam.ai) (STT)
- [Bhashini AI API key](https://pay.bhashini.ai) (TTS)
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

- **TTS voices:** `BHASHINI_TTS_VOICE_EN=Female3` for English, `BHASHINI_TTS_VOICE_HI=hi-f3` for Hindi/other. See full list at `https://app.bhashini.ai/voices.json`.
- **TTS style:** `BHASHINI_TTS_STYLE=Neutral` (options: Neutral, Conversational, News, Book, Command, Happy, Sad, etc.)
- **Turn detection:** tune `SILENCE_THRESHOLD_RMS` (noise floor) and `SILENCE_END_SECONDS` (pause that ends a turn) in `.env`.
- **Default language:** `DEFAULT_LANGUAGE` is used for the greeting before the caller's language is detected.

## Cost Breakdown (2026 pricing, per 5-minute call)

### API unit rates (as of August 2026)

| Service | Provider | Rate | Unit | Source |
|---------|----------|------|------|--------|
| **STT** | Sarvam Saarika | ₹30 | per hour of audio (₹0.50/min) | [sarvam.ai/api-pricing](https://www.sarvam.ai/api-pricing) |
| **TTS (Bulbul v2)** | Sarvam Bulbul v2 | ₹15 | per 10K characters | [sarvam.ai/api-pricing](https://www.sarvam.ai/api-pricing) |
| **TTS (Bulbul v3)** | Sarvam Bulbul v3 | ₹30 | per 10K characters | [sarvam.ai/api-pricing](https://www.sarvam.ai/api-pricing) |
| **LLM** | Gemini 2.5 Flash-Lite | $0.10 / ₹8.50 | per 1M input tokens | [Google AI pricing](https://ai.google.dev/pricing) |
| **LLM** | Gemini 2.5 Flash-Lite | $0.40 / ₹34 | per 1M output tokens | [Google AI pricing](https://ai.google.dev/pricing) |
| **Telephony (outbound mobile)** | Twilio India | $0.0496 / ₹4.20 | per minute | [twilio.com/voice/pricing/in](https://www.twilio.com/en-us/voice/pricing/in) |
| **Telephony (outbound landline)** | Twilio India | $0.0699 / ₹5.95 | per minute | [twilio.com/voice/pricing/in](https://www.twilio.com/en-us/voice/pricing/in) |

> USD → INR conversion used: ₹85 (approximate mid-2026 rate).

### Assumptions for a 5-minute call

| Parameter | Value | Notes |
|-----------|-------|-------|
| Call duration | 5 minutes | |
| Turns (back-and-forth) | ~12 | Agent greeting + ~11 caller/agent exchanges |
| Caller speaks | ~2.5 min total | ~50% of call time |
| Agent speaks | ~2.5 min total | ~50% of call time |
| Agent reply length | ~60 chars/turn | Short spoken Hindi/English sentences |
| Total agent text | ~720 chars | 12 turns x 60 chars |
| LLM input tokens/turn | ~350 | System prompt (~250 first turn, cached) + history + user utterance |
| LLM output tokens/turn | ~40 | 1-2 short spoken sentences |
| Total LLM input tokens | ~4,200 | 12 turns (with growing history) |
| Total LLM output tokens | ~480 | 12 turns x 40 |

### Per-call cost calculation

| Component | Calculation | Cost (₹) |
|-----------|-------------|----------|
| **Sarvam STT** | 2.5 min x ₹0.50/min | **₹1.25** |
| **Gemini LLM (input)** | 4,200 tokens x ₹8.50/1M | **₹0.04** |
| **Gemini LLM (output)** | 480 tokens x ₹34/1M | **₹0.02** |
| **Sarvam TTS (Bulbul v2)** | 720 chars x ₹15/10K | **₹1.08** |
| **Sarvam TTS (Bulbul v3)** | 720 chars x ₹30/10K | **₹2.16** |
| **Twilio (outbound mobile)** | 5 min x ₹4.20/min | **₹21.00** |
| | | |
| **TOTAL with Bulbul v2** | STT + LLM + TTS v2 + Twilio | **≈ ₹23.39** |
| **TOTAL with Bulbul v3** | STT + LLM + TTS v3 + Twilio | **≈ ₹24.47** |
| **AI-only (excl. Twilio)** | STT + LLM + TTS v2 | **≈ ₹2.39** |

### Key takeaways

- **Twilio telephony dominates the cost** (~₹21 of ~₹23 per call = ~90%). The AI stack (STT + LLM + TTS) is remarkably cheap at **₹2–3 per 5-min call**.
- **Gemini Flash-Lite is almost free** at ₹0.06 per call — the cheapest component by far.
- **Sarvam Bulbul v2 vs v3**: v2 is half the price (₹15 vs ₹30 per 10K chars). For production calls where voice quality matters, v3 is worth the ₹1 extra per call.
- **At scale (10,000 calls/month)**: ~₹2.3–2.5 lakh/month total, of which ~₹2.1 lakh is Twilio. Consider Indian telephony providers (Exotel, Plivo) which charge ₹0.80–1.20/min for India mobile, potentially saving 40–60% on telephony.
- **Free tier**: Sarvam gives ₹1,000 free credits (~33 hrs STT or ~650K TTS chars). Gemini gives $300 free. Enough for ~400+ test calls.

## Production roadmap

- Move `CALL_CONTEXTS` to Redis; add auth on `/api/call`
- Switch STT to Sarvam **WebSocket streaming** API for even lower latency
- Switch TTS to Bhashini **WebSocket streaming** (`wss://tts.bhashini.ai/tts/stream`) for chunk-level streaming
- Call recording + transcript storage (PostgreSQL)
- Rate limiting, concurrent-call caps, monitoring/alerting
- If concurrency outgrows Python: port the media orchestration hot path to Go, keep this service as the LLM "brain"
