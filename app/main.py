"""MiraeVaani 6.0 — configurable voice agents.

Where v5 hardcoded one agent from .env, v6 loads an AgentConfig row per call, so
providers, prompt, voice, language and turn-detection thresholds all come from
the UI. Credentials are entered in the browser and stored encrypted.

Endpoints:
    GET  /                          the UI
    GET  /api/catalog               providers, models, voices, languages, starters
    CRUD /api/agents                agent configuration
    CRUD /api/credentials           provider API keys (write-only, masked on read)
    POST /api/tts/preview           voice sample for the builder
    POST /api/call                  place an outbound call with an agent
    GET  /api/calls[/{id}]          call history and detail
    GET  /api/calls/{id}/events     live SSE feed of an in-progress call
    POST /api/twilio/voice|status   Twilio webhooks
    WS   /api/twilio/media-stream   bidirectional audio
"""

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from twilio.rest import Client as TwilioClient

from app import db, registry
from app.audio import mulaw_to_pcm, pcm_to_wav_bytes
from app.call_handler import CallSession
from app.config import get_settings
from app.crypto import mask
from app.dialog import DialogEngine
from app.events import broker
from app.fillers import FillerBank, FillerController
from app.models import AgentConfig, CallRequest
from app.prompts import STARTER_PROMPTS, build_system_prompt, declared_variables
from app.registry import MissingCredential

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# call_id -> {agent_id, variables}. Read, never popped: v5 popped this, so a
# Twilio websocket reconnect lost the call's context and killed the call.
CALL_CONTEXTS: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    logger.info("MiraeVaani 6.0 ready. BASE_URL=%s DB=%s",
                settings.BASE_URL, settings.DB_PATH)
    yield


app = FastAPI(title="MiraeVaani 6.0", version="6.0.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "MiraeVaani 6.0"}


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
@app.get("/api/catalog")
async def get_catalog():
    saved = set(db.list_credential_providers())
    return {
        "stt": registry.CATALOG["stt"],
        "llm": registry.CATALOG["llm"],
        "tts": registry.CATALOG["tts"],
        "languages": registry.LANGUAGES,
        "starters": STARTER_PROMPTS,
        "credentials": [
            {**slot, "configured": slot["key"] in saved}
            for slot in registry.credential_slots()
        ],
        "twilio_configured": "twilio" in saved,
    }


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
@app.get("/api/credentials")
async def get_credentials():
    out = []
    for slot in registry.credential_slots():
        secret = db.get_credential(slot["key"])
        out.append({**slot, "configured": bool(secret), "masked": mask(secret)})

    twilio = db.get_credential_json("twilio")
    return {
        "providers": out,
        "twilio": {
            "account_sid": twilio.get("account_sid", ""),
            "from_number": twilio.get("from_number", ""),
            "auth_token_masked": mask(twilio.get("auth_token", "")),
            "configured": bool(twilio.get("account_sid") and twilio.get("auth_token")),
        },
    }


@app.post("/api/credentials")
async def save_credentials(request: Request):
    body = await request.json()

    for key, value in (body.get("providers") or {}).items():
        if value:  # empty means "leave unchanged"
            db.set_credential(key, value.strip())

    twilio_in = body.get("twilio") or {}
    if twilio_in:
        existing = db.get_credential_json("twilio")
        merged = {
            "account_sid": twilio_in.get("account_sid") or existing.get("account_sid", ""),
            "auth_token": twilio_in.get("auth_token") or existing.get("auth_token", ""),
            "from_number": twilio_in.get("from_number") or existing.get("from_number", ""),
        }
        db.set_credential_json("twilio", merged)

    return {"status": "saved"}


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------
@app.get("/api/agents")
async def get_agents():
    return {"agents": db.list_agents()}


@app.post("/api/agents")
async def post_agent(agent: AgentConfig):
    return db.create_agent(agent).model_dump()


@app.get("/api/agents/{agent_id}")
async def get_agent(agent_id: str):
    agent = db.get_agent(agent_id)
    if agent is None:
        return JSONResponse(status_code=404, content={"error": "Agent not found"})
    return {
        **agent.model_dump(),
        "variables": declared_variables(agent.system_prompt),
    }


@app.put("/api/agents/{agent_id}")
async def put_agent(agent_id: str, agent: AgentConfig):
    updated = db.update_agent(agent_id, agent)
    if updated is None:
        return JSONResponse(status_code=404, content={"error": "Agent not found"})
    return updated.model_dump()


@app.delete("/api/agents/{agent_id}")
async def remove_agent(agent_id: str):
    db.delete_agent(agent_id)
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Voice preview
# ---------------------------------------------------------------------------
@app.post("/api/tts/preview")
async def tts_preview(request: Request):
    """Synthesize a short sample so the builder can audition a voice."""
    body = await request.json()
    probe = AgentConfig(
        tts_provider=body.get("provider", "sarvam"),
        tts_voice=body.get("voice", ""),
        language=body.get("language", "hi-IN"),
    )
    text = body.get("text") or "नमस्ते, मैं वाणी बोल रही हूँ। मैं आपकी कैसे मदद कर सकती हूँ?"

    try:
        tts = registry.build_tts(probe)
    except MissingCredential as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    try:
        mulaw = await tts.synthesize(text, probe.language)
    finally:
        await tts.close()

    if not mulaw:
        return JSONResponse(status_code=502, content={"error": "Synthesis returned no audio"})

    wav = pcm_to_wav_bytes(mulaw_to_pcm(mulaw), 8000)
    return Response(content=wav, media_type="audio/wav")


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------
@app.post("/api/call")
async def initiate_call(req: CallRequest):
    agent = db.get_agent(req.agent_id)
    if agent is None:
        return JSONResponse(status_code=404, content={"error": "Agent not found"})

    twilio = db.get_credential_json("twilio")
    if not (twilio.get("account_sid") and twilio.get("auth_token") and twilio.get("from_number")):
        return JSONResponse(
            status_code=400,
            content={"error": "Twilio is not configured. Add it on the Credentials page."},
        )

    # Fail before dialing if a provider key is missing, rather than answering to silence.
    try:
        for build in (registry.build_stt, registry.build_llm, registry.build_tts):
            probe = build(agent)
            await probe.close()
    except MissingCredential as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    call_id = uuid.uuid4().hex[:16]
    CALL_CONTEXTS[call_id] = {"agent_id": agent.id, "variables": req.variables}
    db.create_call(call_id, agent.id, req.to, req.variables)

    base = settings.BASE_URL.rstrip("/")
    try:
        client = TwilioClient(twilio["account_sid"], twilio["auth_token"])
        call = client.calls.create(
            to=req.to,
            from_=twilio["from_number"],
            url=f"{base}/api/twilio/voice?call_id={call_id}",
            method="POST",
            status_callback=f"{base}/api/twilio/status",
            status_callback_method="POST",
            status_callback_event=["initiated", "answered", "completed"],
        )
    except Exception as exc:
        CALL_CONTEXTS.pop(call_id, None)
        db.update_call(call_id, status="failed")
        logger.exception("Failed to initiate call")
        return JSONResponse(status_code=502, content={"error": str(exc)})

    db.update_call(call_id, call_sid=call.sid, status="dialing")
    logger.info("Outbound call: sid=%s to=%s agent=%s", call.sid, req.to, agent.name)
    return {"call_id": call_id, "call_sid": call.sid, "status": "dialing"}


@app.get("/api/calls")
async def get_calls(limit: int = 50):
    return {"calls": db.list_calls(limit)}


@app.get("/api/calls/{call_id}")
async def get_call(call_id: str):
    call = db.get_call(call_id)
    if call is None:
        return JSONResponse(status_code=404, content={"error": "Call not found"})
    return {"call": call, "turns": db.list_turns(call_id)}


@app.get("/api/calls/{call_id}/events")
async def stream_call_events(call_id: str):
    """Server-sent events so the UI can watch a call turn by turn."""
    queue = broker.subscribe(call_id)

    async def generator():
        try:
            yield "retry: 2000\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
                if payload.get("event") == "call_end":
                    break
        finally:
            broker.unsubscribe(call_id, queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Twilio webhooks
# ---------------------------------------------------------------------------
@app.post("/api/twilio/voice")
async def twilio_voice_webhook(request: Request):
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
    call_sid = form.get("CallSid", "")
    status = form.get("CallStatus", "")
    logger.info("Call status: sid=%s status=%s", call_sid, status)

    record = db.find_call_by_sid(call_sid)
    if record:
        fields: dict = {"status": status}
        if status == "completed" and form.get("CallDuration"):
            fields["duration_s"] = float(form["CallDuration"])
        db.update_call(record["id"], **fields)
        broker.publish(record["id"], "status", {"status": status})
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Twilio Media Stream WebSocket
# ---------------------------------------------------------------------------
@app.websocket("/api/twilio/media-stream")
async def twilio_media_stream(websocket: WebSocket):
    await websocket.accept()

    # Peek the first frame to learn which call (and therefore which agent) this is.
    try:
        first_raw = await websocket.receive_text()
        first = json.loads(first_raw)
    except Exception:
        await websocket.close()
        return

    call_id = ""
    if first.get("event") == "start":
        call_id = (first["start"].get("customParameters") or {}).get("call_id", "")

    context = CALL_CONTEXTS.get(call_id) or {}
    agent = db.get_agent(context.get("agent_id", "")) if context else None
    if agent is None:
        logger.error("No agent for call_id=%s — closing stream", call_id)
        await websocket.close()
        return

    try:
        stt = registry.build_stt(agent)
        tts = registry.build_tts(agent)
        llm = registry.build_llm(agent)
    except MissingCredential:
        logger.exception("Missing credential at stream time")
        await websocket.close()
        return

    system_prompt = build_system_prompt(agent.system_prompt, context.get("variables"))
    dialog = DialogEngine(
        llm,
        system_prompt,
        temperature=agent.temperature,
        max_output_tokens=agent.max_output_tokens,
        on_event=lambda name, data: broker.publish(call_id, name, data),
    )

    fillers = None
    if agent.fillers_enabled:
        languages = [agent.language] + (["en-IN"] if agent.language != "en-IN" else ["hi-IN"])
        fillers = FillerController(FillerBank(tts, languages), enabled=True)

    session = CallSession(
        websocket,
        agent=agent,
        stt=stt,
        tts=tts,
        dialog=dialog,
        fillers=fillers,
        call_id=call_id,
        logs_dir=settings.LOGS_DIR,
        twilio_creds=db.get_credential_json("twilio"),
    )

    try:
        await session._on_start(first["start"])
        await session.handle()
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception:
        logger.exception("Media stream error")
    finally:
        CALL_CONTEXTS.pop(call_id, None)


# ---------------------------------------------------------------------------
# UI (mounted last so it never shadows /api)
# ---------------------------------------------------------------------------
@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.APP_HOST, port=settings.APP_PORT, reload=False)
