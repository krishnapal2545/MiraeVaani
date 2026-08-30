"""Starting and stopping Vaani workers.

A worker is one agent's calling process. On this machine that is a child
process on a local port; in Kubernetes it becomes a pod, and only this module
changes — `LocalProcessRunner` gains a `KubernetesRunner` sibling and the
worker itself does not know the difference.

Two things here are deliberate rather than incidental:

Stopping always drains. A worker is asked to shut down over HTTP, finishes the
calls it already has, and exits on its own. Killing it outright would hang up
on whoever is mid-sentence, which is why editing an agent restarts its worker
instead of hot-swapping the config.

Worker rows outlive the process. If the management app is killed — which on a
laptop means every Ctrl+C — its children survive, and they are still dialing
real people. The registry is what lets the next startup find them.
"""

import asyncio
import logging
import socket
import subprocess
import sys

import httpx

from app import db
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# How long a worker gets to finish live calls after being asked to stop. Calls
# run for minutes, so this is generous on purpose; the alternative is cutting
# someone off.
DRAIN_TIMEOUT = 600.0
HEALTH_TIMEOUT = 2.0

# Child process handles for workers this process started. The DB registry is
# the durable record; this is only what lets us force-kill our own children.
_processes: dict[str, asyncio.subprocess.Process] = {}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


async def _health(port: int) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT) as client:
            response = await client.get(f"{_base_url(port)}/health")
        return response.json() if response.status_code == 200 else None
    except Exception:
        return None


def live_worker(agent_id: str) -> dict | None:
    """The worker that should be handling this agent's calls, if any.

    Read from the registry rather than probed, because this sits in the path of
    an incoming Twilio stream where a health check's latency would be audible.
    A worker marked `draining` still counts: it is finishing calls it already
    owns, and a stream arriving now belongs to one of them.
    """
    worker = db.get_worker(agent_id)
    if worker and worker["status"] in ("running", "draining"):
        return worker
    return None


async def start(agent_id: str) -> dict:
    """Launch this agent's worker, replacing any worker already running for it."""
    existing = db.get_worker(agent_id)
    if existing and existing["status"] == "running":
        await stop(agent_id, wait=False)

    port = _free_port()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.worker",
        "--agent-id",
        agent_id,
        "--port",
        str(port),
        "--parent",
        f"http://127.0.0.1:{settings.APP_PORT}",
    )
    _processes[agent_id] = process
    db.upsert_worker(
        agent_id, process.pid, port, "starting", db.get_config_version(agent_id)
    )

    # Wait for it to answer rather than assuming it came up: a worker that dies
    # on a bad credential should surface here, not when a call arrives.
    for _ in range(40):
        await asyncio.sleep(0.25)
        if process.returncode is not None:
            db.set_worker_status(agent_id, "stopped")
            raise RuntimeError(f"Worker for {agent_id} exited immediately")
        if await _health(port):
            db.set_worker_status(agent_id, "running")
            logger.info("Worker up: agent=%s pid=%s port=%s", agent_id, process.pid, port)
            return {"agent_id": agent_id, "pid": process.pid, "port": port, "status": "running"}

    await stop(agent_id, wait=False)
    raise RuntimeError(f"Worker for {agent_id} did not become healthy")


async def stop(agent_id: str, wait: bool = True) -> None:
    """Ask a worker to drain and exit; force it only if it overstays."""
    worker = db.get_worker(agent_id)
    if worker is None:
        return

    db.set_worker_status(agent_id, "draining")
    try:
        async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT) as client:
            await client.post(f"{_base_url(worker['port'])}/shutdown")
    except Exception:
        logger.info("Worker %s did not accept shutdown; will force", agent_id)

    process = _processes.get(agent_id)
    if wait and process is not None:
        try:
            await asyncio.wait_for(process.wait(), timeout=DRAIN_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("Worker %s still draining after %ss; killing", agent_id, DRAIN_TIMEOUT)
            _force_kill(worker["pid"])
    elif not wait:
        _force_kill(worker["pid"])

    _processes.pop(agent_id, None)
    db.set_worker_status(agent_id, "stopped")


def _force_kill(pid: int) -> None:
    """Last resort. taskkill on Windows, SIGKILL elsewhere.

    `os.kill(pid, 0)` is not usable as a liveness probe here: on Windows any
    signal reaches TerminateProcess, so the conventional "does this pid exist"
    check would kill the process it was asking about.
    """
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )
        else:
            import signal

            import os

            os.kill(pid, signal.SIGKILL)
    except Exception:
        logger.debug("Could not kill pid %s", pid, exc_info=True)


async def restart(agent_id: str) -> None:
    """Replace a running worker so an edited agent takes effect.

    Drains fully before starting the replacement. Running both at once would
    briefly give one agent two workers, each dialing up to its concurrency cap;
    the cost of doing it in sequence is that no new calls are placed until the
    calls already in progress have finished, which is the safer way round.
    """
    await stop(agent_id)
    await start(agent_id)


async def reap_orphans() -> None:
    """Shut down workers left behind by a previous run of the management app.

    Their child handles died with the parent, so they cannot be adopted — and a
    worker whose parent is gone is a process still placing real calls with
    nothing watching it. Each is asked to drain; the registry is cleared either
    way so the UI does not show workers that no longer belong to anyone.
    """
    for worker in db.list_workers():
        if worker["status"] == "stopped":
            continue
        agent_id = worker["agent_id"]
        if await _health(worker["port"]):
            logger.warning("Reaping orphaned worker: agent=%s port=%s", agent_id, worker["port"])
            try:
                async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT) as client:
                    await client.post(f"{_base_url(worker['port'])}/shutdown")
            except Exception:
                _force_kill(worker["pid"])
        db.set_worker_status(agent_id, "stopped")


async def stop_all() -> None:
    """Drain every worker this process started, on its way down."""
    await asyncio.gather(
        *(stop(agent_id) for agent_id in list(_processes)), return_exceptions=True
    )
