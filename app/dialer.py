"""Placing a call, and running the conversation once it connects.

Both halves of the system need this. The management app dials when someone
clicks Call in the builder; a Vaani worker dials when its campaign window is
open and a contact is due. Neither should own the logic, so it lives here and
both import it.

The one deliberate omission is the live LLM-model check. It costs an HTTP round
trip to the provider, which is worth paying once when a campaign starts and
wasteful to repeat on every one of its calls, so it stays with the callers that
know which case they are.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

import httpx
from fastapi import WebSocket
from twilio.rest import Client as TwilioClient

from app import db, registry
from app.call_handler import CallSession
from app.config import get_settings
from app.dialog import DialogEngine
from app.events import broker
from app.fillers import FillerBank, FillerController
from app.models import AgentConfig
from app.prompts import build_system_prompt, render
from app.registry import MissingCredential

logger = logging.getLogger(__name__)
settings = get_settings()

# call_id -> {agent_id, variables}. A cache in front of the calls table, not the
# source of truth: v5 popped an entry when the stream ended, so a Twilio
# websocket reconnect lost the call's context and killed the call. Entries are
# only evicted once CALL_CONTEXT_LIMIT newer calls exist.
CALL_CONTEXTS: dict[str, dict] = {}
CALL_CONTEXT_LIMIT = 500


class CallError(Exception):
    """Dialing failed. `status` is what the HTTP layer should return."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def remember_context(call_id: str, context: dict) -> None:
    CALL_CONTEXTS[call_id] = context
    while len(CALL_CONTEXTS) > CALL_CONTEXT_LIMIT:
        CALL_CONTEXTS.pop(next(iter(CALL_CONTEXTS)), None)


async def place_call(
    agent: AgentConfig,
    to_number: str,
    variables: dict[str, str],
    campaign_id: str = "",
    attempt: int = 1,
    detect_machine: bool = False,
) -> dict[str, str]:
    """Dial one number for one agent. Raises CallError if it cannot.

    `detect_machine` asks Twilio to work out whether a human or an answering
    machine picked up. Campaigns want it: without it a large share of an
    outbound run holds a full conversation with voicemail and records it as a
    successful call. A manual test call does not, because it adds a delay
    before the TwiML runs.
    """
    twilio = db.get_credential_json("twilio")
    if not (twilio.get("account_sid") and twilio.get("auth_token") and twilio.get("from_number")):
        raise CallError("Twilio is not configured. Add it on the Credentials page.")

    # Fail before dialing if a provider key is missing, rather than answering to silence.
    try:
        for build in (registry.build_stt, registry.build_llm, registry.build_tts):
            probe = build(agent)
            await probe.close()
    except MissingCredential as exc:
        raise CallError(str(exc)) from exc

    call_id = uuid.uuid4().hex[:16]
    remember_context(call_id, {"agent_id": agent.id, "variables": variables})
    db.create_call(call_id, agent.id, to_number, variables)
    if campaign_id:
        db.update_call(call_id, campaign_id=campaign_id, attempt=attempt)

    base = settings.BASE_URL.rstrip("/")
    extra: dict[str, Any] = (
        {"machine_detection": "Enable", "async_amd": "true"} if detect_machine else {}
    )
    try:
        client = TwilioClient(twilio["account_sid"], twilio["auth_token"])
        call = client.calls.create(
            to=to_number,
            from_=twilio["from_number"],
            url=f"{base}/api/twilio/voice?call_id={call_id}",
            method="POST",
            status_callback=f"{base}/api/twilio/status",
            status_callback_method="POST",
            status_callback_event=["initiated", "answered", "completed"],
            **extra,
        )
    except Exception as exc:
        CALL_CONTEXTS.pop(call_id, None)
        db.update_call(call_id, status="failed")
        logger.exception("Failed to initiate call")
        raise CallError(str(exc), status=502) from exc

    db.update_call(call_id, call_sid=call.sid, status="dialing")
    logger.info("Outbound call: sid=%s to=%s agent=%s", call.sid, to_number, agent.name)
    return {"call_id": call_id, "call_sid": call.sid, "status": "dialing"}


WEBHOOK_TIMEOUT = 10.0

# asyncio keeps only a weak reference to a running task, so a webhook fired and
# forgotten can be collected before it is sent.
_WEBHOOKS: set[asyncio.Task] = set()


def fire_outcome_webhook(
    agent: AgentConfig, call_id: str, data: dict, variables: dict
) -> None:
    """Tell an outside system what the caller decided.

    This is what makes a campaign do something rather than merely report: the
    caller agrees to pay, and a payment link goes out while they are still on
    the phone. Deliberately fire-and-forget — a slow or broken endpoint on the
    customer's side must never be able to stall a live conversation.
    """
    if not agent.outcome_webhook_url:
        return
    task = asyncio.create_task(_post_outcome(agent, call_id, data, variables))
    _WEBHOOKS.add(task)
    task.add_done_callback(_WEBHOOKS.discard)


async def _post_outcome(
    agent: AgentConfig, call_id: str, data: dict, variables: dict
) -> None:
    call = db.get_call(call_id) or {}
    payload = {
        "call_id": call_id,
        "agent_id": agent.id,
        "agent_name": agent.name,
        "to_number": call.get("to_number", ""),
        "campaign_id": call.get("campaign_id", ""),
        "outcome": data.get("outcome", ""),
        "summary": data.get("summary", ""),
        "variables": variables,
    }
    try:
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT) as client:
            response = await client.post(agent.outcome_webhook_url, json=payload)
        logger.info(
            "Outcome webhook: %s -> HTTP %s", agent.outcome_webhook_url, response.status_code
        )
    except Exception:
        logger.warning("Outcome webhook failed for call %s", call_id, exc_info=True)


def record_attempt(target: dict, succeeded: bool, outcome: str | None = None) -> None:
    """Close a campaign target, or schedule when to try it again.

    An unanswered call is not a failed contact. Someone busy at 11am is often
    reachable at 3pm, and retrying is most of what makes an outbound campaign
    work at all — so a target only becomes `failed` once the campaign's attempt
    budget is actually spent.

    `target` must carry the campaign's `max_attempts` and `retry_after_minutes`;
    `db.target_by_call` joins them in, and the worker merges them from the
    campaign it is dialing for.
    """
    if succeeded:
        db.finish_target(target["id"], "done", outcome)
        return
    if int(target["attempts"]) >= int(target["max_attempts"]):
        db.finish_target(target["id"], "failed", outcome)
        return
    next_at = datetime.now() + timedelta(minutes=int(target["retry_after_minutes"]))
    db.reschedule_target(target["id"], next_at.isoformat(timespec="seconds"))


# ---------------------------------------------------------------------------
# Resolving a media stream back to the call that started it
# ---------------------------------------------------------------------------
def _normalize(name: str) -> str:
    """'Call_Id', 'callId' and 'call_id' all have to mean the same thing."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def resolve_call_context(start: dict, websocket: WebSocket) -> tuple[str, dict]:
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
    remember_context(record["id"], context)
    return record["id"], context


async def read_start_frame(websocket: WebSocket) -> dict | None:
    """Twilio sends a "connected" frame before "start", so read until the start
    frame arrives — that is the one carrying our custom parameters."""
    try:
        for _ in range(10):
            message = json.loads(await websocket.receive_text())
            if message.get("event") == "start":
                return message
    except Exception:
        return None
    return None


async def run_session(
    websocket: WebSocket,
    start_frame: dict,
    call_id: str,
    agent: AgentConfig,
    context: dict,
) -> None:
    """Run one live conversation to completion on an already-accepted socket.

    Events go to the local `broker` whether this runs in the management app or
    in a worker; a worker forwards them on by setting `broker.sink`, so nothing
    on the call path needs to know which process it is in.
    """
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

    def on_event(name: str, data: dict) -> None:
        broker.publish(call_id, name, data)
        if name == "outcome":
            fire_outcome_webhook(agent, call_id, data, context.get("variables") or {})

    dialog = DialogEngine(
        llm,
        system_prompt,
        temperature=agent.temperature,
        max_output_tokens=agent.max_output_tokens,
        on_event=on_event,
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

    await session._on_start(start_frame)
    await session.handle()
