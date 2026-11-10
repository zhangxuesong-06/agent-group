from __future__ import annotations

import asyncio
import hmac
import io
import json
import re
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

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
    LoginInput,
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
        store.purge_sessions(settings.session_days * 86400)
    except Exception:  # noqa: BLE001
        pass
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


SESSION_COOKIE = "cluster_session"
LOGIN_WINDOW = 15 * 60
LOGIN_MAX_FAILURES = 5
_login_failures: dict[str, list[float]] = {}
PUBLIC_API_PATHS = {
    "/api/health",
    "/api/auth/login",
    "/api/auth/session",
    "/api/auth/logout",
}


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"


def _ua_label(user_agent: str) -> str:
    ua = user_agent or ""
    if "Edg/" in ua:
        browser = "Edge"
    elif "OPR/" in ua:
        browser = "Opera"
    elif "Chrome/" in ua:
        browser = "Chrome"
    elif "Firefox/" in ua:
        browser = "Firefox"
    elif "Safari/" in ua:
        browser = "Safari"
    else:
        browser = "浏览器"
    if "iPhone" in ua:
        system = "iPhone"
    elif "iPad" in ua:
        system = "iPad"
    elif "Android" in ua:
        system = "Android"
    elif "Windows" in ua:
        system = "Windows"
    elif "Macintosh" in ua:
        system = "macOS"
    elif "Linux" in ua:
        system = "Linux"
    else:
        system = "未知系统"
    return f"{browser} · {system}"


def _is_secure_request(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or proto == "https"


def _set_session_cookie(response: Response, secret: str, request: Request) -> Response:
    response.set_cookie(
        SESSION_COOKIE,
        secret,
        max_age=settings.session_days * 86400,
        httponly=True,
        samesite="strict",
        secure=_is_secure_request(request),
        path="/",
    )
    return response


def _clear_session_cookie(response: Response) -> Response:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


def _session_from_request(request: Request) -> dict | None:
    secret = request.cookies.get(SESSION_COOKIE, "")
    if not secret:
        return None
    session = store.get_session(
        hash_worker_token(secret), settings.session_days * 86400
    )
    if session is not None:
        store.touch_session(session["id"], time.time())
    return session


def _login_retry_after(key: str) -> float:
    now = time.time()
    recent = [item for item in _login_failures.get(key, []) if now - item < LOGIN_WINDOW]
    if recent:
        _login_failures[key] = recent
    else:
        _login_failures.pop(key, None)
    if len(recent) >= LOGIN_MAX_FAILURES:
        return max(1.0, LOGIN_WINDOW - (now - recent[0]))
    return 0.0


@app.middleware("http")
async def require_auth(request: Request, call_next):
    """Every /api route requires a login session or the server token."""
    path = request.url.path
    if request.method == "OPTIONS" or not path.startswith("/api"):
        return await call_next(request)
    if path in PUBLIC_API_PATHS:
        return await call_next(request)

    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        supplied = header[7:].strip()
        if hmac.compare_digest(supplied, settings.access_token):
            request.state.auth = {"kind": "token"}
            return await call_next(request)
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    session = _session_from_request(request)
    if session is None:
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("x-requested-with") != "XMLHttpRequest":
            return JSONResponse({"detail": "missing csrf header"}, status_code=403)
    request.state.auth = {
        "kind": "session",
        "id": session["id"],
        "label": session.get("label", ""),
    }
    return await call_next(request)


@app.post("/api/auth/login")
async def login(body: LoginInput, request: Request) -> Response:
    key = _client_key(request)
    retry = _login_retry_after(key)
    if retry > 0:
        raise HTTPException(
            status_code=429, detail=f"尝试过多，请 {int(retry)} 秒后再试"
        )
    supplied = body.token.strip()
    if not supplied or not hmac.compare_digest(supplied, settings.access_token):
        _login_failures.setdefault(key, []).append(time.time())
        raise HTTPException(status_code=401, detail="令牌不正确")
    _login_failures.pop(key, None)
    secret = secrets.token_urlsafe(32)
    user_agent = request.headers.get("user-agent", "")
    label = body.label.strip() or _ua_label(user_agent)
    session = store.create_session(
        hash_worker_token(secret),
        label,
        user_agent[:300],
        key,
        time.time(),
    )
    payload = {
        "authenticated": True,
        "session": {"id": session["id"], "label": session["label"]},
        "expires_in_days": settings.session_days,
    }
    return _set_session_cookie(JSONResponse(payload), secret, request)


@app.get("/api/auth/session")
async def auth_session(request: Request) -> dict:
    session = _session_from_request(request)
    if session is None:
        return {"authenticated": False, "expires_in_days": settings.session_days}
    return {
        "authenticated": True,
        "session": {
            "id": session["id"],
            "label": session.get("label", ""),
            "created_at": session.get("created_at"),
            "last_seen": session.get("last_seen"),
        },
        "expires_in_days": settings.session_days,
    }


@app.post("/api/auth/logout")
async def logout(request: Request) -> Response:
    session = _session_from_request(request)
    if session is not None:
        store.delete_session(session["id"])
    return _clear_session_cookie(JSONResponse({"authenticated": False}))


@app.get("/api/auth/sessions")
async def auth_sessions(request: Request) -> dict:
    auth = getattr(request.state, "auth", {})
    current_id = auth.get("id") if auth.get("kind") == "session" else ""
    sessions = [
        {
            "id": item["id"],
            "label": item["label"] or "未知设备",
            "created_at": item["created_at"],
            "last_seen": item["last_seen"],
            "current": item["id"] == current_id,
        }
        for item in store.list_sessions()
    ]
    return {"sessions": sessions, "current": current_id}


@app.delete("/api/auth/sessions/{session_id}")
async def revoke_session(session_id: str, request: Request) -> Response:
    auth = getattr(request.state, "auth", {})
    if auth.get("kind") != "session":
        raise HTTPException(status_code=400, detail="令牌鉴权下不能管理会话")
    store.delete_session(session_id)
    if session_id == auth.get("id"):
        return _clear_session_cookie(JSONResponse({"revoked": True, "current": True}))
    return JSONResponse({"revoked": True, "current": False})


@app.post("/api/auth/sessions/revoke-others")
async def revoke_other_sessions(request: Request) -> dict:
    auth = getattr(request.state, "auth", {})
    if auth.get("kind") != "session":
        raise HTTPException(status_code=400, detail="令牌鉴权下不能管理会话")
    count = store.delete_other_sessions(str(auth.get("id") or ""))
    return {"revoked": count}


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


def _norm_path(path: str) -> str:
    """Lexically normalize a path for cross-OS containment checks.

    Server and worker may run different operating systems (cloud Linux + Windows
    PC), so we cannot use pathlib here. The worker still enforces the real path.
    """
    raw = str(path or "").strip().replace("\\", "/")
    drive = ""
    if re.match(r"^[a-zA-Z]:", raw):
        drive = raw[:2].lower()
        raw = raw[2:]
    absolute = raw.startswith("/")
    parts: list[str] = []
    for segment in raw.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if parts:
                parts.pop()
            continue
        parts.append(segment)
    joined = "/".join(parts)
    if drive:
        value = f"{drive}/{joined}" if joined else f"{drive}/"
    elif absolute:
        value = "/" + joined
    else:
        value = joined
    return value.lower()


def _path_within(path: str, root: str) -> bool:
    if not str(root or "").strip():
        return False
    child = _norm_path(path)
    base = _norm_path(root).rstrip("/")
    return bool(child) and (child == base or child.startswith(base + "/"))


def _join_relative(base: str, relative: str) -> str:
    """Join a device-style root with a posix-style relative path."""
    rel = str(relative or ".").replace("\\", "/").strip("/")
    if rel in ("", "."):
        return str(base)
    parts = []
    for segment in rel.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if parts:
                parts.pop()
            continue
        parts.append(segment)
    if not parts:
        return str(base)
    separator = "\\" if ("\\" in str(base) or re.match(r"^[a-zA-Z]:", str(base))) else "/"
    return str(base).rstrip("\\/") + separator + separator.join(parts)


@app.get("/api/devices/{device_id}/browse")
async def browse_device(
    device_id: str,
    workspace: str = Query(min_length=1, max_length=1000),
    path: str = Query(default=".", max_length=1000),
) -> dict:
    """List subdirectories of a device workspace without leaving the sandbox."""
    record = store.get_device(device_id)
    if record is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    roots = [str(item) for item in record.get("workspaces") or []]
    base = str(workspace).strip()
    if roots and not any(_path_within(base, root) for root in roots):
        raise HTTPException(status_code=400, detail="工作区不在该设备的授权范围")
    target = _join_relative(base, path)
    if not _path_within(target, base):
        raise HTTPException(status_code=400, detail="路径超出工作区")
    if not device_hub.online(device_id):
        raise HTTPException(status_code=409, detail="设备当前离线，无法浏览目录")
    try:
        result = await device_hub.call(
            device_id,
            "fs.list",
            {"workspace": base, "path": path, "depth": 1, "dirs_only": True},
            timeout=30.0,
            wait=0,
        )
    except RuntimeError as exc:
        message = str(exc)
        status = 400 if "WorkspaceError" in message else 409
        raise HTTPException(
            status_code=status, detail=message.replace("WorkspaceError: ", "")
        ) from exc
    entries = []
    for item in result.get("entries") or []:
        if item.get("type") != "dir":
            continue
        rel = str(item.get("path") or "").rstrip("/")
        if not rel:
            continue
        entries.append({"name": rel.rsplit("/", 1)[-1], "path": rel})
    entries.sort(key=lambda item: item["name"].lower())
    current = str(result.get("path") or ".")
    parent = ""
    if current not in ("", "."):
        parts = current.split("/")
        parent = "/".join(parts[:-1])
    return {
        "workspace": base,
        "path": current,
        "parent": parent,
        "absolute": target,
        "entries": entries,
        "truncated": bool(result.get("truncated")),
    }


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


def _request_origin(request: Request) -> str:
    """Best-effort public origin, including reverse-proxy forwarded headers."""
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    host = host or request.headers.get("host", "").strip()
    if host:
        return f"{proto or request.url.scheme}://{host}"
    return str(request.base_url).rstrip("/")


@app.get("/api/network")
async def get_network(request: Request) -> dict:
    return network_info(settings.port, _request_origin(request))


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
        roots = record.get("workspaces") or []
        if not workspace:
            if not roots:
                raise HTTPException(
                    status_code=400, detail="设备尚未上报可用工作区"
                )
            workspace = roots[0]
        if not any(_path_within(workspace, root) for root in roots):
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
        if not any(
            _path_within(device.get("workspace") or "", root)
            for root in (record.get("workspaces") or [])
        ):
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
