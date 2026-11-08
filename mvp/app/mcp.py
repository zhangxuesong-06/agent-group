from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

try:  # The official SDK is an optional dependency at runtime.
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import (
        create_mcp_http_client,
        streamable_http_client,
    )

    MCP_AVAILABLE = True
    MCP_IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001
    MCP_AVAILABLE = False
    MCP_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

from .config import Settings
from .models import new_id
from .store import Store

MCP_SETTINGS_KEY = "mcp_servers"
TRANSPORTS = ("stdio", "sse", "streamable_http")

MCP_PRESETS: list[dict[str, Any]] = [
    {
        "id": "filesystem",
        "name": "文件系统",
        "description": "读写当前工作区中的文件，默认目录就是工作区。",
        "transport": "stdio",
        "command": "npx",
        "args": [
            "-y",
            "@modelcontextprotocol/server-filesystem",
            "{workspace}",
        ],
        "requires": "Node.js / npx",
    },
    {
        "id": "fetch",
        "name": "网页抓取",
        "description": "抓取网页并转换为 Markdown，适合调研和资料整理。",
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-fetch"],
        "requires": "uv / uvx",
    },
    {
        "id": "git",
        "name": "Git 仓库",
        "description": "查看工作区仓库的状态、diff、提交历史和文件内容。",
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-git", "--repository", "{workspace}"],
        "requires": "uv / uvx",
    },
    {
        "id": "sqlite",
        "name": "SQLite 数据库",
        "description": "对工作区中的 SQLite 数据库执行查询和数据操作。",
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-sqlite", "--db-path", "{workspace}/data.db"],
        "requires": "uv / uvx",
    },
    {
        "id": "time",
        "name": "时间与时区",
        "description": "获取当前时间、转换时区。",
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-time", "--local-timezone=Asia/Shanghai"],
        "requires": "uv / uvx",
    },
    {
        "id": "memory",
        "name": "记忆图谱",
        "description": "官方 memory server，用知识图谱保存跨任务的长期记忆。",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "requires": "Node.js / npx",
    },
    {
        "id": "sequential-thinking",
        "name": "分步思考",
        "description": "官方 sequential thinking server，把复杂问题拆成可修订的思考步骤。",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        "requires": "Node.js / npx",
    },
    {
        "id": "context7",
        "name": "Context7 文档",
        "description": "查询主流库的最新文档和示例（Upstash Context7）。",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@upstash/context7-mcp"],
        "requires": "Node.js / npx",
    },
    {
        "id": "playwright",
        "name": "Playwright 浏览器",
        "description": "浏览器自动化：打开页面、点击、填表、截图。",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@playwright/mcp@latest"],
        "requires": "Node.js / npx + 浏览器",
    },
    {
        "id": "brave-search",
        "name": "Brave 搜索",
        "description": "通过 Brave Search API 搜索网页，需要填写 API Key。",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-brave-search"],
        "env": {"BRAVE_API_KEY": ""},
        "requires": "Node.js / npx + BRAVE_API_KEY",
    },
    {
        "id": "github",
        "name": "GitHub 远程 MCP",
        "description": "访问 GitHub 仓库、Issue 和 PR；把个人访问令牌填到 Authorization。",
        "transport": "streamable_http",
        "url": "https://api.githubcopilot.com/mcp/",
        "headers": {"Authorization": "Bearer "},
        "requires": "GitHub PAT",
    },
    {
        "id": "everything",
        "name": "Everything 测试服务器",
        "description": "官方测试服务器，包含示例工具，适合先验证 MCP 接入是否正常。",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-everything"],
        "requires": "Node.js / npx",
    },
]


class MCPConnection:
    """One MCP server connection owned by a dedicated asyncio task.

    The MCP SDK uses anyio task groups internally, so all session calls and the
    context teardown must happen in the task that opened the connection. A
    small request queue gives the rest of the app a safe async API.
    """

    def __init__(self, config: dict[str, Any], settings: Settings) -> None:
        self.config = config
        self.settings = settings
        self.tools: list[dict[str, Any]] = []
        self.error = ""
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._ready = asyncio.Event()
        self._task: asyncio.Task[Any] | None = None
        self._closing = False

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done()) and not self.error

    async def start(self, timeout: float | None = None) -> None:
        if self._task and not self._task.done():
            return
        if timeout is None:
            timeout = (
                120.0
                if self.config.get("transport", "stdio") == "stdio"
                else 45.0
            )
        self._task = asyncio.create_task(
            self._run(), name=f"mcp-{self.config.get('name', 'server')}"
        )
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            await self.stop()
            raise ValueError(f"连接超时（{timeout:.0f}s）") from exc
        if self.error:
            raise ValueError(self.error)

    async def _run(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                transport = self.config.get("transport", "stdio")
                if transport == "stdio":
                    env = os.environ.copy()
                    env.update(
                        {
                            str(key): str(value)
                            for key, value in (self.config.get("env") or {}).items()
                        }
                    )
                    cwd = str(self.config.get("cwd") or "").strip()
                    if cwd and not Path(cwd).is_absolute():
                        cwd = str((self.settings.workspace_dir / cwd).resolve())
                    command = str(self.config.get("command") or "")
                    arguments = [
                        str(item) for item in (self.config.get("args") or [])
                    ]
                    if os.name == "nt" and command:
                        resolved = shutil.which(command) or command
                        if resolved.lower().endswith((".cmd", ".bat")):
                            comspec = os.environ.get("COMSPEC", "cmd.exe")
                            arguments = [
                                "/d",
                                "/s",
                                "/c",
                                subprocess.list2cmdline([resolved, *arguments]),
                            ]
                            command = comspec
                        else:
                            command = resolved
                    params = StdioServerParameters(
                        command=command,
                        args=arguments,
                        env=env,
                        cwd=cwd or None,
                    )
                    read, write = await stack.enter_async_context(stdio_client(params))
                elif transport == "sse":
                    read, write = await stack.enter_async_context(
                        sse_client(
                            str(self.config.get("url") or ""),
                            headers=self.config.get("headers") or None,
                            timeout=20.0,
                        )
                    )
                else:
                    headers = self.config.get("headers") or None
                    http_client = await stack.enter_async_context(
                        create_mcp_http_client(headers=headers, timeout=None)
                    )
                    read, write = await stack.enter_async_context(
                        streamable_http_client(
                            str(self.config.get("url") or ""),
                            http_client=http_client,
                        )
                    )
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                listed = await session.list_tools()
                self.tools = [
                    {
                        "name": tool.name,
                        "description": getattr(tool, "description", "") or "",
                        "input_schema": getattr(tool, "input_schema", None)
                        or getattr(tool, "inputSchema", None)
                        or {"type": "object", "properties": {}},
                    }
                    for tool in (listed.tools or [])
                ]
                self._ready.set()
                while True:
                    request = await self._queue.get()
                    if request is None:
                        break
                    kind, payload, future = request
                    if future.done():
                        continue
                    try:
                        if kind == "call":
                            name, arguments = payload
                            result = await session.call_tool(name, arguments or {})
                            future.set_result(self._serialize_result(result))
                        elif kind == "list":
                            listed = await session.list_tools()
                            future.set_result(
                                [
                                    {
                                        "name": tool.name,
                                        "description": getattr(tool, "description", "")
                                        or "",
                                        "input_schema": getattr(
                                            tool, "input_schema", None
                                        )
                                        or {"type": "object", "properties": {}},
                                    }
                                    for tool in (listed.tools or [])
                                ]
                            )
                        else:
                            future.set_exception(ValueError(f"未知请求: {kind}"))
                    except Exception as exc:  # noqa: BLE001
                        if not future.done():
                            future.set_exception(exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
        finally:
            self._closing = True
            self._ready.set()
            while not self._queue.empty():
                try:
                    request = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if request is not None:
                    _, _, future = request
                    if not future.done():
                        future.set_exception(ValueError("MCP 连接已关闭"))

    @staticmethod
    def _serialize_result(result: Any) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        for item in getattr(result, "content", None) or []:
            if hasattr(item, "text"):
                content.append({"type": "text", "text": str(item.text)})
            elif hasattr(item, "data") and hasattr(item, "mime_type"):
                content.append(
                    {
                        "type": "image",
                        "mime_type": str(item.mime_type),
                        "bytes": len(item.data or ""),
                    }
                )
            elif hasattr(item, "resource"):
                resource = item.resource
                content.append(
                    {
                        "type": "resource",
                        "uri": str(getattr(resource, "uri", "")),
                        "text": str(getattr(resource, "text", ""))[:4000],
                    }
                )
            else:
                content.append({"type": type(item).__name__, "text": str(item)[:2000]})
        return {
            "is_error": bool(getattr(result, "is_error", False)),
            "content": content,
            "structured": getattr(result, "structured_content", None),
        }

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.running:
            raise ValueError(self.error or "MCP 服务器未连接")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        await self._queue.put(("call", (name, arguments), future))
        return await asyncio.wait_for(future, timeout=180.0)

    async def list_tools(self) -> list[dict[str, Any]]:
        if not self.running:
            raise ValueError(self.error or "MCP 服务器未连接")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        await self._queue.put(("list", None, future))
        return await asyncio.wait_for(future, timeout=30.0)

    async def stop(self) -> None:
        task = self._task
        if task is None or task.done():
            return
        self._closing = True
        try:
            await self._queue.put(None)
            await asyncio.wait_for(task, timeout=6.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None


class MCPManager:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        self.servers: list[dict[str, Any]] = self._load()
        self._connections: dict[str, MCPConnection] = {}
        self._errors: dict[str, str] = {}
        self._tool_index: dict[str, tuple[str, str]] = {}
        self._reload_task: asyncio.Task[Any] | None = None
        self._startup_task: asyncio.Task[Any] | None = None
        self._lock = asyncio.Lock()

    # ---------- config ----------

    def _load(self) -> list[dict[str, Any]]:
        raw = self.store.get_app_setting(MCP_SETTINGS_KEY)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return [item for item in data if isinstance(item, dict) and item.get("id")]

    def _save(self) -> None:
        self.store.set_app_setting(
            MCP_SETTINGS_KEY, json.dumps(self.servers, ensure_ascii=False)
        )

    def get_config(self, server_id: str) -> dict[str, Any] | None:
        return next((item for item in self.servers if item["id"] == server_id), None)

    @staticmethod
    def validate(payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        transport = str(payload.get("transport") or "stdio")
        if not name:
            raise ValueError("名称不能为空")
        if transport not in TRANSPORTS:
            raise ValueError("不支持的传输方式")
        command = str(payload.get("command") or "").strip()
        url = str(payload.get("url") or "").strip()
        if transport == "stdio" and not command:
            raise ValueError("stdio 模式必须填写命令")
        if transport != "stdio" and not url.startswith(("http://", "https://")):
            raise ValueError("HTTP 模式必须填写 http(s) URL")
        args = payload.get("args") or []
        if isinstance(args, str):
            args = [line.strip() for line in args.splitlines() if line.strip()]
        if not isinstance(args, list):
            raise ValueError("args 必须是字符串列表")
        env = payload.get("env")
        headers = payload.get("headers")
        if env is not None and not isinstance(env, dict):
            raise ValueError("env 必须是对象")
        if headers is not None and not isinstance(headers, dict):
            raise ValueError("headers 必须是对象")
        return {
            "name": name[:80],
            "transport": transport,
            "command": command[:1000],
            "args": [str(item)[:500] for item in args][:100],
            "cwd": str(payload.get("cwd") or "").strip()[:1000],
            "url": url[:2000],
            "enabled": bool(payload.get("enabled", True)),
            "env": {str(k)[:120]: str(v)[:4000] for k, v in (env or {}).items()},
            "headers": {
                str(k)[:120]: str(v)[:4000] for k, v in (headers or {}).items()
            },
        }

    async def upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            server_id = str(payload.get("id") or "").strip()
            existing = self.get_config(server_id) if server_id else None
            if server_id and existing is None:
                raise ValueError("MCP 服务器不存在或已被删除")
            clean = self.validate(payload)
            if existing:
                # Empty secret fields mean "keep the saved values".
                if not payload.get("env"):
                    clean["env"] = existing.get("env") or {}
                if not payload.get("headers"):
                    clean["headers"] = existing.get("headers") or {}
                existing.update(clean)
                config = existing
            else:
                server_id = new_id("mcp")
                config = {"id": server_id, **clean, "created_at": time.time()}
                self.servers.append(config)
            self._save()
            await self._apply(config)
            return config

    async def delete(self, server_id: str) -> bool:
        async with self._lock:
            config = self.get_config(server_id)
            if config is None:
                return False
            await self._stop(server_id)
            self.servers = [item for item in self.servers if item["id"] != server_id]
            self._save()
            return True

    async def _apply(self, config: dict[str, Any]) -> None:
        server_id = config["id"]
        await self._stop(server_id)
        if not config.get("enabled", True):
            return
        await self._start(server_id, raise_on_error=False)

    # ---------- lifecycle ----------

    async def _start(self, server_id: str, *, raise_on_error: bool = True) -> None:
        config = self.get_config(server_id)
        if config is None:
            raise ValueError("MCP 服务器不存在")
        if not MCP_AVAILABLE:
            self._errors[server_id] = f"MCP SDK 未安装: {MCP_IMPORT_ERROR}"
            if raise_on_error:
                raise ValueError(self._errors[server_id])
            return
        await self._stop(server_id)
        connection = MCPConnection(config, self.settings)
        try:
            await connection.start()
        except Exception as exc:  # noqa: BLE001
            self._errors[server_id] = str(exc)
            if raise_on_error:
                raise ValueError(str(exc)) from exc
            return
        self._errors.pop(server_id, None)
        self._connections[server_id] = connection

    async def _stop(self, server_id: str) -> None:
        connection = self._connections.pop(server_id, None)
        if connection is not None:
            await connection.stop()

    async def start_server(self, server_id: str) -> None:
        async with self._lock:
            await self._start(server_id, raise_on_error=True)

    async def stop_server(self, server_id: str) -> bool:
        async with self._lock:
            if self.get_config(server_id) is None:
                return False
            await self._stop(server_id)
            return True

    async def start_enabled(self) -> None:
        """Connect enabled servers without blocking web UI startup.

        npx-based stdio servers can take a long time (or hit the network) on
        first launch. The console should still open immediately; tools appear
        as soon as the connection is ready.
        """
        configs = [item for item in self.servers if item.get("enabled", True)]
        if not configs:
            return

        async def connect_all() -> None:
            await asyncio.gather(
                *(
                    self._start(item["id"], raise_on_error=False)
                    for item in configs
                ),
                return_exceptions=True,
            )

        self._startup_task = asyncio.create_task(
            connect_all(), name="mcp-startup"
        )

    async def stop_all(self) -> None:
        task = self._startup_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._startup_task = None
        for server_id in list(self._connections):
            await self._stop(server_id)

    async def reload(self) -> None:
        async with self._lock:
            await self.stop_all()
            self.servers = self._load()
            self._errors.clear()
            await self.start_enabled()

    def schedule_reload(self) -> None:
        """Restart servers in the background after workspace changes."""
        if self._reload_task and not self._reload_task.done():
            return
        self._reload_task = asyncio.create_task(self._reload_safely())

    async def _reload_safely(self) -> None:
        try:
            await self.reload()
        except Exception:  # noqa: BLE001 - background maintenance
            pass

    # ---------- tools ----------

    def tool_name(self, server_id: str, tool: str) -> str:
        base = re.sub(r"[^a-zA-Z0-9_-]", "_", f"mcp__{server_id}__{tool}")
        if len(base) > 64:
            digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
            base = f"{base[:55]}_{digest}"
        return base

    def schemas(self) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        self._tool_index = {}
        used: set[str] = set()
        for server_id, connection in self._connections.items():
            config = self.get_config(server_id) or {}
            for tool in connection.tools:
                schema = tool.get("input_schema") or {
                    "type": "object",
                    "properties": {},
                }
                if not isinstance(schema, dict):
                    schema = {"type": "object", "properties": {}}
                base = self.tool_name(server_id, tool["name"])
                name = base
                suffix = 2
                while name in used:
                    name = f"{base[:55]}_{suffix}"
                    suffix += 1
                used.add(name)
                self._tool_index[name] = (server_id, tool["name"])
                schemas.append(
                    {
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": (
                                f"[MCP · {config.get('name', server_id)}] "
                                f"{tool.get('description', '')}"
                            )[:900],
                            "parameters": schema,
                        },
                    }
                )
        return schemas

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        target = self._tool_index.get(name)
        if target is not None:
            server_id, tool_name = target
        else:
            parts = name.split("__", 2)
            if len(parts) != 3 or parts[0] != "mcp":
                raise ValueError(f"未知 MCP 工具: {name}")
            server_id, tool_name = parts[1], parts[2]
        connection = self._connections.get(server_id)
        if connection is None:
            raise ValueError("MCP 服务器未连接")
        result = await connection.call(tool_name, arguments)
        texts = [
            item.get("text", "")
            for item in result.get("content", [])
            if item.get("type") == "text" and item.get("text")
        ]
        payload: dict[str, Any] = {
            "server": server_id,
            "tool": tool_name,
            "is_error": result.get("is_error", False),
            "content": "\n".join(texts),
        }
        if result.get("structured") is not None:
            payload["structured"] = result["structured"]
        return json.dumps(payload, ensure_ascii=False, default=str)

    # ---------- introspection ----------

    def public_state(self) -> dict[str, Any]:
        servers = []
        for config in self.servers:
            connection = self._connections.get(config["id"])
            servers.append(
                {
                    "id": config["id"],
                    "name": config["name"],
                    "transport": config.get("transport", "stdio"),
                    "command": config.get("command", ""),
                    "args": config.get("args", []),
                    "cwd": config.get("cwd", ""),
                    "url": config.get("url", ""),
                    "enabled": bool(config.get("enabled", True)),
                    "env_keys": sorted((config.get("env") or {}).keys()),
                    "header_keys": sorted((config.get("headers") or {}).keys()),
                    "connected": bool(connection and connection.running),
                    "tools": (
                        [
                            {
                                "name": tool["name"],
                                "description": tool.get("description", ""),
                                "exposed_as": self.tool_name(config["id"], tool["name"]),
                            }
                            for tool in connection.tools
                        ]
                        if connection
                        else []
                    ),
                    "error": self._errors.get(config["id"], "")
                    or (connection.error if connection else ""),
                }
            )
        return {
            "available": MCP_AVAILABLE,
            "error": MCP_IMPORT_ERROR,
            "servers": servers,
            "presets": MCP_PRESETS,
            "tool_count": sum(len(item["tools"]) for item in servers),
        }

    async def test(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not MCP_AVAILABLE:
            raise ValueError(f"MCP SDK 未安装: {MCP_IMPORT_ERROR}")
        clean = self.validate(payload)
        connection = MCPConnection(clean, self.settings)
        try:
            await connection.start()
            return {
                "ok": True,
                "tools": [
                    {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                    }
                    for tool in connection.tools
                ],
            }
        finally:
            await connection.stop()

    async def test_server(self, server_id: str) -> dict[str, Any]:
        connection = self._connections.get(server_id)
        if connection is not None and connection.running:
            return {
                "ok": True,
                "reused": True,
                "tools": [
                    {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                    }
                    for tool in connection.tools
                ],
            }
        config = self.get_config(server_id)
        if config is None:
            raise ValueError("MCP 服务器不存在")
        return await self.test(config)
