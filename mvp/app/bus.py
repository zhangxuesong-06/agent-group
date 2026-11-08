from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any

from .models import new_id
from .store import Store


class EventBus:
    """In-process fan-out + durable log.

    Replace this class with Redis Streams / NATS in production; the rest of the
    system only depends on `publish` and `subscribe`.
    """

    def __init__(self, store: Store) -> None:
        self._store = store
        self._subs: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def publish(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        task_id: str | None = None,
        *,
        transient: bool = False,
    ) -> dict[str, Any]:
        event = {
            "id": new_id("evt"),
            "run_id": run_id,
            "task_id": task_id,
            "type": event_type,
            "ts": time.time(),
            "payload": payload or {},
        }
        if transient:
            # Streaming deltas can be very frequent. Fan them out live but
            # keep them out of SQLite: the final message is persisted anyway.
            event["transient"] = True
        else:
            event = self._store.append_event(event)
        async with self._lock:
            queues = list(self._subs.get(run_id, ()))
        for queue in queues:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)
        return event

    async def subscribe(self, run_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._subs[run_id].add(queue)
        return queue

    async def unsubscribe(self, run_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            self._subs.get(run_id, set()).discard(queue)
