# AI 集群（agent-group）交接文档

> 交接时间：2026-11-15  
> 代码基线：`c4cbd1a 优化了登录认证`（`HANDOVER.md` 与 README 链接为交接新增，未提交）  
> 读者：接手后续开发的工程师 / Agent  
> 建议阅读顺序：本文件 → `README.md` → `docs/架构设计.md` → 具体模块源码  
> 注意：`docs/架构设计.md` 是早期设计文档；认证、设备、目录浏览等后续实现以本文件和第 2 节为准。

---

## 0. 30 秒版本

这是一个**单人使用**的多 Agent 集群平台：

- 服务器（本机或云端）负责对话、任务规划、调度、审批、事件持久化
- 每台电脑运行一个 worker，主动外连服务器；任务只在该电脑授权的工作区里读写文件、执行命令
- 手机/平板/任意浏览器打开网页即可当控制器：发任务、审批、看结果
- 控制面强制登录；每台设备首次输入 `ACCESS_TOKEN`，之后保持会话，可单独撤销
- 没有模型 Key 时自动进入 mock 模式，能完整跑通「规划 → 调度 → 工具 → 汇总」

一句话验收口径：

> **手机打开一个地址，选一台在线电脑和目录，发任务；需要审批时手机能收到提醒；电脑断线、服务器重启都不丢任务；记录能看出是哪台机器、哪个目录跑的。**

---

## 1. 目标与边界

### 1.1 最终效果

1. 一台常开的服务器（先本机，后云端）
2. 手机/平板/电脑浏览器打开即用，不需要安装客户端
3. 网页里选择在线设备 + 目录，Agent 只在那里执行
4. 任务可暂停、审批、断线等待、重启恢复
5. 所有关键操作可回溯：设备、目录、命令、产物
6. 认证：没登录的设备无法访问；每台设备可单独撤销
7. 部署简单：一个 `start.cmd` 起服务，一个 `start-worker.cmd` 起 worker

### 1.2 非目标

- 不做多用户 / RBAC / 团队协作
- 不做 SaaS 计费
- 不追求多副本高可用（当前是单进程）
- 不把 worker 做成强安全沙箱（见 16 节）

### 1.3 使用约束

- 单人使用；所有控制器共用一个 `ACCESS_TOKEN` 作为首次登录凭证
- worker 每台一个独立 token，哈希存库，可删除吊销
- 文件传输建议走 Tailscale 或 HTTPS；局域网 HTTP 是明文
- 不自动 git commit / push
- 开发完成后不要把服务器/worker 留在后台；由用户自己启动

---

## 2. 当前状态

### 2.1 已验证可用

- 多模型集群：Planner 拆 DAG → 调度 → Agent 执行 → 汇总
- 单模型直答模式
- 专家团主理人：直接回复，或调用 `expert_action` 派任务 / 追加指导
- 对话流式输出（正文与 reasoning 分离）
- 任务断点：`tasks.checkpoint`、启动恢复、优雅关闭
- 命令审批：逐条 / 本次运行都允许 / 超时拒绝
- 远程设备：网页配对、worker 外连、文件/命令 RPC、目录浏览、子目录工作区
- 设备掉线等待重连（默认 60 秒），只读操作自动重试
- 访问页：Tailscale / 局域网地址探测、二维码、worker 命令
- 认证：强制登录、HttpOnly 会话、CSRF、登录限速、会话列表与撤销
- 移动端布局、审批标题闪烁 + 提示音

### 2.2 提交历史（近期）

```text
c4cbd1a 优化了登录认证
8e3eb19 增加设备记录于在线状态检查
a2e745b 增加超时等待
1b284ad 添加外部访问功能，优化命令审批，添加局域网/扫码访问入口
039f90b 云端功能测试
8737b01 chore: expand gitignore for secrets and runtime data
bdec17c v1
```

### 2.3 交接时数据现状

真实数据库 `mvp/data/cluster.db`：

```text
runs          12
tasks         43
messages      32
events       597
devices        0
sessions       0
clusters       2
providers      4
evaluations    3
active runs    0
```

说明：

- `devices = 0`：还没有配对过真实 worker，远程流程需要先配对
- `sessions = 0`：认证代码提交后还没在真实库登录过
- `mvp/.env` 已存在，里面是自动生成的 `ACCESS_TOKEN`（不要提交）
- 当前 8000 端口没有服务在跑

### 2.4 未完成

- 运行详情/历史无条件显示设备与目录
- 手机端 Web Notification、PWA、断流提示
- 同一设备并发上限 / 排队
- 取消运行时真正终止远程命令
- 云端 Docker 实跑验证、备份脚本、Tailscale 实测
- 自动化测试（目前只有 smoke + 手工验证）

---

## 3. 快速开始

### 3.1 服务器

Windows：

```powershell
.\start.cmd
```

Linux / macOS：

```bash
./start.sh
```

手动：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r mvp\requirements.txt
.\.venv\Scripts\python.exe mvp\run.py
```

默认监听 `0.0.0.0:8000`。启动后告警会提示 `ACCESS_TOKEN` 状态。

### 3.2 首次登录

- 如果 `ACCESS_TOKEN` 未设置，启动时自动生成，写入 `mvp/.env`，终端打印一次
- 打开 `http://127.0.0.1:8000`
- 输入该 token；登录一次后，这台设备保持会话（默认 180 天）
- 设置 → 访问 → 已登录设备，可以撤销

### 3.3 设备 worker

```powershell
.\start-worker.cmd
```

首次会提示：

1. 服务器地址（默认 `http://127.0.0.1:8000`）
2. 设备 token（网页「设置 → 设备 → ＋ 配对设备」生成）
3. 设备名称（默认电脑名）
4. 工作区绝对路径（默认 `mvp/data/workspace`）

配置保存在 `mvp/data/worker.json`，之后双击直接连。重配：

```powershell
.\start-worker.cmd --reset
```

其他电脑需要 Python 3.10+，最简依赖只有 `websockets>=13`。

### 3.4 mock 冒烟

```powershell
# 另开终端，服务需已启动
.\.venv\Scripts\python.exe mvp\scripts\smoke.py
```

`smoke.py` 会从环境变量或 `mvp/.env` 读取 `ACCESS_TOKEN`。

---

## 4. 架构

### 4.1 组件图

```text
浏览器 / 手机（控制器）
   │  HTTPS 或 Tailscale，HttpOnly 会话
   ▼
┌─────────────────────────────────────────────┐
│ 服务器 mvp/app (FastAPI)                     │
│  - REST + SSE + 静态页面                     │
│  - 认证 / 会话 / 审批 / 事件持久化            │
│  - Orchestrator：规划、调度、恢复             │
│  - EventBus：进程内 fan-out + SQLite 事件日志 │
│  - DeviceHub：在线设备与 RPC                  │
└───────────┬─────────────────────────────────┘
            │ WebSocket（设备主动外连）
            ▼
┌─────────────────────────────────────────────┐
│ 电脑 worker mvp/worker/agent.py              │
│  - fs.list / fs.read / fs.write / fs.edit    │
│  - shell.run                                 │
│  - 只允许授权根目录及其子目录                 │
│  - 断线自动重连（1s → 30s 退避）             │
└─────────────────────────────────────────────┘
```

### 4.2 请求 / 事件流

```text
POST /api/runs
  → 校验设备与工作区（必须在设备授权根目录内）
  → 创建 Run(planning)，保存 cluster 快照（含 device/workspace）
  → 单模型：直接一次模型调用
  → 多模型：Planner 产出任务 DAG → 落库 → run.planned
  → Scheduler 找依赖已满足的任务 → 并发执行
  → 每个任务：Agent loop（模型 → 工具 → 观察 → 下一步）
  → 工具通过 WebSocket RPC 路由到设备
  → 汇总 → run.completed
```

事件通过 `EventBus.publish` 写入 `events` 表，并通过 SSE `/api/runs/{id}/events` 推送。流式 token delta 标记 `transient`，不落库。

### 4.3 运行生命周期

```text
Run:   planning → running → succeeded / failed / cancelled
                   ↘ cancelling → cancelled

Task:  pending → ready → running → done / failed / skipped
```

- 任务成功后保留 `checkpoint=None`
- 任务执行中每一步写 `tasks.checkpoint`（messages / observations / next_step）
- 进程重启：`recover_interrupted_runs()` 把 `planning/running` 的运行重新拉起，已完成的步骤不重跑
- `cancelling` 的运行在恢复时直接置为 `cancelled`

### 4.4 三种模式

1. **单模型直答**：`cluster_mode=false` 或集群只有 1 个模型；`_execute_direct`
2. **集群模式**：Planner 拆 DAG，多 Agent 并行
3. **专家团**：对话中的后续消息走 `/api/runs/{id}/messages`；主理人要么直接回复（不调用工具），要么调用 `expert_action`：
   - `action=team`：起 1–5 个专家任务
   - `action=guide`：给正在运行的任务追加指导
   - `reply` 字段：展示给用户的说明，不是动作

### 4.5 恢复与上下文压缩

- checkpoint 存在 `tasks.checkpoint` JSON：模型消息、工具观察、下一步编号
- 启动恢复只重置未完成任务，不重跑已完成任务
- 上下文压缩 `_context_messages()`：保留头尾，中间替换为摘要；**原始消息仍在 checkpoint/数据库**
- 压缩触发阈值：`CONTEXT_COMPACT_TOKENS`（默认 40000）
- 压缩会发 `context.compacted` 事件

---

## 5. 目录与文件职责

```text
集群/
├─ README.md                     # 使用说明、功能、部署、环境变量
├─ HANDOVER.md                   # 本文件
├─ start.cmd / start.sh          # 启动服务器
├─ start-worker.cmd              # Windows 一键启动 worker
├─ docker-compose.yml            # 本地 Docker
├─ docker-compose.cloud.yml      # 云端：cluster + Caddy
├─ deploy/
│  ├─ Caddyfile                  # 自动 HTTPS 反代
│  └─ .env.cloud.example
├─ docs/
│  ├─ 架构设计.md                 # 组件、协议、可靠性、路线图
│  ├─ 远程访问与部署.md            # 局域网 / Tailscale / 隧道 / VPS 评估
│  ├─ cloud-deployment.md        # 云端 + 设备配对 + 安全
│  ├─ tailscale.md               # 手机接入、serve、ACL、限制
│  └─ agent-cluster-research-2026-11.md
└─ mvp/
   ├─ run.py                     # 入口：打印地址、启动 uvicorn
   ├─ requirements.txt
   ├─ Dockerfile                 # 只复制 app/static/run.py，不含 worker
   ├─ static/index.html          # 单页控制台（CSS + JS 全内联）
   ├─ app/                       # 服务器
   └─ worker/agent.py            # 设备端 worker
```

---

## 6. 后端模块

### 6.1 `mvp/app/main.py`（53 KB）

FastAPI 入口，负责：

- 认证中间件 `require_auth`
- 登录 / 会话 / 撤销
- 运行创建与校验（设备在线、工作区包含关系）
- SSE 事件流 `/api/runs/{id}/events`
- 设备 WS `/api/worker/ws`
- 目录浏览 `/api/devices/{id}/browse`
- 网络探测 / 二维码
- 模型供应商、MCP、集群、评测、运行、产物等 CRUD

几个关键点：

- `create_run` 在创建时把 `device` 快照写进 `run.cluster`，之后重跑使用快照
- 工作区校验用跨平台词法比较 `_path_within`，不依赖服务器本机文件系统（云端 Linux + Windows worker 也能用）
- `browse_device` 用 `_join_relative` 生成设备风格路径，`dirs_only=True` 只取目录
- `stream_events` 会先回放历史，再发 `event: snapshot` 和 `event: live`
- 静态首页 `/` 不要求登录；未登录时页面自己弹登录框

### 6.2 `mvp/app/orchestrator.py`（90 KB，核心）

负责整个运行生命周期：

| 函数 | 作用 |
|---|---|
| `front_chat` | 专家团主理人处理后续消息 |
| `_lead_decision` / `_expert_tool_schema` | 主理人 `expert_action` 决策 |
| `_run_round` / `_summarize_round` | 一轮专家任务 |
| `_plan` / `_llm_plan` / `_heuristic_plan` | 生成任务 DAG |
| `_schedule` | 按依赖和并发执行 |
| `_execute` / `_agent_loop` | 单个任务的多步工具循环 |
| `_save_checkpoint` / `_clear_checkpoint` | 任务断点 |
| `recover_interrupted_runs` | 进程重启恢复 |
| `_context_messages` | 上下文压缩视图 |
| `_check_budget` / `_account` | 预算与用量 |
| `_finish` / `_finalize` | 汇总与收尾 |

注意：

- 这是**单进程 asyncio** 设计；`DeviceHub`、审批 future 都在内存
- 不要在任务里做阻塞调用；工具层已经是 async
- 修改 checkpoint 结构要向后兼容旧运行

### 6.3 `mvp/app/store.py`（46 KB）

SQLite 持久化，`SCHEMA` 里是全部建表语句。

关键方法：

- 运行/任务：`load_run_state`、`update_run`、`update_task`
- 事件：`append_event`、`get_events`、`last_event_seq`
- 设备：`create_device`、`get_device_by_token_hash`、`update_device`、`list_devices`
- 会话：`create_session`、`get_session`、`touch_session`、`list_sessions`、`delete_session`、`delete_other_sessions`、`purge_sessions`
- 设置：`get_app_setting` / `set_app_setting`

注意：

- 没有 Alembic；新增表用 `CREATE TABLE IF NOT EXISTS`，新增列要在初始化时做幂等迁移（参考 `tasks.checkpoint`、`_migrate_providers`）
- JSON 字段以 TEXT 存，读取时解析

### 6.4 `mvp/app/devices.py`（8 KB）

- `DeviceHub`：在线连接注册表
- `_WorkerConnection`：单个设备的 RPC future
- `wait_online`：设备离线时等待重连
- `DeviceDisconnected`：区分「连接断了」和「worker 返回错误」
- `RemoteWorkspace`：把文件/命令工具适配到远程设备
- `REMOTE_TOOL_OPS`：`list_files/read_file/write_file/edit_file/run_command` → `fs.* / shell.run`

掉线语义：

- 调用前设备离线：最多等 `DEVICE_WAIT_SEC`（默认 60 秒）
- 调用中断线：`fs.list` / `fs.read` 重连后重试一次；写文件/命令不重试，避免重复执行
- 写/命令断开时错误会提示「可能已执行，请先检查结果」

### 6.5 `mvp/worker/agent.py`（13.6 KB）

设备端：

- 用 `websockets` 主动连 `ws(s)://server/api/worker/ws`
- token 放在 `Authorization: Bearer` 头
- `hello` 上报名称、平台、授权根目录
- 处理 `call` 消息，返回 `result`
- 每 20 秒发心跳
- 断线后指数退避重连（1s → 30s）

安全边界：

- `Worker.resolve()` 要求 workspace 是授权根目录**或其子目录**
- `target` 必须仍在 workspace 内
- 危险命令 DENY 列表（弱，不是沙箱）
- 文件读写大小限制：读 200 KB、写 1 MB、命令输出 200 KB
- 命令超时最大 600 秒

### 6.6 其他模块

| 文件 | 职责 |
|---|---|
| `tools.py` | 工具注册表、本地文件/命令、远程路由、结果截断（8000 字符） |
| `approvals.py` | 命令审批 future、`scope=once/run`、超时、取消 |
| `bus.py` | 进程内事件 fan-out + SQLite 事件日志 |
| `config.py` | 环境变量、数据目录、首次生成 `ACCESS_TOKEN` |
| `network.py` | Tailscale / 局域网地址探测、推荐地址、10 秒缓存 |
| `providers.py` | 供应商预设、Key 掩码、模型拉取、连接测试、诊断 |
| `llm.py` | OpenAI Chat / Responses / Anthropic 兼容客户端、流式、reasoning |
| `mcp.py` | MCP 服务器管理、工具注册、stdio/SSE/HTTP |
| `clusters.py` | 集群快照构建 |
| `routing.py` | 领域评分与模型路由 |
| `evaluation.py` | 模型实测评分套件 |
| `models.py` | Pydantic 请求模型 + dataclass 运行状态 |

---

## 7. HTTP / WebSocket 接口

### 7.1 认证

```text
POST   /api/auth/login                  {token, label?}
GET    /api/auth/session                {authenticated, session?, expires_in_days}
POST   /api/auth/logout
GET    /api/auth/sessions
DELETE /api/auth/sessions/{id}
POST   /api/auth/sessions/revoke-others
```

公开路径（不需要登录）：

```text
/api/health
/api/auth/login
/api/auth/session
/api/auth/logout
```

其余 `/api` 全部要求会话 cookie，或 `Authorization: Bearer <ACCESS_TOKEN>`。

### 7.2 运行

```text
POST   /api/runs
GET    /api/runs
GET    /api/runs/{id}
POST   /api/runs/{id}/messages          # 专家团后续消息（流式）
POST   /api/runs/{id}/cancel
POST   /api/runs/{id}/resume
POST   /api/runs/{id}/rerun
POST   /api/runs/{id}/tasks/{task_id}/retry
DELETE /api/runs/{id}
GET    /api/runs/{id}/events            # SSE
GET    /api/runs/{id}/artifacts
GET    /api/runs/{id}/artifacts/{name}
PATCH  /api/runs/{id}/messages/{message_id}
DELETE /api/runs/{id}/messages/{message_id}
POST   /api/runs/{id}/branch
```

`POST /api/runs` 请求：

```json
{
  "goal": "...",
  "budget": {"max_steps": 60, "max_usd": 1.0, "max_tokens": 2000000, "deadline_sec": 3600},
  "cluster_id": null,
  "cluster_mode": true,
  "device_id": "dev_xxx",
  "workspace": "C:\\work\\project"
}
```

### 7.3 设备 / 网络

```text
GET    /api/devices
POST   /api/devices                     {name} → {device, token}
DELETE /api/devices/{id}
GET    /api/devices/{id}/browse?workspace=...&path=...
WS     /api/worker/ws?token=...          # 也支持 Authorization 头
GET    /api/network
GET    /api/network/qr?url=...
```

`browse` 返回：

```json
{
  "workspace": "C:\\work",
  "path": "sub/dir",
  "parent": "sub",
  "absolute": "C:\\work\\sub\\dir",
  "entries": [{"name": "child", "path": "sub/dir/child"}],
  "truncated": false
}
```

### 7.4 配置类

```text
GET/POST /api/settings
GET/POST /api/providers ...（含 activate/enabled/default/delete/test/models/diagnose/evaluate）
GET/POST/DELETE /api/clusters ...
GET/POST/DELETE /api/mcp/servers ...（start/stop/test）
GET/POST /api/approvals
GET      /api/meta
GET      /api/health
```

### 7.5 SSE 事件

帧类型：

```text
id: <seq>
data: {...}

event: snapshot
data: <完整 run>

event: live
data: {}

event: end
data: {}
```

持久事件类型：

```text
run.created / run.planned / run.status / run.completed / run.failed
run.cancelled / run.cancelling / run.recovered / run.resumed
task.ready / task.started / task.completed / task.failed / task.retried
tool.called / tool.result / tool.rejected
tool.approval_required / tool.approval_resolved / tool.approval_timeout
context.compacted
```

`transient` 事件（流式 token delta）不落库、无 `seq`。

---

## 8. 数据模型

| 表 | 关键字段 | 说明 |
|---|---|---|
| `runs` | `budget`, `usage`, `cluster`(JSON), `status`, `deadline_at` | `cluster` 里含 `device` 快照和模型列表 |
| `tasks` | `depends_on`(JSON), `status`, `checkpoint`(JSON), `attempts`, `steps`, `tokens` | checkpoint 用于恢复 |
| `messages` | `role`, `kind`, `content`, `meta`(JSON) | 对话与 goal/result 消息 |
| `events` | `seq`, `type`, `payload`(JSON) | 事件回放 |
| `devices` | `token_hash`, `platform`, `workspaces`(JSON), `last_seen`, `enabled` | worker 凭据与授权根目录 |
| `sessions` | `token_hash`, `label`, `user_agent`, `ip`, `created_at`, `last_seen` | 控制器登录会话 |
| `app_settings` | `key`, `value` | workspace_dir / shell_enabled / command_approval / approval_timeout |
| `clusters` | `provider_ids`(JSON), `planner_id`, `is_default` | 集群预设 |
| `providers` | `base_url`, `model`, `api_key`, `auth_style`, `scores`(JSON), `group_id` | Key 存库，API 只返回掩码 |
| `evaluations` | `scores`/`results`/`errors`/`usage`(JSON) | 模型实测评分 |

`run.cluster.device` 结构：

```json
{"id": "dev_xxx", "name": "工作站", "workspace": "C:\\work\\project"}
```

---

## 9. 认证与安全

### 9.1 首次令牌

`config._ensure_access_token()`：

- 环境变量 `ACCESS_TOKEN` 优先
- 为空时生成 `secrets.token_urlsafe(32)`
- 追加写入 `mvp/.env`，设置 `os.chmod(600)`
- 终端打印一次
- 如果 `.env` 已存在 token 但环境没加载（缺 dotenv），会解析并复用

### 9.2 登录流程

```text
POST /api/auth/login {token}
  失败：401；同一 IP 15 分钟内失败 5 次 → 429
  成功：生成 32 字节随机 secret，sha256 后存 sessions
        Set-Cookie: cluster_session=<secret>
          HttpOnly; SameSite=Strict; Path=/
          Max-Age=SESSION_DAYS*86400（默认 180 天）
          Secure 当请求是 HTTPS 或 X-Forwarded-Proto=https
```

后续请求只带 cookie；服务端按 hash 查会话，更新 `last_seen`（节流 60 秒）。

### 9.3 CSRF

- cookie 鉴权下，非 GET/HEAD/OPTIONS 请求必须带：
  ```text
  X-Requested-With: XMLHttpRequest
  ```
- 前端 `api()` 和 `chatHeaders()` 都会带
- Bearer token 鉴权不受此限制（脚本用）

### 9.4 会话管理

设置 → 访问：

- 列出所有会话：浏览器 · 系统、最近使用、登录日期
- 当前设备标记
- 单台撤销 / 撤销其他 / 退出当前
- 撤销后该 cookie 立即 401

### 9.5 worker 凭据

- 每设备独立 token，`wbt_` 前缀
- 只存 sha256
- 可删除设备吊销
- WS 通过 `Authorization: Bearer` 传，不走 URL

### 9.6 安全边界

- 所有控制器共用一个 `ACCESS_TOKEN` 做首次登录；设备区分靠会话
- 改 `ACCESS_TOKEN` 只阻止新登录，**不会**让已有会话失效；泄露时还要撤销会话
- 登录限速在内存，重启清零；`X-Forwarded-For` 可伪造（仅影响限速）
- 局域网 HTTP 下 cookie 没有 Secure，是明文
- API 文档默认关闭；`ENABLE_API_DOCS=1` 后 `/docs`、`/redoc`、`/openapi.json` 仍需登录
- 本地与 worker 命令子进程不继承 `ACCESS_TOKEN`、`*_API_KEY`、`*_TOKEN`、`*_SECRET`、`*_PASSWORD`、`*_CREDENTIALS`、常见数据库 URL 和代理变量；这是按名单剔除，不是完整沙箱
- `fetch_url` 只访问解析到公网单播地址的 HTTP(S)，连接钉住已验证 IP，正文上限 1 MiB，不跟随重定向；它**不使用**环境代理（代理会绕过地址校验），需要代理时在出口网关做同等限制
- `web_search`、MCP、模型调用在服务器端，不走设备 token

---

## 10. 设备与远程工作区

### 10.1 配对

网页：设置 → 设备 → ＋ 配对设备 → 生成 token → 复制命令。

命令行：

```bash
python mvp/scripts/pair_device.py --server http://127.0.0.1:8000 --name "工作站" --access-token "<token>"
```

### 10.2 worker 协议

```text
客户端 → 服务端
  hello  {name, platform, workspaces[]}
  call   {id, op, args}

服务端 → 客户端
  welcome {device_id, name}
  call   {id, op, args}
  result {id, ok, result|error}
  heartbeat
```

op：

```text
fs.list  {workspace, path, depth<=4, dirs_only?}
fs.read  {workspace, path, max_bytes<=200000}
fs.write {workspace, path, content}
fs.edit  {workspace, path, old, new}
shell.run{workspace, command, cwd?, timeout<=600}
```

### 10.3 沙箱规则

- workspace 必须是授权根目录或其中子目录
- 目标必须是 workspace 内
- `..` / 绝对路径越界由 worker 拒绝
- 服务端创建任务前也做词法包含检查（跨平台，不依赖本机文件系统）

### 10.4 目录浏览

- 输入框旁「浏览…」或设备卡片「浏览」
- 只列目录，最多 400 条，按层 BFS
- 每台设备的上次目录存在 `localStorage.ai_device_workspaces`
- 选中的子目录会作为该次运行的 `workspace`

### 10.5 重连

- 调用前离线：等 `DEVICE_WAIT_SEC`（默认 60 秒）
- 只读操作断线重试一次
- 写文件/命令不重试，避免重复执行
- 运行中设备离线，工具调用会挂起等待，而不是立刻失败

---

## 11. 执行细节

### 11.1 审批

- 只覆盖 `run_command`（本地和远程）
- 默认开启：`COMMAND_APPROVAL=1`
- 审批请求进 `approvals._pending`，SSE 推 `tool.approval_required`
- 三个选择：拒绝 / 批准一次 / 本次运行都允许
- `scope=run` 存在内存 `_run_allowed`；删除该运行或进程重启后清除（运行结束时不会立即释放，属于无害的小泄漏）
- 超时默认 300 秒，视为拒绝
- 停止运行会取消该运行所有待审批

### 11.2 预算

- `max_steps`：总步数（默认 60）
- `max_usd`：美元预算（默认 1.0）
- `max_tokens`：token 预算（默认 2,000,000）
- `deadline_sec`：超时（默认 3600）
- 每个 LLM 调用后 `_account` 累计；超限发 `run.failed`

### 11.3 产物

- 写在 `mvp/data/artifacts/<run_id>/`
- 通过 `/api/runs/{id}/artifacts` 列表和下载
- 前端 `renderArtifacts` 直接 `<a>` 打开

### 11.4 上下文压缩

- 触发：估算 token 超过 `CONTEXT_COMPACT_TOKENS` 且消息 > 12
- 保留头 2 条 + 尾 10 条，中间变成摘要
- 原始消息保留在 checkpoint/数据库
- 事件 `context.compacted`

---

## 12. 前端

### 12.1 结构

单文件 `mvp/static/index.html`（约 227 KB），CSS 和 JS 全部内联，没有构建步骤。

主要区域：

```text
左侧栏     历史运行、设置入口、状态
主区       欢迎页 / 对话线程 / 任务 DAG / 日志 / 结果 / 产物
底部输入   目标、模型集群、设备、工作区、参数、发送
弹窗       登录、设置、确认、审批、目录浏览、模型详情
```

### 12.2 关键 JS

| 区域 | 函数 |
|---|---|
| 认证 | `ensureAuth`、`loginWithToken`、`tryLegacyLogin`、`saveToken`、`loadSessions`、`revokeSession`、`logoutDevice` |
| API | `api`（加 CSRF 头，401 弹登录） |
| 运行列表 | `loadRuns`、`renderHeader` |
| 对话 | `sendChat`、`handleChatEvent`、`renderMessagesUI` |
| 运行详情 | `applyRunData`、`refreshRun`、`renderTasks`、`renderResult`、`renderArtifacts` |
| SSE | `openStream`、`handleEvent`、`startPolling` |
| 设备 | `loadDevices`、`renderDeviceSelect`、`renderDeviceList` |
| 目录浏览 | `openWorkspaceBrowser`、`loadBrowse`、`renderBrowse`、`useBrowsedWorkspace` |
| 访问页 | `loadNetwork`、`renderAccess`、`renderAccessQr` |
| 设置 | `setSettingsTab`、`openProviders`、`renderProviderList`、`renderClusterList`、`loadMcpServers` |
| 审批 | `pollApprovals`、`showApproval`、`resolveApproval`、`startApprovalAlert` |

### 12.3 流式

- 首次消息：`POST /api/runs`，然后 `openStream(run_id)`
- 后续消息：`POST /api/runs/{id}/messages`，读 `data:` 帧，`handleChatEvent` 处理
- SSE 帧：`snapshot` 应用整份状态，`live` 后只应用增量，`end` 关闭
- `EventSource.onerror` 会主动 `closeStream()`、刷新一次状态，并切换到轮询降级（不是依赖浏览器自动重连）

### 12.4 移动端

- 断点 `max-width: 760px`
- 侧栏折叠、输入区 sticky、审批/工作区弹窗纵向排列
- 审批提醒：标题闪烁 + WebAudio 提示音

---

## 13. 配置

环境变量（`mvp/.env` 或进程环境）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8000` | 端口 |
| `DATA_DIR` | `mvp/data` | SQLite、产物、工作区 |
| `WORKSPACE_DIR` | `DATA_DIR/workspace` | 服务器本机工作区 |
| `ACCESS_TOKEN` | 自动生成 | 首次登录令牌 |
| `ENABLE_API_DOCS` | `0` | 设为 1 才开放 `/docs`、`/redoc`、`/openapi.json`（仍需登录） |
| `SESSION_DAYS` | `180` | 会话有效期 |
| `CORS_ORIGINS` | `*` | CORS |
| `LLM_BASE_URL` | 空 | OpenAI 兼容地址；为空则 mock |
| `LLM_API_KEY` | 空 | 模型 Key |
| `LLM_MODEL` | `gpt-4o-mini` | 默认模型 |
| `LLM_TIMEOUT` | `300` | 模型请求超时 |
| `LLM_PRICE_IN` / `LLM_PRICE_OUT` | `0.15` / `0.60` | 每百万 token 价格 |
| `TAVILY_API_KEY` / `BRAVE_API_KEY` / `SEARXNG_URL` | 空 | 搜索 |
| `ENABLE_SHELL` | `1` | 是否允许命令 |
| `SHELL_TIMEOUT` | `60` | 本地命令超时 |
| `COMMAND_MAX_OUTPUT` | `12000` | 命令输出截断 |
| `COMMAND_APPROVAL` | `1` | 命令审批 |
| `APPROVAL_TIMEOUT` | `300` | 审批超时 |
| `MAX_CONCURRENCY` | `4` | 并发任务数 |
| `MAX_STEPS_PER_TASK` | `10` | 单任务步数 |
| `CONTEXT_COMPACT_TOKENS` | `40000` | 压缩阈值 |
| `MAX_TOTAL_STEPS` | `60` | 运行总步数 |
| `MAX_TOKENS` | `2000000` | 运行 token 预算 |
| `MAX_USD` | `1.0` | 运行美元预算 |
| `DEADLINE_SEC` | `3600` | 运行超时 |
| `DEVICE_WAIT_SEC` | `60` | 设备重连等待（`devices.py` 读取） |
| `OPEN_BROWSER` | `0` | 启动后开浏览器 |
| `RELOAD` | `0` | uvicorn reload |

---

## 14. 部署与运维

### 14.1 本机

- `start.cmd` / `start.sh`
- 默认 `0.0.0.0:8000`
- 局域网手机访问：`http://<局域网IP>:8000`

### 14.2 本地 Docker

```bash
docker compose -f docker-compose.yml up -d --build
```

- 使用 `mvp/.env`
- 数据卷 `cluster-data`

### 14.3 云端

```bash
cp deploy/.env.cloud.example deploy/.env.cloud
# DOMAIN / ACME_EMAIL / ACCESS_TOKEN
docker compose --env-file deploy/.env.cloud -f docker-compose.cloud.yml up -d --build
```

- Caddy 自动 HTTPS，反代 `cluster:8000`
- Caddy 默认保留 Host 并设置 `X-Forwarded-*`
- 镜像只包含 `app/static/run.py`，不含 worker

### 14.4 Tailscale

见 `docs/tailscale.md`：

- 服务器和手机登录同一 tailnet
- `http://100.x.y.z:8000` 或 `tailscale serve --bg 8000`
- 不要开 Funnel
- 访问页会自动推荐地址并生成二维码

### 14.5 备份

最稳妥：停服务后复制

```powershell
Copy-Item mvp\data\cluster.db "backup\cluster-$(Get-Date -Format yyyyMMdd).db"
Copy-Item mvp\data\artifacts "backup\artifacts-$(Get-Date -Format yyyyMMdd)" -Recurse
Copy-Item mvp\data\workspace "backup\workspace-$(Get-Date -Format yyyyMMdd)" -Recurse
```

`.env` 单独备份（包含 `ACCESS_TOKEN`，注意保密）。

### 14.6 日志

- 服务端：stdout/stderr
- worker：stderr
- 事件日志在 SQLite `events` 表
- 设备 RPC 等待会打印 `[device] 等待设备 ... 重连`

---

## 15. 测试与验收

### 15.1 静态检查

```powershell
.\.venv\Scripts\python.exe -m compileall -q mvp\app mvp\worker mvp\scripts
```

JS 语法：

```powershell
@'
from pathlib import Path
import re
text = Path("mvp/static/index.html").read_text(encoding="utf-8")
m = re.search(r"<script>(.*)</script>", text, re.S)
Path("mvp/static/_check.js").write_text(m.group(1), encoding="utf-8")
'@ | .\.venv\Scripts\python.exe -
node --check mvp\static\_check.js
Remove-Item mvp\static\_check.js
```

### 15.2 smoke

```powershell
.\.venv\Scripts\python.exe mvp\scripts\smoke.py
```

mock 模式下应输出 `{"ok": true, ...}`。

### 15.3 手工验收清单

1. 启动服务 → 未登录访问 `/api/meta` 返回 401
2. 错误 token → 401；连续 5 次 → 429
3. 正确 token → 登录，`cluster_session` 在 `document.cookie` 中不可见
4. 无 CSRF 头的 POST → 403；带 `X-Requested-With` → 成功
5. 设置 → 访问 → 看到当前设备；撤销其他设备后对方 401
6. 配对 worker → 设备在线
7. 浏览目录 → 进入子目录 → 使用该目录
8. mock 运行 → 任务 DAG、事件、结果、产物
9. 开启审批 → 要求执行命令 → 批准一次 / 本次运行都允许
10. 运行中杀 worker → 工具调用等待；重启 worker 后继续
11. 运行中重启服务器 → 启动恢复，已完成任务不重跑
12. 刷新页面 → 会话保持；退出登录 → 回到登录页

### 15.4 自动化测试

已有 pytest 回归网，入口：

```powershell
.\.venv\Scripts\python.exe -m pip install -r mvp\requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

覆盖：登录/CSRF/限流/撤销、空与异常 `ACCESS_TOKEN`、API 文档鉴权、子进程环境隔离、SSE 分页与重连、JSON 列容错、运行删除级联、设备断连与非法 WS 帧、`fetch_url` 地址与大小限制。测试通过 `conftest.py` 在导入应用前设置临时 `DATA_DIR` 和 `ACCESS_TOKEN`，不会写真实库；跑完可对比 `mvp/.env` 与 `mvp/data` 的哈希确认。

尚未覆盖：不需要真实 LLM 的编排取消/调度锁（M2）、前端交互（M3）。

---

## 16. 已知问题与坑

1. **取消不终止远程命令**：`cancel` 只停服务器侧调度；worker 没有 cancel RPC，远程命令可能继续跑。
2. **同设备无并发上限**：多个运行可以同时写同一工作区，可能互相覆盖。建议加设备级队列。
3. **单进程**：`DeviceHub`、审批 future、EventBus 订阅都在内存；多副本需要 Redis/NATS。
4. **没有正式迁移框架**：新增列必须幂等处理。
5. **换 token 不会踢掉已有会话**：`ACCESS_TOKEN` 只影响新登录；泄露时还要撤销会话。
6. **登录限速在内存**：重启清零；`X-Forwarded-For` 可伪造（只影响限速）。
7. **局域网 HTTP 明文**：cookie 不能加 Secure；建议 Tailscale/HTTPS。
8. **uvicorn 默认不 reload**：改后端要重启；`RELOAD=1` 可开。
9. **`start.cmd` 的端口检测**用 `netstat | findstr ":8000"`，可能匹配 `18000`。
10. **前端单文件 227 KB**：没有构建和前端测试，改 UI 要小心。
11. **远程 shell 不是沙箱**：DENY 列表很弱；审批和独立账号/容器才是边界。
12. **Windows 控制台编码**：部分中文在 cp936 控制台会乱码；`start-worker.cmd` 已 `chcp 65001`。
13. **首次 token 生成有导入副作用**：任何导入 `app.config` 的脚本在未设 `ACCESS_TOKEN` 时都会写 `mvp/.env`。测试请显式设环境变量。
14. **运行详情只在有 `cluster.name` 时显示设备**：`applyRunData` 里设备文案在 `if (cluster.name)` 内，历史列表也没有设备标记（P1 要修）。
15. **工具结果截断**：`tools.call` 超过 8000 字符会截断，大文件读取要注意。
16. **没有产物打包下载**：只能逐个文件下载。
17. **真实库 `devices=0`**：远程链路需要先配对一台设备才能验收。
18. **`scope=run` 审批授权释放不彻底**：运行结束后仍留在内存，删除运行或重启才清理；影响很小，可在 `_finish` 里加 `forget_run`。

> **2026-11-17 补充审计**：另发现 P0 × 1、P1 × 17、P2 × 约 47 条尚未记录的问题，详见 `docs/2026-11-17-审计报告.md`。
>
> **已完成（M0 + M1）**：SEC-01/02/03（空/异常 `ACCESS_TOKEN` 绕过与 Unicode 500）、SEC-04（文档端点默认关闭且开启后需登录）、SEC-07（子进程环境变量剔除）、SEC-08（`fetch_url` SSRF、DNS rebinding、重定向与响应上限）、DATA-01（删除运行级联 messages）、DATA-02（JSON 列容错）、DATA-03（SSE 分页回放与 `Last-Event-ID`）、DATA-04/05（设备删除断连与 worker 非法帧容错）。回归见 `mvp/tests`，当前 147 passed；跑测试前后 `mvp/.env` 与 `mvp/data` 哈希不变。
>
> **下一步**：`docs/superpowers/plans/2026-11-17-quality-hardening.md` 的 Task 9–12（M2 调度/取消/checkpoint），随后 M3 前端与 M4 可见性。

---

## 17. 路线图

建议顺序：**质量硬化 M0–M1（已完成）→ M2 运行/调度 → M3 前端状态 → M4 P1 可见性 → P2 → P3 → P4 → P5**。先补 pytest 地基和鉴权/数据正确性，避免后续功能验收反复返工。

### P1 可信回溯（优先）

1. **运行详情无条件显示设备与目录**
   - 位置：`static/index.html` `applyRunData`
   - 现在设备文案嵌在 `if (cluster.name)` 里；把它移出来，用 `r.cluster.device`
2. **历史列表设备标记**
   - 位置：`loadRuns` 的 `run-item` 模板
   - 显示设备名缩写；没有设备显示「本机」
3. **工具事件带目标路径**
   - 位置：`tools.call` / `orchestrator` 的 `tool.called` payload
   - 远程调用记录 workspace + 相对路径
4. **产物下载手机验证**
   - 已有 `/api/runs/{id}/artifacts/{name}`，确认移动端新窗口/下载行为

验收：随便点开一条历史，能回答「哪台机器、哪个目录、改了什么」。

### P2 手机闭环

1. 页面隐藏时的 Web Notification（需要用户授权）
2. SSE 断流后的明确提示与状态恢复
3. PWA manifest（可选，添加到主屏）
4. 审批弹窗/设备选择器再压一轮窄屏

验收：手机锁屏再回来，审批不漏、页面自动恢复。

### P3 可靠性与并发

1. 同一设备并发上限（默认 1）+ 排队
2. 取消运行时给 worker 发 cancel，终止远程进程
3. 服务器重启后的前端恢复提示
4. 设备心跳超时判定（现在只靠 WS 断开）

验收：两个任务打到同一电脑不互相踩目录；停止后远程进程确实结束。

### P4 部署与安全

1. 云端 `docker-compose.cloud.yml` 实跑（Caddy + WS + 转发头）
2. 备份/恢复脚本
3. Tailscale serve 实测清单
4. 未登录 API 的 401 覆盖检查（新增路由别忘了）

### P5 易用性

1. Windows worker 开机自启/托盘
2. 最近使用目录
3. 一键复制连接诊断信息

---

## 18. 接手第一天做什么

1. 读本文件 + `README.md` + `docs/架构设计.md`
2. 确认环境：
   ```powershell
   .\.venv\Scripts\python.exe --version   # 3.14.1
   node -v                                # v24.19.0
   ```
3. 跑静态检查和 smoke
4. 启动服务，登录一次，检查会话列表
5. 配对一台 worker，浏览目录，跑一个 mock 任务
6. 先补 pytest 骨架（认证 + devices browse + mock run）
7. 再开始 P1：先改运行详情显示，再改历史列表

---

## 19. 关键代码索引

| 想改什么 | 先看哪里 |
|---|---|
| 新增 API / 改鉴权 | `mvp/app/main.py` |
| 任务规划、调度、恢复 | `mvp/app/orchestrator.py` |
| 数据表 / 会话 / 事件 | `mvp/app/store.py` |
| 设备连接、RPC、重连 | `mvp/app/devices.py` |
| 设备端文件/命令 | `mvp/worker/agent.py` |
| 工具注册与路由 | `mvp/app/tools.py` |
| 审批 | `mvp/app/approvals.py` |
| 模型供应商 | `mvp/app/providers.py`、`llm.py` |
| MCP | `mvp/app/mcp.py` |
| 地址探测 / 二维码 | `mvp/app/network.py`、`main.py` |
| UI 全部 | `mvp/static/index.html` |
| 启动入口 | `mvp/run.py`、`start.cmd`、`start-worker.cmd` |
| 部署 | `docker-compose*.yml`、`deploy/Caddyfile` |
| 认证说明 | `README.md`、`docs/cloud-deployment.md`、`docs/tailscale.md` |

---

## 20. 术语

| 术语 | 含义 |
|---|---|
| Run | 一次完整任务运行 |
| Task | Run 里的一个 DAG 节点 |
| Agent | 能力角色（researcher / analyst / writer / reviewer 等） |
| 集群 | 一组参与路由的模型供应商 |
| 主理人 | 专家团对话的 lead，用 `expert_action` 决策 |
| 设备 / worker | 一台运行 `mvp.worker.agent` 的电脑 |
| 工作区 | 设备上被授权的目录；可以是根目录的子目录 |
| 会话 | 控制器登录后拿到的 HttpOnly cookie |
| checkpoint | 任务级断点，存在 `tasks.checkpoint` |
| 事件 | Run/Task/Tool 的状态变化，存 `events` 表并通过 SSE 推送 |

---

## 21. 给新 harness 的开场提示（可直接粘贴）

```text
工作区：C:\Users\HUAWEI\Desktop\work\集群

先完整阅读 HANDOVER.md，再读 README.md；不要跳过第 16 节「已知问题与坑」。

这是一个单人使用的多 Agent 集群：FastAPI + SQLite + 单页前端 + 设备 worker。

硬约束：
- 不要自动 git commit / push
- 不要把服务器或 worker 留在后台，交付前停掉
- 不要修改或提交 mvp/.env、mvp/data
- 改后端要重启服务验证；改前端要跑 node --check
- 新增 /api 路由默认需要登录，别绕过认证中间件

第一个任务：按 HANDOVER.md 的 P1，先让运行详情和历史无条件显示设备与目录。
先给计划和验收标准，再动代码。
```

---

## 附：交接验证记录（2026-11-15）

```text
git status                     代码提交干净（c4cbd1a）；HANDOVER.md 为新增未提交
git log -1                     c4cbd1a 优化了登录认证
Python                         3.14.1
Node                           v24.19.0
依赖                           fastapi / websockets / segno 可导入
8000 端口                      无服务监听
mvp/.env                       存在，仅含 ACCESS_TOKEN
mvp/data/cluster.db            存在，12 runs / 4 providers / 2 clusters
```
