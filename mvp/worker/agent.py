from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import socket
import sys
import time
from pathlib import Path
from typing import Any

import websockets

MAX_FILE_BYTES = 200_000
MAX_WRITE_BYTES = 1_000_000
MAX_OUTPUT = 200_000

DENY = (
    "rm -rf /",
    "mkfs",
    "format c:",
    "shutdown",
    "reboot",
    "poweroff",
    "diskpart",
    "reg delete",
    ":(){ :|:& };:",
)


def log(message: str) -> None:
    print(f"[worker] {message}", file=sys.stderr, flush=True)


class WorkspaceError(ValueError):
    pass


class Worker:
    def __init__(self, roots: list[Path], *, allow_shell: bool = True) -> None:
        self.roots = [root.expanduser().resolve() for root in roots]
        self.allow_shell = allow_shell

    def resolve(self, workspace: str, relative: str) -> tuple[Path, Path]:
        try:
            root = Path(workspace).expanduser().resolve()
        except OSError as exc:
            raise WorkspaceError(f"无效工作区: {exc}") from exc
        # A workspace may be a subdirectory of an authorized root.
        if not any(root == item or root.is_relative_to(item) for item in self.roots):
            raise WorkspaceError("工作区未授权")
        candidate = Path(relative)
        target = (
            candidate.expanduser().resolve()
            if candidate.is_absolute()
            else (root / candidate).resolve()
        )
        if target != root and root not in target.parents:
            raise WorkspaceError("路径超出工作区")
        return root, target

    def relative(self, root: Path, target: Path) -> str:
        try:
            return str(target.relative_to(root)).replace("\\", "/")
        except ValueError:
            return str(target)

    async def handle(self, op: str, args: dict[str, Any]) -> Any:
        workspace = str(args.get("workspace") or "")
        if op == "fs.list":
            return self.list_files(workspace, args)
        if op == "fs.read":
            return self.read_file(workspace, args)
        if op == "fs.write":
            return self.write_file(workspace, args)
        if op == "fs.edit":
            return self.edit_file(workspace, args)
        if op == "shell.run":
            return await self.run_command(workspace, args)
        raise WorkspaceError(f"未知操作: {op}")

    def list_files(self, workspace: str, args: dict[str, Any]) -> dict[str, Any]:
        root, target = self.resolve(workspace, str(args.get("path") or "."))
        if not target.is_dir():
            raise WorkspaceError("不是目录")
        try:
            depth = int(args.get("depth", 2))
        except (TypeError, ValueError):
            depth = 2
        depth = max(1, min(depth, 4))
        dirs_only = bool(args.get("dirs_only"))
        entries: list[dict[str, Any]] = []
        truncated = False
        # Bounded breadth-first walk. `rglob` would traverse (and sort) the whole
        # tree before the 400-entry cap applies, which is slow on big workspaces.
        frontier: list[tuple[Path, int]] = [(target, 1)]
        while frontier and not truncated:
            current, level = frontier.pop(0)
            try:
                children = sorted(current.iterdir(), key=lambda item: item.name.lower())
            except OSError:
                continue
            for path in children:
                if path.is_dir():
                    entries.append(
                        {
                            "path": self.relative(root, path) + "/",
                            "type": "dir",
                        }
                    )
                    if level < depth:
                        frontier.append((path, level + 1))
                elif path.is_file() and not dirs_only:
                    entries.append(
                        {
                            "path": self.relative(root, path),
                            "type": "file",
                            "size": path.stat().st_size,
                        }
                    )
                if len(entries) >= 400:
                    truncated = True
                    break
        return {
            "path": self.relative(root, target),
            "workspace": str(root),
            "entries": entries,
            "truncated": truncated,
        }

    def read_file(self, workspace: str, args: dict[str, Any]) -> dict[str, Any]:
        root, target = self.resolve(workspace, str(args.get("path") or ""))
        if not target.is_file():
            raise WorkspaceError("不是文件")
        try:
            max_bytes = int(args.get("max_bytes", 20_000))
        except (TypeError, ValueError):
            max_bytes = 20_000
        max_bytes = max(1000, min(max_bytes, MAX_FILE_BYTES))
        data = target.read_bytes()
        truncated = len(data) > max_bytes
        return {
            "path": self.relative(root, target),
            "bytes": len(data),
            "truncated": truncated,
            "content": data[:max_bytes].decode("utf-8", errors="replace"),
        }

    def write_file(self, workspace: str, args: dict[str, Any]) -> dict[str, Any]:
        content = args.get("content")
        if content is None:
            raise WorkspaceError("content 不能为空")
        content = str(content)
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_WRITE_BYTES:
            raise WorkspaceError("内容过大")
        root, target = self.resolve(workspace, str(args.get("path") or ""))
        if target == root:
            raise WorkspaceError("path 必须是文件")
        target.parent.mkdir(parents=True, exist_ok=True)
        append = bool(args.get("append", False))
        with target.open("a" if append else "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        return {
            "path": self.relative(root, target),
            "bytes": len(encoded),
            "mode": "append" if append else "write",
        }

    def edit_file(self, workspace: str, args: dict[str, Any]) -> dict[str, Any]:
        old = str(args.get("old_string") or "")
        new = str(args.get("new_string") or "")
        if not old:
            raise WorkspaceError("old_string 不能为空")
        if len(new.encode("utf-8")) > MAX_WRITE_BYTES:
            raise WorkspaceError("new_string 过大")
        root, target = self.resolve(workspace, str(args.get("path") or ""))
        if not target.is_file():
            raise WorkspaceError("不是文件")
        raw = target.read_bytes().decode("utf-8")
        newline = "\r\n" if "\r\n" in raw else "\n"
        text = raw.replace("\r\n", "\n").replace("\r", "\n")
        old = old.replace("\r\n", "\n")
        new = new.replace("\r\n", "\n")
        count = text.count(old)
        if count == 0:
            raise WorkspaceError("找不到 old_string")
        replace_all = bool(args.get("replace_all", False))
        if count > 1 and not replace_all:
            raise WorkspaceError(f"old_string 出现 {count} 次，请提供更多上下文")
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        if newline == "\r\n":
            updated = updated.replace("\n", "\r\n")
        target.write_bytes(updated.encode("utf-8"))
        return {
            "path": self.relative(root, target),
            "replacements": count if replace_all else 1,
            "bytes": len(updated.encode("utf-8")),
        }

    async def run_command(self, workspace: str, args: dict[str, Any]) -> dict[str, Any]:
        if not self.allow_shell:
            raise WorkspaceError("设备端已禁用命令执行")
        command = str(args.get("command") or "").strip()
        if not command:
            raise WorkspaceError("command 不能为空")
        lowered = command.lower()
        if any(pattern in lowered for pattern in DENY):
            raise WorkspaceError("命令被设备端安全策略拦截")
        root, cwd = self.resolve(workspace, str(args.get("cwd") or "."))
        if not cwd.is_dir():
            raise WorkspaceError("cwd 不是目录")
        try:
            timeout = int(args.get("timeout", 60))
        except (TypeError, ValueError):
            timeout = 60
        timeout = max(1, min(timeout, 600))
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise WorkspaceError(f"命令超时（{timeout} 秒）")
        text = output.decode("utf-8", errors="replace")
        return {
            "command": command,
            "cwd": self.relative(root, cwd),
            "exit_code": process.returncode,
            "output": text[:MAX_OUTPUT],
            "truncated": len(text) > MAX_OUTPUT,
        }


async def heartbeat(websocket: Any) -> None:
    while True:
        await asyncio.sleep(20)
        await websocket.send_json({"type": "heartbeat", "ts": time.time()})


async def run_session(uri: str, token: str, worker: Worker, name: str) -> None:
    major = int(websockets.__version__.split(".", 1)[0])
    header_key = "additional_headers" if major >= 14 else "extra_headers"
    kwargs = {
        header_key: {"Authorization": f"Bearer {token}"},
        "ping_interval": 20,
        "ping_timeout": 20,
        "max_size": 16 * 1024 * 1024,
    }
    async with websockets.connect(uri, **kwargs) as websocket:
        await websocket.send(
            json.dumps(
                {
                    "type": "hello",
                    "name": name,
                    "platform": f"{platform.system()} {platform.release()}",
                    "workspaces": [str(root) for root in worker.roots],
                },
                ensure_ascii=False,
            )
        )
        welcome = json.loads(await websocket.recv())
        if welcome.get("type") != "welcome":
            raise RuntimeError(f"server rejected worker: {welcome}")
        log(f"connected as {welcome.get('name')} ({welcome.get('device_id')})")
        hb = asyncio.create_task(heartbeat(websocket))
        try:
            async for raw in websocket:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if message.get("type") != "call":
                    continue
                call_id = message.get("id")
                try:
                    result = await worker.handle(
                        str(message.get("op") or ""),
                        dict(message.get("args") or {}),
                    )
                    await websocket.send(
                        json.dumps(
                            {"type": "result", "id": call_id, "ok": True, "result": result},
                            ensure_ascii=False,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - return to server
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "result",
                                "id": call_id,
                                "ok": False,
                                "error": f"{type(exc).__name__}: {exc}",
                            },
                            ensure_ascii=False,
                        )
                    )
        finally:
            hb.cancel()


async def main_async(args: argparse.Namespace) -> None:
    roots = [Path(item) for item in args.workspace]
    if not roots:
        roots = [Path.cwd()]
    worker = Worker(roots, allow_shell=not args.no_shell)
    name = args.name or socket.gethostname()
    base = args.server.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    uri = f"{base}/api/worker/ws"
    delay = 1.0
    while True:
        try:
            await run_session(uri, args.token, worker, name)
            delay = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reconnect forever
            log(f"disconnected: {type(exc).__name__}: {exc}; retrying in {delay:.0f}s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Connect a workspace on this machine to the AI Cluster server."
    )
    parser.add_argument("--server", required=True, help="http(s)://host[:port]")
    parser.add_argument(
        "--token",
        default=os.getenv("CLUSTER_WORKER_TOKEN", ""),
        help="device token (or CLUSTER_WORKER_TOKEN)",
    )
    parser.add_argument("--name", default="", help="display name")
    parser.add_argument(
        "--workspace",
        action="append",
        default=[],
        help="authorized workspace root (repeatable; default: current directory)",
    )
    parser.add_argument("--no-shell", action="store_true", help="disable commands")
    args = parser.parse_args()
    if not args.token:
        parser.error("--token or CLUSTER_WORKER_TOKEN is required")
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
