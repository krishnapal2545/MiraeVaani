"""MiraeVaani 4.0 — FastAPI entry point.

Pipeline: Twilio Media Streams <-> Sarvam Saarika STT -> Gemini 2.5 Flash-Lite -> Sarvam Bulbul TTS
"""

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from app.asr import SarvamSTT
from app.call_handler import CallSession
from app.call_manager import CallManager
from app.config import get_settings
from app.tts import SarvamTTS

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# In-memory call context store (POC — use Redis in production).
CALL_CONTEXTS: dict[str, dict] = {}

# Shared API clients (connection pooling across calls).
stt_client: SarvamSTT | None = None
tts_client: SarvamTTS | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global stt_client, tts_client
    stt_client = SarvamSTT()
    tts_client = SarvamTTS()
    logger.info("Sarvam clients initialized. BASE_URL=%s", settings.BASE_URL)
    yield
    await stt_client.close()
    await tts_client.close()


app = FastAPI(
    title="MiraeVaani 4.0",
    description="Multilingual AI voice agent (Sarvam + Gemini + Twilio)",
    version="4.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "MiraeVaani 4.0"}


# ---------------------------------------------------------------------------
# Outbound call API
# ---------------------------------------------------------------------------
@app.post("/api/call")
async def initiate_call(request: Request):
    """Initiate an outbound call.

    Body JSON:
    {
        "to": "+919876543210",
        "scenario": "margin_shortfall",
        "customer_name": "Rajesh Kumar",
        "metadata": {
            "account_id": "SHA123456",
            "shortfall_amount": "50000 rupees",
            "deadline": "30 June, 3:30 PM"
        }
    }
    """
    body = await request.json()
    to_number = body.get("to")
    if not to_number:
        return JSONResponse(status_code=400, content={"error": "Missing 'to' phone number"})

    call_id = uuid.uuid4().hex
    CALL_CONTEXTS[call_id] = {
        "scenario": body.get("scenario", "margin_shortfall"),
        "customer_name": body.get("customer_name", "Customer"),
        **(body.get("metadata") or {}),
    }

    try:
        call_sid = CallManager().initiate_call(to_number=to_number, call_id=call_id)
        return {"call_sid": call_sid, "call_id": call_id, "status": "initiated"}
    except Exception as e:
        CALL_CONTEXTS.pop(call_id, None)
        logger.exception("Failed to initiate call")
        return JSONResponse(status_code=500, content={"error": str(e)})


# ---------------------------------------------------------------------------
# Twilio webhooks
# ---------------------------------------------------------------------------
@app.post("/api/twilio/voice")
async def twilio_voice_webhook(request: Request):
    """Returns TwiML connecting the call to our bidirectional media stream.

    Outbound calls carry ?call_id=...; inbound calls (no call_id) get the
    generic inbound support scenario.
    """
    call_id = request.query_params.get("call_id", "")
    ws_url = (
        settings.BASE_URL.rstrip("/")
        .replace("https://", "wss://")
        .replace("http://", "ws://")
        + "/api/twilio/media-stream"
    )

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}">
            <Parameter name="call_id" value="{call_id}"/>
        </Stream>
    </Connect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.post("/api/twilio/status")
async def twilio_status_callback(request: Request):
    form = await request.form()
    logger.info(
        "Call status: sid=%s status=%s",
        form.get("CallSid", ""), form.get("CallStatus", ""),
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Twilio Media Stream WebSocket
# ---------------------------------------------------------------------------
def _resolve_context(call_id: str | None) -> dict:
    if call_id and call_id in CALL_CONTEXTS:
        return CALL_CONTEXTS.pop(call_id)
    return {"scenario": "inbound", "customer_name": "Customer"}


@app.websocket("/api/twilio/media-stream")
async def twilio_media_stream(websocket: WebSocket):
    await websocket.accept()
    session = CallSession(
        websocket=websocket,
        stt=stt_client,
        tts=tts_client,
        context_resolver=_resolve_context,
    )
    try:
        await session.handle()
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception:
        logger.exception("Media stream error")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.APP_HOST, port=settings.APP_PORT, reload=False)
