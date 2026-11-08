from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from typing import Any


def new_worker_token() -> str:
    return "wbt_" + secrets.token_urlsafe(32)


def hash_worker_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


REMOTE_TOOL_OPS = {
    "list_files": "fs.list",
    "read_file": "fs.read",
    "write_file": "fs.write",
    "edit_file": "fs.edit",
    "run_command": "shell.run",
}


class _WorkerConnection:
    def __init__(self, device_id: str, websocket: Any) -> None:
        self.device_id = device_id
        self.websocket = websocket
        self.pending: dict[str, asyncio.Future[Any]] = {}

    async def call(self, op: str, args: dict[str, Any], timeout: float) -> Any:
        call_id = secrets.token_hex(8)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self.pending[call_id] = future
        try:
            await self.websocket.send_json(
                {"type": "call", "id": call_id, "op": op, "args": args}
            )
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self.pending.pop(call_id, None)

    def resolve(self, message: dict[str, Any]) -> None:
        call_id = str(message.get("id") or "")
        future = self.pending.get(call_id)
        if future is None or future.done():
            return
        if message.get("ok"):
            future.set_result(message.get("result"))
        else:
            future.set_exception(
                RuntimeError(str(message.get("error") or "worker error"))
            )

    def cancel_all(self, reason: str = "设备连接已断开") -> None:
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(RuntimeError(reason))
        self.pending.clear()


class DeviceHub:
    """In-process registry of connected device workers."""

    def __init__(self) -> None:
        self._connections: dict[str, _WorkerConnection] = {}

    def online(self, device_id: str | None) -> bool:
        return bool(device_id and device_id in self._connections)

    def online_ids(self) -> set[str]:
        return set(self._connections)

    async def attach(self, device_id: str, websocket: Any) -> _WorkerConnection:
        old = self._connections.get(device_id)
        if old is not None and old.websocket is not websocket:
            try:
                await old.websocket.close(code=4000, reason="replaced by a new connection")
            except Exception:  # noqa: BLE001
                pass
            old.cancel_all("设备使用了新的连接")
        connection = _WorkerConnection(device_id, websocket)
        self._connections[device_id] = connection
        return connection

    def detach(self, device_id: str, websocket: Any | None = None) -> None:
        current = self._connections.get(device_id)
        if current is None:
            return
        if websocket is not None and current.websocket is not websocket:
            return
        current.cancel_all()
        self._connections.pop(device_id, None)

    def resolve(self, device_id: str, message: dict[str, Any]) -> None:
        current = self._connections.get(device_id)
        if current is not None:
            current.resolve(message)

    async def call(
        self,
        device_id: str,
        op: str,
        args: dict[str, Any],
        timeout: float = 120.0,
    ) -> Any:
        connection = self._connections.get(device_id)
        if connection is None:
            raise RuntimeError("目标设备当前不在线，请确认设备端 worker 已连接")
        return await connection.call(op, args, timeout)


class RemoteWorkspace:
    """Adapts the local file/command tools to a connected device worker."""

    def __init__(self, hub: DeviceHub, device_id: str, workspace: str) -> None:
        self.hub = hub
        self.device_id = device_id
        self.workspace = workspace

    async def call(self, tool: str, args: dict[str, Any]) -> str:
        op = REMOTE_TOOL_OPS.get(tool)
        if op is None:
            raise ValueError(f"工具 {tool} 不支持远程执行")
        payload = dict(args)
        payload["workspace"] = self.workspace
        timeout = 120.0
        if tool == "run_command":
            try:
                timeout = min(660.0, max(120.0, float(args.get("timeout") or 120) + 30))
            except (TypeError, ValueError):
                timeout = 150.0
        result = await self.hub.call(self.device_id, op, payload, timeout=timeout)
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False)
