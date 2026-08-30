# STT vs TTS vs LLM — Top 10 Provider Comparison

> **Context:** MiraeVaani multilingual Indian voice agent. Priority = Indian language accuracy, low latency, and cost efficiency.
>
> **Last updated:** August 2026 — prices verified from official provider websites.
>
> **Currency:** ₹85 = $1 (mid-2026 approximate rate)

---

## 1. Speech-to-Text (STT) — Top 10

| # | Provider | Model | Accuracy (English) | Accuracy (Indian langs) | Languages Supported | Latency | Cost/min | Cost/hr | Best For |
|---|----------|-------|-------------------|------------------------|---------------------|---------|----------|---------|----------|
| 1 | **Sarvam AI** | Saarika v2.5 | ~92% | **~90–94%** (22 Indian langs) | 22 Indian + English | ~1.5s (batch) | ₹0.50 ($0.0056) | ₹30 ($0.35) | ✅ Indian languages, code-mixing (Hinglish/Tanglish), auto lang detect |
| 2 | **Google Cloud** | Chirp 2 / V2 | **~95%** | ~85–90% | 125+ | ~300ms (streaming) | ₹1.36 ($0.016) | ₹81.60 ($0.96) | Global coverage, streaming, speaker diarization |
| 3 | **Deepgram** | Nova-3 Multilingual | **~95%** | ~80–85% (Hindi) | 45+ | **~200ms** (streaming) | ₹0.78 ($0.0092) streaming | ₹46.92 ($0.552) | Ultra-low latency, real-time agents |
| 4 | **Microsoft Azure** | Speech Services | ~94% | ~85–88% | 100+ | ~400ms (streaming) | ₹1.42 ($0.0167) | ₹85 ($1.00) | Enterprise, custom models, on-prem/container option |
| 5 | **OpenAI** | gpt-4o-mini-transcribe | ~94% | ~82–87% | 99+ | ~1–2s (batch) | ₹0.255 ($0.003) | ₹15.30 ($0.18) | Cheapest quality batch STT |
| 6 | **OpenAI** | Whisper / gpt-4o-transcribe | ~95% | ~84–88% | 99+ | ~2–4s (batch) | ₹0.51 ($0.006) | ₹30.60 ($0.36) | Higher accuracy, diarization support |
| 7 | **Deepgram** | Nova-3 Mono (pre-recorded) | **~96%** | ~80% (Hindi only) | 45+ | ~100ms (batch) | ₹0.41 ($0.0048) | ₹24.48 ($0.288) | Cheapest streaming provider, batch use |
| 8 | **AWS** | Transcribe | ~93% | ~82–85% (Hindi, Tamil, Telugu) | 100+ | ~500ms (streaming) | ₹1.02 ($0.012) | ₹61.20 ($0.72) | AWS ecosystem, medical/legal vocab |
| 9 | **AssemblyAI** | Universal-2 | **~95%** | ~80% (limited Indian) | 20+ | ~400ms (streaming) | ₹0.92 ($0.0108) | ₹55.08 ($0.648) | Summarization, sentiment, PII redaction |
| 10 | **Bhashini.ai** | AI4Bharat models | ~88% | **~88–92%** (12 Indian) | 12 Indian + English | ~2–3s (batch) | ₹0.55/min ($0.0065) | ₹33/hr ($0.39) | Indian languages, govt-backed; Starter plan ₹1,000/mo |

### STT Recommendation for MiraeVaani

| Priority | Winner | Why |
|----------|--------|-----|
| **Indian language accuracy** | 🥇 Sarvam Saarika | Best Indian lang accuracy + auto language detection + code-mixing |
| **Lowest latency** | 🥇 Deepgram Nova-3 | ~200ms streaming, but weak on Indian languages |
| **Cheapest (production)** | 🥇 OpenAI gpt-4o-mini-transcribe | $0.003/min but batch-only, no streaming |
| **Cheapest streaming** | 🥇 Deepgram Nova-3 Mono | $0.0048/min pre-recorded, $0.0077/min streaming |
| **Best overall for MiraeVaani** | 🥇 **Sarvam Saarika** | Optimal balance of Indian accuracy, cost, and language coverage |

---

## 2. Text-to-Speech (TTS) — Top 10

| # | Provider | Model | Voice Quality | Indian Language Support | Languages | Latency (TTFB) | Cost Basis | Cost/hr equiv* | Best For |
|---|----------|-------|--------------|------------------------|-----------|-----------------|------------|----------------|----------|
| 1 | **Sarvam AI** | Bulbul v2 | ★★★★ | **11 Indian langs** (native speakers) | 11 Indian + English | ~500ms | ₹15/10K chars ($0.18/1K) | ~₹10.80/hr† | ✅ Natural Indian voices, correct prosody |
| 2 | **Sarvam AI** | Bulbul v3 | ★★★★★ | **11 Indian langs** | 11 Indian + English | sub-250ms (WebSocket) | ₹30/10K chars ($0.35/1K) | ~₹21.60/hr† | Premium Indian voice quality, streaming |
| 3 | **Google Cloud** | WaveNet / Neural2 | ★★★★ | Hindi, Tamil, Telugu, Bengali, Kannada, Malayalam | 50+ | ~300ms | $16/1M chars (₹1,360/1M) | ~₹115.20/hr† | Wide language range, neural voices |
| 4 | **Microsoft Azure** | Neural TTS | ★★★★ | Hindi, Tamil, Telugu, Kannada, Malayalam, Marathi, Gujarati | 140+ | ~250ms | $4/1M chars (₹340/1M) | ~₹28.80/hr† | Enterprise, SSML, custom neural voice, cheapest big-cloud |
| 5 | **ElevenLabs** | Multilingual v2/v3 | ★★★★★ | Hindi (accent, not native) | 74 | ~300ms | $0.10/1K chars (₹8.50/1K) | ~₹61.20/hr† | Best voice clone, emotion, 74 languages |
| 6 | **ElevenLabs** | Flash/Turbo | ★★★★ | Hindi (accent) | 32 | ~150ms | $0.05/1K chars (₹4.25/1K) | ~₹30.60/hr† | Fastest ElevenLabs, half the cost |
| 7 | **Deepgram** | Aura-2 | ★★★★ | Limited | 20+ | ~200ms | $0.030/1K chars (₹2.55/1K) | ~₹18.36/hr† | Low-latency voice agents |
| 8 | **OpenAI** | TTS-1 / TTS-1-HD | ★★★★ | ~15 (auto-detect) | ~57 | ~400ms | $15/1M chars (₹1,275/1M) | ~₹91.80/hr† | Simple API, decent multilingual |
| 9 | **AWS** | Polly (Neural) | ★★★½ | Hindi only | 30+ | ~200ms | $16/1M chars (₹1,360/1M) | ~₹97.92/hr† | AWS ecosystem, SSML support |
| 10 | **Bhashini.ai** | AI4Bharat TTS | ★★★ | **12 Indian langs** | 12 Indian + English | ~1–2s | ₹0.95/1K chars ($0.011/1K) | ~₹6.84/hr† | Indian languages; Starter plan ₹1,000/mo, zero-shot voice clone |

> **†** *Cost/hr equivalent assumes ~7,200 characters of agent speech per hour (based on ~120 chars/min spoken rate). Actual cost depends on text length, not audio duration.*

### TTS Recommendation for MiraeVaani

| Priority | Winner | Why |
|----------|--------|-----|
| **Indian voice naturalness** | 🥇 Sarvam Bulbul v3 | Most natural Indian regional voices, sub-250ms streaming |
| **Cost-effective Indian TTS** | 🥇 Sarvam Bulbul v2 | Half the price of v3, still good quality |
| **Lowest latency** | 🥇 ElevenLabs Flash / Deepgram Aura-2 | ~150–200ms TTFB, but limited/no Indian lang support |
| **Cheapest per character** | 🥇 Deepgram Aura-2 | $0.030/1K chars, but limited Indian voices |
| **Best overall for MiraeVaani** | 🥇 **Sarvam Bulbul v2/v3** | Only provider with 11 Indian languages in natural voices at low cost |

---

## 3. Large Language Model (LLM) APIs — Top 10

| # | Provider | Model | Indian Language Quality | Languages | Latency (TTFT) | Input Cost/1M tokens | Output Cost/1M tokens | Cost/5-min call‡ | Best For |
|---|----------|-------|------------------------|-----------|-----------------|---------------------|----------------------|------------------|----------|
| 1 | **Google** | Gemini 2.5 Flash-Lite | ★★★★ | 100+ | **~200ms** | ₹8.50 ($0.10) | ₹34 ($0.40) | **₹0.05** | ✅ Fastest + cheapest, good Indian langs |
| 2 | **Google** | Gemini 2.5 Flash | ★★★★★ | 100+ | ~400ms | ₹12.75 ($0.15) | ₹51 ($0.60) | ₹0.08 | Better quality, still fast & cheap |
| 3 | **Google** | Gemini 2.5 Pro | ★★★★★ | 100+ | ~800ms | ₹106.25 ($1.25) | ₹425 ($5.00) | ₹0.65 | Best quality, higher cost |
| 4 | **OpenAI** | GPT-4o mini | ★★★½ | 50+ | ~350ms | ₹12.75 ($0.15) | ₹51 ($0.60) | ₹0.08 | Balanced speed/quality |
| 5 | **OpenAI** | GPT-4o | ★★★★ | 50+ | ~500ms | ₹212.50 ($2.50) | ₹850 ($10.00) | ₹1.30 | Top-tier English, good multilingual |
| 6 | **Anthropic** | Claude 3.5 Haiku | ★★★½ | 30+ | ~300ms | ₹85 ($1.00) | ₹425 ($5.00) | ₹0.56 | Fast, good instruction following |
| 7 | **Anthropic** | Claude 4 Sonnet | ★★★★ | 30+ | ~600ms | ₹255 ($3.00) | ₹1,275 ($15.00) | ₹1.68 | Best reasoning, safety |
| 8 | **Groq** | Llama 3.3 70B | ★★★ | 20+ | **~100ms** | ₹50.15 ($0.59) | ₹67.15 ($0.79) | ₹0.24 | Ultra-fast inference (~394 tok/sec) |
| 9 | **Mistral** | Mistral Large | ★★★½ | 20+ | ~400ms | ₹170 ($2.00) | ₹510 ($6.00) | ₹1.07 | EU-hosted, good multilingual |
| 10 | **DeepSeek** | DeepSeek V3 | ★★★★ | 30+ | ~500ms | ₹23.80 ($0.28) | ₹35.70 ($0.42) | ₹0.12 | Cheap, strong multilingual, open-weight |

> **‡** *Cost per 5-min call based on MiraeVaani usage: ~4,200 input tokens + ~480 output tokens per call (12 turns).*

### LLM Recommendation for MiraeVaani

| Priority | Winner | Why |
|----------|--------|-----|
| **Indian language quality** | 🥇 Gemini 2.5 Flash / Pro | Best Indian language understanding & generation |
| **Lowest latency (TTFT)** | 🥇 Groq (Llama 3.3) | ~100ms TTFT, hardware-optimized LPU inference |
| **Cheapest** | 🥇 Gemini 2.5 Flash-Lite | ₹0.05/call — cheapest with good Indian language quality |
| **Best cost/quality balance** | 🥇 **Gemini 2.5 Flash-Lite** | ₹0.05/call, ~200ms TTFT, strong Indian language support |
| **Best overall for MiraeVaani** | 🥇 **Gemini 2.5 Flash-Lite** | Optimal latency + Indian language quality + cost |

---

## 4. Combined Stack Cost Comparison (per 5-min call)

| Stack | STT | LLM | TTS | AI Cost/call | Latency Profile |
|-------|-----|-----|-----|-------------|-----------------|
| **MiraeVaani (current)** | Sarvam Saarika (₹1.25) | Gemini Flash-Lite (₹0.05) | Sarvam Bulbul v2 (₹1.08) | **₹2.38** | Medium (~2s total) |
| **Premium Indian** | Sarvam (₹1.25) | Gemini 2.5 Flash (₹0.08) | Sarvam Bulbul v3 (₹2.16) | **₹3.49** | Medium (~2s total) |
| **Budget Indian** | Bhashini STT (₹1.38) | Gemini Flash-Lite (₹0.05) | Bhashini TTS (₹0.68) | **₹2.11** | High (~4s); Starter plan ₹1,000/mo |
| **Global low-latency** | Deepgram Nova-3 (₹0.98) | Groq Llama (₹0.24) | Deepgram Aura-2 (₹0.46) | **₹1.68** | **Low (~0.8s total)** |
| **Google full-stack** | Google STT (₹3.40) | Gemini Flash-Lite (₹0.05) | Google TTS (₹1.38) | **₹4.83** | Low (~1.2s total) |
| **Azure full-stack** | Azure STT (₹3.54) | GPT-4o mini (₹0.08) | Azure TTS (₹0.41) | **₹4.03** | Low (~1.2s total) |
| **OpenAI full-stack** | gpt-4o-mini-transcribe (₹0.64) | GPT-4o mini (₹0.08) | OpenAI TTS-1 (₹1.22) | **₹1.94** | Medium (~2s total) |
| **Most expensive** | Azure STT (₹3.54) | GPT-4o (₹1.30) | ElevenLabs v3 (₹6.12) | **₹10.96** | Medium (~1.5s total) |

---

## 5. Final Verdict for MiraeVaani

| Criteria | Current Stack | Verdict |
|----------|--------------|---------|
| **Indian language accuracy** | Sarvam STT + Gemini + Sarvam TTS | ✅ **Best available** — no other stack matches 22-language STT + 11-language TTS |
| **Cost** | ₹2.38/call AI-only | ✅ **Excellent** — among the cheapest Indian-language stacks |
| **Latency** | ~2s end-to-end | ⚠️ **Acceptable** — could improve with Sarvam streaming APIs |
| **Voice quality** | Sarvam Bulbul v2 | ✅ **Good** — upgrade to v3 for ₹1 more/call + sub-250ms streaming |

### Potential Optimizations

| Change | Impact | Trade-off |
|--------|--------|-----------|
| Switch to Sarvam **streaming STT** (WebSocket) | Latency ↓ ~500ms | Check API availability |
| Switch to Sarvam **Bulbul v3 streaming TTS** | Latency ↓ to sub-250ms TTFB | ₹1 more per call |
| Add **Groq Llama** as fallback LLM | Latency ↓ (~100ms TTFT) | Weaker on Indian langs, costs more than Gemini ($0.59/$0.79 vs $0.10/$0.40 per 1M) |
| Replace Twilio with **Exotel/Plivo** | Telephony cost ↓ 40–60% | Less global, India-only |
| Use **Bhashini** as cheaper Indian alternative | STT ₹0.55/min (vs Sarvam ₹0.50/min), TTS ₹0.95/1K (vs Sarvam ₹1.50/1K) | Higher latency (~2–3s), fewer languages (12 vs 22), ₹1,000/mo plan |

---

## 6. Pricing Sources (verified Aug 2026)

| Provider | Source URL |
|----------|-----------|
| Sarvam AI | [sarvam.ai/api-pricing](https://www.sarvam.ai/api-pricing) |
| Google Cloud STT | [cloud.google.com/speech-to-text/pricing](https://cloud.google.com/speech-to-text/pricing) |
| Deepgram | [deepgram.com/pricing](https://deepgram.com/pricing) |
| Microsoft Azure | [azure.microsoft.com/pricing/details/speech](https://azure.microsoft.com/en-us/pricing/details/speech/) |
| OpenAI | [developers.openai.com/api/docs/pricing](https://developers.openai.com/api/docs/pricing) |
| ElevenLabs | [elevenlabs.io/pricing/api](https://elevenlabs.io/pricing/api) |
| Groq | [console.groq.com/pricing](https://console.groq.com) |
| Bhashini | [bhashini.ai pricing page](https://www.bhashini.ai/pricing) — Starter ₹1,000/mo, STT ₹0.55/min, TTS ₹0.95/1K chars |
| Google Gemini | [ai.google.dev/pricing](https://ai.google.dev/pricing) |

---

> **Disclaimer:** Pricing and accuracy benchmarks are based on publicly available data verified in August 2026. Prices change frequently — always confirm current rates on provider websites before making procurement decisions. Accuracy percentages for Indian languages are estimated from community benchmarks and may vary by dialect, accent, and audio quality.
