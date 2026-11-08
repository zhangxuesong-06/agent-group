# 用 Tailscale 让手机安全接入

目标：不暴露公网端口，让手机（或平板、笔记本）在任何网络下都能打开这个集群的网页；真正的文件和命令仍在已配对的电脑 worker 上执行。

Tailscale 是一个基于 WireGuard 的私有组网工具。所有设备登录同一个 tailnet 后，会拿到 `100.x.y.z` 的私有地址，彼此可以直接访问，流量端到端加密。

## 1. 角色划分

```text
手机（控制器）                服务器（Hub）                 电脑（Worker）
浏览器打开网页        ──►     FastAPI + SQLite      ◄──      mvp.worker.agent
发任务 / 审批 / 看结果         对话、任务、事件流            读写文件、执行命令
        │                        ▲                             ▲
        └──────── Tailscale 私有网络（同一 tailnet）──────────┘
```

- **手机**只当控制器，不跑 worker，不执行任务。
- **服务器**负责调度、持久化、审批。
- **电脑**主动连服务器，任务在电脑自己的工作区里跑。

三台设备都要装 Tailscale 并登录同一个账号（或同一个 tailnet）。

## 2. 在服务器上安装 Tailscale

### Windows

1. 打开 <https://tailscale.com/download/windows> 下载安装。
2. 安装后登录。
3. 确认状态：

```powershell
tailscale status
tailscale ip -4
```

如果 `tailscale` 命令找不到，用完整路径：

```powershell
& "C:\Program Files\Tailscale\tailscale.exe" status
```

### macOS

从 App Store 或 <https://tailscale.com/download/mac> 安装，登录后从菜单栏确认已连接。

### Linux

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4
```

> 如果服务器是云主机，Tailscale 不需要开放任何入站安全组端口。只要允许出站 UDP 41641（或让 Tailscale 走 DERP 中继）即可。

## 3. 确认服务器监听地址

网页要能被 Tailscale 地址访问，服务必须监听 `0.0.0.0`，不能只监听 `127.0.0.1`。

```bash
# Linux / macOS
HOST=0.0.0.0 PORT=8000 python mvp/run.py
```

```powershell
# Windows PowerShell
$env:HOST="0.0.0.0"; $env:PORT="8000"; .\.venv\Scripts\python.exe mvp\run.py
```

JSON 里的 `HOST` 也可以直接改，默认已经是 `0.0.0.0`。

## 4. 三种访问方式

打开网页 → 左下角「设置」→「访问」，页面会自动列出当前可用地址，并给出二维码。

### 方式 A：Tailscale IP（最简单）

```text
http://100.x.y.z:8000
```

把 `100.x.y.z` 换成服务器 `tailscale ip -4` 的输出。手机扫码或在浏览器输入即可。

- 优点：不用配证书，不依赖域名。
- 缺点：HTTP 明文；只在 tailnet 内可见（这通常正是你要的）。

### 方式 B：Tailscale HTTPS（推荐长期使用）

Tailscale 可以为你的机器签发 `*.ts.net` 证书，并自动续期：

1. 先在 Tailscale 管理后台 → DNS 中启用 **MagicDNS** 和 **HTTPS Certificates**。
2. 在服务器上执行：

```bash
sudo tailscale serve --bg 8000
# 查看状态
tailscale serve status
```

Windows 用管理员 PowerShell，把 `sudo` 去掉：

```powershell
tailscale serve --bg 8000
```

然后在手机打开：

```text
https://<机器名>.<tailnet>.ts.net
```

例如 `https://my-pc.tail1234.ts.net`。

- `tailscale serve` 只对 tailnet 内设备开放，不会公开到公网。
- 停用：

```bash
sudo tailscale serve --bg off
```

### 方式 C：局域网地址

手机和服务器在同一个 Wi-Fi 时，也可以直接用 `http://192.168.x.x:8000`。适合临时调试。

## 5. 手机接入

1. 安装 Tailscale：
   - iOS：App Store 搜索 Tailscale
   - Android：Google Play 或 F-Droid 搜索 Tailscale
2. 登录**同一个账号**。
3. 确认开关是 Connected。
4. 打开网页地址，输入 `ACCESS_TOKEN`。
5. 在底部输入框选择一台在线电脑作为执行设备。

手机浏览器第一次输入 token 后会写入 cookie 和 localStorage，之后不用重复输入。

## 6. 让电脑 worker 用 Tailscale 地址连接

在电脑上运行 worker 时，`--server` 用服务器在 tailnet 内的地址：

```bash
python -m mvp.worker.agent \
  --server http://100.x.y.z:8000 \
  --token "<设备 token>" \
  --name "我的工作站" \
  --workspace "C:\work\project"
```

如果服务器启用了 `tailscale serve`，也可以用 HTTPS：

```bash
python -m mvp.worker.agent \
  --server https://my-pc.tail1234.ts.net \
  --token "<设备 token>" \
  --name "我的工作站" \
  --workspace "C:\work\project"
```

worker 是主动外连的，电脑不需要开放任何端口。

设备 token 在网页「设置 → 设备 → ＋ 配对设备」里生成。

## 7. 安全建议

### 一定要设置 ACCESS_TOKEN

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

不设 token 时，同一 tailnet 内任何人都能打开并操作这个集群。

### 不要启用 Funnel

`tailscale funnel` 会把服务公开到公网。除非你明确需要公开访问，否则不要执行：

```bash
# 不要执行
sudo tailscale funnel 8000
```

只想给 tailnet 内设备用时，`tailscale serve` 就够了。

### 用 ACL 限制谁能访问

Tailscale 管理后台 → Access Controls，可以只允许你自己的设备互相访问。单人使用时，默认的 `autogroup:member` 规则通常已经够用；如果 tailnet 里有其他人，建议显式限制：

```jsonc
{
  "acls": [
    {
      "action": "accept",
      "src": ["your-email@example.com"],
      "dst": ["*:*"]
    }
  ]
}
```

### 设备丢失时

1. Tailscale 管理后台 → Machines，移除丢失的设备。
2. 网页「设置 → 设备」里删除对应 worker，吊销它的 token。

## 8. 手机能不能跑 worker？

**不建议，且多数情况下不可行。**

- iOS 不允许后台长期运行进程，worker 会在切后台后被挂起。
- Android 可以用 Termux 实验：

```bash
pkg install python
pip install "websockets>=13"
python -m mvp.worker.agent \
  --server http://100.x.y.z:8000 \
  --token "<设备 token>" \
  --name "手机" \
  --workspace ~/work
```

但 Termux 也会被系统省电策略杀后台，需要手动加白名单。手机更适合当控制器。

## 9. 故障排查

| 现象 | 排查 |
| --- | --- |
| 网页打不开 | 服务器是否监听 `0.0.0.0`；`tailscale status` 是否 Connected |
| 能 ping 通但网页超时 | 防火墙是否放行 8000；云主机安全组只影响公网，Tailscale 走私有网卡 |
| 二维码地址是 `127.0.0.1` | 说明没检测到 Tailscale 和局域网地址；检查服务是否在运行、`tailscale up` 是否成功 |
| 手机提示证书错误 | 用的是 `https://...ts.net` 但没执行 `tailscale serve`，或证书还没签发完，等几十秒重试 |
| worker 连不上 | `--server` 地址在 worker 那台机器上能否 `curl` 通；token 是否被删除 |
| 改了配置页面没更新 | 「设置 → 访问」点「重新检测」；后端有 10 秒缓存 |

查看后端识别的网络信息：

```bash
curl http://127.0.0.1:8000/api/network
```

返回里 `tailscale.installed`、`tailscale.online`、`urls`、`recommended` 可以直接对照。

## 10. 和公网部署的关系

- **只想自己用**：Tailscale + `tailscale serve` 就够了，不需要域名、不需要 Caddy、不需要开放 80/443。
- **需要给不在 tailnet 里的设备用**：再按 [cloud-deployment.md](cloud-deployment.md) 用 Caddy + 域名部署。
- 两种方式可以并存：服务器同时监听 Tailscale 地址和公网域名，网页里的 `ACCESS_TOKEN` 是同一套。
