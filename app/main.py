"""MiraeVaani 6.0 — configurable voice agents.

Where v5 hardcoded one agent from .env, v6 loads an AgentConfig row per call, so
providers, prompt, voice, language and turn-detection thresholds all come from
the UI. Credentials are entered in the browser and stored encrypted.

Endpoints:
    GET  /                          the UI
    GET  /api/catalog               providers, models, voices, languages, starters
    CRUD /api/agents                agent configuration
    CRUD /api/credentials           provider API keys (write-only, masked on read)
    POST /api/credentials/test      re-check the stored keys against the providers
    POST /api/tts/preview           voice sample for the builder
    POST /api/call                  place an outbound call with an agent
    GET  /api/calls[/{id}]          call history and detail
    GET  /api/agents/{id}/calls     one agent's own call log
    GET  /api/calls/{id}/audio      recorded turns, plus the stitched full.wav
    GET  /api/calls/{id}/events     live SSE feed of an in-progress call
    POST /api/twilio/voice|status   Twilio webhooks
    WS   /api/twilio/media-stream   bidirectional audio
"""

import asyncio
import json
import logging
import re
import time
import uuid
import wave
from contextlib import asynccontextmanager
from html import escape
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

from app import db, registry, verify
from app.audio import (
    mulaw_to_pcm,
    pcm_to_wav_bytes,
    resample_pcm,
    wav_bytes_to_pcm,
)
from app.call_handler import CallSession
from app.config import get_settings
from app.crypto import mask
from app.dialog import DialogEngine
from app.events import broker
from app.fillers import FillerBank, FillerController
from app.models import AgentConfig, CallRequest
from app.prompts import (
    STARTER_PROMPTS,
    build_system_prompt,
    declared_variables,
    render,
)
from app.registry import MissingCredential

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# call_id -> {agent_id, variables}. A cache in front of the calls table, not the
# source of truth: v5 popped an entry when the stream ended, so a Twilio
# websocket reconnect lost the call's context and killed the call. Entries are
# only evicted once CALL_CONTEXT_LIMIT newer calls exist.
CALL_CONTEXTS: dict[str, dict] = {}
CALL_CONTEXT_LIMIT = 500


def _remember_context(call_id: str, context: dict) -> None:
    CALL_CONTEXTS[call_id] = context
    while len(CALL_CONTEXTS) > CALL_CONTEXT_LIMIT:
        CALL_CONTEXTS.pop(next(iter(CALL_CONTEXTS)), None)


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
# provider -> (fetched_at, model ids). Live lists keep the dropdown honest
# without an HTTP round trip on every page load.
_MODEL_CACHE: dict[str, tuple[float, list[str]]] = {}
MODEL_CACHE_TTL = 300.0


async def _live_models(provider: str, credential: str) -> list[str] | None:
    cached = _MODEL_CACHE.get(provider)
    if cached and (time.monotonic() - cached[0]) < MODEL_CACHE_TTL:
        return cached[1] or None

    models = await verify.list_llm_models(provider, db.get_credential(credential))
    # Failures are cached too, or a provider that cannot list (xAI without
    # credits) is re-probed on every page load.
    _MODEL_CACHE[provider] = (time.monotonic(), models or [])
    return models


async def _llm_catalog() -> list[dict]:
    """The static list is the fallback; the provider's own list wins.

    Model ids get retired — Groq dropped the llama-3.x ids this app shipped
    with — and a stale dropdown entry only fails once a call is already live.
    """
    entries = registry.CATALOG["llm"]
    listings = await asyncio.gather(
        *(_live_models(e["provider"], e["credential"]) for e in entries)
    )
    return [
        {**entry, "models": models or entry["models"], "models_live": bool(models)}
        for entry, models in zip(entries, listings)
    ]


@app.get("/api/catalog")
async def get_catalog():
    saved = set(db.list_credential_providers())
    return {
        "stt": registry.CATALOG["stt"],
        "llm": await _llm_catalog(),
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
    """Save keys, but probe each one against its provider first.

    A key the provider rejects outright is not written: storing it only defers
    the failure to the middle of a call, where it looks like dead air.
    """
    body = await request.json()
    check = body.get("verify", True)

    incoming = {
        key: value.strip()
        for key, value in (body.get("providers") or {}).items()
        if value and value.strip()  # empty means "leave unchanged"
    }
    results = await verify.verify_many(incoming) if check else {}

    saved: list[str] = []
    rejected: list[str] = []
    for key, secret in incoming.items():
        if results.get(key) and results[key].rejected:
            rejected.append(key)
            continue
        db.set_credential(key, secret)
        saved.append(key)

    twilio_in = body.get("twilio") or {}
    if twilio_in:
        existing = db.get_credential_json("twilio")
        merged = {
            "account_sid": twilio_in.get("account_sid") or existing.get("account_sid", ""),
            "auth_token": twilio_in.get("auth_token") or existing.get("auth_token", ""),
            "from_number": twilio_in.get("from_number") or existing.get("from_number", ""),
        }
        if any(merged.values()):
            result = (
                await verify.verify_twilio(**merged)
                if check
                else verify.VerifyResult(verify.UNKNOWN, "Not checked.")
            )
            results["twilio"] = result
            if result.rejected:
                rejected.append("twilio")
            else:
                db.set_credential_json("twilio", merged)
                saved.append("twilio")

    return {
        "status": "saved" if not rejected else "partial",
        "saved": saved,
        "rejected": rejected,
        "results": verify.summarize(results),
    }


@app.post("/api/credentials/test")
async def test_credentials():
    """Re-check what is already stored, without changing anything."""
    stored = {
        slot["key"]: db.get_credential(slot["key"]) for slot in registry.credential_slots()
    }
    results = await verify.verify_many({k: v for k, v in stored.items() if v})

    twilio = db.get_credential_json("twilio")
    if twilio.get("account_sid") or twilio.get("auth_token"):
        results["twilio"] = await verify.verify_twilio(
            twilio.get("account_sid", ""),
            twilio.get("auth_token", ""),
            twilio.get("from_number", ""),
        )

    return {"results": verify.summarize(results)}


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
        # The greeting is rendered with the same values, so a placeholder used
        # only there must still be asked for before dialling.
        "variables": declared_variables(
            f"{agent.system_prompt}\n{agent.greeting_text}"
        ),
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

    # A retired model id is indistinguishable from a working agent until the
    # caller picks up and every turn 404s into the fallback line.
    credential = registry.credential_key("llm", agent.llm_provider)
    available = await _live_models(agent.llm_provider, credential) if credential else None
    if available and agent.llm_model not in available:
        return JSONResponse(
            status_code=400,
            content={
                "error": (
                    f"'{agent.llm_model}' is not available on your {agent.llm_provider} "
                    f"account. Edit the agent and pick one of: {', '.join(available[:8])}"
                )
            },
        )

    call_id = uuid.uuid4().hex[:16]
    _remember_context(call_id, {"agent_id": agent.id, "variables": req.variables})
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
async def get_calls(limit: int = 50, agent_id: str | None = None):
    return {"calls": db.list_calls(limit, agent_id)}


@app.get("/api/agents/{agent_id}/calls")
async def get_agent_calls(agent_id: str, limit: int = 50):
    return {"calls": db.list_calls(limit, agent_id)}


@app.get("/api/calls/{call_id}")
async def get_call(call_id: str):
    call = db.get_call(call_id)
    if call is None:
        return JSONResponse(status_code=404, content={"error": "Call not found"})
    return {"call": call, "turns": db.list_turns(call_id)}


# ---------------------------------------------------------------------------
# Call audio
#
# Every turn is already written to the call's log folder as a WAV by
# CallLogger, so playback needs no new capture path — it only needs those files
# named, ordered and (for the whole conversation) stitched into one stream.
# ---------------------------------------------------------------------------
TURN_CLIP_RE = re.compile(r"^turn_(\d{3})_(user|agent)\.wav$")
PLAYBACK_RATE = 16000
GAP_SECONDS = 0.25


def _audio_dir(call: dict) -> Path | None:
    log_dir = call.get("log_dir")
    if not log_dir:
        return None
    directory = Path(log_dir) / "audio"
    return directory if directory.is_dir() else None


def _clips(call: dict) -> list[dict]:
    """Turn WAVs in the order they were spoken — the caller before the reply."""
    directory = _audio_dir(call)
    if directory is None:
        return []
    found = []
    for path in directory.iterdir():
        match = TURN_CLIP_RE.match(path.name)
        if match:
            role = match.group(2)
            found.append({
                "turn": int(match.group(1)),
                "role": role,
                "name": path.name,
                # Within a turn the caller speaks first, so `user` sorts ahead.
                "_order": (int(match.group(1)), 0 if role == "user" else 1),
            })
    found.sort(key=lambda c: c["_order"])
    for clip in found:
        clip.pop("_order")
    return found


@app.get("/api/calls/{call_id}/audio")
async def get_call_audio(call_id: str):
    call = db.get_call(call_id)
    if call is None:
        return JSONResponse(status_code=404, content={"error": "Call not found"})
    clips = _clips(call)
    for clip in clips:
        clip["url"] = f"/api/calls/{call_id}/audio/clip?name={clip['name']}"
    return {
        "available": bool(clips),
        "clips": clips,
        "full_url": f"/api/calls/{call_id}/audio/full.wav" if clips else None,
    }


@app.get("/api/calls/{call_id}/audio/full.wav")
async def get_call_audio_full(call_id: str):
    call = db.get_call(call_id)
    if call is None:
        return JSONResponse(status_code=404, content={"error": "Call not found"})
    directory = _audio_dir(call)
    clips = _clips(call)
    if directory is None or not clips:
        return JSONResponse(status_code=404, content={"error": "No recording for this call"})

    gap = b"\x00" * (int(PLAYBACK_RATE * GAP_SECONDS) * 2)
    merged = bytearray()
    for clip in clips:
        try:
            pcm, rate = wav_bytes_to_pcm((directory / clip["name"]).read_bytes())
        except (OSError, wave.Error):
            logger.warning("Skipping unreadable clip %s", clip["name"])
            continue
        if merged:
            merged.extend(gap)
        merged.extend(resample_pcm(pcm, rate, PLAYBACK_RATE))

    if not merged:
        return JSONResponse(status_code=404, content={"error": "No recording for this call"})
    return Response(
        content=pcm_to_wav_bytes(bytes(merged), PLAYBACK_RATE),
        media_type="audio/wav",
        headers={"Content-Disposition": f'inline; filename="{call_id}.wav"'},
    )


@app.get("/api/calls/{call_id}/audio/clip")
async def get_call_audio_clip(call_id: str, name: str):
    call = db.get_call(call_id)
    if call is None:
        return JSONResponse(status_code=404, content={"error": "Call not found"})
    directory = _audio_dir(call)
    # The name is matched against the turn pattern rather than sanitized, so no
    # traversal or arbitrary filename can reach the filesystem here.
    if directory is None or not TURN_CLIP_RE.match(name):
        return JSONResponse(status_code=404, content={"error": "Clip not found"})
    path = directory / name
    if not path.is_file():
        return JSONResponse(status_code=404, content={"error": "Clip not found"})
    return FileResponse(path, media_type="audio/wav")


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
    # The id travels three ways on purpose: a custom parameter, the stream URL's
    # query string, and (in the DB) the CallSid. Twilio has been seen to deliver
    # a start frame with no customParameters at all, which used to strand the
    # call on "No agent for call_id=".
    safe_id = escape(call_id, quote=True)
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}?call_id={safe_id}">
            <Parameter name="call_id" value="{safe_id}"/>
        </Stream>
    </Connect>
</Response>"""
    if not call_id:
        logger.warning("Voice webhook hit without a call_id: %s", request.url)
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
def _normalize(name: str) -> str:
    """'Call_Id', 'callId' and 'call_id' all have to mean the same thing."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _resolve_call_context(start: dict, websocket: WebSocket) -> tuple[str, dict]:
    """Work out which call this stream belongs to, cheapest source first.

    Custom parameters are the intended channel, but they are the one part of the
    start frame Twilio has been observed to drop, so the stream URL's query
    string and finally the CallSid (always present) back them up. The DB is
    consulted when the in-memory context is gone — a server restart between
    dialing and answering used to be fatal.
    """
    params = {_normalize(k): v for k, v in (start.get("customParameters") or {}).items()}
    call_id = params.get("callid") or websocket.query_params.get("call_id", "") or ""

    if call_id and call_id in CALL_CONTEXTS:
        return call_id, CALL_CONTEXTS[call_id]

    record = db.get_call(call_id) if call_id else None
    if record is None:
        call_sid = start.get("callSid") or ""
        record = db.find_call_by_sid(call_sid) if call_sid else None
        if record is not None:
            logger.warning(
                "Stream carried no usable call_id; recovered %s from CallSid %s",
                record["id"],
                call_sid,
            )

    if record is None:
        return call_id, {}

    try:
        variables = json.loads(record.get("variables") or "{}")
    except json.JSONDecodeError:
        variables = {}
    context = {"agent_id": record["agent_id"], "variables": variables}
    _remember_context(record["id"], context)
    return record["id"], context


@app.websocket("/api/twilio/media-stream")
async def twilio_media_stream(websocket: WebSocket):
    await websocket.accept()

    # Twilio sends a "connected" frame before "start", so read until the start
    # frame arrives — that is the one carrying our custom parameters.
    first = None
    try:
        for _ in range(10):
            message = json.loads(await websocket.receive_text())
            if message.get("event") == "start":
                first = message
                break
    except Exception:
        first = None

    if first is None:
        logger.error("No start frame on media stream — closing")
        await websocket.close()
        return

    call_id, context = _resolve_call_context(first["start"], websocket)
    agent = db.get_agent(context.get("agent_id", "")) if context else None
    if agent is None:
        logger.error(
            "No agent for call_id=%r — closing stream. start frame=%s",
            call_id,
            json.dumps(first["start"], default=str)[:600],
        )
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
    # A static greeting is authored with the same $placeholders as the prompt, so
    # it has to be rendered too — otherwise the caller is greeted by name as
    # "$customer_name".
    if agent.greeting_text:
        agent.greeting_text = render(agent.greeting_text, context.get("variables"))
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


# ---------------------------------------------------------------------------
# UI (mounted last so it never shadows /api)
# ---------------------------------------------------------------------------
@app.get("/")
async def index():
    # The shell carries the ?v= stamps for the CSS/JS, so it must never be the
    # stale copy a browser held on to.
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.APP_HOST, port=settings.APP_PORT, reload=False)
