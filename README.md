<div align="center">

<img src="./desktop/assets/app.svg" width="96" height="96" alt="WorkBuddy2API">

# WorkBuddy2API

**把 WorkBuddy / CodeBuddy 桌面端登录态，变成你本机可用的 OpenAI / Anthropic 兼容 API**

单文件 EXE 双击即用 · 也支持 Docker / 服务器多账号共享

[![Release](https://img.shields.io/github/v/release/xiaofan6ya/workbuddy2api?label=release)](https://github.com/xiaofan6ya/workbuddy2api/releases)
[![Build](https://github.com/xiaofan6ya/workbuddy2api/actions/workflows/desktop-release.yml/badge.svg)](https://github.com/xiaofan6ya/workbuddy2api/actions/workflows/desktop-release.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%20x64-0078D6)](#-windows-桌面版推荐)
[![License](https://img.shields.io/badge/license-see%20LICENSE-lightgrey)](./LICENSE)

[快速开始](#-快速开始) · [功能](#-功能一览) · [客户端接入](#-客户端接入) · [架构](#-架构) · [配置](#-配置参考) · [排障](#-排障)

</div>

---

## 这是什么

`workbuddy2api` 是一个**本地反向代理网关**。它读取你**已经登录**的 WorkBuddy / CodeBuddy 桌面端凭据，把腾讯后端的私有协议翻译成标准的 **OpenAI Chat Completions**、**OpenAI Responses** 和 **Anthropic Messages** 协议，于是任何支持这三套协议的客户端（Codex CLI、Claude Code、Cherry Studio、NextChat、各种 SDK…）都能直接接入。

它**不**替你登录、**不**模拟桌面端操作、**不**包含任何第三方账号或 SDK。

> [!IMPORTANT]
> 本项目与**腾讯、WorkBuddy、CodeBuddy、OpenAI、Anthropic 均无官方关联**。
> 请仅在你**合法拥有订阅**的前提下使用，并自行承担使用风险。详见[免责声明](#-免责声明)。

---

## ✨ 功能一览

<table>
<tr><td width="50%" valign="top">

### 🔌 三套协议，一个网关

| 协议 | 端点 |
|------|------|
| OpenAI Chat | `POST /v1/chat/completions` |
| OpenAI Responses | `POST /v1/responses` |
| Anthropic Messages | `POST /v1/messages` |
| 模型列表 | `GET /v1/models` |
| 余额查询 | `GET /v1/balance` |

流式与非流式都支持。上游只提供流式，非流式由网关**内部聚合**成标准 JSON 返回（不会把 SSE 原样吐给 `stream:false` 的调用方）。

</td><td width="50%" valign="top">

### 🎛 多账号运营平台

- **号池自动轮转**：4 层选号 + 在途租约 + 会话粘性路由
- **错误分类矩阵**：区分可重试 / 需换号 / 需降级，自动熔断与恢复
- **积分托管**：为每个 API Key 设额度、查用量、看明细
- **自动化任务**：每日签到、余额同步、模型同步、成长任务

</td></tr>
<tr><td valign="top">

### 🖥 Windows 桌面版

单文件 EXE，双击运行。内置 Python 运行时与全部依赖，自动准备 Node / WebView2，无需安装 MySQL、Redis 或配置环境变量。

</td><td valign="top">

### 🛡 面向生产的设计

请求级重试与退避、WAF 级别的 IP fail-fast、并发闸门、限流、脱敏、超时与熔断，以及一套可在 CI 复现的回归测试。

</td></tr>
</table>

---

## 🚀 快速开始

### Windows 桌面版（推荐）

1. 打开 [**Releases**](https://github.com/xiaofan6ya/workbuddy2api/releases)
2. 下载 `WorkBuddy2API-<版本>-windows-x64.exe`
3. **双击运行** —— 完成

无需安装 Python、pip、MySQL 或 Redis，也无需填写后台用户名密码。首次启动会自动准备运行环境（需要联网），之后直接复用。

启动后：

- 管理后台：`http://127.0.0.1:8790/admin`
- API 地址：`http://127.0.0.1:8790/v1`（左下角可复制）

> [!NOTE]
> 数据与日志保存在 `%LOCALAPPDATA%\WorkBuddy2API`，与 EXE 位置无关，升级不丢数据。
> 详见 [desktop/README.md](./desktop/README.md)。

### Docker / 服务器部署

```bash
git clone https://github.com/xiaofan6ya/workbuddy2api.git
cd workbuddy2api

# 必须显式提供，否则 compose 会拒绝启动（不允许弱口令兜底）
cat > .env <<'EOF'
ADMIN_PASSWORD=换成你的强密码
ADMIN_JWT_SECRET=至少32字节的随机串
CONVERTER_API_KEY=用于 /gw 的独立密钥
EOF

docker compose up -d
```

访问 `http://<主机>:8790/admin`。

### 从源码运行

需要 **Python 3.10+**（推荐 3.12）。MySQL 与 Redis 可用时功能最完整；桌面模式会自动降级到 SQLite + 内存限流。

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt

# 生成配置（可选，但生产必须配 ADMIN_JWT_SECRET）
cp .env.example .env

python main.py            # 单端口一体化：管理后台 + 网关 + 内嵌 converter，默认 8790
```

其他运行方式：

```bash
python converter.py                        # 只跑反代网关（默认 8787）
python -m uvicorn admin.server:app --port 8790   # 只跑管理后台
```

> [!TIP]
> **重启就是再跑一次**，不需要先手动杀进程。`main.py` 会先探测端口：
> 端口空闲则正常启动；占用者是**本项目自己的旧实例**则自动接管；是**别的程序**则报错退出并打印其 pid 与命令行，绝不误杀。

---

## 📦 环境要求

| 组件 | 版本 | 是否必需 | 说明 |
|------|------|:--------:|------|
| Python | 3.10+（推荐 3.12） | ✅ | 从源码运行时需要 |
| WorkBuddy / CodeBuddy 桌面端 | 已登录 | ✅ | 提供登录态与设备风控 SDK |
| Node.js | 任意 LTS | ⬜ | 生成设备风控头 `turing_helper.js`；桌面版会自动下载 |
| WebView2 | ≥ 86 | ⬜ | 仅桌面版 GUI 需要；Windows 10/11 通常已内置 |
| MySQL | 8.x | ⬜ | 多账号共享模式；缺失时降级 SQLite |
| Redis | 6+ | ⬜ | 限流与缓存；缺失时降级进程内内存 |

---

## 🔌 客户端接入

所有客户端都只需要 **Base URL** 和 **API Key** 两件事。

### Codex CLI

```toml
# ~/.codex/config.toml
model_provider = "workbuddy"

[model_providers.workbuddy]
name = "WorkBuddy"
base_url = "http://127.0.0.1:8790/v1"
env_key = "WORKBUDDY_API_KEY"
wire_api = "responses"

[profiles.workbuddy]
model = "auto"
model_provider = "workbuddy"
```

```bash
export WORKBUDDY_API_KEY="sk-你的Key"      # Windows: setx WORKBUDDY_API_KEY "sk-..."
codex --profile workbuddy
```

仓库内提供了 [codex-codebuddy.example.toml](./codex-codebuddy.example.toml) 可直接参考。

### Claude Code

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8790"
export ANTHROPIC_AUTH_TOKEN="sk-你的Key"
claude
```

### 任意 OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8790/v1", api_key="sk-你的Key")

resp = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

```bash
curl http://127.0.0.1:8790/v1/chat/completions \
  -H "Authorization: Bearer sk-你的Key" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"你好"}]}'
```

> `model` 填 `auto` 由网关按号池状态自动挑模型；也可以填具体模型名（见后台「模型」页）。

### API Key 怎么来

登录管理后台 → **API Key** → 新建，即可拿到 `sk-...`。Key 在数据库中只存 **SHA-256 哈希**，明文仅在创建/轮换时展示一次。

---

## 🏗 架构

```
                         ┌──────────────────────────────────────────┐
   客户端                │              workbuddy2api               │
  (Codex / Claude /      │                                          │
   SDK / curl)           │  ┌────────────┐      ┌─────────────────┐ │
        │                │  │  协议适配层  │      │   号池与路由     │ │
        │  OpenAI /      │  │            │      │                 │ │
        └───────────────►│  │ chat       │      │ 4 层选号         │ │
           Anthropic     │  │ responses  │─────►│ 在途租约         │ │
           协议          │  │ anthropic  │      │ 会话粘性         │ │
                         │  └────────────┘      │ 熔断 / 降级      │ │
                         │         │            └─────────────────┘ │
                         │         ▼                     │          │
                         │  ┌────────────┐      ┌────────▼────────┐ │
                         │  │  治理层     │      │   凭据与鉴权     │ │
                         │  │ 限流 / 闸门 │      │ token 刷新       │ │
                         │  │ 退避 / 重试 │      │ 设备风控头       │ │
                         │  │ 脱敏        │      └─────────────────┘ │
                         │  └────────────┘               │          │
                         └───────────────────────────────┼──────────┘
                                                         ▼
                                          腾讯 WorkBuddy 后端
```

### 一次请求发生了什么

1. **鉴权** — 校验 `X-API-Key` / `Bearer`，检查 Key 状态与积分配额
2. **限流** — 并发闸门与每分钟配额（Redis 或进程内内存）
3. **选号** — 按「余额 / 快过期 / 闲置」三因子加权抽签，过滤在途已满、冷却中、熔断中的账号
4. **粘性路由** — 同一会话尽量复用同一账号，保住上游前缀缓存、也让流量更像真人
5. **构造请求** — 注入鉴权头、设备风控头、会话头族
6. **发往上游** — 只支持流式；网关按需聚合
7. **错误处理** — 按错误分类矩阵决定「重试 / 换号 / 换模型 / 降级 / 熔断」
8. **记账** — 落用量日志、扣积分、更新账号状态

### 目录结构

```
workbuddy2api/
├── converter.py              # 反代核心：协议转换 + 凭据刷新 + 上游直连
├── main.py                   # 单端口一体化入口（后台 + 网关 + converter）
├── desensitize.py            # 请求脱敏：压缩 harness 提示词
├── upstream_compat.py        # 上游兼容：消息清洗、孤儿 tool_call 清理
├── anthropic_adapter.py      # Anthropic ⇄ OpenAI 协议互转
├── responses_adapter.py      # OpenAI Responses 协议适配
├── responses_projection.py   # 长上下文的压缩投影
├── turing_helper.js          # 设备风控头（依赖桌面端 SDK）
├── wb_*.py                   # 桌面端定位 / 解包 / 安装辅助
├── admin/                    # 管理后台
│   ├── server.py             # FastAPI 入口、登录、静态页托管
│   ├── security.py           # JWT、密码哈希、API Key
│   ├── pool.py               # 号池：选号 / 租约 / 粘性 / 熔断
│   ├── gateway_limits.py     # 限流与并发准入
│   ├── ratelimit.py          # 登录防爆破
│   ├── scheduler.py          # 定时任务调度
│   ├── backend.py            # 单账号后端会话
│   ├── wb_login.py           # 纯协议登录（手机号验证）
│   ├── routers/              # 各业务路由
│   └── static/               # 前端（单页，无构建步骤）
├── desktop/                  # Windows 桌面版（PyInstaller + PyWebView）
├── tests/                    # 回归测试
└── .github/workflows/        # CI：测试 + 构建 Windows EXE
```

---

## ⚙️ 配置参考

配置全部走环境变量，可写在 `.env` 里（桌面版写 `config.json`）。

### 核心

| 变量 | 默认 | 说明 |
|------|------|------|
| `ADMIN_USERNAME` | `admin` | 后台用户名 |
| `ADMIN_PASSWORD` | — | 后台密码。**设置后由环境变量接管**，后台改密会被拒绝 |
| `ADMIN_JWT_SECRET` | 随机生成 | JWT 签名密钥。**未设置时每次重启随机生成**，登录态全部失效 —— 生产务必固定 |
| `ADMIN_PORT` | `8790` | 后台监听端口 |
| `ADMIN_HOST` | `0.0.0.0` | 后台监听地址。建议改为 `127.0.0.1` 并走反代 |
| `ADMIN_ENABLE_DOCS` | `0` | 是否开放 `/docs`。默认关闭（对攻击者等于攻击面清单） |
| `CONVERTER_API_KEY` | — | 内嵌 `/gw` 网关密钥。**未配置则拒绝挂载 `/gw`**（fail-closed） |

### 数据库与缓存

| 变量 | 默认 | 说明 |
|------|------|------|
| `ADMIN_DATABASE_URL` | `mysql+pymysql://root:root@127.0.0.1:3306/workbuddy_admin` | 数据库连接串 |
| `ADMIN_REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis 连接串 |
| `GATEWAY_LIMIT_BACKEND` | `redis` | 限流后端；`memory` 用于单进程 / 桌面版 |

### 号池与治理

| 变量 | 默认 | 说明 |
|------|------|------|
| `MAX_IN_FLIGHT` | 见代码 | 单账号最大在途请求数 |
| `ADMIN_GATEWAY_MAX_CONCURRENT` | `4` | 每 Key 最大并发请求数 |
| `ADMIN_GATEWAY_RATE_PER_MINUTE` | `0`（不限） | 每 Key 每分钟请求上限 |
| `STICKY_ENABLED` | `1` | 会话粘性路由 |
| `STICKY_TTL_SECONDS` | `1800` | 粘性绑定存活时间 |
| `ADMIN_POOL_RETRY_WINDOW` | `90` | 轮转重试的时间窗（秒） |
| `ADMIN_LOGIN_MAX_ATTEMPTS` | `5` | 登录失败几次后锁定 |
| `ADMIN_LOGIN_LOCK_SECONDS` | `900` | 登录锁定时长（秒） |

完整清单见 [.env.example](./.env.example)。

> [!WARNING]
> `ADMIN_JWT_SECRET` 未设置时程序会**生成随机密钥并打印警告**。这比硬编码弱密钥安全，
> 但意味着**每次重启所有登录态失效**。生产环境必须显式配置。

---

## 🧪 测试

```bash
python -m unittest discover -s tests -p "test_regressions.py" -v
```

`tests/test_regressions.py` 收录了历次安全与正确性审查中修过的缺陷，每个用例的 docstring 都写明「修之前会怎样」，防止重构时把修复悄悄改回去。**不需要 MySQL、Redis 或上游网络**，可在 CI 直接跑。

CI（[desktop-release.yml](./.github/workflows/desktop-release.yml)）在每次推送时运行：

1. 纯逻辑回归测试 + 全量字节码编译
2. Windows 上的桌面版测试与 EXE 构建

---

## 🖥 Windows 桌面版

| | |
|---|---|
| **系统** | Windows 10 1809+ / Windows 11，x64 |
| **使用** | 双击 EXE，无需任何配置 |
| **数据目录** | `%LOCALAPPDATA%\WorkBuddy2API` |
| **数据库** | SQLite（WAL 模式），升级保留 |
| **监听** | 默认仅本机 `127.0.0.1` |
| **更新** | 内置检查 GitHub Releases，校验 SHA256 后替换 |

Node.js 22.14.0 首次使用时后台自动下载（npmmirror → 华为云 → 官方源，逐个回退并校验固定 SHA256），不阻塞窗口。WebView2 缺失时自动从 Microsoft 官方地址下载经签名的引导程序安装。

构建 EXE：

```powershell
python -m venv .desktop-build
.\.desktop-build\Scripts\python.exe -m pip install -r requirements-desktop-lock.txt
.\.desktop-build\Scripts\python.exe -m unittest discover -s tests -p test_desktop.py -v
.\.desktop-build\Scripts\python.exe scripts\build_desktop.py
```

产物：`dist/WorkBuddy2API-<版本>-windows-x64.exe` 与同名 `.sha256`。

推送 `vX.Y.Z` tag 后 CI 会自动构建并上传到 Releases（tag 与 `desktop/version.json` 不一致会被拒绝）。细节见 [desktop/README.md](./desktop/README.md)。

---

## 🔐 安全说明

已经落地的措施：

- **默认拒绝**：未配置 `CONVERTER_API_KEY` 时**不挂载** `/gw`，而不是裸奔
- **密钥不落明文**：后台密码用 PBKDF2-SHA256（10 万次迭代）存储；API Key 只存 SHA-256
- **令牌可撤销**：JWT 带版本号，改密码或登出立即作废所有已签发令牌
- **凭据不外泄**：账号临时凭据文件用 `mkstemp` 原子创建并收紧 ACL，且注册了 GC 兜底清理
- **CSV 防注入**：导出对 `=`/`+`/`-`/`@` 开头的字段做中和，避免 Excel 执行公式
- **响应头**：`X-Content-Type-Options`、`X-Frame-Options: DENY`、`Referrer-Policy`，管理页 `no-store`
- **文档默认关闭**：`/docs` 需显式开启
- **静态资源**：路径穿越已被阻断

### 部署建议

> [!CAUTION]
> 这是把**你自己的账号额度**开放出去的网关。请务必：

1. **不要直接暴露到公网** —— 用 `ADMIN_HOST=127.0.0.1` 或放在反代 + 认证之后
2. **务必设置强 `ADMIN_JWT_SECRET`**，否则重启后登录态失效
3. **务必设置强 `ADMIN_PASSWORD`**，compose 已强制要求（无弱口令兜底）
4. **启用 HTTPS**，否则 Key 在链路上是明文
5. **限制后台访问来源**，并定期轮换 API Key

---

## 🩺 排障

<details>
<summary><b>启动后打不开后台 / 登录 503</b></summary>

后端会明确告知原因。常见情况：

- **提示「未配置登录凭据」** —— 既没有 `ADMIN_PASSWORD` 环境变量，数据库里也没有密码记录。设置其一即可。
- **提示「当前密码由环境变量指定，无法在后台修改」** —— 部署时设了 `ADMIN_PASSWORD`，改 `.env` 后重启。
- **端口被占** —— `python main.py` 会自动判断占用者；若是别的程序会打印其 pid 与命令行。
</details>

<details>
<summary><b>请求返回 401 / 403</b></summary>

| 状态 | 含义 |
|------|------|
| `401` | Key 不存在或格式不对 |
| `402` | 积分已耗尽 |
| `403` | Key 被停用 |
| `503` | 号池没有可用账号（全部禁用 / 冷却 / 熔断） |

后台「模型健康」页可以看到每个模型的可用账号数与失败原因分布。
</details>

<details>
<summary><b>上游频繁限流 / 429</b></summary>

号池会自动轮转、退避、熔断并降级。如果持续出现：

1. 检查账号是否触发了上游风控（后台看错误分类）
2. 降低 `ADMIN_GATEWAY_MAX_CONCURRENT`，避免把并发压到单个账号上
3. 确认设备风控头正常（`turing_helper.js` 依赖桌面端 SDK）
</details>

<details>
<summary><b>Windows 桌面版窗口白屏</b></summary>

多半是 **WebView2 运行时缺失**。到 [Microsoft 官网](https://developer.microsoft.com/microsoft-edge/webview2/) 安装 Evergreen Runtime 后重启桌面版。日志见 `%LOCALAPPDATA%\WorkBuddy2API\logs\`。
</details>

<details>
<summary><b>日志在哪</b></summary>

| 场景 | 位置 |
|------|------|
| 源码 / Docker | 控制台输出；`main.py` 会写 `logs/admin.log` |
| Windows 桌面版 | `%LOCALAPPDATA%\WorkBuddy2API\logs\` |

`admin.*` 的日志默认输出 INFO 及以上，可通过 `WORKBUDDY_TURING_DEBUG=1` 打开设备风控头的调试输出。
</details>

---

## ❓ FAQ

<details>
<summary><b>需要我有 WorkBuddy 账号吗？</b></summary>

需要。本项目只做「协议转换」，不提供账号，也不绕过登录。额度来自你自己的账号。
</details>

<details>
<summary><b>没有 MySQL / Redis 能用吗？</b></summary>

可以。桌面版固定使用 SQLite + 内存限流。源码部署时若连不上 MySQL/Redis，也会降级运行（单进程场景）。
</details>

<details>
<summary><b>会泄露我的账号凭据吗？</b></summary>

凭据只存在你自己的数据库里，网关只在本机与上游之间转发。对外返回的账号列表**不包含**凭据原文；导出功能需要管理员权限。临时凭据文件用原子创建 + ACL 收紧，并有 GC 兜底清理。
</details>

<details>
<summary><b>`model` 该填什么？</b></summary>

填 `auto` 让网关按号池状态自动挑；或填具体模型名（后台「模型」页有完整列表与倍率）。
</details>

<details>
<summary><b>支持多用户共享吗？</b></summary>

支持。每个 API Key 可设独立额度与模型分组，用量与积分按 Key 隔离。
</details>

---

## 🤝 参与贡献

欢迎 Issue 与 PR。

1. Fork 并新建分支
2. 改动请附带测试（`tests/test_regressions.py` 是天然的去处）
3. 确保 `python -m unittest discover -s tests -p "test_regressions.py"` 全绿
4. 提交 PR 并说明动机与验证方式

> [!TIP]
> 如果你修的是**已收录的回归缺陷**，请顺手补一个用例 —— 每个用例的 docstring 都记录了「修之前会怎样」。

---

## 📄 免责声明

本项目为**技术研究与学习**目的，用于访问你**自己合法拥有**的服务。

- 与**腾讯、WorkBuddy、CodeBuddy、OpenAI、Anthropic 均无官方关联**，不是任何一方的官方客户端或 SDK
- 不提供账号、不绕过登录、不破解付费
- 使用者需自行确保符合相关服务条款与当地法律法规
- 因使用本项目产生的任何后果（包括账号被限制、服务中断等）**由使用者自行承担**
- 请勿用于商业转售或对他人提供服务

**如果你不同意以上任何一条，请立即停止使用并删除本项目。**

---

<div align="center">

## 🙏 致谢

感谢 [DeepSeek](https://platform.deepseek.com) 提供的模型能力，
以及 [deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) 提供的智能体工具链 ——
本项目的代码、排查与文档是在「人提需求 → 智能体落地」的循环中协作产出的。

**如果这个项目对你有帮助，欢迎点一个 ⭐**

</div>
