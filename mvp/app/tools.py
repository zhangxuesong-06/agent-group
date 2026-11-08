from __future__ import annotations

import ast
import asyncio
import html as html_lib
import ipaddress
import json
import locale
import operator
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlparse

import httpx

from .config import Settings
from .devices import REMOTE_TOOL_OPS
from .store import Store


KNOWLEDGE = [
    {
        "title": "多 Agent 编排的基本结构",
        "text": "控制面负责鉴权与 Run 管理，编排器负责规划 DAG 与调度，Worker 执行 Agent Loop。"
        "三者通过事件总线解耦，状态变更必须事件化，才能回放和审计。",
    },
    {
        "title": "上下文传递策略",
        "text": "上游任务只向 downstream 传递摘要与产物引用，不要拼接完整对话。"
        "长上下文应放进检索库，由下游按需查询，否则 token 成本会指数增长。",
    },
    {
        "title": "成本与预算",
        "text": "Run 级设置 max_steps、max_tokens、max_usd 与 deadline，"
        "每次模型响应后累加，超限立即熔断并取消剩余任务。",
    },
    {
        "title": "可靠性",
        "text": "消息队列采用至少一次投递，消费端用 task_id + attempt 做幂等键。"
        "重试需要指数退避，并区分可重试与不可重试错误。",
    },
    {
        "title": "插件与工具协议",
        "text": "工具通过 MCP 协议接入，平台作为 MCP Client 并统一走工具网关。"
        "网关负责 JSON Schema 校验、超时、权限白名单、沙箱与审计。",
    },
    {
        "title": "AI 集群产品路线",
        "text": "先做单机 MVP 验证场景，再做可运营版本（Postgres + Redis + 多租户），"
        "最后平台化（K8s、弹性伸缩、可视化编排、评测体系）。",
    },
]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any], dict[str, Any]], Awaitable[str]]


_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _safe_eval(node: ast.AST) -> float | int:
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        left = _safe_eval(node.left)
        right = _safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("指数过大")
        return _ALLOWED_BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
        return _ALLOWED_UNARY[type(node.op)](_safe_eval(node.operand))
    raise ValueError("只支持数字与 + - * / // % ** 运算")


async def _calc(args: dict[str, Any], ctx: dict[str, Any]) -> str:
    expr = str(args.get("expression", "")).strip()
    if not expr or len(expr) > 200:
        raise ValueError("expression 不能为空且不超过 200 字符")
    tree = ast.parse(expr, mode="eval")
    value = _safe_eval(tree)
    return json.dumps({"expression": expr, "value": value}, ensure_ascii=False)


async def _now(args: dict[str, Any], ctx: dict[str, Any]) -> str:
    return json.dumps(
        {
            "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "local": datetime.now().isoformat(timespec="seconds"),
        },
        ensure_ascii=False,
    )


async def _search_knowledge(args: dict[str, Any], ctx: dict[str, Any]) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        raise ValueError("query 不能为空")
    terms = [t for t in re.split(r"[\s,，。;；:：]+", query.lower()) if len(t) > 1]
    hits: list[tuple[int, str, str]] = []
    for entry in KNOWLEDGE:
        haystack = (entry["title"] + entry["text"]).lower()
        score = sum(haystack.count(t) for t in terms)
        if score:
            hits.append((score, entry["title"], entry["text"]))
    # Also surface results already produced by upstream tasks in this run.
    run = ctx["store"].get_run(ctx["run_id"])
    if run:
        for task in run["tasks"]:
            if task["status"] == "done" and task["result"]:
                haystack = task["result"].lower()
                score = sum(haystack.count(t) for t in terms)
                if score:
                    hits.append((score, f"上游结果：{task['title']}", task["result"][:600]))
    hits.sort(key=lambda x: -x[0])
    if not hits:
        return "未找到直接资料。请基于任务上下文做保守推断，并标注不确定性。"
    return "\n\n".join(f"[{title}]\n{text}" for _, title, text in hits[:4])


async def _search_tavily(query: str, max_results: int, settings: Settings) -> list[dict[str, str]]:
    async with httpx.AsyncClient(timeout=25) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": settings.tavily_api_key,
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
                "include_answer": False,
            },
        )
    if resp.status_code >= 400:
        raise ValueError(f"Tavily 返回 {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return [
        {
            "title": str(item.get("title", "")),
            "url": str(item.get("url", "")),
            "snippet": str(item.get("content", "")),
        }
        for item in data.get("results", [])[:max_results]
    ]


async def _search_brave(query: str, max_results: int, settings: Settings) -> list[dict[str, str]]:
    async with httpx.AsyncClient(timeout=25) as client:
        resp = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={
                "X-Subscription-Token": settings.brave_api_key,
                "Accept": "application/json",
            },
            params={"q": query, "count": max_results},
        )
    if resp.status_code >= 400:
        raise ValueError(f"Brave 返回 {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return [
        {
            "title": str(item.get("title", "")),
            "url": str(item.get("url", "")),
            "snippet": str(item.get("description", "")),
        }
        for item in (data.get("web") or {}).get("results", [])[:max_results]
    ]


async def _search_searxng(query: str, max_results: int, settings: Settings) -> list[dict[str, str]]:
    async with httpx.AsyncClient(timeout=25) as client:
        resp = await client.get(
            f"{settings.searxng_url}/search",
            params={"q": query, "format": "json", "language": "zh-CN"},
        )
    if resp.status_code >= 400:
        raise ValueError(f"SearXNG 返回 {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return [
        {
            "title": str(item.get("title", "")),
            "url": str(item.get("url", "")),
            "snippet": str(item.get("content", "")),
        }
        for item in data.get("results", [])[:max_results]
    ]


async def _search_duckduckgo(query: str, max_results: int) -> list[dict[str, str]]:
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        resp = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (compatible; ai-cluster/0.1)"},
        )
    if resp.status_code >= 400:
        raise ValueError(f"DuckDuckGo 返回 {resp.status_code}")
    text = resp.text
    links = re.findall(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        text,
        flags=re.S | re.I,
    )
    snippets = re.findall(
        r'class="result__snippet"[^>]*>(.*?)</(?:a|div)>', text, flags=re.S | re.I
    )
    results = []
    for index, (url, title) in enumerate(links[:max_results]):
        parsed = urlparse(url)
        if "duckduckgo.com" in (parsed.hostname or "") and parsed.path.startswith("/l/"):
            url = parse_qs(parsed.query).get("uddg", [url])[0]
        clean_title = html_lib.unescape(re.sub(r"<[^>]+>", "", title)).strip()
        snippet = ""
        if index < len(snippets):
            snippet = html_lib.unescape(re.sub(r"<[^>]+>", "", snippets[index])).strip()
        results.append({"title": clean_title, "url": url, "snippet": snippet})
    return results


async def _search_bing(query: str, max_results: int) -> list[dict[str, str]]:
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        resp = await client.get(
            "https://www.bing.com/search",
            params={"q": query, "count": max_results},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
    if resp.status_code >= 400:
        raise ValueError(f"Bing 返回 {resp.status_code}")
    text = resp.text
    blocks = re.findall(
        r'<li[^>]+class="b_algo"[^>]*>(.*?)</li>', text, flags=re.S | re.I
    )
    if not blocks:
        blocks = re.findall(
            r'<li class="b_algo">(.*?)</li>', text, flags=re.S | re.I
        )
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for block in blocks:
        match = re.search(
            r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            block,
            flags=re.S | re.I,
        )
        if not match:
            continue
        url = html_lib.unescape(match.group(1)).strip()
        title = html_lib.unescape(re.sub(r"<[^>]+>", "", match.group(2))).strip()
        snippet_match = re.search(r"<p[^>]*>(.*?)</p>", block, flags=re.S | re.I)
        snippet = (
            html_lib.unescape(re.sub(r"<[^>]+>", "", snippet_match.group(1))).strip()
            if snippet_match
            else ""
        )
        if not url or url in seen:
            continue
        seen.add(url)
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results


async def _web_search(args: dict[str, Any], ctx: dict[str, Any]) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        raise ValueError("query 不能为空")
    try:
        max_results = int(args.get("max_results", 5))
    except (TypeError, ValueError):
        max_results = 5
    max_results = max(1, min(10, max_results))
    settings: Settings = ctx["settings"]

    providers: list[tuple[str, Any]] = []
    if settings.tavily_api_key:
        providers.append(
            ("tavily", lambda q, n: _search_tavily(q, n, settings))
        )
    if settings.brave_api_key:
        providers.append(
            ("brave", lambda q, n: _search_brave(q, n, settings))
        )
    if settings.searxng_url:
        providers.append(
            ("searxng", lambda q, n: _search_searxng(q, n, settings))
        )
    # Bing is usually reachable without an API key and works as a fallback
    # in networks where DuckDuckGo / public SearXNG instances are blocked.
    providers.append(("bing", _search_bing))
    providers.append(("duckduckgo", _search_duckduckgo))

    errors: list[str] = []
    for name, search in providers:
        try:
            results = await search(query, max_results)
        except Exception as exc:  # noqa: BLE001 - try the next provider
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        if not results:
            errors.append(f"{name}: 无结果")
            continue
        return json.dumps(
            {"provider": name, "query": query, "results": results[:max_results]},
            ensure_ascii=False,
        )

    raise ValueError(
        "公网检索不可用（已尝试 "
        + "、".join(name for name, _ in providers)
        + "）："
        + "；".join(errors)[:500]
    )


async def _fetch_url(args: dict[str, Any], ctx: dict[str, Any]) -> str:
    url = str(args.get("url", "")).strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("只允许 http/https URL")
    host = parsed.hostname
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("禁止访问内网地址")
    except ValueError as exc:
        if "禁止" in str(exc):
            raise
    if host.lower() in ("localhost", "metadata.google.internal"):
        raise ValueError("禁止访问内网地址")
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        resp = await client.get(url, headers={"User-Agent": "ai-cluster-mvp/0.1"})
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", resp.text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return f"HTTP {resp.status_code}\n{text[:2000]}"


async def _save_artifact(args: dict[str, Any], ctx: dict[str, Any]) -> str:
    name = re.sub(r"[^a-zA-Z0-9._\u4e00-\u9fa5-]", "_", str(args.get("name", "artifact.md")))[:80]
    content = str(args.get("content", ""))
    if not content:
        raise ValueError("content 不能为空")
    settings: Settings = ctx["settings"]
    target = Path(settings.artifact_dir) / ctx["run_id"]
    target.mkdir(parents=True, exist_ok=True)
    path = target / (name or "artifact.md")
    path.write_text(content, encoding="utf-8")
    return json.dumps(
        {"saved": str(path.relative_to(settings.data_dir)), "bytes": len(content.encode("utf-8"))},
        ensure_ascii=False,
    )


# ----------------------------------------------------------------------
# Local workspace tools
# ----------------------------------------------------------------------

MAX_FILE_BYTES = 200_000
MAX_WRITE_BYTES = 1_000_000

_DENY_COMMANDS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+(-[a-z]+\s+)*-[a-z]*r[a-z]*f[a-z]*\s+/(?:\s|$)", re.I), "递归删除根目录"),
    (re.compile(r"\brm\s+(-[a-z]+\s+)*-[a-z]*f[a-z]*r[a-z]*\s+/(?:\s|$)", re.I), "递归删除根目录"),
    (re.compile(r"\bmkfs(?:\.\w+)?\b", re.I), "格式化磁盘"),
    (re.compile(r"\bformat\s+[a-z]:", re.I), "格式化磁盘"),
    (re.compile(r"\bshutdown\b", re.I), "关机/重启"),
    (re.compile(r"\breboot\b", re.I), "关机/重启"),
    (re.compile(r"\bpoweroff\b", re.I), "关机/重启"),
    (re.compile(r"\bdiskpart\b", re.I), "磁盘分区"),
    (re.compile(r"\breg\s+delete\b", re.I), "删除注册表"),
    (re.compile(r"remove-item\s+.*-recurse\s+.*[a-z]:\\(?:\s|$)", re.I), "递归删除盘符根目录"),
    (re.compile(r"\bdel\s+/[sq]\s+[a-z]:\\", re.I), "递归删除盘符根目录"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:", re.I), "fork bomb"),
]


def _workspace_path(settings: Settings, raw: Any, *, must_exist: bool = False) -> Path:
    base = settings.workspace_dir
    text = str(raw or ".").strip() or "."
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    resolved = candidate.resolve()
    try:
        common = os.path.commonpath([str(base), str(resolved)])
    except ValueError:
        common = ""
    if os.path.normcase(common) != os.path.normcase(str(base)):
        raise ValueError("路径超出工作区")
    if must_exist and not resolved.exists():
        raise ValueError(f"路径不存在: {text}")
    return resolved


def _relative_to_workspace(settings: Settings, path: Path) -> str:
    try:
        return path.relative_to(settings.workspace_dir).as_posix() or "."
    except ValueError:
        return str(path)


def _check_command(command: str) -> None:
    if len(command) > 4000:
        raise ValueError("命令过长（上限 4000 字符）")
    for pattern, reason in _DENY_COMMANDS:
        if pattern.search(command):
            raise ValueError(f"命令被安全策略拒绝：{reason}")


async def _list_files(args: dict[str, Any], ctx: dict[str, Any]) -> str:
    settings: Settings = ctx["settings"]
    root = _workspace_path(settings, args.get("path", "."), must_exist=True)
    if not root.is_dir():
        raise ValueError("不是目录")
    try:
        depth = int(args.get("depth", 2))
    except (TypeError, ValueError):
        depth = 2
    depth = max(1, min(depth, 4))
    entries: list[dict[str, Any]] = []
    truncated = False
    for path in sorted(root.rglob("*")):
        try:
            rel_root = path.relative_to(root)
        except ValueError:
            continue
        if len(rel_root.parts) > depth:
            continue
        if path.is_dir():
            entries.append({"path": _relative_to_workspace(settings, path) + "/", "type": "dir"})
        elif path.is_file():
            entries.append(
                {
                    "path": _relative_to_workspace(settings, path),
                    "type": "file",
                    "size": path.stat().st_size,
                }
            )
        else:
            continue
        if len(entries) >= 400:
            truncated = True
            break
    return json.dumps(
        {
            "path": _relative_to_workspace(settings, root),
            "workspace": str(settings.workspace_dir),
            "entries": entries,
            "truncated": truncated,
        },
        ensure_ascii=False,
    )


async def _read_file(args: dict[str, Any], ctx: dict[str, Any]) -> str:
    settings: Settings = ctx["settings"]
    path = _workspace_path(settings, args.get("path", ""), must_exist=True)
    if not path.is_file():
        raise ValueError("不是文件")
    try:
        max_bytes = int(args.get("max_bytes", 20_000))
    except (TypeError, ValueError):
        max_bytes = 20_000
    max_bytes = max(1000, min(max_bytes, MAX_FILE_BYTES))
    data = path.read_bytes()
    truncated = len(data) > max_bytes
    text = data[:max_bytes].decode("utf-8", errors="replace")
    return json.dumps(
        {
            "path": _relative_to_workspace(settings, path),
            "bytes": len(data),
            "truncated": truncated,
            "content": text,
        },
        ensure_ascii=False,
    )


async def _write_file(args: dict[str, Any], ctx: dict[str, Any]) -> str:
    settings: Settings = ctx["settings"]
    content = args.get("content")
    if content is None:
        raise ValueError("content 不能为空")
    content = str(content)
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_WRITE_BYTES:
        raise ValueError(f"内容过大（上限 {MAX_WRITE_BYTES} 字节）")
    path = _workspace_path(settings, args.get("path", ""))
    if path == settings.workspace_dir:
        raise ValueError("path 必须是文件")
    path.parent.mkdir(parents=True, exist_ok=True)
    append = bool(args.get("append", False))
    with path.open("a" if append else "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    return json.dumps(
        {
            "path": _relative_to_workspace(settings, path),
            "bytes": len(encoded),
            "mode": "append" if append else "write",
        },
        ensure_ascii=False,
    )


async def _edit_file(args: dict[str, Any], ctx: dict[str, Any]) -> str:
    settings: Settings = ctx["settings"]
    old = str(args.get("old_string", ""))
    new = str(args.get("new_string", ""))
    if not old:
        raise ValueError("old_string 不能为空")
    if len(new.encode("utf-8")) > MAX_WRITE_BYTES:
        raise ValueError("new_string 过大")
    path = _workspace_path(settings, args.get("path", ""), must_exist=True)
    if not path.is_file():
        raise ValueError("不是文件")
    raw = path.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in raw else "\n"
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    old = old.replace("\r\n", "\n")
    new = new.replace("\r\n", "\n")
    count = text.count(old)
    if count == 0:
        raise ValueError("在文件中找不到 old_string")
    replace_all = bool(args.get("replace_all", False))
    if count > 1 and not replace_all:
        raise ValueError(
            f"old_string 在文件中出现 {count} 次；请提供更多上下文，或设置 replace_all=true"
        )
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    if newline == "\r\n":
        updated = updated.replace("\n", "\r\n")
    path.write_bytes(updated.encode("utf-8"))
    return json.dumps(
        {
            "path": _relative_to_workspace(settings, path),
            "replacements": count if replace_all else 1,
            "bytes": len(updated.encode("utf-8")),
        },
        ensure_ascii=False,
    )


async def _run_command(args: dict[str, Any], ctx: dict[str, Any]) -> str:
    settings: Settings = ctx["settings"]
    if not settings.shell_enabled:
        raise ValueError("命令执行已禁用（ENABLE_SHELL=0）")
    command = str(args.get("command", "")).strip()
    if not command:
        raise ValueError("command 不能为空")
    _check_command(command)
    cwd = _workspace_path(settings, args.get("cwd", "."), must_exist=True)
    if not cwd.is_dir():
        raise ValueError("cwd 不是目录")
    try:
        timeout = int(args.get("timeout", settings.shell_timeout))
    except (TypeError, ValueError):
        timeout = settings.shell_timeout
    timeout = max(1, min(timeout, 600))

    try:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        raise ValueError(f"无法启动命令: {exc}") from exc

    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        raise ValueError(f"命令超时（{timeout} 秒）")
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise

    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError:
        text = output.decode(
            locale.getpreferredencoding(False) or "utf-8", errors="replace"
        )
    limit = max(1000, settings.command_max_output)
    return json.dumps(
        {
            "command": command,
            "cwd": _relative_to_workspace(settings, cwd),
            "exit_code": process.returncode,
            "output": text[:limit],
            "truncated": len(text) > limit,
        },
        ensure_ascii=False,
    )



TOOLS: dict[str, Tool] = {
    "calc": Tool(
        name="calc",
        description="安全计算数学表达式，例如 (1200*0.8)+350。",
        parameters={
            "type": "object",
            "properties": {"expression": {"type": "string", "description": "数学表达式"}},
            "required": ["expression"],
        },
        handler=_calc,
    ),
    "now": Tool(
        name="now",
        description="获取当前 UTC 与本地时间。",
        parameters={"type": "object", "properties": {}},
        handler=_now,
    ),
    "search_knowledge": Tool(
        name="search_knowledge",
        description="检索内置知识库与本次运行中已完成任务的结果。",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "检索关键词"}},
            "required": ["query"],
        },
        handler=_search_knowledge,
    ),
    "web_search": Tool(
        name="web_search",
        description="搜索公开网页，返回标题、URL 和摘要。按 Tavily / Brave / SearXNG / Bing / DuckDuckGo 顺序自动回退。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "max_results": {
                    "type": "integer",
                    "description": "返回条数，1-10，默认 5",
                },
            },
            "required": ["query"],
        },
        handler=_web_search,
    ),
    "fetch_url": Tool(
        name="fetch_url",
        description="抓取公开网页并返回纯文本（禁止内网地址）。",
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "http/https URL"}},
            "required": ["url"],
        },
        handler=_fetch_url,
    ),
    "save_artifact": Tool(
        name="save_artifact",
        description="把文本产物保存到本次运行的产物目录。",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "文件名，如 report.md"},
                "content": {"type": "string", "description": "文件内容"},
            },
            "required": ["name", "content"],
        },
        handler=_save_artifact,
    ),
    "list_files": Tool(
        name="list_files",
        description=(
            "列出本地工作区中的文件和目录。工作区路径由 WORKSPACE_DIR 配置；"
            "所有路径都相对于工作区，禁止越界。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作区的目录，默认 ."},
                "depth": {
                    "type": "integer",
                    "description": "递归深度 1-4，默认 2",
                },
            },
        },
        handler=_list_files,
    ),
    "read_file": Tool(
        name="read_file",
        description="读取工作区内的文本文件；大文件会截断。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作区的文件路径"},
                "max_bytes": {
                    "type": "integer",
                    "description": "最多读取字节数，默认 20000，上限 200000",
                },
            },
            "required": ["path"],
        },
        handler=_read_file,
    ),
    "write_file": Tool(
        name="write_file",
        description="在工作区内创建或覆盖文本文件（自动创建父目录）。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作区的文件路径"},
                "content": {"type": "string", "description": "完整文件内容"},
                "append": {
                    "type": "boolean",
                    "description": "为 true 时追加而不是覆盖",
                },
            },
            "required": ["path", "content"],
        },
        handler=_write_file,
    ),
    "edit_file": Tool(
        name="edit_file",
        description=(
            "对工作区内的文本文件做精确替换（类似编辑器 patch）。"
            "old_string 必须在文件中唯一，除非 replace_all=true。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作区的文件路径"},
                "old_string": {"type": "string", "description": "要被替换的原文"},
                "new_string": {"type": "string", "description": "替换后的内容"},
                "replace_all": {
                    "type": "boolean",
                    "description": "是否替换所有匹配，默认 false",
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
        handler=_edit_file,
    ),
    "run_command": Tool(
        name="run_command",
        description=(
            "在工作区内执行本地命令并返回 stdout/stderr 与退出码。"
            "默认 60 秒超时；危险命令（格式化磁盘、关机、递归删除根目录等）会被拒绝。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"},
                "cwd": {
                    "type": "string",
                    "description": "相对工作区的执行目录，默认 .",
                },
                "timeout": {
                    "type": "integer",
                    "description": "超时秒数，默认 60，上限 600",
                },
            },
            "required": ["command"],
        },
        handler=_run_command,
    ),
}


def schemas(names: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": TOOLS[name].name,
                "description": TOOLS[name].description,
                "parameters": TOOLS[name].parameters,
            },
        }
        for name in names
        if name in TOOLS
    ]


async def call(name: str, args: dict[str, Any], ctx: dict[str, Any]) -> str:
    if name.startswith("mcp__"):
        manager = ctx.get("mcp")
        if manager is None:
            raise ValueError("没有可用的 MCP 客户端")
        if not isinstance(args, dict):
            raise ValueError("工具参数必须是对象")
        result = await manager.call(name, args)
        if len(result) > 8000:
            result = result[:8000] + "\n...[截断]"
        return result
    if not isinstance(args, dict):
        raise ValueError("工具参数必须是对象")
    remote = ctx.get("remote")
    if remote is not None and name in REMOTE_TOOL_OPS:
        # Keep server-side safety checks even when the command runs remotely.
        if name == "run_command":
            if not ctx["settings"].shell_enabled:
                raise ValueError("命令执行已禁用（ENABLE_SHELL=0）")
            _check_command(str(args.get("command") or ""))
        result = await remote.call(name, args)
        if len(result) > 8000:
            result = result[:8000] + "\n...[截断]"
        return result
    tool = TOOLS.get(name)
    if tool is None:
        raise ValueError(f"未知工具: {name}")
    if not isinstance(args, dict):
        raise ValueError("工具参数必须是对象")
    result = await tool.handler(args, ctx)
    if len(result) > 8000:
        result = result[:8000] + "\n...[截断]"
    return result


def build_context(
    store: Store,
    settings: Settings,
    run_id: str,
    task_id: str,
    mcp: Any = None,
    remote: Any = None,
) -> dict[str, Any]:
    return {
        "store": store,
        "settings": settings,
        "run_id": run_id,
        "task_id": task_id,
        "mcp": mcp,
        "remote": remote,
    }
