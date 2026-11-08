from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from .config import Settings
from .models import new_id
from .routing import parse_scores
from .store import Store


PRESETS: list[dict[str, str]] = [
    {
        "id": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "api_style": "openai_chat",
        "auth_style": "bearer",
        "note": "官方 Chat Completions 接口，需要海外网络环境",
    },
    {
        "id": "openai_responses",
        "name": "OpenAI Responses",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4.1-mini",
        "api_style": "openai_responses",
        "auth_style": "bearer",
        "note": "新版 Responses API（/responses），适配只支持该接口的模型",
    },
    {
        "id": "opencode_go",
        "name": "OpenCode Go",
        "base_url": "https://opencode.ai/inference/go/openai/v1",
        "model": "deepseek-v4.1-flash",
        "api_style": "openai_chat",
        "auth_style": "bearer",
        "note": "OpenCode Go 订阅；Chat / Responses / Anthropic 三种接口均可用，会自动补 x-opencode-session 与对应路径。模型 ID 如 deepseek-v4.1-flash、gpt-6-luna、claude-haiku-5-5",
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_style": "openai_chat",
        "auth_style": "bearer",
        "note": "OpenAI 兼容，性价比高",
    },
    {
        "id": "moonshot",
        "name": "Moonshot Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "api_style": "openai_chat",
        "auth_style": "bearer",
        "note": "月之暗面，OpenAI 兼容",
    },
    {
        "id": "zhipu",
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "api_style": "openai_chat",
        "auth_style": "bearer",
        "note": "智谱开放平台",
    },
    {
        "id": "qwen",
        "name": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "api_style": "openai_chat",
        "auth_style": "bearer",
        "note": "阿里云 DashScope 兼容模式",
    },
    {
        "id": "siliconflow",
        "name": "SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "api_style": "openai_chat",
        "auth_style": "bearer",
        "note": "聚合多种开源模型",
    },
    {
        "id": "groq",
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "api_style": "openai_chat",
        "auth_style": "bearer",
        "note": "低延迟推理",
    },
    {
        "id": "gemini",
        "name": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.0-flash",
        "api_style": "openai_chat",
        "auth_style": "bearer",
        "note": "官方 OpenAI 兼容端点",
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-4o-mini",
        "api_style": "openai_chat",
        "auth_style": "bearer",
        "note": "一个 Key 访问多家模型",
    },
    {
        "id": "anthropic",
        "name": "Anthropic Claude",
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-4-20250514",
        "api_style": "anthropic",
        "auth_style": "x-api-key",
        "note": "原生 Messages API，使用 x-api-key",
    },
    {
        "id": "ollama",
        "name": "Ollama 本地",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
        "api_style": "openai_chat",
        "auth_style": "none",
        "note": "本机模型，无需 API Key",
    },
    {
        "id": "custom",
        "name": "自定义供应商",
        "base_url": "",
        "model": "",
        "api_style": "openai_chat",
        "auth_style": "bearer",
        "note": "任何 OpenAI / Anthropic 兼容接口",
    },
]


def normalize_base_url(value: str, api_style: str = "openai_chat") -> str:
    """Normalize a base URL and strip common endpoint suffixes.

    Users often paste `https://host/v1/chat/completions`; accepting that and
    turning it back into a base URL removes a whole class of 404s.
    """
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Base URL 必须是 http:// 或 https:// 地址")

    path = parsed.path.rstrip("/")
    lower = path.lower()
    for suffix in ("/chat/completions", "/responses", "/messages", "/models"):
        if lower.endswith(suffix):
            path = path[: -len(suffix)]
            break
    path = path.rstrip("/")

    host = (parsed.hostname or "").lower()
    if path in ("", "/"):
        if api_style == "anthropic":
            path = "/v1"
        elif host in ("api.openai.com",):
            path = "/v1"

    # OpenCode Go exposes separate OpenAI and Anthropic base paths.
    if host == "opencode.ai" and "/inference/go/" in path:
        path = (
            "/inference/go/anthropic/v1"
            if api_style == "anthropic"
            else "/inference/go/openai/v1"
        )

    return urlunparse(parsed._replace(path=path, fragment=""))


def join_endpoint(base_url: str, suffix: str) -> str:
    """Join a path while preserving query strings (needed by Azure OpenAI)."""
    parsed = urlparse(base_url)
    path = parsed.path.rstrip("/")
    if suffix:
        path = f"{path}/{suffix.lstrip('/')}"
    return urlunparse(parsed._replace(path=path))


def parse_extra_headers(raw: str) -> dict[str, str]:
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("附加请求头必须是合法的 JSON 对象") from exc
    if not isinstance(data, dict):
        raise ValueError("附加请求头必须是 JSON 对象，例如 {\"X-Title\": \"ai-cluster\"}")
    return {str(key): str(value) for key, value in data.items()}


def build_headers(
    api_key: str,
    auth_style: str,
    extra_headers: str,
    api_style: str,
    base_url: str = "",
    session_id: str = "",
) -> dict[str, str]:
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "User-Agent": "ai-cluster/0.1",
    }
    if api_key:
        if auth_style == "x-api-key":
            headers["x-api-key"] = api_key
        elif auth_style == "api-key":
            headers["api-key"] = api_key
        elif auth_style == "bearer":
            headers["Authorization"] = f"Bearer {api_key}"
    extra = parse_extra_headers(extra_headers)
    headers.update(extra)
    if api_style == "anthropic" and not any(k.lower() == "anthropic-version" for k in headers):
        headers["anthropic-version"] = "2023-06-01"
    # OpenCode Go rejects requests without a session header.
    host = (urlparse(base_url).hostname or "").lower() if base_url else ""
    if host == "opencode.ai" and api_style == "anthropic" and api_key:
        if not any(k.lower() == "x-api-key" for k in headers):
            headers["x-api-key"] = api_key
    if host == "opencode.ai" and not any(k.lower() == "x-opencode-session" for k in headers):
        headers["x-opencode-session"] = session_id or new_id("sess")
    return headers


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return "****" + key[-4:]


def _extra_header_keys(raw: str) -> list[str]:
    try:
        data = json.loads(raw or "{}")
        return sorted(data.keys()) if isinstance(data, dict) else []
    except json.JSONDecodeError:
        return []


def public_provider(row: dict[str, Any]) -> dict[str, Any]:
    keys = _extra_header_keys(row.get("extra_headers", "{}"))
    proxy = row.get("proxy") or ""
    return {
        "id": row["id"],
        "name": row["name"],
        "base_url": row["base_url"],
        "model": row["model"],
        "api_style": row.get("api_style", "openai_chat"),
        "auth_style": row.get("auth_style", "bearer"),
        "api_key_set": bool(row["api_key"]),
        "api_key_hint": mask_key(row["api_key"]),
        "extra_headers_set": bool(keys),
        "extra_headers_keys": keys,
        "proxy_set": bool(proxy),
        "proxy_hint": mask_url(proxy) if proxy else "",
        "scores": parse_scores(row.get("scores")),
        "group_id": row.get("group_id") or row["id"],
        "reasoning_effort": row.get("reasoning_effort", ""),
        "timeout": row.get("timeout", 0),
        "stream": bool(row.get("stream", 1)),
        "enabled": bool(row["is_active"]),
        "is_default": bool(row.get("is_default", 0)),
        "is_active": bool(row["is_active"]),
        "updated_at": row["updated_at"],
    }


def seed_from_env(store: Store, settings: Settings) -> None:
    """Import the legacy .env configuration as a provider on first run."""
    if store.count_providers() > 0:
        return
    if settings.llm_base_url and settings.llm_api_key:
        store.upsert_provider(
            None,
            "环境变量配置",
            normalize_base_url(settings.llm_base_url),
            settings.llm_api_key,
            settings.llm_model,
            api_style="openai_chat",
            auth_style="bearer",
            enabled=True,
            is_default=True,
        )


def _hint_for(status: int) -> str:
    if status in (401, 403):
        return "检查 API Key，以及认证方式是否选对（Bearer / x-api-key / api-key）"
    if status == 404:
        return (
            "Base URL 可能多了或少了路径：Chat 接口通常以 /v1 结尾，"
            "不要填写 /chat/completions、/responses 或 /models"
        )
    if status == 400:
        return "检查模型名是否存在，以及该模型是否支持所选的 API 类型"
    if status == 422:
        return "请求参数不被该供应商接受，尝试切换 API 类型或模型"
    if status == 429:
        return "触发限流或额度不足，稍后重试或检查账户余额"
    if status == 524:
        return (
            "上游/Cloudflare 网关超时：开启流式输出、降低思考强度、"
            "换更快的模型，或让供应商提高超时"
        )
    if status >= 500:
        return "供应商服务端异常，稍后重试"
    return "检查 Base URL、模型名与认证方式"


def mask_url(url: str) -> str:
    """Keep host/path, hide credentials and query values in logs and errors."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    netloc = parsed.netloc
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        netloc = f"***@{host}"
    query = parsed.query
    if query:
        parts = []
        for part in query.split("&"):
            key = part.split("=", 1)[0]
            parts.append(f"{key}=***")
        query = "&".join(parts)
    return urlunparse(parsed._replace(netloc=netloc, query=query))


def network_hint(exc: BaseException) -> str:
    text = f"{type(exc).__name__} {exc}".lower()
    if any(key in text for key in ("certificate", "ssl", "tls", "cert verify")):
        return "TLS/证书校验失败：检查代理、企业证书，或设置 SSL_CERT_FILE"
    if any(
        key in text
        for key in ("name or service", "getaddrinfo", "nodename", "name resolution", "dns")
    ):
        return "域名解析失败：检查 Base URL 域名、DNS 或代理"
    if "refused" in text:
        return "连接被拒绝：端口未监听或被防火墙拦截"
    if any(key in text for key in ("timed out", "timeout")):
        return "连接超时：检查网络、代理、防火墙，或供应商是否限制该服务器 IP"
    if "proxy" in text:
        return "代理配置异常：检查 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY"
    if "all connection attempts failed" in text:
        return (
            "无法建立连接：检查域名解析、网络、防火墙、代理，"
            "以及供应商是否允许服务器访问"
        )
    return "检查网络、代理、TLS 证书、Base URL 是否可从服务器访问"


def _error_detail(resp: httpx.Response) -> str:
    detail = resp.text[:500]
    try:
        data = resp.json()
        message = (data.get("error") or {}).get("message") or data.get("message")
        if message:
            detail = str(message)
    except Exception:  # noqa: BLE001
        pass
    return detail


async def test_connection(
    base_url: str,
    api_key: str,
    model: str,
    api_style: str = "openai_chat",
    auth_style: str = "bearer",
    extra_headers: str = "",
    proxy: str = "",
) -> dict[str, Any]:
    base_url = normalize_base_url(base_url, api_style)
    started = time.perf_counter()
    try:
        headers = build_headers(
            api_key,
            auth_style,
            extra_headers,
            api_style,
            base_url=base_url,
            session_id=new_id("sess"),
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "hint": "修正附加请求头 JSON"}

    if api_style == "anthropic":
        endpoint = join_endpoint(base_url, "messages")
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "Reply with OK."}],
        }
    elif api_style == "openai_responses":
        endpoint = join_endpoint(base_url, "responses")
        payload = {"model": model, "input": "Reply with OK."}
    else:
        endpoint = join_endpoint(base_url, "chat/completions")
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
        }

    try:
        async with httpx.AsyncClient(timeout=30, proxy=proxy or None) as client:
            resp = await client.post(endpoint, headers=headers, json=payload)
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "ok": False,
            "error": f"连接失败: {type(exc).__name__}: {exc}",
            "hint": network_hint(exc),
            "endpoint": mask_url(endpoint),
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }

    latency = int((time.perf_counter() - started) * 1000)
    if resp.status_code >= 400:
        return {
            "ok": False,
            "error": f"HTTP {resp.status_code}: {_error_detail(resp)}",
            "hint": _hint_for(resp.status_code),
            "endpoint": mask_url(endpoint),
            "latency_ms": latency,
        }
    return {
        "ok": True,
        "latency_ms": latency,
        "model": model,
        "status": resp.status_code,
        "endpoint": mask_url(endpoint),
        "api_style": api_style,
    }


def _extract_models(data: Any) -> list[str]:
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        raw = data.get("data") or data.get("models") or []
    else:
        raw = []
    models: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            model = item
        elif isinstance(item, dict):
            model = item.get("id") or item.get("name") or item.get("model")
        else:
            continue
        if not model:
            continue
        model = str(model)
        if model.startswith("models/"):
            model = model[len("models/"):]
        models.add(model)
    return sorted(models)


async def list_models(
    base_url: str,
    api_key: str,
    api_style: str = "openai_chat",
    auth_style: str = "bearer",
    extra_headers: str = "",
    proxy: str = "",
) -> dict[str, Any]:
    base_url = normalize_base_url(base_url, api_style)
    try:
        headers = build_headers(
            api_key,
            auth_style,
            extra_headers,
            api_style,
            base_url=base_url,
            session_id=new_id("sess"),
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "hint": "修正附加请求头 JSON"}

    candidates: list[str] = []
    parsed = urlparse(base_url)
    if (parsed.hostname or "").lower() == "opencode.ai" and "/inference/go/" in parsed.path:
        candidates.append("https://opencode.ai/inference/go/v1/models")
    candidates.append(join_endpoint(base_url, "models"))
    if parsed.path in ("", "/"):
        candidates.append(join_endpoint(base_url, "v1/models"))

    last_error = ""
    for endpoint in candidates:
        safe_endpoint = mask_url(endpoint)
        try:
            async with httpx.AsyncClient(timeout=20, proxy=proxy or None) as client:
                resp = await client.get(endpoint, headers=headers)
        except (httpx.HTTPError, ValueError) as exc:
            return {
                "ok": False,
                "error": f"连接失败: {type(exc).__name__}: {exc}",
                "hint": network_hint(exc),
                "endpoint": safe_endpoint,
            }
        if resp.status_code == 404:
            last_error = f"HTTP 404: {safe_endpoint}"
            continue
        if resp.status_code >= 400:
            return {
                "ok": False,
                "error": f"HTTP {resp.status_code}: {_error_detail(resp)}",
                "hint": _hint_for(resp.status_code),
                "endpoint": safe_endpoint,
            }
        try:
            models = _extract_models(resp.json())
        except Exception:  # noqa: BLE001
            return {
                "ok": False,
                "error": "模型列表不是合法 JSON",
                "hint": "该供应商可能不支持 /models 接口，请手动填写模型名",
                "endpoint": safe_endpoint,
            }
        if models:
            return {"ok": True, "models": models, "endpoint": safe_endpoint, "count": len(models)}
        return {
            "ok": False,
            "error": "接口返回成功，但没有解析到模型",
            "hint": "请手动填写模型名，或检查该供应商的 /models 响应格式",
            "endpoint": safe_endpoint,
        }

    return {
        "ok": False,
        "error": last_error or "未找到 /models 接口",
        "hint": "该供应商可能不提供模型列表，请手动填写模型名",
    }


async def diagnose(
    base_url: str,
    api_key: str,
    model: str,
    api_style: str = "openai_chat",
    auth_style: str = "bearer",
    extra_headers: str = "",
    proxy: str = "",
) -> dict[str, Any]:
    base_url = normalize_base_url(base_url, api_style)
    parsed = urlparse(base_url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = {
        "openai_chat": "chat/completions",
        "openai_responses": "responses",
        "anthropic": "messages",
    }.get(api_style, "chat/completions")
    endpoint = join_endpoint(base_url, path)

    report: dict[str, Any] = {
        "ok": False,
        "config": {
            "base_url": mask_url(base_url),
            "endpoint": mask_url(endpoint),
            "api_style": api_style,
            "auth_style": auth_style,
            "model": model,
            "api_key_set": bool(api_key),
            "api_key_hint": mask_key(api_key),
            "extra_header_keys": _extra_header_keys(extra_headers),
            "proxy": mask_url(proxy) if proxy else "",
        },
        "environment": {
            "python": sys.version.split()[0],
            "httpx": getattr(httpx, "__version__", "unknown"),
            "proxy": {},
        },
        "dns": {},
        "tcp": {},
        "test": {},
        "models": {},
    }

    proxies = {}
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        value = os.getenv(name)
        if value:
            proxies[name] = mask_url(value)
    report["environment"]["proxy"] = proxies
    if any(
        name in proxies
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        )
    ):
        report["environment"]["proxy_note"] = (
            "检测到代理环境变量：HTTP 请求会走代理，TCP 直连失败不一定代表服务不可用"
        )
    if proxy:
        report["environment"]["provider_proxy"] = mask_url(proxy)
        report["environment"]["proxy_note"] = (
            "已为该供应商配置代理：HTTP 请求会走代理，TCP 直连目标地址失败不代表服务不可用"
        )

    if not host:
        report["dns"] = {"ok": False, "error": "Base URL 中没有主机名"}
        return report

    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), timeout=5
        )
        report["dns"] = {
            "ok": True,
            "addresses": sorted({info[4][0] for info in infos}),
        }
    except Exception as exc:  # noqa: BLE001
        report["dns"] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "hint": network_hint(exc),
        }

    try:
        writer = None
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=8
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        report["tcp"] = {"ok": True, "target": f"{host}:{port}"}
    except Exception as exc:  # noqa: BLE001
        report["tcp"] = {
            "ok": False,
            "target": f"{host}:{port}",
            "error": f"{type(exc).__name__}: {exc}",
            "hint": network_hint(exc),
        }

    if model:
        report["test"] = await test_connection(
            base_url, api_key, model, api_style, auth_style, extra_headers, proxy
        )
    else:
        report["test"] = {"ok": False, "skipped": "未填写模型，跳过对话测试"}

    report["models"] = await list_models(
        base_url, api_key, api_style, auth_style, extra_headers, proxy
    )

    test_ok = bool(report["test"].get("ok"))
    models_ok = bool(report["models"].get("ok"))
    report["ok"] = test_ok or models_ok
    if test_ok:
        report["summary"] = "对话接口测试成功，供应商配置可用"
    elif models_ok:
        report["summary"] = (
            "模型列表可访问，但对话接口失败：重点检查 API 类型、模型名和认证方式"
        )
    else:
        report["summary"] = "HTTP 请求失败：重点检查网络、Base URL、认证和模型名"
    return report
