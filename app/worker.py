"""Vaani — one agent's calling process.

Run as `python -m app.worker --agent-id X --port P --parent http://127.0.0.1:8000`.
The management app starts it; on Kubernetes the same command becomes a pod's
entrypoint and nothing in here changes.

The agent's configuration is read **once**, at startup. That is a decision, not
an oversight: a worker that re-read the row mid-call could change voice, prompt
or language between two sentences of the same conversation, and "has this
worker picked up my edit yet?" becomes unanswerable. Editing an agent instead
starts a fresh worker and drains this one, so a given call runs start to finish
on exactly one version of the config.
"""

import argparse
import asyncio
import contextlib
import json
import logging

import httpx
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from app import db, dialer, schedule
from app.config import get_settings
from app.events import broker

logger = logging.getLogger(__name__)
settings = get_settings()

# The parent is checked this often. A worker outliving its management app is a
# process placing real calls with nothing watching it, so it exits rather than
# carry on alone — on a laptop the parent dies every time someone hits Ctrl+C.
PARENT_POLL_SECONDS = 15.0
PARENT_MISSES_BEFORE_EXIT = 4


class Worker:
    """One agent, its live calls, and its own lifecycle."""

    def __init__(self, agent_id: str, parent_url: str) -> None:
        self.agent_id = agent_id
        self.parent_url = parent_url.rstrip("/")
        self.agent = db.get_agent(agent_id)
        self.config_version = db.get_config_version(agent_id)
        self.live = 0
        self.draining = False
        self.server: uvicorn.Server | None = None
        self._client: httpx.AsyncClient | None = None
        # create_task returns a task nothing else holds; without a reference the
        # garbage collector is free to cancel a relay mid-flight.
        self._relays: set[asyncio.Task] = set()

    # -- event relay ------------------------------------------------------
    def relay(self, call_id: str, event: str, data: dict) -> None:
        """Forward a call event to the parent, where the browser is listening."""
        if self._client is None:
            return
        task = asyncio.create_task(self._post_event(call_id, event, data))
        self._relays.add(task)
        task.add_done_callback(self._relays.discard)

    async def _post_event(self, call_id: str, event: str, data: dict) -> None:
        try:
            await self._client.post(
                f"{self.parent_url}/api/internal/events",
                json={"call_id": call_id, "event": event, "data": data},
            )
        except Exception:
            # A live call must never be affected by the dashboard being away.
            logger.debug("Event relay failed for %s", call_id, exc_info=True)

    # -- lifecycle --------------------------------------------------------
    async def watch_parent(self) -> None:
        misses = 0
        while not self.draining:
            await asyncio.sleep(PARENT_POLL_SECONDS)
            try:
                assert self._client is not None
                response = await self._client.get(f"{self.parent_url}/health", timeout=5.0)
                misses = 0 if response.status_code == 200 else misses + 1
            except Exception:
                misses += 1
            if misses >= PARENT_MISSES_BEFORE_EXIT:
                logger.warning("Management app unreachable — draining and exiting")
                asyncio.create_task(self.drain())
                return

    # -- dispatch ---------------------------------------------------------
    def headroom(self) -> int:
        """How many more calls this agent may have up right now.

        Three caps apply and the tightest wins: the agent's own, the campaign's,
        and a global one that exists so a laptop is not asked to hold more
        concurrent conversations than it can actually carry.
        """
        agent_cap = self.agent.max_concurrent_calls if self.agent else 0
        return max(0, min(agent_cap, settings.GLOBAL_MAX_CONCURRENT_CALLS)
                   - db.count_live_calls(self.agent_id))

    async def dispatch_once(self) -> int:
        """One pass: dial whatever this agent's open campaigns are owed."""
        if self.draining or self.agent is None:
            return 0

        placed = 0
        for campaign in db.running_campaigns(self.agent_id):
            if not schedule.window_open(campaign):
                continue
            room = min(self.headroom(), campaign["max_concurrent"] - db.count_live_calls(self.agent_id))
            for target in db.claim_targets(self.agent_id, room):
                if await self._dial(target, campaign):
                    placed += 1
        return placed

    async def _dial(self, target: dict, campaign: dict) -> bool:
        phone = target["phone_e164"]
        if db.is_suppressed(phone, self.agent_id, target["org_id"]):
            logger.info("Skipping suppressed number %s", phone)
            db.finish_target(target["id"], "suppressed")
            return False

        try:
            variables = json.loads(target.get("contact_variables") or "{}")
        except json.JSONDecodeError:
            variables = {}

        try:
            result = await dialer.place_call(
                self.agent,
                phone,
                variables,
                campaign_id=campaign["id"],
                attempt=target["attempts"] + 1,
                detect_machine=True,
            )
        except dialer.CallError as exc:
            # A failure to place the call is an attempt: the retry policy owns
            # what happens next, so this is handed back rather than dropped.
            logger.warning("Could not dial %s: %s", phone, exc)
            db.set_target_call(target["id"], "")
            dialer.record_attempt(
                {
                    **target,
                    "attempts": target["attempts"] + 1,
                    "max_attempts": campaign["max_attempts"],
                    "retry_after_minutes": campaign["retry_after_minutes"],
                },
                succeeded=False,
                outcome=str(exc)[:200],
            )
            return False

        db.set_target_call(target["id"], result["call_id"])
        return True

    async def dispatch_loop(self) -> None:
        while not self.draining:
            try:
                await self.dispatch_once()
            except Exception:
                logger.exception("Dispatch pass failed")
            await asyncio.sleep(settings.DISPATCHER_TICK_SECONDS)

    async def drain(self) -> None:
        """Stop taking new work, let live calls finish, then exit."""
        if self.draining:
            return
        self.draining = True
        db.set_worker_status(self.agent_id, "draining")
        logger.info("Draining: %s live call(s) to finish", self.live)
        while self.live > 0:
            await asyncio.sleep(1.0)
        db.set_worker_status(self.agent_id, "stopped")
        if self.server is not None:
            self.server.should_exit = True


worker: Worker | None = None
app = FastAPI(title="Vaani worker")


@app.get("/health")
async def health():
    assert worker is not None
    return {
        "status": "draining" if worker.draining else "ok",
        "agent_id": worker.agent_id,
        "config_version": worker.config_version,
        "live_calls": worker.live,
    }


@app.post("/shutdown")
async def shutdown():
    assert worker is not None
    asyncio.create_task(worker.drain())
    return {"status": "draining", "live_calls": worker.live}


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    """The real conversation, relayed here by the management app.

    The parent had to read the start frame to know which worker to hand the
    stream to, so it replays that frame first and this reads it again exactly
    as if it had come straight from Twilio.
    """
    assert worker is not None
    await websocket.accept()

    first = await dialer.read_start_frame(websocket)
    if first is None:
        logger.error("No start frame reached the worker — closing")
        await websocket.close()
        return

    call_id, context = dialer.resolve_call_context(first["start"], websocket)
    if worker.agent is None or context.get("agent_id") != worker.agent_id:
        logger.error(
            "Stream for agent %r arrived at worker for %s — closing",
            context.get("agent_id"),
            worker.agent_id,
        )
        await websocket.close()
        return

    worker.live += 1
    try:
        # A copy per call: run_session renders $variables into greeting_text,
        # and the worker's own config must survive that for the next caller.
        await dialer.run_session(
            websocket, first["start"], call_id, worker.agent.model_copy(deep=True), context
        )
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception:
        logger.exception("Media stream error")
    finally:
        worker.live -= 1


@app.on_event("startup")
async def _startup() -> None:
    assert worker is not None
    worker._client = httpx.AsyncClient(timeout=5.0)
    broker.sink = worker.relay
    worker._relays.add(asyncio.create_task(worker.watch_parent()))
    worker._relays.add(asyncio.create_task(worker.dispatch_loop()))
    logger.info(
        "Vaani worker ready: agent=%s (%s) config_version=%s",
        worker.agent_id,
        worker.agent.name if worker.agent else "?",
        worker.config_version,
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    assert worker is not None
    if worker._client is not None:
        await worker._client.aclose()


def main() -> None:
    global worker

    parser = argparse.ArgumentParser(description="Vaani worker — one agent's calls")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--parent", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format=f"%(asctime)s [%(levelname)s] vaani[{args.agent_id}]: %(message)s",
    )

    db.init()
    worker = Worker(args.agent_id, args.parent)
    if worker.agent is None:
        raise SystemExit(f"No agent {args.agent_id}")

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning")
    )
    worker.server = server
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(server.serve())


if __name__ == "__main__":
    main()
