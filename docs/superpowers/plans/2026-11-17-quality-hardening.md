# 质量硬化实施计划（M0–M1）

> 进度（2026-11-17）：M0 与 M1 已完成。Task 1–8、SEC-08（原计划遗漏）已实施并有回归测试，`pytest -q` 147 passed；Task 9–12（M2 调度/取消/checkpoint）尚未开始。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> 本文件覆盖 **M0 测试地基** 与 **M1 安全/数据正确性**，按 TDD 逐步写。M2–M4 在本文件末尾以任务卡形式列出，M1 验收后再展开成同粒度的步骤。
>
> **按 HANDOVER §1.3：不自动 `git commit` / `git push`。** 每个任务末尾只“跑验证并汇报”，由用户决定提交。本文中的 commit 步骤仅为记录建议信息，执行者不得自动执行。

**Goal:** 先让鉴权不可绕过、数据不会因一行坏 JSON 瘫痪、长运行的 SSE 历史不丢，并建立 pytest 回归网。

**Architecture:** 不改变单进程 asyncio 与 SQLite 结构。新增一个纯函数鉴权比较入口、一个 JSON 容错 helper、一个环境变量剔除 helper；测试通过 conftest 的临时 `DATA_DIR` 与显式 `ACCESS_TOKEN` 隔离真实数据。

**Tech Stack:** Python 3.14、FastAPI、httpx、SQLite、pytest、Node（仅前端语法检查）。

**Spec:** `docs/superpowers/specs/2026-11-17-quality-hardening-design.md`
**Audit:** `docs/2026-11-17-审计报告.md`

## Global Constraints

- 不自动 commit / push；每个任务结束只汇报改动与验证输出。
- 不修改、不提交 `mvp/.env`、`mvp/data/**`；测试必须使用临时 `DATA_DIR`。
- 测试必须在导入 `mvp.app` **之前** 设置 `ACCESS_TOKEN`、`DATA_DIR`、`WORKSPACE_DIR`、`ENABLE_SHELL=0`、`LLM_BASE_URL=""`。
- 后端改动后跑 `python -m compileall -q mvp/app mvp/worker mvp/scripts`；前端改动后跑 `node --check`。
- 新增 `/api` 路由默认需要登录，不得绕过 `require_auth`。
- 修改 checkpoint 结构必须向后兼容旧运行（字段缺失有默认值）。
- 交付前停掉服务器与 worker，不留后台进程。

## Review Focus

1. **空/异常 `ACCESS_TOKEN`**：`ACCESS_TOKEN=`、`#ACCESS_TOKEN=`、非 ASCII、纯空白。期望拒绝启动或 401，绝不放行。→ Task 3。
2. **运行处于 `planning`/`cancelling` 时继续发消息、取消、重跑**：期望明确拒绝或安全排队，不产生第二个调度器。→ Task 8、9。
3. **事件数 > 2000 或客户端断线重连**：期望历史完整、从 `Last-Event-ID` 续传、无静默缺口。→ Task 6。
4. **同一运行被重复触发**（双 Enter、连点重跑、两台设备同时操作）：期望只产生一个运行/一次副作用。→ Task 8、M3。
5. **远程设备工作区含符号链接、超大文件、字符串布尔值**：期望不越界、不 OOM、不误判。→ M2 任务卡。

---

## M0 测试地基

### Task 1: pytest 骨架与隔离夹具

**Files:**
- Create: `mvp/requirements-dev.txt`
- Create: `mvp/tests/conftest.py`
- Create: `mvp/tests/test_smoke.py`
- Modify: `.gitignore`（追加 `.pytest_cache/`、`__pycache__/` 已有则跳过）

**Interfaces:**
- Produces: session 级 `client`（`TestClient`，已触发 lifespan）
- Produces: session 级 `auth_client`（已带登录 cookie）
- Produces: `tmp_workspace`（临时目录 `Path`）
- Consumes: `mvp.app.main.app`、`mvp.app.config.settings`

- [ ] **Step 1: 安装测试依赖**

```bash
.venv/Scripts/python.exe -m pip install "pytest>=8"
```

`mvp/requirements-dev.txt` 内容：

```text
pytest>=8
```

- [ ] **Step 2: 写 `mvp/tests/conftest.py`**

关键约束：所有环境变量在 `from mvp.app.main import app` 之前设置；`DATA_DIR` 指向 `tempfile.mkdtemp()`；`ACCESS_TOKEN` 固定为 `test-token`；`LLM_BASE_URL` 为空走 mock。

```python
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="cluster-test-"))
os.environ["ACCESS_TOKEN"] = "test-token"
os.environ["DATA_DIR"] = str(_TMP / "data")
os.environ["WORKSPACE_DIR"] = str(_TMP / "workspace")
os.environ["ENABLE_SHELL"] = "0"
os.environ["COMMAND_APPROVAL"] = "0"
os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_API_KEY"] = ""
os.environ["CORS_ORIGINS"] = ""

import pytest
from fastapi.testclient import TestClient

from mvp.app.config import settings
from mvp.app.main import app


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def auth_client():
    test_client = TestClient(app)
    response = test_client.post("/api/auth/login", json={"token": "test-token"})
    assert response.status_code == 200
    return test_client


@pytest.fixture()
def tmp_workspace(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return workspace


@pytest.fixture(scope="session")
def orchestrator():
    from mvp.app.main import orchestrator as instance
    return instance


@pytest.fixture()
def store(tmp_path):
    from mvp.app.store import Store
    return Store(str(tmp_path / "test.db"))
```

- [ ] **Step 3: 写 `mvp/tests/test_smoke.py`**

```python
from mvp.app.config import settings


def test_settings_use_temp_data_dir():
    assert "cluster-test-" in str(settings.data_dir)
    assert settings.workspace_dir.exists()


def test_health_is_public(client):
    assert client.get("/api/health").status_code == 200


def test_meta_requires_auth(client):
    assert client.get("/api/meta").status_code == 401


def test_login_then_meta_ok(auth_client):
    assert auth_client.get("/api/meta").status_code == 200
```

- [ ] **Step 4: 跑测试**

```bash
.venv/Scripts/python.exe -m pytest mvp/tests -q
```

Expected: 4 passed。

- [ ] **Step 5: 确认没有污染真实数据**

```bash
git status --porcelain mvp/.env mvp/data
```

Expected: 无输出（`mvp/.env` 未被改写，`mvp/data` 无新文件）。

- [ ] **Step 6: 汇报（不提交）**

汇报：新增文件、`pytest` 输出、`git status` 输出。

---

### Task 2: 认证基线与 CSRF 回归测试

**Files:**
- Create: `mvp/tests/test_auth.py`

**Interfaces:**
- Consumes: `client`、`auth_client`
- Consumes: `POST /api/auth/login`、`POST /api/settings`、`GET /api/auth/sessions`、`DELETE /api/auth/sessions/{id}`

- [ ] **Step 1: 写现有行为的回归测试（这些应当直接通过）**

```python
def test_login_wrong_token_is_401(client):
    response = client.post(
        "/api/auth/login",
        json={"token": "wrong"},
        headers={"x-forwarded-for": "10.10.0.1"},
    )
    assert response.status_code == 401


def test_login_rate_limited_after_five_failures(client):
    headers = {"x-forwarded-for": "10.10.0.2"}
    for _ in range(5):
        client.post("/api/auth/login", json={"token": "wrong"}, headers=headers)
    response = client.post("/api/auth/login", json={"token": "wrong"}, headers=headers)
    assert response.status_code == 429


def test_post_without_csrf_header_is_403(auth_client):
    response = auth_client.post("/api/settings", json={})
    assert response.status_code == 403


def test_post_with_csrf_header_is_allowed(auth_client):
    response = auth_client.post(
        "/api/settings",
        json={},
        headers={"x-requested-with": "XMLHttpRequest"},
    )
    assert response.status_code == 200


def test_revoked_session_is_401(client):
    first = client.post("/api/auth/login", json={"token": "test-token"})
    assert first.status_code == 200
    sessions = first.get("/api/auth/sessions").json()["sessions"]
    current = next(item for item in sessions if item["current"])
    response = first.delete(
        f"/api/auth/sessions/{current['id']}",
        headers={"x-requested-with": "XMLHttpRequest"},
    )
    assert response.status_code == 200
    assert first.get("/api/meta").status_code == 401
```

- [ ] **Step 2: 跑测试**

```bash
.venv/Scripts/python.exe -m pytest mvp/tests/test_auth.py -q
```

Expected: 5 passed。若 `test_login_rate_limited_after_five_failures` 因共享限速表失败，给每个测试使用不同的 `x-forwarded-for`（已这样做）。

- [ ] **Step 3: 汇报（不提交）**

---

## M1 安全与数据正确性

### Task 3: 拒绝空/异常 `ACCESS_TOKEN`（SEC-01、SEC-02、SEC-03）

**Files:**
- Modify: `mvp/app/config.py:30-58`、`mvp/app/config.py:125-136`
- Modify: `mvp/app/main.py:272-278`、`mvp/app/main.py:302-305`
- Test: `mvp/tests/test_config_token.py`、`mvp/tests/test_auth.py`

**Interfaces:**
- Produces: `config._resolve_access_token(base_dir: Path) -> str`（返回非空 token；无法确定时生成并写盘；不得返回 `""`）
- Produces: `config._read_env_token(env_text: str) -> str`（只认 `^\s*ACCESS_TOKEN\s*=\s*(.+)$`，忽略注释行；最后一条生效）
- Produces: `main._bearer_ok(header: str, expected: str) -> bool`（空值、无空格、非 ASCII 一律 False；用 UTF-8 bytes 比较）
- Consumes: `settings.access_token`

- [ ] **Step 1: 写失败测试**

`mvp/tests/test_config_token.py`：

```python
import pytest

from mvp.app import config


def _write_env(tmp_path, text):
    env_path = tmp_path / ".env"
    env_path.write_text(text, encoding="utf-8")
    return env_path


def test_empty_token_line_is_regenerated(tmp_path, monkeypatch):
    monkeypatch.delenv("ACCESS_TOKEN", raising=False)
    env_path = _write_env(tmp_path, "ACCESS_TOKEN=\n")
    token = config._resolve_access_token(tmp_path)
    assert token
    assert f"ACCESS_TOKEN={token}" in env_path.read_text(encoding="utf-8")


def test_commented_token_is_not_used(tmp_path, monkeypatch):
    monkeypatch.delenv("ACCESS_TOKEN", raising=False)
    env_path = _write_env(tmp_path, "#ACCESS_TOKEN=old\n")
    token = config._resolve_access_token(tmp_path)
    assert token and token != "old"
    assert f"ACCESS_TOKEN={token}" in env_path.read_text(encoding="utf-8")


def test_valid_token_is_reused(tmp_path, monkeypatch):
    monkeypatch.delenv("ACCESS_TOKEN", raising=False)
    _write_env(tmp_path, "ACCESS_TOKEN=abc123\n")
    assert config._resolve_access_token(tmp_path) == "abc123"


def test_no_token_in_file_generates_and_writes(tmp_path, monkeypatch):
    monkeypatch.delenv("ACCESS_TOKEN", raising=False)
    env_path = _write_env(tmp_path, "")
    token = config._resolve_access_token(tmp_path)
    assert token
    assert token in env_path.read_text(encoding="utf-8")
```

`mvp/tests/test_auth.py` 追加：

```python
import pytest

from mvp.app import main


@pytest.mark.parametrize(
    "header",
    ["Bearer", "Bearer ", "Bearer\t", "bearer   ", "Bearer 中文"],
)
def test_empty_or_non_ascii_bearer_is_rejected(client, header):
    response = client.get("/api/meta", headers={"Authorization": header})
    assert response.status_code == 401


def test_bearer_helper_rejects_empty_expected():
    assert main._bearer_ok("Bearer ", "") is False
    assert main._bearer_ok("Bearer test-token", "test-token") is True
    assert main._bearer_ok("Bearer 中文", "test-token") is False


def test_non_ascii_login_is_401_not_500(client):
    response = client.post("/api/auth/login", json={"token": "中文"})
    assert response.status_code == 401
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/Scripts/python.exe -m pytest mvp/tests/test_config_token.py mvp/tests/test_auth.py -q
```

Expected: `_resolve_access_token` / `_bearer_ok` 不存在（AttributeError）；非 ASCII 登录得到 500。

- [ ] **Step 3: 实现**

`config.py`：
- 把 `_ensure_access_token` 改为基于 `_read_env_token` 的实现；解析到空串时视为“没有 token”，生成新值并**重写** `.env`（删除所有 `ACCESS_TOKEN` 行后追加一条），保留其他变量。
- `_read_env_token` 用 `re.MULTILINE` 的 `^\s*ACCESS_TOKEN\s*=\s*(.+?)\s*$`；跳过以 `#` 开头的行；返回最后一个匹配的值。
- `Settings.__post_init__`：`if not self.access_token: self.access_token = _ensure_access_token(...)`；随后 `if not self.access_token: raise RuntimeError("ACCESS_TOKEN 不能为空")`。
- 保留 `os.chmod` 尝试，但不再依赖它。

`main.py`：
- 新增纯函数：

```python
def _bearer_ok(header: str, expected: str) -> bool:
    if not expected or not header.lower().startswith("bearer "):
        return False
    supplied = header[7:].strip()
    if not supplied:
        return False
    try:
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
    except (UnicodeError, TypeError):
        return False
```

- 中间件改为 `if _bearer_ok(header, settings.access_token): ...`。
- 登录改为 `if not _bearer_ok("Bearer " + body.token.strip(), settings.access_token)` 或直接复用 bytes 比较 helper `_token_ok(supplied, expected)`；确保非 ASCII 返回 401 而不是抛异常。

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/Scripts/python.exe -m pytest mvp/tests/test_config_token.py mvp/tests/test_auth.py -q
```

Expected: 全部 passed，且 `test_empty_or_non_ascii_bearer_is_rejected` 覆盖 SEC-01。

- [ ] **Step 5: 回归**

```bash
.venv/Scripts/python.exe -m pytest mvp/tests -q
.venv/Scripts/python.exe -m compileall -q mvp/app mvp/worker mvp/scripts
```

- [ ] **Step 6: 汇报（不提交）**

注明：`mvp/.env` 里如果是 `ACCESS_TOKEN=`，首次启动会被重写并打印新令牌；提醒用户更新手机端登录。

---

### Task 4: 默认关闭文档端点（SEC-04）

**Files:**
- Modify: `mvp/app/config.py`（新增 `enable_api_docs`）
- Modify: `mvp/app/main.py:157`
- Test: `mvp/tests/test_auth.py`

**Interfaces:**
- Produces: `settings.enable_api_docs: bool`（`ENABLE_API_DOCS=1` 时为 True，默认 False）

- [ ] **Step 1: 写失败测试**

```python
def test_openapi_hidden_by_default(client):
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
```

- [ ] **Step 2: 跑测试确认失败**（当前返回 200）

```bash
.venv/Scripts/python.exe -m pytest mvp/tests/test_auth.py::test_openapi_hidden_by_default -q
```

- [ ] **Step 3: 实现**

`config.py` 新增：

```python
enable_api_docs: bool = field(
    default_factory=lambda: os.getenv("ENABLE_API_DOCS", "0").strip().lower()
    in ("1", "true", "yes", "on")
)
```

`main.py`：

```python
_docs = settings.enable_api_docs
app = FastAPI(
    title="AI Cluster MVP",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs else None,
    redoc_url="/redoc" if _docs else None,
    openapi_url="/openapi.json" if _docs else None,
)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/Scripts/python.exe -m pytest mvp/tests/test_auth.py -q
```

- [ ] **Step 5: 汇报（不提交）**

说明开发时需要 `ENABLE_API_DOCS=1` 才能看 Swagger。

---

### Task 5: 子进程环境变量白名单（SEC-07）

**Files:**
- Modify: `mvp/app/tools.py:583-588`（`_run_command`）
- Modify: `mvp/worker/agent.py:220-225`（`run_command`）
- Test: `mvp/tests/test_tools_env.py`

**Interfaces:**
- Produces: `tools._child_env(base: Mapping[str, str] | None = None) -> dict[str, str]`
- Produces: `worker.agent._child_env(base: Mapping[str, str] | None = None) -> dict[str, str]`
- 两处必须使用同一剔除名单常量：
  - 精确名：`ACCESS_TOKEN`、`LLM_API_KEY`、`TAVILY_API_KEY`、`BRAVE_API_KEY`、`SEARXNG_URL`、`CLUSTER_WORKER_TOKEN`
  - 后缀：`_API_KEY`、`_TOKEN`、`_SECRET`、`_PASSWORD`
  - 代理：`HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`（大小写两种）

- [ ] **Step 1: 写失败测试**

```python
import importlib

from mvp.app import tools


def test_child_env_drops_secrets(monkeypatch):
    monkeypatch.setenv("ACCESS_TOKEN", "secret-a")
    monkeypatch.setenv("LLM_API_KEY", "secret-b")
    monkeypatch.setenv("MY_SERVICE_TOKEN", "secret-c")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = tools._child_env()
    assert "ACCESS_TOKEN" not in env
    assert "LLM_API_KEY" not in env
    assert "MY_SERVICE_TOKEN" not in env
    assert env["PATH"] == "/usr/bin"


def test_worker_uses_same_deny_list():
    tools_mod = importlib.import_module("mvp.app.tools")
    worker_mod = importlib.import_module("mvp.worker.agent")
    assert set(tools_mod._SECRET_ENV_NAMES) == set(worker_mod._SECRET_ENV_NAMES)
    assert tuple(tools_mod._SECRET_ENV_SUFFIXES) == tuple(worker_mod._SECRET_ENV_SUFFIXES)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/Scripts/python.exe -m pytest mvp/tests/test_tools_env.py -q
```

Expected: AttributeError `_child_env`。

- [ ] **Step 3: 实现**

在两个模块各定义同一份常量与 6 行 helper（worker 不依赖 `mvp.app`，所以保留小重复，用测试锁定一致性）：

```python
_SECRET_ENV_NAMES = {
    "ACCESS_TOKEN", "LLM_API_KEY", "TAVILY_API_KEY", "BRAVE_API_KEY",
    "SEARXNG_URL", "CLUSTER_WORKER_TOKEN",
}
_SECRET_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")

def _child_env(base=None):
    source = dict(os.environ if base is None else base)
    for key in list(source):
        upper = key.upper()
        if upper in _SECRET_ENV_NAMES or upper.endswith(_SECRET_ENV_SUFFIXES):
            source.pop(key, None)
    return source
```

把两处 `create_subprocess_shell(...)` 加上 `env=_child_env()`。

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/Scripts/python.exe -m pytest mvp/tests/test_tools_env.py -q
```

- [ ] **Step 5: 手工验证**

临时启动本地 mock 服务，执行 `run_command {"command": "python -c \"import os;print(os.getenv('ACCESS_TOKEN'))\""}`，确认输出 `None`；确认 `PATH` 仍可用（`python --version` 能跑）。

- [ ] **Step 6: 汇报（不提交）**

---

### Task 6: SSE 回放分页与 `Last-Event-ID`（DATA-03）

**Files:**
- Modify: `mvp/app/store.py:836-847`
- Modify: `mvp/app/main.py:1411-1440`
- Test: `mvp/tests/test_store_events.py`、`mvp/tests/test_sse.py`

**Interfaces:**
- Produces: `Store.iter_events(run_id: str, after: int = 0, batch: int = 1000) -> Iterator[dict[str, Any]]`
- 保留 `get_events` 供其他调用方使用，内部改为 `list(iter_events(...))` 的截断版本（或保持原语义）
- Consumes: `stream_events(run_id, after)` 新增读取 `Last-Event-ID` 请求头

- [ ] **Step 1: 写失败测试**

```python
from mvp.app.models import new_id


def test_iter_events_returns_all_batches(store):
    run_id = new_id("run")
    for index in range(2501):
        store.append_event({
            "id": new_id("evt"), "run_id": run_id, "task_id": None,
            "type": "tool.result", "ts": float(index), "payload": {"i": index},
        })
    events = list(store.iter_events(run_id, batch=1000))
    assert len(events) == 2501
    assert events[-1]["payload"]["i"] == 2500
```

`store` 夹具已在 Task 1 的 `conftest.py` 中定义，这里直接使用。

`test_sse.py`：先用 API 创建一个运行并写入 2100 条事件，再 `GET /api/runs/{id}/events`，读响应流直到 `event: live`，统计 `data:` 行数 ≥ 2100。若流式读取在 TestClient 里不稳定，退化为直接测 `store.iter_events` + 一个单元测试验证 `Last-Event-ID` 解析 helper。

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/Scripts/python.exe -m pytest mvp/tests/test_store_events.py -q
```

Expected: AttributeError `iter_events`。

- [ ] **Step 3: 实现**

`store.py`：

```python
def iter_events(self, run_id, after=0, batch=1000):
    cursor = after
    while True:
        rows = self.get_events(run_id, cursor, batch)
        if not rows:
            return
        for row in rows:
            cursor = max(cursor, int(row["seq"]))
            yield row
        if len(rows) < batch:
            return
```

`main.py` `stream_events`：
- 签名增加 `last_event_id: str | None = Header(default=None, alias="Last-Event-ID")`。
- 起始位点：`start = after if after else int(last_event_id or 0)`。
- 回放改为 `for event in store.iter_events(run_id, start): yield ...`。
- 回放结束后仍发 `event: snapshot`、`event: live`，再进入实时队列循环。

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/Scripts/python.exe -m pytest mvp/tests/test_store_events.py mvp/tests/test_sse.py -q
```

- [ ] **Step 5: 汇报（不提交）**

---

### Task 7: JSON 列容错与删除运行级联（DATA-01、DATA-02）

**Files:**
- Modify: `mvp/app/store.py:318-319`、`477-478`、`845`、`1235`、`1171`、`1193`、`1206-1212`
- Test: `mvp/tests/test_store_json.py`

**Interfaces:**
- Produces: `Store._json_or(value: str | None, default: T) -> T`（模块级 `_json_or` 亦可）
- Produces: `Store.delete_run` 同时删除 `messages`

- [ ] **Step 1: 写失败测试**

```python
import sqlite3

from mvp.app.models import Budget, RunState, new_id


def _make_run(run_id):
    return RunState(id=run_id, goal="g", status="failed", budget=Budget())


def test_broken_budget_does_not_break_list(store):
    run_id = new_id("run")
    store._conn.execute(
        "INSERT INTO runs (id, goal, status, result, error, budget, usage, cluster, created_at, updated_at, deadline_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, "g", "failed", "", "", "{not json", "{}", "{}", 0.0, 0.0, None),
    )
    store._conn.commit()
    runs = store.list_runs(limit=10)
    assert any(item["id"] == run_id and item["budget"] == {} for item in runs)


def test_delete_run_removes_messages(store):
    run_id = new_id("run")
    store.create_run(_make_run(run_id))
    store.add_message(run_id, "user", "hello")
    assert store.list_messages(run_id)
    assert store.delete_run(run_id) is True
    assert store.list_messages(run_id) == []
```

`_make_run` 辅助函数用 `RunState` 构造最小对象（`id=run_id, goal="g", budget=Budget()`）。

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/Scripts/python.exe -m pytest mvp/tests/test_store_json.py -q
```

Expected: `json.decoder.JSONDecodeError`；`list_messages` 非空。

- [ ] **Step 3: 实现**

```python
def _json_or(value, default):
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
```

- 把 `runs.budget/usage/cluster`、`events.payload`、`tasks.depends_on`、`evaluations` 四列改为 `_json_or(..., {})` / `_json_or(..., [])`。
- `delete_run` 增加 `self._conn.execute("DELETE FROM messages WHERE run_id=?", (run_id,))`，与其他两条 DELETE 在同一事务。

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/Scripts/python.exe -m pytest mvp/tests/test_store_json.py -q
```

- [ ] **Step 5: 回归**

```bash
.venv/Scripts/python.exe -m pytest mvp/tests -q
```

- [ ] **Step 6: 汇报（不提交）**

---

### Task 8: 设备删除关闭 WS + worker 非法帧容错（DATA-04、DATA-05）

**Files:**
- Modify: `mvp/app/devices.py:138-148`
- Modify: `mvp/app/main.py:760-767`、`mvp/app/main.py:897-941`
- Test: `mvp/tests/test_devices_ws.py`

**Interfaces:**
- Produces: `DeviceHub.detach(device_id, websocket=None, *, close: bool = False) -> None`，改为 `async def`；`close=True` 时尝试 `await websocket.close(code=4401)`
- 调用点同步改：`main.py:765` 改为 `await device_hub.detach(device_id, close=True)`；`main.py:941` 的 `finally` 改为 `await device_hub.detach(device_id, websocket)`
- 修改 `worker_socket`：非 dict / 非 JSON 帧忽略并继续；`except (ValueError, AttributeError, TypeError)` 不让连接崩

- [ ] **Step 1: 写失败测试**

用一个假 websocket 验证 `detach(close=True)` 会调用 `close`：

```python
class FakeWS:
    def __init__(self):
        self.closed = None
    async def close(self, code=1000, reason=""):
        self.closed = code


def test_detach_can_close_socket():
    from mvp.app.devices import DeviceHub
    import asyncio

    hub = DeviceHub()
    ws = FakeWS()
    asyncio.run(hub.attach("dev_test", ws))
    asyncio.run(hub.detach("dev_test", ws, close=True))
    assert ws.closed == 4401
```

WS 集成测试（若 TestClient 的 websocket 行为不稳定，标记 `@pytest.mark.skip` 并保留手工清单）：连接 `/api/worker/ws?token=<配对 token>`，先发 `not json`，再发合法 `hello`，断言连接仍可收发。

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/Scripts/python.exe -m pytest mvp/tests/test_devices_ws.py -q
```

- [ ] **Step 3: 实现**

`devices.py`：

```python
async def detach(self, device_id, websocket=None, *, close=False):
    current = self._connections.get(device_id)
    if current is None:
        return
    if websocket is not None and current.websocket is not websocket:
        return
    if close:
        try:
            await current.websocket.close(code=4401)
        except Exception:
            pass
    current.cancel_all()
    self._connections.pop(device_id, None)
    ...
```

`main.py` `delete_device`：`await device_hub.detach(device_id, close=True)`。
`worker_socket`：

```python
try:
    message = await websocket.receive_json()
except (ValueError, TypeError):
    continue
if not isinstance(message, dict):
    continue
```

并把 `except` 列表补上 `ValueError, AttributeError, TypeError`。

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/Scripts/python.exe -m pytest mvp/tests/test_devices_ws.py -q
```

- [ ] **Step 5: 汇报（不提交）**

---

### Task 9: 拒绝 planning/cancelling 期间的新轮 + 单一调度锁（RUN-01）

**Files:**
- Modify: `mvp/app/orchestrator.py:285-305`、`391-411`、`767-798`、`1169-1186`
- Test: `mvp/tests/test_orchestrator_locks.py`

**Interfaces:**
- Produces: `Orchestrator._run_lock(run_id: str) -> asyncio.Lock`（复用/替代 `_round_locks`）
- `_drive` 在 `plan` + `_schedule` 期间持有该锁
- `_run_round` 继续持有同一把锁
- `front_chat` 在 `run.status in ("planning", "cancelling")` 时返回一条明确的 SSE error，不创建任务

- [ ] **Step 1: 写失败测试**

用 monkeypatch 让 `_plan` 阻塞在 `asyncio.Event` 上，验证 `front_chat` 不会派发新轮。项目不引入 pytest-asyncio，测试用 `asyncio.run(...)` 包裹异步场景：

```python
import asyncio
from mvp.app.models import Budget


def test_front_chat_rejected_during_planning(orchestrator, monkeypatch):
    async def scenario():
        gate = asyncio.Event()

        async def slow_plan(run):
            await gate.wait()
            return []

        monkeypatch.setattr(orchestrator, "_plan", slow_plan)
        run_id = await orchestrator.start("goal", Budget())
        await asyncio.sleep(0)
        events = [
            event
            async for event in orchestrator.front_chat(run_id, "再做一件事")
        ]
        assert any("规划" in str(event) for event in events)
        gate.set()

    asyncio.run(scenario())
```

再加一个锁测试：monkeypatch `_schedule` 计数进入/退出，断言最大并发为 1。

- [ ] **Step 2: 跑测试确认失败**

```bash
.venv/Scripts/python.exe -m pytest mvp/tests/test_orchestrator_locks.py -q
```

- [ ] **Step 3: 实现**

- `_drive`：`async with self._run_lock(run.id):` 包住 `_plan`、`create_tasks`、`_schedule`、`_finish`。
- `_run_round`：把 `self._round_locks.setdefault(...)` 换成 `self._run_lock(run.id)`。
- `front_chat` 在读取 `fresh` 后：
  ```python
  if fresh and fresh.get("status") in ("planning", "cancelling"):
      yield self._sse({"type": "error", "error": "运行正在规划/停止中，请稍后再发"})
      return
  ```
- `_drive` 把 `run.tasks = await self._plan(run)` 改为 `run.tasks[:] = await self._plan(run)`，避免丢失已追加的轮次任务。

- [ ] **Step 4: 跑测试确认通过**

```bash
.venv/Scripts/python.exe -m pytest mvp/tests/test_orchestrator_locks.py -q
```

- [ ] **Step 5: 手工回归**

mock 模式创建一个运行，在 planning 瞬间发第二句，确认界面提示“正在规划”且最终只有一个调度器、无“调度死锁”。

- [ ] **Step 6: 汇报（不提交）**

---

### Task 10: 轮次可取消，取消不改写终态（RUN-02、RUN-04、RUN-05）

**Files:**
- Modify: `mvp/app/orchestrator.py:443-447`、`767-820`、`884-942`、`1699-1712`、`1740-1778`
- Test: `mvp/tests/test_orchestrator_cancel.py`

**Interfaces:**
- `cancel(run_id)` 取消该 run 的轮次任务（登记在 `_run_tasks[run_id]` 或新的 `_round_tasks[run_id]`）
- `_run_round` 在 `_schedule` 返回后先判 `run.cancelled`：发布 `run.cancelled`，跳过 `_summarize_round`
- CancelledError 分支：`if task.status in ("done", "failed"): raise`
- `task.status = "running"` 与 `task.started` 事件移入 `try`

- [ ] **Step 1: 写失败测试**

```python
def test_cancel_round_marks_cancelled_not_failed(orchestrator, monkeypatch):
    ...  # 启动一轮，任务阻塞；调用 cancel；等待 round task 结束
    assert run.status == "cancelled"
    assert summarize_called is False
```

```python
def test_done_task_is_not_downgraded_by_cancel(orchestrator):
    task = TaskState(...); task.status = "done"
    # 直接调用 CancelledError 分支所在 helper，或取消一个在 publish 状态的任务
    assert task.status == "done"
```

- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**（按 Interfaces 的 4 条）
- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 手工回归**：mock 专家团运行中停止，DB `runs.status=cancelled`，事件流有 `run.cancelled`，停止后无新的 `chat.message` 总结。
- [ ] **Step 6: 汇报（不提交）**

---

### Task 11: checkpoint 深拷贝与完整批次（RUN-03）

**Files:**
- Modify: `mvp/app/orchestrator.py:2131-2147`、`1785`、`1908-1928`
- Test: `mvp/tests/test_checkpoint.py`

**Interfaces:**
- `_save_checkpoint(...)` 内部 `copy.deepcopy(messages)`
- 新增 `Orchestrator._checkpoint_consistent(messages: list[dict]) -> bool`：每个 assistant 的 `tool_calls[].id` 都有对应 `role="tool"` 消息
- 保存点只在 `_checkpoint_consistent(messages)` 为真时写库；恢复读取时若不满足，丢弃尾部未配对的 assistant tool_calls

- [ ] **Step 1: 写失败测试**

```python
def test_checkpoint_is_detached_from_live_list(task, store):
    messages = [{"role": "user", "content": "hi"}]
    orchestrator._save_checkpoint(task, messages, [], False, 1)
    messages.append({"role": "assistant", "content": "later"})
    assert len(task.checkpoint["messages"]) == 1


def test_inconsistent_checkpoint_is_not_saved(task, store):
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}]},
    ]
    orchestrator._save_checkpoint(task, messages, [], False, 1)
    assert task.checkpoint is None
```

- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 汇报（不提交）**

---

### Task 12: token 估算与并发下限（RUN-07、RUN-06）

**Files:**
- Modify: `mvp/app/llm.py:34-39`
- Modify: `mvp/app/config.py:113`
- Test: `mvp/tests/test_llm_tokens.py`

**Interfaces:**
- `estimate_tokens(value) -> int`：CJK 字符按 1 token，其他按 1/4 token，向上取整；JSON 序列化后统计
- `Settings.max_concurrency = max(1, _env_int("MAX_CONCURRENCY", 4))`

- [ ] **Step 1: 写失败测试**

```python
from mvp.app.llm import estimate_tokens


def test_cjk_is_not_underestimated():
    text = "中文" * 1000
    assert estimate_tokens(text) >= 1800


def test_ascii_stays_reasonable():
    assert 900 <= estimate_tokens("a" * 4000) <= 1200
```

- [ ] **Step 2: 跑测试确认失败**（当前 CJK 约 666）
- [ ] **Step 3: 实现**
- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 汇报（不提交）**

---

## M1 验收

- [ ] `pytest -q` 全绿（Task 1–12 的测试）。
- [ ] `python -m compileall -q mvp/app mvp/worker mvp/scripts` 通过。
- [ ] `git status --porcelain mvp/.env mvp/data` 无输出。
- [ ] 手工：`ACCESS_TOKEN=` 启动被重新生成并打印；`Authorization: Bearer ` 返回 401；`/openapi.json` 404。
- [ ] 手工：`run_command "env"` 结果不含 `ACCESS_TOKEN`/`*_API_KEY`。
- [ ] 手工：2500 条事件的运行打开详情，日志完整；刷新页面不重复、不丢尾。

---

## M2–M4 任务卡（M1 验收后展开为同粒度步骤）

### M2 运行与调度正确性

| 任务 | 文件 | 测试/验收 | 关联 |
|---|---|---|---|
| M2-1 恢复遍历按状态查询、逐 run 容错 | `orchestrator.py:972-1033` | 造 250 条运行，中断 3 条，全部恢复；一条坏 budget 不影响其余 | RUN-11 |
| M2-2 专家团轮次用量累加、只重置 deadline | `orchestrator.py:405-410` | 两轮 chat 后 `usage.steps` 为两轮之和；`max_usd` 跨轮累计生效 | RUN-08 |
| M2-3 主理人/总结调用记账 | `orchestrator.py:535-540`、`862-867` | mock LLM 返回固定 usage，断言 `run.tokens` 包含这两次 | RUN-09 |
| M2-4 resume 保留 checkpoint | `orchestrator.py:1085-1096` | 失败任务重试不从第 0 步重放；`steps` 连续 | RUN-12 |
| M2-5 命令超时杀进程树 | `tools.py:594-599`、`worker/agent.py:228-231` | 起一个派生子进程的命令，超时后子孙进程不存在 | DEV-01 |
| M2-6 worker DENY 与本地规则对齐 | `worker/agent.py:20-30`、`tools.py:383-396` | 同一组命令在两端判定一致；`rm -rf /tmp/build` 不被误杀 | DEV-04 |
| M2-7 心跳超时判定在线 | `devices.py:94-98`、`main.py:920-924` | 伪造 61 秒前 `last_seen`，`online()` 返回 False | DEV-09 |

### M3 前端状态与手机闭环

| 任务 | 文件 | 验收 | 关联 |
|---|---|---|---|
| M3-1 提交去重 | `index.html:4694-4722`、`5113-5119` | 双 Enter 只建一个运行 | UI-01 |
| M3-2 运行切换守卫与事件作用域 | `index.html:2267-2286`、`2310-2317`、`2353` | 快速 A→B，最终流与高亮都是 B；A 的事件不改 B | UI-02 |
| M3-3 对话流作用域 + AbortController | `index.html:1913-1952` | 切运行后旧流不 push 消息；`pendingUser` 清空 | UI-03 |
| M3-4 重跑保护 | `index.html:1317`、`4840-4850`；`main.py:1314-1350` | 运行中重跑按钮不可见；后端对 active 源运行 409 | UI-04 |
| M3-5 后台刷新去门控 | `index.html:5351-5360`、`4365-4370` | 桌面空闲时 10 秒内看到手机发起的运行与审批 | UI-05 |
| M3-6 401 停止轮询 | `index.html:1499-1518` | 会话撤销后只弹一次登录框；登录后恢复轮询 | UI-06 |
| M3-7 422 错误可读 | `index.html:1515-1518` | 超长目标提示具体字段，不出现 `[object Object]` | UI-07 |

### M4 交接 P1 可见性

| 任务 | 文件 | 验收 | 关联 |
|---|---|---|---|
| M4-1 运行详情无条件显示设备与工作区 | `index.html` `applyRunData` | 打开任意历史，头部显示“设备 · 目录” | HANDOVER §16.14 |
| M4-2 历史列表设备标记 | `index.html` `loadRuns` | 每条历史有设备缩写；无设备显示“本机” | HANDOVER §17 P1 |
| M4-3 工具事件带目标路径 | `tools.py` / `orchestrator.py` `tool.called` payload | 远程调用事件含 workspace + 相对路径 | HANDOVER §17 P1 |

---

## 计划自检

- **Spec 覆盖**：设计文档 M0–M4 的每个里程碑在本文件中都有任务或任务卡；M1 的 10 条 P0/P1 全部有测试。
- **步骤粒度**：每个任务以“写失败测试 → 跑失败 → 实现 → 跑通过 → 汇报”为一轮，单轮 2–5 分钟级。
- **类型一致**：`_resolve_access_token`、`_bearer_ok`、`_child_env`、`_json_or`、`iter_events`、`_run_lock`、`_checkpoint_consistent` 在首次出现的任务里定义了签名，后续任务只引用。
- **Review Focus 落地**：5 条分别落在 Task 3、9、6、8/M3-1、M2-5/M2-6。
- **比例**：本计划不含产品代码正文，只有签名、测试断言与取值规则；M2–M4 以任务卡形式延后展开，避免在执行前写死细节。
