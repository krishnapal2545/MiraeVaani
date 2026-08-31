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
import contextlib
import json
import logging
import re
import time
import uuid
import wave
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path

from websockets.asyncio.client import connect as ws_connect

from fastapi import (
    FastAPI,
    File,
    Form,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from app import contacts, db, dialer, registry, runner, schedule, verify
from app.audio import (
    mulaw_to_pcm,
    pcm_to_wav_bytes,
    resample_pcm,
    wav_bytes_to_pcm,
)
from app.config import get_settings
from app.crypto import mask
from app.events import broker
from app.models import AgentConfig, CallRequest
from app.prompts import STARTER_PROMPTS, declared_variables
from app.registry import MissingCredential

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# asyncio only holds a weak reference to a running task, so a fire-and-forget
# create_task() can be collected part-way through. Restarting a worker must not
# be abandoned half-done, so the handles are kept until the task finishes.
_BACKGROUND: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    # Workers from a previous run are still holding ports and may still be
    # dialing; their process handles died with that run, so they are found
    # through the registry rather than adopted.
    await runner.reap_orphans()
    logger.info("MiraeVaani 6.0 ready. BASE_URL=%s DB=%s",
                settings.BASE_URL, settings.DB_PATH)
    yield
    await runner.stop_all()


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
_VOICE_CACHE: dict[str, tuple[float, list[dict]]] = {}
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


async def _live_voices(provider: str, credential: str) -> list[dict] | None:
    cached = _VOICE_CACHE.get(provider)
    if cached and (time.monotonic() - cached[0]) < MODEL_CACHE_TTL:
        return cached[1] or None

    voices = await verify.list_tts_voices(provider, db.get_credential(credential))
    _VOICE_CACHE[provider] = (time.monotonic(), voices or [])
    return voices


async def _tts_catalog() -> list[dict]:
    """Same bargain as the model lists: the account's own voices win.

    Smallest.ai carries 217 voices plus whatever the account has cloned, so the
    hardcoded list in the adapter can only ever be a starting point.
    """
    entries = registry.CATALOG["tts"]
    listings = await asyncio.gather(
        *(_live_voices(e["provider"], e["credential"]) for e in entries)
    )
    return [
        {**entry, "voices": voices or entry["voices"], "voices_live": bool(voices)}
        for entry, voices in zip(entries, listings)
    ]


async def _check_model_available(agent: AgentConfig) -> str:
    """The reason the agent cannot dial, or "" if it can.

    A retired model id is indistinguishable from a working agent until the
    caller picks up and every turn 404s into the fallback line. This costs an
    HTTP round trip to the provider, so a campaign runs it once when it starts
    rather than before each of its calls.
    """
    credential = registry.credential_key("llm", agent.llm_provider)
    if not credential:
        return ""
    available = await _live_models(agent.llm_provider, credential)
    if available and agent.llm_model not in available:
        return (
            f"'{agent.llm_model}' is not available on your {agent.llm_provider} "
            f"account. Edit the agent and pick one of: {', '.join(available[:8])}"
        )
    return ""


@app.get("/api/catalog")
async def get_catalog():
    saved = set(db.list_credential_providers())
    return {
        "stt": registry.CATALOG["stt"],
        "llm": await _llm_catalog(),
        "tts": await _tts_catalog(),
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


def _language_error(agent: AgentConfig) -> str:
    """Why this agent's languages cannot work on the providers it names.

    Saving an unsupported pair silently is what produced a Smallest agent that
    spoke Tamil and transcribed nothing for a whole call, so the save is refused
    rather than quietly narrowed.
    """
    usable = registry.supported_languages(agent.stt_provider, agent.tts_provider)
    wanted = [agent.language, *agent.allowed_languages.split(",")]
    unusable = [
        code for code in dict.fromkeys(c.strip() for c in wanted)
        if code and code not in usable
    ]
    if not unusable:
        return ""
    names = {lang["code"]: lang["name"] for lang in registry.LANGUAGES}
    return (
        f"{', '.join(names.get(c, c) for c in unusable)} cannot be used with "
        f"{agent.stt_provider} speech-to-text and {agent.tts_provider} "
        f"text-to-speech. Supported: "
        f"{', '.join(names.get(c, c) for c in usable) or 'nothing in common'}."
    )


@app.post("/api/agents")
async def post_agent(agent: AgentConfig):
    error = _language_error(agent)
    if error:
        return JSONResponse(status_code=400, content={"error": error})
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
    error = _language_error(agent)
    if error:
        return JSONResponse(status_code=400, content={"error": error})
    updated = db.update_agent(agent_id, agent)
    if updated is None:
        return JSONResponse(status_code=404, content={"error": "Agent not found"})
    # A worker holds the config it read at startup, so an edit only reaches it
    # by replacing it. This runs in the background: the old worker finishes the
    # calls it already has, each on the config it began the call with.
    if runner.live_worker(agent_id) is not None:
        _spawn(runner.restart(agent_id))
    return updated.model_dump()


@app.delete("/api/agents/{agent_id}")
async def remove_agent(agent_id: str):
    await runner.stop(agent_id, wait=False)
    db.delete_worker(agent_id)
    db.delete_agent(agent_id)
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------
@app.get("/api/workers")
async def get_workers():
    return {"workers": db.list_workers()}


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------
@app.get("/api/campaigns")
async def get_campaigns():
    return {"campaigns": db.list_campaigns()}


@app.post("/api/campaigns")
async def post_campaign(request: Request):
    body = await request.json()
    if db.get_agent(body.get("agent_id", "")) is None:
        return JSONResponse(status_code=400, content={"error": "Unknown agent"})
    if db.get_contact_list(body.get("list_id", "")) is None:
        return JSONResponse(status_code=400, content={"error": "Unknown contact list"})
    return {"id": db.create_campaign(body)}


@app.put("/api/campaigns/{campaign_id}")
async def put_campaign(campaign_id: str, request: Request):
    """Edit a campaign in place. Schedule and limits apply to a running
    campaign within one dispatch cycle, because the worker re-reads the
    campaign row every time round.

    Swapping the agent or the list is different: the queued targets carry the
    agent id they were enqueued with, so they have to be realigned, and doing
    that under a worker that is claiming from the same rows would hand a few
    contacts to the wrong agent. Hence the pause requirement.
    """
    campaign = db.get_campaign(campaign_id)
    if campaign is None:
        return JSONResponse(status_code=404, content={"error": "Campaign not found"})

    body = await request.json()
    agent_id = body.get("agent_id", campaign["agent_id"])
    list_id = body.get("list_id", campaign["list_id"])
    if db.get_agent(agent_id) is None:
        return JSONResponse(status_code=400, content={"error": "Unknown agent"})
    if db.get_contact_list(list_id) is None:
        return JSONResponse(status_code=400, content={"error": "Unknown contact list"})

    moved = agent_id != campaign["agent_id"] or list_id != campaign["list_id"]
    if moved and campaign["status"] == "running":
        return JSONResponse(status_code=409, content={
            "error": "Pause the campaign before changing its agent or contact list."
        })

    db.update_campaign(campaign_id, body)
    if moved:
        db.retarget_pending(campaign_id, agent_id, list_id)
    return db.get_campaign(campaign_id)


@app.post("/api/campaigns/{campaign_id}/start")
async def start_campaign(campaign_id: str):
    """Check what can only be checked once, enqueue the list, start the worker.

    The model check costs a round trip to the provider, so it happens here
    rather than before every call: a retired model id would otherwise only
    surface once each customer had already picked up.
    """
    campaign = db.get_campaign(campaign_id)
    if campaign is None:
        return JSONResponse(status_code=404, content={"error": "Campaign not found"})
    agent = db.get_agent(campaign["agent_id"])
    if agent is None:
        return JSONResponse(status_code=400, content={"error": "Agent no longer exists"})

    error = await _check_model_available(agent)
    if error:
        return JSONResponse(status_code=400, content={"error": error})

    added = db.materialise_targets(campaign)
    db.set_campaign_status(campaign_id, "running")

    if runner.live_worker(agent.id) is None:
        try:
            await runner.start(agent.id)
        except Exception as exc:
            db.set_campaign_status(campaign_id, "paused")
            logger.exception("Could not start worker for campaign %s", campaign_id)
            return JSONResponse(status_code=502, content={"error": str(exc)})

    return {
        "status": "running",
        "queued": added,
        "stats": db.campaign_stats(campaign_id),
    }


@app.post("/api/campaigns/{campaign_id}/pause")
async def pause_campaign(campaign_id: str):
    """Stop claiming new contacts. Calls already in progress finish."""
    if db.get_campaign(campaign_id) is None:
        return JSONResponse(status_code=404, content={"error": "Campaign not found"})
    db.set_campaign_status(campaign_id, "paused")
    return {"status": "paused", "stats": db.campaign_stats(campaign_id)}


@app.post("/api/campaigns/{campaign_id}/stop")
async def stop_campaign(campaign_id: str):
    """Pause, and give up on everything still queued.

    Live calls are still allowed to finish — hanging up on someone mid-sentence
    is never what "stop the campaign" is meant to mean.
    """
    campaign = db.get_campaign(campaign_id)
    if campaign is None:
        return JSONResponse(status_code=404, content={"error": "Campaign not found"})
    db.set_campaign_status(campaign_id, "completed")
    cancelled = db.cancel_pending_targets(campaign_id)
    return {"status": "completed", "cancelled": cancelled}


@app.get("/api/campaigns/{campaign_id}/stats")
async def get_campaign_stats(campaign_id: str):
    campaign = db.get_campaign(campaign_id)
    if campaign is None:
        return JSONResponse(status_code=404, content={"error": "Campaign not found"})
    return {
        "campaign": campaign,
        "stats": db.campaign_stats(campaign_id),
        "window_open": schedule.window_open(campaign),
        "live_calls": db.count_live_calls(campaign["agent_id"]),
        "worker": db.get_worker(campaign["agent_id"]),
    }


# ---------------------------------------------------------------------------
# Suppressions
# ---------------------------------------------------------------------------
@app.get("/api/suppressions")
async def get_suppressions():
    return {"suppressions": db.list_suppressions()}


@app.post("/api/suppressions")
async def post_suppression(request: Request):
    body = await request.json()
    phone = contacts.normalise_phone(body.get("phone", ""))
    if phone is None:
        return JSONResponse(status_code=400, content={"error": "Not a valid phone number"})
    return {
        "id": db.add_suppression(phone, body.get("agent_id", ""), body.get("reason", "")),
        "phone_e164": phone,
    }


@app.delete("/api/suppressions/{suppression_id}")
async def remove_suppression(suppression_id: str):
    db.delete_suppression(suppression_id)
    return {"status": "deleted"}


@app.post("/api/agents/{agent_id}/worker/start")
async def start_worker(agent_id: str):
    if db.get_agent(agent_id) is None:
        return JSONResponse(status_code=404, content={"error": "Agent not found"})
    try:
        return await runner.start(agent_id)
    except Exception as exc:
        logger.exception("Could not start worker for %s", agent_id)
        return JSONResponse(status_code=502, content={"error": str(exc)})


@app.post("/api/agents/{agent_id}/worker/stop")
async def stop_worker(agent_id: str):
    await runner.stop(agent_id)
    return {"status": "stopped"}


@app.post("/api/internal/events")
async def relay_worker_event(request: Request):
    """Republish a worker's call event so the browser's SSE stream sees it.

    Workers run in their own processes, but the dashboard is connected here, so
    events have to come back to this broker. In production this is what Redis
    pub/sub replaces.
    """
    body = await request.json()
    call_id = body.get("call_id", "")
    if call_id:
        broker.publish(call_id, body.get("event", ""), body.get("data") or {})
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Contact lists
# ---------------------------------------------------------------------------
@app.post("/api/contact-lists/preview")
async def preview_contact_list(
    file: UploadFile = File(...), agent_id: str = Form(""),
):
    """Parse a CSV without storing it: headers, a few rows, a proposed mapping.

    Importing is two steps on purpose. The agent's prompt already declares the
    values every call needs, so the browser can show which column will feed
    which `$variable` and let that be corrected — before a mis-mapped file
    becomes a list of real numbers to dial.
    """
    headers, rows = contacts.parse_csv(await file.read())
    if not headers:
        return JSONResponse(
            status_code=400, content={"error": "No columns found in that CSV."}
        )

    agent = db.get_agent(agent_id) if agent_id else None
    variables = (
        declared_variables(f"{agent.system_prompt}\n{agent.greeting_text}")
        if agent is not None
        else []
    )
    return {
        "headers": headers,
        "rows": rows[:5],
        "row_count": len(rows),
        "variables": variables,
        "phone_column": contacts.guess_phone_column(headers),
        "mapping": contacts.suggest_mapping(headers, variables),
    }


@app.post("/api/contact-lists")
async def post_contact_list(
    file: UploadFile = File(...),
    name: str = Form(""),
    phone_column: str = Form(...),
    name_column: str = Form(""),
    mapping: str = Form("{}"),
):
    headers, rows = contacts.parse_csv(await file.read())
    if phone_column not in headers:
        return JSONResponse(
            status_code=400,
            content={"error": f"Column '{phone_column}' is not in that CSV."},
        )
    try:
        variable_map = json.loads(mapping)
    except json.JSONDecodeError:
        return JSONResponse(
            status_code=400, content={"error": "mapping is not valid JSON."}
        )

    parsed, rejects = contacts.build_contacts(
        rows, phone_column, name_column, variable_map
    )
    if not parsed:
        return JSONResponse(
            status_code=400,
            content={
                "error": "No dialable numbers in that file.",
                "rejects": rejects[:20],
            },
        )

    list_id = db.create_contact_list(
        name or file.filename or "Imported list", file.filename or ""
    )
    stored = db.insert_contacts(contacts.to_rows(parsed, list_id, db.DEFAULT_ORG))
    # Rejects are capped: a badly formatted 10k-row file should report the
    # problem, not return ten thousand copies of it.
    return {
        "id": list_id,
        "stored": stored,
        "rejected": len(rejects),
        "rejects": rejects[:20],
    }


@app.get("/api/contact-lists")
async def get_contact_lists():
    return {"lists": db.list_contact_lists()}


@app.get("/api/contact-lists/{list_id}/contacts")
async def get_contact_list_contacts(list_id: str, limit: int = 200):
    if db.get_contact_list(list_id) is None:
        return JSONResponse(status_code=404, content={"error": "List not found"})
    return {"contacts": db.list_contacts(list_id, limit)}


@app.delete("/api/contact-lists/{list_id}")
async def remove_contact_list(list_id: str):
    db.delete_contact_list(list_id)
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

    error = await _check_model_available(agent)
    if error:
        return JSONResponse(status_code=400, content={"error": error})

    try:
        return await dialer.place_call(agent, req.to, req.variables)
    except dialer.CallError as exc:
        return JSONResponse(status_code=exc.status, content={"error": str(exc)})


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
        _settle_campaign_target(record, status, form.get("AnsweredBy", ""))
    return Response(status_code=204)


# Statuses that mean nobody was reached. Worth trying again later; the campaign's
# attempt budget decides how many times.
RETRYABLE_STATUSES = {"no-answer", "busy", "failed", "canceled"}
# Twilio's answering-machine detection. Voicemail is not a conversation, and
# calling the same number back to reach the same voicemail is just spend.
MACHINE_ANSWERS = {"machine_start", "machine_end_beep", "machine_end_silence",
                   "machine_end_other", "fax"}


def _settle_campaign_target(call: dict, status: str, answered_by: str) -> None:
    """Close out or reschedule the campaign target this call belonged to."""
    if status not in RETRYABLE_STATUSES and status != "completed":
        return
    target = db.target_by_call(call["id"])
    if target is None:
        return

    if answered_by in MACHINE_ANSWERS:
        dialer.record_attempt(target, succeeded=True, outcome=f"voicemail ({answered_by})")
        return
    dialer.record_attempt(
        target,
        succeeded=status == "completed",
        outcome=call.get("outcome") or status,
    )


# ---------------------------------------------------------------------------
# Twilio Media Stream WebSocket
# ---------------------------------------------------------------------------
async def proxy_media_stream(
    websocket: WebSocket, worker: dict, first: dict, call_id: str
) -> None:
    """Relay Twilio's audio socket to the agent's worker, in both directions.

    Twilio can only be given one public URL, so a stream belonging to an agent
    whose worker is a separate process has to be forwarded to it. In Kubernetes
    the Service does this and the function disappears; at laptop volumes a
    Python relay is fine.
    """
    url = f"ws://127.0.0.1:{worker['port']}/media-stream?call_id={call_id}"
    try:
        async with ws_connect(url, max_size=None) as upstream:
            # The start frame was consumed to work out which worker to use, so
            # replay it first — the worker reads it exactly as Twilio sent it.
            await upstream.send(json.dumps(first))
            await _relay_both_ways(websocket, upstream)
    except Exception:
        logger.exception("Proxy to worker failed for call %s", call_id)
        with contextlib.suppress(Exception):
            await websocket.close()


async def _relay_both_ways(client: WebSocket, upstream) -> None:
    async def to_worker() -> None:
        while True:
            await upstream.send(await client.receive_text())

    async def to_twilio() -> None:
        async for message in upstream:
            await client.send_text(message)

    tasks = [asyncio.create_task(to_worker()), asyncio.create_task(to_twilio())]
    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


@app.websocket("/api/twilio/media-stream")
async def twilio_media_stream(websocket: WebSocket):
    """Twilio's audio socket, either handled here or handed to the agent's worker.

    Twilio can only reach one public URL, so every stream arrives here whichever
    process is meant to run it. When the agent has a Vaani worker up, this relays
    the socket to it; otherwise the conversation runs in-process, which is what
    keeps the single test call in the builder working with no worker at all.
    """
    await websocket.accept()

    first = await dialer.read_start_frame(websocket)
    if first is None:
        logger.error("No start frame on media stream — closing")
        await websocket.close()
        return

    call_id, context = dialer.resolve_call_context(first["start"], websocket)
    agent = db.get_agent(context.get("agent_id", "")) if context else None
    if agent is None:
        logger.error(
            "No agent for call_id=%r — closing stream. start frame=%s",
            call_id,
            json.dumps(first["start"], default=str)[:600],
        )
        await websocket.close()
        return

    worker = runner.live_worker(agent.id)
    if worker is not None:
        await proxy_media_stream(websocket, worker, first, call_id)
        return

    try:
        await dialer.run_session(websocket, first["start"], call_id, agent, context)
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
