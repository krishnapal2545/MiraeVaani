"""In-process pub/sub so the browser can watch a call as it happens.

The UI subscribes over SSE (`/api/calls/{id}/events`) and receives the same
events the call logger writes to disk, so the transcript appears turn by turn
instead of only after the call ends.
"""

import asyncio
import logging
from collections import defaultdict
from typing import Callable

logger = logging.getLogger(__name__)

MAX_QUEUE = 200


class EventBroker:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)
        # A Vaani worker sets this to forward everything to the management app,
        # because that is where the browser's SSE connection actually lives.
        # Leaving publishers talking to their own local broker keeps the
        # difference between running in-process and running in a worker out of
        # the call path entirely.
        self.sink: Callable[[str, str, dict], None] | None = None

    def subscribe(self, call_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        self._subscribers[call_id].append(queue)
        return queue

    def unsubscribe(self, call_id: str, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get(call_id)
        if not subs:
            return
        if queue in subs:
            subs.remove(queue)
        if not subs:
            self._subscribers.pop(call_id, None)

    def publish(self, call_id: str, event: str, data: dict) -> None:
        """Fire-and-forget. A slow browser must never stall a live call."""
        if self.sink is not None:
            try:
                self.sink(call_id, event, data)
            except Exception:
                logger.debug("Event sink failed for %s", call_id, exc_info=True)
        payload = {"event": event, **data}
        for queue in list(self._subscribers.get(call_id, [])):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                logger.debug("Dropping event for slow subscriber: %s", call_id)


broker = EventBroker()
