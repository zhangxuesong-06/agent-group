# 云端部署与远程设备工作区

目标：服务器部署在云端；任何经认证的浏览器/设备登入后，可以指定一台**在线设备**，让 Agent 只在该设备授权的工作区内读写文件、执行命令。

## 架构

```text
浏览器 / 手机 / 其他电脑
        │  HTTPS + ACCESS_TOKEN
        ▼
┌──────────────────────────────┐
│ 云端服务器                    │
│ FastAPI + SQLite + DeviceHub │
│  - 对话/任务/审批/事件流       │
│  - 设备注册与在线状态          │
│  - 工具调用路由                │
└──────────┬───────────────────┘
           │  WebSocket（设备主动外连，wss）
           ▼
┌──────────────────────────────┐
│ 你的电脑 / 工作站              │
│ mvp.worker.agent             │
│  - 只暴露授权 workspace       │
│  - 执行 fs.* / shell.run      │
│  - 不需要公网 IP / 端口映射    │
└──────────────────────────────┘
```

- 云端只暴露 `80/443`；设备端**主动外连**，不要开放入站端口。
- 浏览器和服务器之间用 `ACCESS_TOKEN`；设备与服务器之间用**每设备独立 token**。
- 任务创建时选择设备 + 工作区；文件/命令工具通过 WebSocket RPC 转发到该设备。
- 没有选择设备时，行为与以前一致：在服务器的本地 workspace 执行。
- 不想要公网域名时，可以只用 Tailscale 组网，见 [`tailscale.md`](tailscale.md)。

## 1. 云端部署

要求：一台有公网 IP 的 Linux 服务器、一个域名（DNS A 记录指向服务器）、Docker + Docker Compose。

```bash
git clone <your-repo> agent-group
cd agent-group

cp deploy/.env.cloud.example deploy/.env.cloud
# 编辑 deploy/.env.cloud：
#   DOMAIN=cluster.example.com
#   ACME_EMAIL=you@example.com
#   ACCESS_TOKEN=<足够长的随机串>

docker compose --env-file deploy/.env.cloud \
  -f docker-compose.cloud.yml up -d --build
```

Caddy 会自动申请 HTTPS 证书；访问 `https://cluster.example.com`。

生成 `ACCESS_TOKEN`：

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

数据保存在 Docker volume `cluster-data`（SQLite、产物、workspace）。备份：

```bash
docker run --rm -v agent-group_cluster-data:/data -v "$PWD":/backup \
  alpine tar czf /backup/cluster-data.tar.gz /data
```

## 2. 配对设备

### 方式 A：网页里配对（推荐）

1. 打开网页 → 左下角「设置」→「设备」
2. 点「＋ 配对设备」，输入设备名称
3. 点「生成 token」，复制页面给出的 worker 命令
4. token 只显示一次；如果丢了，删除设备重新配对

### 方式 B：命令行配对

在能访问服务器的机器上（或直接在服务器上）执行：

```bash
python mvp/scripts/pair_device.py \
  --server https://cluster.example.com \
  --name "我的工作站" \
  --access-token "<ACCESS_TOKEN>"
```

它会输出：

- `device_id`
- 设备 token（只显示一次）
- 一条在设备上运行的命令

也可以在网页里用 API：

```bash
curl -X POST https://cluster.example.com/api/devices \
  -H "Authorization: Bearer <ACCESS_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"name":"我的工作站"}'
```

## 3. 在设备上启动 worker

设备需要 Python 3.10+。只需要 `websockets`：

```bash
pip install "websockets>=13"
```

把 `mvp/worker/agent.py` 复制到设备，或直接 clone 仓库。运行：

```bash
python -m mvp.worker.agent \
  --server https://cluster.example.com \
  --token "<设备 token>" \
  --name "我的工作站" \
  --workspace "C:\work\project-a" \
  --workspace "D:\research"
```

- `--workspace` 可重复，最多 32 个；Agent 只能访问这些根目录及其子目录。
- 默认允许命令执行；文件-only 设备加 `--no-shell`。
- worker 断线会自动重连（1s → 30s 退避）。
- 不需要在路由器上开端口；设备只发起 outbound `wss`。

## 4. 在网页里使用

1. 打开 `https://cluster.example.com`，输入 `ACCESS_TOKEN`
2. 底部输入框上方选择设备（在线设备会显示名称；离线会标注）
3. 如果设备上报了多个工作区，再选择具体工作区
4. 正常发任务；文件读写、`run_command` 会在该设备的工作区执行

运行详情里会显示 `设备名:工作区路径`。没有选择设备时显示「服务器本机」。

## 5. 权限与安全

- **必须设置 `ACCESS_TOKEN`**；为空时 API 对同网络所有人开放。
- **必须用 HTTPS/WSS**；不要把 `8000` 直接暴露到公网。
- 每台设备一个 token；泄露后在服务器删除该设备即可：
  ```bash
  curl -X DELETE https://cluster.example.com/api/devices/<device_id> \
    -H "Authorization: Bearer <ACCESS_TOKEN>"
  ```
- worker 只接受授权 workspace 内的路径，拒绝 `..` 越界。
- 命令执行仍会经过服务器端的审批 UI（如果 `COMMAND_APPROVAL=1`）；worker 端还有一层危险命令拦截。
- worker 的 shell 不是强沙箱。要执行不可信命令时，建议：
  - 用独立系统账号运行 worker；
  - 或把 worker 放进容器/虚拟机，只挂载工作区；
  - 或者用 `--no-shell` 只允许文件操作。

## 6. 当前限制

- 单用户单 `ACCESS_TOKEN`；多用户/RBAC 还没做。
- 设备在线状态和 RPC 在单进程内存里；多副本部署需要 Redis/NATS 或数据库租约。
- SQLite 适合单机/小规模；多用户或高并发建议迁移 PostgreSQL。
- 任务运行中设备掉线时，该工具调用会失败；设备重新上线后可继续未完成任务。
- 远程 worker 目前支持文件读写、编辑、列目录、执行命令；`web_search`、MCP、模型调用仍在服务器端。

## 7. 故障排查

| 现象 | 检查 |
|---|---|
| 设备显示离线 | 设备 worker 是否在运行；`wss://域名/api/worker/ws` 是否被代理；token 是否被撤销 |
| 创建任务返回 409 | 设备离线或工作区不在授权列表 |
| 工具报“路径超出工作区” | 请求的路径不在 `--workspace` 根目录内 |
| 命令被拒绝 | 服务器审批拒绝，或 worker 危险命令拦截，或 `--no-shell` |
| 网页能开但 WebSocket 失败 | 反向代理未转发 Upgrade 头；Caddy 默认支持，Nginx 需要显式配置 |
| HTTPS 证书失败 | 域名 DNS 未指向服务器，或 80 端口被占用/未放行 |
