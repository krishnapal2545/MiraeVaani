"""MiraeVaani 2.0 — FastAPI application entry point."""

import logging
from html import escape as xml_escape
from urllib.parse import urlencode

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from app.call_handler import CallHandler
from app.call_manager import CallManager
from app.config import get_settings
from app.llm import DialogEngine
from app.prompts import get_prompt

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="MiraeVaani 2.0",
    description="AI Voice Agent — Open Source Stack (Whisper + Ollama + XTTS)",
    version="2.0.0",
)

# In-memory stores (use Redis in production)
active_calls: dict[str, dict] = {}
dialog_sessions: dict[str, DialogEngine] = {}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "MiraeVaani 2.0",
        "stt_url": settings.STT_BASE_URL or "NOT CONFIGURED",
        "llm_url": settings.LLM_BASE_URL or "NOT CONFIGURED",
        "tts_url": settings.TTS_BASE_URL or "NOT CONFIGURED",
    }


# ---------------------------------------------------------------------------
# Outbound call API
# ---------------------------------------------------------------------------
@app.post("/api/call")
async def initiate_call(request: Request):
    """
    Initiate an outbound call.

    Body JSON example:
    {
        "to": "+919876543210",
        "scenario": "margin_shortfall",
        "customer_name": "Rajesh Kumar",
        "metadata": {
            "account_id": "SHA123456",
            "shortfall_amount": "50000",
            "deadline": "2 July 2025, 3:30 PM"
        }
    }
    """
    body = await request.json()
    to_number = body.get("to")
    scenario = body.get("scenario", "margin_shortfall")
    customer_name = body.get("customer_name", "Customer")
    metadata = body.get("metadata", {})

    if not to_number:
        return JSONResponse(status_code=400, content={"error": "Missing 'to' phone number"})

    try:
        manager = CallManager()
        call_sid = manager.initiate_call(
            to_number=to_number,
            scenario=scenario,
            customer_name=customer_name,
            metadata=metadata,
        )
        active_calls[call_sid] = {
            "scenario": scenario,
            "customer_name": customer_name,
            "metadata": metadata,
        }
        return {"call_sid": call_sid, "status": "initiated"}

    except Exception as e:
        logger.exception("Failed to initiate call")
        return JSONResponse(status_code=500, content={"error": str(e)})


# ---------------------------------------------------------------------------
# Twilio voice webhook — returns TwiML to open a Media Stream WebSocket
# ---------------------------------------------------------------------------
@app.post("/api/twilio/voice")
async def twilio_voice_webhook(request: Request):
    """
    Twilio hits this when the call connects.
    We return TwiML that opens a bidirectional Media Stream WebSocket
    so we can pipe audio through our VAD → ASR → LLM → TTS pipeline.
    """
    scenario = request.query_params.get("scenario", "margin_shortfall")
    customer_name = request.query_params.get("customer_name", "Customer")
    account_id = request.query_params.get("account_id", "N/A")
    shortfall_amount = request.query_params.get("shortfall_amount", "N/A")
    deadline = request.query_params.get("deadline", "N/A")
    expiry_date = request.query_params.get("expiry_date", "N/A")

    form = await request.form()
    call_sid = form.get("CallSid", "unknown")

    # Build WebSocket URL for the media stream
    base_url = settings.BASE_URL.rstrip("/")
    ws_base = base_url.replace("https://", "wss://").replace("http://", "ws://")

    ws_params = urlencode({
        "scenario": scenario,
        "customer_name": customer_name,
        "account_id": account_id,
        "shortfall_amount": shortfall_amount,
        "deadline": deadline,
        "expiry_date": expiry_date,
        "call_sid": call_sid,
    })
    ws_url = f"{ws_base}/api/twilio/media-stream?{ws_params}"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{xml_escape(ws_url)}" />
    </Connect>
</Response>"""

    logger.info(
        "Voice webhook: scenario=%s customer=%s call_sid=%s",
        scenario,
        customer_name,
        call_sid,
    )
    return Response(content=twiml, media_type="application/xml")


# ---------------------------------------------------------------------------
# Twilio Media Stream WebSocket — real-time bidirectional audio
# ---------------------------------------------------------------------------
@app.websocket("/api/twilio/media-stream")
async def twilio_media_stream(websocket: WebSocket):
    """
    Bidirectional WebSocket for Twilio Media Streams.
    Receives raw mulaw audio from Twilio and sends back synthesized speech.
    """
    await websocket.accept()

    scenario = websocket.query_params.get("scenario", "margin_shortfall")
    customer_name = websocket.query_params.get("customer_name", "Customer")
    account_id = websocket.query_params.get("account_id", "N/A")
    shortfall_amount = websocket.query_params.get("shortfall_amount", "N/A")
    deadline = websocket.query_params.get("deadline", "N/A")
    expiry_date = websocket.query_params.get("expiry_date", "N/A")
    call_sid = websocket.query_params.get("call_sid", "unknown")

    system_prompt = get_prompt(
        scenario,
        customer_name=customer_name,
        account_id=account_id,
        shortfall_amount=shortfall_amount,
        deadline=deadline,
        expiry_date=expiry_date,
    )

    logger.info(
        "Media stream connected: scenario=%s customer=%s call=%s",
        scenario,
        customer_name,
        call_sid,
    )

    dialog_engine = DialogEngine(system_prompt=system_prompt)
    handler = CallHandler(websocket=websocket, dialog_engine=dialog_engine)

    try:
        await handler.handle()
    except WebSocketDisconnect:
        logger.info("Media stream WebSocket disconnected: call=%s", call_sid)
    except Exception:
        logger.exception("Media stream error: call=%s", call_sid)
    finally:
        active_calls.pop(call_sid, None)


# ---------------------------------------------------------------------------
# Twilio status callback
# ---------------------------------------------------------------------------
@app.post("/api/twilio/status")
async def twilio_status_callback(request: Request):
    """Receive call status updates from Twilio."""
    form = await request.form()
    call_sid = form.get("CallSid", "")
    call_status = form.get("CallStatus", "")
    logger.info("Call status: SID=%s status=%s", call_sid, call_status)

    if call_status in ("completed", "failed", "busy", "no-answer"):
        active_calls.pop(call_sid, None)

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Active calls listing (debug)
# ---------------------------------------------------------------------------
@app.get("/api/calls")
async def list_active_calls():
    return {"active_calls": active_calls}
