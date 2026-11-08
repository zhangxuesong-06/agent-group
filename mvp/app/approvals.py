from __future__ import annotations

import asyncio
import time
from typing import Any

from .bus import EventBus
from .models import new_id


class ApprovalManager:
    """Pending human approvals for tool calls (currently run_command)."""

    def __init__(self, bus: EventBus, timeout: int = 300) -> None:
        self.bus = bus
        self.timeout = max(10, timeout)
        self._pending: dict[str, dict[str, Any]] = {}
        self._futures: dict[str, asyncio.Future[bool]] = {}
        # Runs where the user chose "allow for this run".
        self._run_allowed: set[str] = set()

    async def request(
        self,
        run_id: str,
        task_id: str | None,
        tool: str,
        args: dict[str, Any],
    ) -> bool:
        if run_id in self._run_allowed:
            return True
        approval_id = new_id("appr")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        now = time.time()
        record = {
            "id": approval_id,
            "run_id": run_id,
            "task_id": task_id,
            "tool": tool,
            "args": args,
            "created_at": now,
            "expires_at": now + self.timeout,
        }
        self._pending[approval_id] = record
        self._futures[approval_id] = future
        try:
            await self.bus.publish(
                run_id,
                "tool.approval_required",
                record,
                task_id,
            )
            return await asyncio.wait_for(future, timeout=self.timeout)
        except asyncio.TimeoutError:
            await self.bus.publish(
                run_id,
                "tool.approval_timeout",
                {"id": approval_id, "message": "等待审批超时，已拒绝执行"},
                task_id,
            )
            return False
        finally:
            self._pending.pop(approval_id, None)
            self._futures.pop(approval_id, None)

    def list_pending(self, run_id: str | None = None) -> list[dict[str, Any]]:
        items = list(self._pending.values())
        if run_id:
            items = [item for item in items if item["run_id"] == run_id]
        items.sort(key=lambda item: item["created_at"])
        return items

    async def resolve(
        self, approval_id: str, approved: bool, scope: str = "once"
    ) -> dict[str, Any] | None:
        record = self._pending.get(approval_id)
        future = self._futures.get(approval_id)
        if record is None or future is None or future.done():
            return None
        if approved and scope == "run":
            self._run_allowed.add(record["run_id"])
        future.set_result(approved)
        await self.bus.publish(
            record["run_id"],
            "tool.approval_resolved",
            {
                "id": approval_id,
                "approved": approved,
                "tool": record["tool"],
            },
            record.get("task_id"),
        )
        return record

    def forget_run(self, run_id: str) -> None:
        self._run_allowed.discard(run_id)

    def cancel_run(self, run_id: str) -> int:
        count = 0
        for approval_id, record in list(self._pending.items()):
            if record["run_id"] != run_id:
                continue
            future = self._futures.get(approval_id)
            if future is not None and not future.done():
                future.set_result(False)
                count += 1
        return count
