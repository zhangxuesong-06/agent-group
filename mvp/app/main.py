from __future__ import annotations

import asyncio
import hmac
import io
import json
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import unquote

from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
import segno

from .agents import AgentRegistry
from .approvals import ApprovalManager
from .bus import EventBus
from .clusters import build_cluster_snapshot, cluster_public
from .config import settings
from .devices import DeviceHub, hash_worker_token, new_worker_token
from .evaluation import EVAL_TESTS, EvaluationRunner
from .llm import LLMClient
from .mcp import MCPManager
from .network import network_info
from .models import (
    ApprovalInput,
    BatchScoreInput,
    BranchInput,
    Budget,
    ClusterInput,
    CreateRunRequest,
    DeviceInput,
    MessageInput,
    MCPServerInput,
    ProviderEnabledInput,
    ProviderInput,
    ProviderScoreInput,
    ProviderTestInput,
    ResumeRunInput,
    RunState,
    RuntimeSettingsInput,
    new_id,
    now,
)
from .orchestrator import Orchestrator
from .routing import DOMAINS, ModelRouter, parse_scores
from .store import Store
from . import providers as provider_service
from . import tools


store = Store(str(settings.db_path))
provider_service.seed_from_env(store, settings)


def _apply_saved_runtime_settings() -> None:
    """Apply web-editable settings over the environment defaults."""
    workspace = store.get_app_setting("workspace_dir")
    if workspace:
        try:
            path = Path(workspace).expanduser()
            if path.is_absolute():
                path.mkdir(parents=True, exist_ok=True)
                if path.is_dir():
                    settings.workspace_dir = path.resolve()
        except OSError:
            pass
    for key, attr in (("shell_enabled", "shell_enabled"), ("command_approval", "command_approval")):
        value = store.get_app_setting(key)
        if value in ("0", "1"):
            setattr(settings, attr, value == "1")
    timeout = store.get_app_setting("approval_timeout")
    if timeout and timeout.isdigit():
        settings.approval_timeout = max(30, min(int(timeout), 3600))


_apply_saved_runtime_settings()
bus = EventBus(store)
llm = LLMClient(
    settings,
    lambda provider_id=None: (
        store.get_provider(provider_id) if provider_id else store.get_active_provider()
    ),
)
registry = AgentRegistry()
router = ModelRouter(store)
approvals = ApprovalManager(bus, settings.approval_timeout)
mcp_manager = MCPManager(store, settings)
device_hub = DeviceHub()
orchestrator = Orchestrator(
    store,
    bus,
    llm,
    registry,
    settings,
    router,
    approvals=approvals,
    mcp=mcp_manager,
    devices=device_hub,
)
evaluator = EvaluationRunner(store, llm, settings)

TERMINAL_EVENTS = {"run.completed", "run.failed", "run.cancelled"}
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "draft"}
STATIC_DIR = settings.base_dir / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        await mcp_manager.start_enabled()
    except Exception:  # noqa: BLE001 - MCP errors must not block the web UI
        pass
    try:
        recovered = await orchestrator.recover_interrupted_runs()
        if recovered:
            print(f"  [recover] resumed {len(recovered)} interrupted run(s): {', '.join(recovered)}")
    except Exception as exc:  # noqa: BLE001 - recovery must not block the web UI
        print(f"  [recover] failed: {type(exc).__name__}: {exc}")
    try:
        yield
    finally:
        try:
            await orchestrator.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            await mcp_manager.stop_all()
        except Exception:  # noqa: BLE001
            pass
        store.close()


app = FastAPI(title="AI Cluster MVP", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def require_access_token(request: Request, call_next):
    """Optional bearer-token gate. Disabled when ACCESS_TOKEN is empty."""
    token = settings.access_token
    path = request.url.path
    if (
        not token
        or not path.startswith("/api")
        or path == "/api/health"
        or request.method == "OPTIONS"
    ):
        return await call_next(request)

    supplied = ""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        supplied = header[7:].strip()
    if not supplied:
        supplied = unquote(request.cookies.get("cluster_token", ""))
    if not supplied:
        supplied = request.query_params.get("token", "")
    if not hmac.compare_digest(supplied, token):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


# ----------------------------------------------------------------------
# Meta
# ----------------------------------------------------------------------


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "mock": llm.is_mock}


@app.get("/api/meta")
async def meta() -> dict:
    active = store.get_active_provider()
    mcp_state = mcp_manager.public_state()
    return {
        "mock": llm.is_mock,
        "model": llm.model_name,
        "provider": (
            {
                "id": active["id"],
                "name": active["name"],
                "model": active["model"],
                "api_style": active.get("api_style", "openai_chat"),
            }
            if active
            else None
        ),
        "models": [
            {
                "id": p["id"],
                "name": p["name"],
                "model": p["model"],
                "is_default": bool(p["is_default"]),
                "scores": parse_scores(p.get("scores")),
            }
            for p in store.get_enabled_providers()
        ],
        "auth_required": bool(settings.access_token),
        "max_concurrency": settings.max_concurrency,
        "workspace": {
            "path": str(settings.workspace_dir),
            "shell_enabled": settings.shell_enabled,
            "shell_timeout": settings.shell_timeout,
            "command_approval": settings.command_approval,
            "approval_timeout": approvals.timeout,
        },
        "mcp": {
            "available": mcp_state["available"],
            "server_count": len(mcp_manager.servers),
            "tool_count": mcp_state["tool_count"],
        },
        "agents": [
            {
                "id": a.id,
                "name": a.name,
                "capabilities": a.capabilities,
                "description": a.description,
                "tools": a.tools,
                "max_steps": a.max_steps,
            }
            for a in registry.all()
        ],
        "tools": [
            {"name": name, "description": tool.description}
            for name, tool in tools.TOOLS.items()
        ]
        + [
            {
                "name": "mcp",
                "description": "已连接 MCP 服务器提供的动态工具（按服务器自动注册）",
            }
        ],
    }


# ----------------------------------------------------------------------
# Providers
# ----------------------------------------------------------------------


@app.get("/api/providers")
async def get_providers() -> dict:
    active = store.get_active_provider()
    latest = store.latest_evaluations()
    providers = []
    for provider in store.list_providers():
        item = provider_service.public_provider(provider)
        evaluation = latest.get(provider["id"])
        if evaluation:
            item["latest_evaluation"] = {
                "id": evaluation["id"],
                "average": evaluation["average"],
                "status": evaluation["status"],
                "created_at": evaluation["created_at"],
                "finished_at": evaluation["finished_at"],
                "scores": evaluation["scores"],
                "usage": evaluation["usage"],
            }
        else:
            item["latest_evaluation"] = None
        providers.append(item)
    return {
        "presets": provider_service.PRESETS,
        "providers": providers,
        "active_id": active["id"] if active else None,
    }


@app.post("/api/providers")
async def save_provider(body: ProviderInput) -> dict:
    name = body.name.strip()
    model = body.model.strip()
    if not name or not model:
        raise HTTPException(status_code=400, detail="名称和模型不能为空")
    try:
        base_url = provider_service.normalize_base_url(body.base_url, body.api_style)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    extra_headers = body.extra_headers.strip()
    if extra_headers:
        try:
            provider_service.parse_extra_headers(extra_headers)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if body.id and store.get_provider(body.id) is None:
        raise HTTPException(status_code=404, detail="provider not found")

    try:
        scores = {
            key: max(0.0, min(10.0, float(value)))
            for key, value in body.scores.items()
            if key in DOMAINS
        }
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="能力评分必须是 0-10 的数字") from exc
    existing = store.get_provider(body.id) if body.id else None
    if existing and not body.scores:
        scores_json = existing.get("scores") or "{}"
    else:
        scores_json = json.dumps(scores, ensure_ascii=False)

    source = store.get_provider(body.copy_from) if body.copy_from else None
    if body.copy_from and source is None:
        raise HTTPException(status_code=404, detail="copy source provider not found")
    api_key = body.api_key.strip()
    group_id = body.group_id
    proxy_value = body.proxy.strip()
    if source is not None:
        group_id = group_id or source.get("group_id") or source["id"]
        if not api_key:
            api_key = source["api_key"]
        if not extra_headers:
            extra_headers = source.get("extra_headers") or ""
        if not proxy_value:
            proxy_value = source.get("proxy") or ""

    row = store.upsert_provider(
        body.id,
        name,
        base_url,
        api_key,
        model,
        api_style=body.api_style,
        auth_style=body.auth_style,
        extra_headers=extra_headers,
        proxy=proxy_value,
        scores=scores_json,
        reasoning_effort=body.reasoning_effort,
        timeout=body.timeout,
        stream=body.stream,
        enabled=body.enabled,
        is_default=body.is_default,
        group_id=group_id,
    )
    active = store.get_active_provider()
    return {
        "provider": provider_service.public_provider(row),
        "active_id": active["id"] if active else None,
    }


@app.post("/api/providers/{provider_id}/activate")
async def activate_provider(provider_id: str) -> dict:
    if not store.set_active_provider(provider_id):
        raise HTTPException(status_code=404, detail="provider not found")
    return {"active_id": provider_id}


@app.post("/api/providers/{provider_id}/enabled")
async def enable_provider(provider_id: str, body: ProviderEnabledInput) -> dict:
    if not store.set_provider_enabled(provider_id, body.enabled):
        raise HTTPException(status_code=404, detail="provider not found")
    active = store.get_active_provider()
    return {
        "enabled": body.enabled,
        "active_id": active["id"] if active else None,
    }


@app.post("/api/providers/{provider_id}/default")
async def set_default_provider(provider_id: str) -> dict:
    if not store.set_default_provider(provider_id):
        raise HTTPException(status_code=404, detail="provider not found")
    return {"active_id": provider_id}


@app.delete("/api/providers/{provider_id}")
async def remove_provider(provider_id: str) -> dict:
    if not store.delete_provider(provider_id):
        raise HTTPException(status_code=404, detail="provider not found")
    active = store.get_active_provider()
    return {"deleted": True, "active_id": active["id"] if active else None}


def _provider_input(body: ProviderTestInput) -> tuple[str, str, str, str, str, str, str]:
    base_url = (body.base_url or "").strip()
    api_key = body.api_key.strip()
    model = (body.model or "").strip()
    api_style = body.api_style
    auth_style = body.auth_style
    extra_headers = body.extra_headers.strip()
    proxy = body.proxy.strip()

    if body.copy_from:
        source = store.get_provider(body.copy_from)
        if source is None:
            raise HTTPException(status_code=404, detail="copy source provider not found")
        base_url = base_url or source["base_url"]
        api_key = api_key or source["api_key"]
        api_style = api_style or source.get("api_style") or "openai_chat"
        auth_style = auth_style or source.get("auth_style") or "bearer"
        extra_headers = extra_headers or source.get("extra_headers") or ""
        proxy = proxy or source.get("proxy") or ""

    if body.id:
        existing = store.get_provider(body.id)
        if existing is None:
            raise HTTPException(status_code=404, detail="provider not found")
        base_url = base_url or existing["base_url"]
        api_key = api_key or existing["api_key"]
        model = model or existing["model"]
        api_style = api_style or existing.get("api_style") or "openai_chat"
        auth_style = auth_style or existing.get("auth_style") or "bearer"
        extra_headers = extra_headers or existing.get("extra_headers") or ""
        proxy = proxy or existing.get("proxy") or ""

    api_style = api_style or "openai_chat"
    auth_style = auth_style or "bearer"
    if base_url:
        try:
            base_url = provider_service.normalize_base_url(base_url, api_style)
            provider_service.parse_extra_headers(extra_headers)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return base_url, api_key, model, api_style, auth_style, extra_headers, proxy


@app.post("/api/providers/test")
async def test_provider(body: ProviderTestInput) -> dict:
    base_url, api_key, model, api_style, auth_style, extra_headers, proxy = _provider_input(body)
    if not base_url or not model:
        raise HTTPException(status_code=400, detail="Base URL 与模型不能为空")
    return await provider_service.test_connection(
        base_url, api_key, model, api_style, auth_style, extra_headers, proxy
    )


@app.post("/api/providers/models")
async def get_models(body: ProviderTestInput) -> dict:
    base_url, api_key, _model, api_style, auth_style, extra_headers, proxy = _provider_input(body)
    if not base_url:
        raise HTTPException(status_code=400, detail="Base URL 不能为空")
    return await provider_service.list_models(
        base_url, api_key, api_style, auth_style, extra_headers, proxy
    )


@app.post("/api/providers/diagnose")
async def diagnose_provider(body: ProviderTestInput) -> dict:
    base_url, api_key, model, api_style, auth_style, extra_headers, proxy = _provider_input(body)
    if not base_url:
        raise HTTPException(status_code=400, detail="Base URL 不能为空")
    return await provider_service.diagnose(
        base_url, api_key, model, api_style, auth_style, extra_headers, proxy
    )


@app.post("/api/providers/{provider_id}/evaluate")
async def evaluate_provider(provider_id: str, body: ProviderScoreInput) -> dict:
    if store.get_provider(provider_id) is None:
        raise HTTPException(status_code=404, detail="provider not found")
    try:
        job_id = await evaluator.start(provider_id, body.judge_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"job_id": job_id, "total": len(EVAL_TESTS)}


@app.post("/api/evaluations/batch")
async def evaluate_batch(body: BatchScoreInput) -> dict:
    try:
        batch_id = await evaluator.start_batch(body.provider_ids, body.judge_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"batch_id": batch_id}


@app.get("/api/evaluations/batch/{batch_id}")
async def get_evaluation_batch(batch_id: str) -> dict:
    batch = evaluator.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="evaluation batch not found")
    return batch


@app.post("/api/evaluations/batch/{batch_id}/cancel")
async def cancel_evaluation_batch(batch_id: str) -> dict:
    ok = await evaluator.cancel_batch(batch_id)
    if not ok:
        raise HTTPException(status_code=409, detail="batch cannot be cancelled")
    return {"cancelled": True}


@app.get("/api/evaluations/{job_id}")
async def get_evaluation(job_id: str) -> dict:
    job = evaluator.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="evaluation not found")
    return job


@app.get("/api/providers/{provider_id}/evaluations")
async def provider_evaluations(
    provider_id: str, limit: int = Query(default=20, ge=1, le=100)
) -> dict:
    if store.get_provider(provider_id) is None:
        raise HTTPException(status_code=404, detail="provider not found")
    return {"evaluations": store.list_evaluations(provider_id, limit)}


# ----------------------------------------------------------------------
# Runtime settings, approvals, MCP
# ----------------------------------------------------------------------


def _runtime_settings() -> dict:
    return {
        "workspace_dir": str(settings.workspace_dir),
        "shell_enabled": settings.shell_enabled,
        "command_approval": settings.command_approval,
        "approval_timeout": approvals.timeout,
        "shell_timeout": settings.shell_timeout,
    }


@app.get("/api/devices")
async def list_devices() -> dict:
    online = device_hub.online_ids()
    devices = []
    for device in store.list_devices():
        devices.append({**device, "online": device["id"] in online})
    return {"devices": devices}


@app.post("/api/devices", status_code=201)
async def create_device(body: DeviceInput) -> dict:
    token = new_worker_token()
    device = store.create_device(body.name, hash_worker_token(token))
    public = {key: value for key, value in device.items() if key != "token_hash"}
    public["online"] = False
    return {"device": public, "token": token}


@app.delete("/api/devices/{device_id}")
async def delete_device(device_id: str) -> dict:
    record = store.get_device(device_id)
    if record is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    device_hub.detach(device_id)
    store.delete_device(device_id)
    return {"deleted": True}


@app.websocket("/api/worker/ws")
async def worker_socket(websocket: WebSocket, token: str = Query(default="")) -> None:
    authorization = websocket.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    record = store.get_device_by_token_hash(hash_worker_token(token))
    if record is None:
        await websocket.close(code=4401, reason="invalid worker token")
        return
    await websocket.accept()
    device_id = record["id"]
    try:
        hello = await asyncio.wait_for(websocket.receive_json(), timeout=20)
        workspaces = [
            str(item).strip()
            for item in (hello.get("workspaces") or [])
            if str(item).strip()
        ][:32]
        platform = str(hello.get("platform") or "")[:120]
        name = str(hello.get("name") or record["name"])[:80]
        store.update_device(
            device_id,
            name=name,
            platform=platform,
            workspaces=workspaces,
            last_seen=time.time(),
        )
        await device_hub.attach(device_id, websocket)
        await websocket.send_json(
            {"type": "welcome", "device_id": device_id, "name": name}
        )
        while True:
            message = await websocket.receive_json()
            kind = message.get("type")
            if kind == "result":
                device_hub.resolve(device_id, message)
            elif kind in ("heartbeat", "ping"):
                store.update_device(device_id, last_seen=time.time())
                await websocket.send_json({"type": "pong"})
            elif kind == "hello":
                workspaces = [
                    str(item).strip()
                    for item in (message.get("workspaces") or [])
                    if str(item).strip()
                ][:32]
                store.update_device(
                    device_id,
                    name=str(message.get("name") or name)[:80],
                    platform=str(message.get("platform") or platform)[:120],
                    workspaces=workspaces,
                    last_seen=time.time(),
                )
    except (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError):
        pass
    finally:
        device_hub.detach(device_id, websocket)


@app.get("/api/network")
async def get_network(request: Request) -> dict:
    return network_info(
        settings.port, str(request.base_url).rstrip("/")
    )


@app.get("/api/network/qr")
async def network_qr(url: str = Query(min_length=1, max_length=500)) -> Response:
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="only http(s) URLs can be encoded")
    qr = segno.make(url, error="m")
    buffer = io.BytesIO()
    qr.save(buffer, kind="svg", scale=6, border=2)
    return Response(
        content=buffer.getvalue(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/settings")
async def get_runtime_settings() -> dict:
    return _runtime_settings()


@app.post("/api/settings")
async def update_runtime_settings(body: RuntimeSettingsInput) -> dict:
    workspace_changed = False
    if body.workspace_dir is not None:
        raw = body.workspace_dir.strip()
        if raw:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                raise HTTPException(status_code=400, detail="工作区必须是绝对路径")
            if str(path) == path.anchor:
                raise HTTPException(status_code=400, detail="不能把磁盘根目录设为工作区")
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise HTTPException(
                    status_code=400, detail=f"无法创建或访问目录: {exc}"
                ) from exc
            if not path.is_dir():
                raise HTTPException(status_code=400, detail="工作区不是目录")
            resolved = path.resolve()
            if resolved != settings.workspace_dir:
                workspace_changed = True
            settings.workspace_dir = resolved
            store.set_app_setting("workspace_dir", str(settings.workspace_dir))
    if body.shell_enabled is not None:
        settings.shell_enabled = body.shell_enabled
        store.set_app_setting("shell_enabled", "1" if body.shell_enabled else "0")
    if body.command_approval is not None:
        settings.command_approval = body.command_approval
        store.set_app_setting(
            "command_approval", "1" if body.command_approval else "0"
        )
    if body.approval_timeout is not None:
        approvals.timeout = body.approval_timeout
        settings.approval_timeout = body.approval_timeout
        store.set_app_setting("approval_timeout", str(body.approval_timeout))
    if workspace_changed and mcp_manager.servers:
        mcp_manager.schedule_reload()
    return _runtime_settings()


@app.get("/api/approvals")
async def list_approvals(run_id: str | None = None) -> dict:
    return {"approvals": approvals.list_pending(run_id)}


@app.post("/api/approvals/{approval_id}")
async def resolve_approval(approval_id: str, body: ApprovalInput) -> dict:
    record = await approvals.resolve(
        approval_id, body.approved, scope=body.scope
    )
    if record is None:
        raise HTTPException(status_code=404, detail="审批已失效或已被处理")
    return {"resolved": True, "approved": body.approved}


@app.get("/api/mcp/servers")
async def list_mcp_servers() -> dict:
    return mcp_manager.public_state()


@app.post("/api/mcp/servers")
async def upsert_mcp_server(body: MCPServerInput) -> dict:
    try:
        config = await mcp_manager.upsert(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state = mcp_manager.public_state()
    state["saved_id"] = config["id"]
    return state


@app.delete("/api/mcp/servers/{server_id}")
async def delete_mcp_server(server_id: str) -> dict:
    if not await mcp_manager.delete(server_id):
        raise HTTPException(status_code=404, detail="MCP server not found")
    return mcp_manager.public_state()


@app.post("/api/mcp/servers/{server_id}/start")
async def start_mcp_server(server_id: str) -> dict:
    try:
        await mcp_manager.start_server(server_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return mcp_manager.public_state()


@app.post("/api/mcp/servers/{server_id}/stop")
async def stop_mcp_server(server_id: str) -> dict:
    if not await mcp_manager.stop_server(server_id):
        raise HTTPException(status_code=404, detail="MCP server not found")
    return mcp_manager.public_state()


@app.post("/api/mcp/servers/{server_id}/test")
async def test_mcp_server(server_id: str) -> dict:
    if mcp_manager.get_config(server_id) is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    try:
        result = await mcp_manager.test_server(server_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@app.post("/api/mcp/test")
async def test_mcp_config(body: MCPServerInput) -> dict:
    data = body.model_dump()
    if body.id:
        existing = mcp_manager.get_config(body.id)
        if existing is not None:
            if not body.env:
                data["env"] = existing.get("env") or {}
            if not body.headers:
                data["headers"] = existing.get("headers") or {}
    try:
        result = await mcp_manager.test(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@app.post("/api/mcp/reload")
async def reload_mcp() -> dict:
    await mcp_manager.reload()
    return mcp_manager.public_state()


# ----------------------------------------------------------------------
# Cluster presets
# ----------------------------------------------------------------------


@app.get("/api/clusters")
async def list_clusters() -> dict:
    clusters = [cluster_public(store, cluster) for cluster in store.list_clusters()]
    default = store.default_cluster()
    return {"clusters": clusters, "default_id": default["id"] if default else None}


@app.post("/api/clusters")
async def save_cluster(body: ClusterInput) -> dict:
    if body.id and store.get_cluster(body.id) is None:
        raise HTTPException(status_code=404, detail="cluster not found")
    provider_ids = [
        provider_id
        for provider_id in body.provider_ids
        if store.get_provider(provider_id) is not None
    ]
    if not provider_ids:
        raise HTTPException(status_code=400, detail="集群至少需要一个现有模型")
    planner_id = body.planner_id if body.planner_id in provider_ids else provider_ids[0]
    cluster = store.save_cluster(
        {
            "id": body.id,
            "name": body.name,
            "provider_ids": provider_ids,
            "planner_id": planner_id,
            "is_default": body.is_default,
        }
    )
    return {"cluster": cluster_public(store, cluster)}


@app.delete("/api/clusters/{cluster_id}")
async def delete_cluster(cluster_id: str) -> dict:
    if not store.delete_cluster(cluster_id):
        raise HTTPException(status_code=404, detail="cluster not found")
    return {"deleted": True}


@app.post("/api/clusters/{cluster_id}/default")
async def default_cluster(cluster_id: str) -> dict:
    if not store.set_default_cluster(cluster_id):
        raise HTTPException(status_code=404, detail="cluster not found")
    return {"default_id": cluster_id}


# ----------------------------------------------------------------------
# Runs
# ----------------------------------------------------------------------


@app.post("/api/runs", status_code=201)
async def create_run(body: CreateRunRequest) -> dict:
    try:
        cluster = build_cluster_snapshot(
            store,
            body.cluster_id,
            body.cluster_mode,
            mock=llm.is_mock,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    device = None
    if body.device_id:
        record = store.get_device(body.device_id)
        if record is None:
            raise HTTPException(status_code=404, detail="设备不存在")
        if not device_hub.online(record["id"]):
            raise HTTPException(status_code=409, detail="目标设备当前不在线")
        workspace = (body.workspace or "").strip()
        if not workspace:
            roots = record.get("workspaces") or []
            if not roots:
                raise HTTPException(
                    status_code=400, detail="设备尚未上报可用工作区"
                )
            workspace = roots[0]
        if workspace not in (record.get("workspaces") or []):
            raise HTTPException(
                status_code=400, detail="该工作区不在设备授权列表中"
            )
        device = {
            "id": record["id"],
            "name": record["name"],
            "workspace": workspace,
        }

    run_id = await orchestrator.start(
        body.goal, body.budget, cluster=cluster, device=device
    )
    return {"run_id": run_id, "status": "planning"}


@app.get("/api/runs")
async def list_runs(limit: int = Query(default=30, ge=1, le=200)) -> dict:
    return {"runs": store.list_runs(limit)}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    run["last_event_seq"] = store.last_event_seq(run_id)
    return run


@app.patch("/api/runs/{run_id}/messages/{message_id}")
async def edit_message(
    run_id: str, message_id: str, body: MessageInput
) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    message = store.get_message(message_id)
    if message is None or message["run_id"] != run_id:
        raise HTTPException(status_code=404, detail="message not found")
    if message["role"] != "user":
        raise HTTPException(status_code=400, detail="只能编辑用户消息")
    if run["status"] not in TERMINAL_STATUSES:
        raise HTTPException(status_code=400, detail="运行中不能编辑消息")
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="内容不能为空")
    updated = store.update_message(message_id, content)
    if message["kind"] == "goal":
        store.update_run_goal(run_id, content)
    return {"message": updated}


@app.delete("/api/runs/{run_id}/messages/{message_id}")
async def delete_message(run_id: str, message_id: str) -> dict:
    message = store.get_message(message_id)
    if message is None or message["run_id"] != run_id:
        raise HTTPException(status_code=404, detail="message not found")
    if message["kind"] in ("goal", "result"):
        raise HTTPException(status_code=400, detail="不能单独删除目标或结果消息")
    store.delete_message(message_id)
    return {"deleted": True}


@app.post("/api/runs/{run_id}/branch", status_code=201)
async def branch_run(run_id: str, body: BranchInput) -> dict:
    source = store.get_run(run_id)
    if source is None:
        raise HTTPException(status_code=404, detail="run not found")
    messages = source.get("messages") or []
    index = next(
        (i for i, message in enumerate(messages) if message["id"] == body.message_id),
        None,
    )
    if index is None:
        raise HTTPException(status_code=404, detail="message not found")
    cloned = messages[: index + 1]
    last_result = next(
        (m for m in reversed(cloned) if m["kind"] == "result"), None
    )
    result = last_result["content"] if last_result else ""
    run = RunState(
        id=new_id("run"),
        goal=source["goal"],
        status="succeeded" if result else "draft",
        budget=Budget(**source["budget"]),
        result=result,
        cluster=source.get("cluster") or {},
        deadline_at=now(),
    )
    store.create_run(run)
    for message in cloned:
        store.add_message(
            run.id,
            message["role"],
            message["content"],
            kind=message["kind"],
            meta=message.get("meta") or {},
            ts=message["ts"],
        )
    return {"run_id": run.id}


@app.post("/api/runs/{run_id}/messages")
async def post_message(run_id: str, body: MessageInput) -> StreamingResponse:
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    return StreamingResponse(
        orchestrator.front_chat(run_id, body.content),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict:
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    ok = await orchestrator.cancel(run_id)
    return {"cancelled": ok}


@app.post("/api/runs/{run_id}/rerun", status_code=201)
async def rerun(run_id: str) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    budget_data = dict(run["budget"])
    # Runs created before the 2M-token default should not inherit the old 200k
    # limit and fail immediately.
    if budget_data.get("max_tokens") == 200_000:
        budget_data["max_tokens"] = settings.max_tokens
    if budget_data.get("deadline_sec") == 900:
        budget_data["deadline_sec"] = settings.deadline_sec
    cluster = run.get("cluster") or {}
    device = cluster.get("device") or None
    if device is not None:
        record = store.get_device(device.get("id", ""))
        if record is None or not device_hub.online(record["id"]):
            raise HTTPException(status_code=409, detail="原设备当前不在线")
        if device.get("workspace") not in (record.get("workspaces") or []):
            raise HTTPException(status_code=400, detail="原设备工作区已失效")
    if cluster.get("id"):
        try:
            cluster = build_cluster_snapshot(
                store,
                cluster["id"],
                not cluster.get("single_model"),
                mock=llm.is_mock,
            )
        except ValueError:
            pass
    new_run_id = await orchestrator.start(
        run["goal"], Budget(**budget_data), cluster=cluster, device=device
    )
    return {"run_id": new_run_id, "status": "planning"}


@app.post("/api/runs/{run_id}/resume")
async def resume_run(run_id: str, body: ResumeRunInput | None = None) -> dict:
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    task_id = body.task_id if body else None
    ok = await orchestrator.resume(run_id, task_id)
    if not ok:
        raise HTTPException(status_code=409, detail="run cannot be resumed")
    return {"resumed": True, "run_id": run_id, "task_id": task_id}


@app.post("/api/runs/{run_id}/tasks/{task_id}/retry")
async def retry_task(run_id: str, task_id: str) -> dict:
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    ok = await orchestrator.resume(run_id, task_id)
    if not ok:
        raise HTTPException(status_code=409, detail="task cannot be retried")
    return {"resumed": True, "run_id": run_id, "task_id": task_id}


@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: str) -> dict:
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    if orchestrator.is_active(run_id):
        raise HTTPException(status_code=409, detail="运行仍在进行，请先取消")
    store.delete_run(run_id)
    approvals.forget_run(run_id)
    shutil.rmtree(settings.artifact_dir / run_id, ignore_errors=True)
    return {"deleted": True}


@app.get("/api/runs/{run_id}/artifacts")
async def list_artifacts(run_id: str) -> dict:
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    directory = settings.artifact_dir / run_id
    items = []
    if directory.is_dir():
        for path in sorted(directory.iterdir()):
            if path.is_file():
                items.append({"name": path.name, "size": path.stat().st_size})
    return {"artifacts": items}


@app.get("/api/runs/{run_id}/artifacts/{name}")
async def get_artifact(run_id: str, name: str) -> FileResponse:
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    base = (settings.artifact_dir / run_id).resolve()
    target = (base / name).resolve()
    if base not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(target, filename=target.name)


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: str, after: int = Query(default=0, ge=0)) -> StreamingResponse:
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")

    queue = await bus.subscribe(run_id)

    def encode(event: dict) -> str:
        data = json.dumps(event, ensure_ascii=False)
        if event.get("transient") or "seq" not in event:
            # Token deltas are live-only and never persisted, so they have no
            # sequence number to resume from.
            return f"data: {data}\n\n"
        return f"id: {event['seq']}\ndata: {data}\n\n"

    async def generator() -> AsyncIterator[str]:
        last = after
        try:
            # A resumed run has historical terminal events in its log. Only
            # treat them as the end of the stream when the run is terminal
            # *right now*; otherwise replay the log and continue into live
            # events after the marker below.
            current = store.get_run(run_id)
            terminal_now = bool(
                current and current["status"] in TERMINAL_STATUSES
            )
            for event in store.get_events(run_id, after):
                last = event["seq"]
                yield encode(event)
                if terminal_now and event["type"] in TERMINAL_EVENTS:
                    yield "event: end\ndata: {}\n\n"
                    return

            if not terminal_now:
                # Send the authoritative state at the history/live boundary.
                # Events replayed above are log-only, so the client would
                # otherwise miss anything that happened between its own fetch
                # and this stream opening.
                snapshot = store.get_run(run_id)
                if snapshot is not None:
                    yield (
                        "event: snapshot\ndata: "
                        + json.dumps(snapshot, ensure_ascii=False)
                        + "\n\n"
                    )
                # Everything after this marker is live; the client logs the
                # replayed history but only applies state changes from here on.
                yield "event: live\ndata: {}\n\n"

            run = store.get_run(run_id)
            if run and run["status"] in TERMINAL_STATUSES:
                yield "event: end\ndata: {}\n\n"
                return

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    run = store.get_run(run_id)
                    if run and run["status"] in TERMINAL_STATUSES:
                        yield "event: end\ndata: {}\n\n"
                        return
                    yield ": ping\n\n"
                    continue
                if event.get("transient"):
                    yield encode(event)
                    continue
                if event["seq"] <= last:
                    continue
                last = event["seq"]
                yield encode(event)
                if event["type"] in TERMINAL_EVENTS:
                    yield "event: end\ndata: {}\n\n"
                    return
        finally:
            await bus.unsubscribe(run_id, queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ----------------------------------------------------------------------
# Static UI
# ----------------------------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    path = STATIC_DIR / "index.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="UI not found")
    # The UI ships as a single file; never let the browser keep an old copy
    # after the server has been updated.
    return FileResponse(path, headers={"Cache-Control": "no-store, must-revalidate"})
