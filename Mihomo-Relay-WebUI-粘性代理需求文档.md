# Mihomo-Relay-WebUI 粘性代理需求文档

> 项目地址：https://github.com/zhoushu44/Mihomo-Relay-WebUI
> 文档日期：2026-08-15
> 文档版本：v1.0

---

## 一、项目背景

Mihomo-Relay-WebUI 是基于 mihomo（Clash.Meta）的 Web 代理中转管理面板，支持直连、SOCKS5/HTTP 上游代理、API 按需提取和 Clash 订阅轮换，向外提供带独立认证的 SOCKS5、HTTP 与 API 入口。

当前系统所有代理场景均使用 `strategy: round-robin`（纯轮换），**未实现粘性会话（sticky session）**。

---

## 二、现有问题

### 2.1 无粘性会话（核心问题）

| 场景 | 当前行为 | 问题 |
|---|---|---|
| 场景 A（直连） | 不涉及 | 无 |
| 场景 B/C/D（挂代理） | round-robin 轮换 | 每个请求换一个出口 IP |
| 场景 E（API 提取） | round-robin 轮换 | 同上 |
| 场景 F（Clash 订阅） | round-robin 轮换 | 同上 |

**导致的问题：**
- 注册流程中途 IP 变化 → 触发目标网站风控
- 同一会话 Cookie/Session 绑定 IP → 下个请求 IP 变了被踢出
- 无法用于需要 IP 一致性的场景（并发注册、电商、社媒养号）

### 2.2 其他已识别问题

| 问题 | 严重性 | 类型 | 位置 |
|---|---|---|---|
| `/api/rotate` 并发竞态，无锁保护 | 中危 | 逻辑漏洞 | app.py L972-984 |
| `/api/connections` 明文泄露代理凭证（返回完整 URL 含密码） | 高危 | 信息泄露 | app.py L958 |
| 场景 F 存在 SSRF（服务端直接请求用户提供的 URL） | 高危 | 安全漏洞 | app.py L763 |
| 健康检查使用 HTTP 明文（http://ip-api.com/json） | 中危 | 安全风险 | 多处 |
| API Key 通过 URL 查询参数传递（日志/Referer 泄露） | 中危 | 信息泄露 | app.py L934 |
| 无速率限制 + 无登录锁定 | 中危 | 安全风险 | 全局 |
| Session Cookie 安全配置缺失 | 低危 | 安全配置 | Flask 默认 |
| 轮换计数器 docker restart 后重置 | 低危 | 逻辑问题 | app.py L981 |

---

## 三、目标场景

### 3.1 核心场景：并发注册

用户使用代理池进行并发账号注册，每个注册任务是一个多步骤流程：

```
任务A: GET /register → POST /signup → 验证邮箱 → 完成
任务B: GET /register → POST /signup → 验证邮箱 → 完成
任务C: ...
```

**需求：** 同一注册任务的全流程必须使用同一个出口 IP（粘性），不同任务使用不同 IP（隔离）。

### 3.2 代理来源

系统已有两种代理来源，均需支持粘性：

| 来源 | 对应场景 | 格式 | 说明 |
|---|---|---|---|
| API 提取 | 场景 E | 动态提取 `ip:port` | 服务器 IP 需在上游平台加白 |
| 手填 SOCKS5 列表 | 场景 B/C/D | `ip:port` 或 `ip:port:user:pass` | 静态代理列表 |

### 3.3 IP 不足时的策略

**需求：** 当任务数 > IP 数时，允许 IP 轮换复用，但需控制同一 IP 的并发数。

示例：10 个 SOCKS5 代理，15 个并发注册任务

```
时刻 T1：
  任务1  → IP-A    任务6  → IP-F
  任务2  → IP-B    任务7  → IP-G
  任务3  → IP-C    任务8  → IP-H
  任务4  → IP-D    任务9  → IP-I
  任务5  → IP-E    任务10 → IP-J
  任务11 → IP-A（复用，第11个任务轮换到第一个 SOCKS5）
  任务12 → IP-B（复用）
  ...
  任务15 → IP-E（复用）
```

**策略规则：**
- 每个 IP 最多同时承载 N 个并发任务（可配置，默认 2）
- 10 个 IP × 每个 2 并发 = 最多 20 个并发任务
- 超过并发上限的任务排队等待
- 任务完成后释放 IP 槽位，排队任务接上
- 同一 IP 同时多个注册请求需评估目标网站风控严格程度

---

## 四、功能需求

### 4.1 粘性会话核心模块

#### 4.1.1 任务 ID → 代理绑定

| 需求项 | 说明 |
|---|---|
| 会话标识 | 每个注册任务携带唯一任务 ID（task_id / session_id） |
| 绑定关系 | task_id → 固定代理节点，整个任务生命周期不变 |
| 自动分配 | 新任务自动从代理池分配一个代理（轮询 + 并发数检查） |
| 自动释放 | 任务完成后释放代理槽位，归还池中 |
| 强制换 IP | 支持 `/api/rotate?session=task_id` 对单个会话强制换 IP |

#### 4.1.2 代理池管理

| 需求项 | 说明 |
|---|---|
| 代理来源 | 兼容现有场景 E（API 提取）和场景 B/C/D（手填列表） |
| 健康检查 | 定时检测代理可用性，标记不可用节点 |
| 自动剔除 | 连续失败 N 次的代理自动从池中移除 |
| 动态补充 | 场景 E 下代理过期时自动提取新代理补充池 |
| 状态查询 | API 查询代理池状态：总数、可用、占用、排队 |

#### 4.1.3 并发控制

| 参数 | 默认值 | 说明 |
|---|---|---|
| `MAX_CONCURRENCY_PER_IP` | 2 | 每个 IP 最多同时承载的任务数 |
| `MAX_QUEUE_SIZE` | 100 | 排队等待队列最大长度 |
| `SESSION_TIMEOUT` | 600s | 会话超时自动释放（防止任务异常不释放） |
| `IP_REUSE_STRATEGY` | round-robin | IP 复用时的选择策略 |

### 4.2 API 接口设计

#### 新增接口

| 接口 | 方法 | 功能 | 参数 |
|---|---|---|---|
| `/api/session/acquire` | POST | 获取一个粘性会话（分配代理） | `?key=API_KEY`，body: `{task_id}` |
| `/api/session/release` | POST | 释放会话（归还代理） | `?key=API_KEY`，body: `{task_id}` |
| `/api/session/status` | GET | 查询会话状态 | `?key=API_KEY&session=task_id` |
| `/api/session/list` | GET | 列出所有活跃会话 | `?key=API_KEY` |
| `/api/session/rotate` | POST | 强制某个会话换 IP | `?key=API_KEY`，body: `{task_id}` |
| `/api/pool/status` | GET | 代理池状态 | `?key=API_KEY` |

#### 接口示例

**获取会话：**
```
POST /api/session/acquire?key=API_KEY
Content-Type: application/json

{"task_id": "register_001"}

# 响应
{
  "ok": true,
  "session": {
    "task_id": "register_001",
    "proxy": "1.2.3.4:1080",
    "proxy_type": "socks5",
    "acquired_at": "2026-08-15T10:00:00Z",
    "expires_at": "2026-08-15T10:10:00Z"
  }
}
```

**释放会话：**
```
POST /api/session/release?key=API_KEY
Content-Type: application/json

{"task_id": "register_001"}

# 响应
{
  "ok": true,
  "message": "会话已释放，代理已归还池中"
}
```

**代理池状态：**
```
GET /api/pool/status?key=API_KEY

# 响应
{
  "ok": true,
  "pool": {
    "total": 10,
    "available": 3,
    "in_use": 7,
    "queued_tasks": 5,
    "max_concurrency_per_ip": 2,
    "proxies": [
      {"proxy": "1.2.3.4:1080", "in_use": 2, "max": 2, "status": "busy"},
      {"proxy": "5.6.7.8:1080", "in_use": 1, "max": 2, "status": "available"},
      ...
    ]
  }
}
```

#### 现有接口变更

| 接口 | 变更 |
|---|---|
| `/api/connections` | 移除明文 `url` 字段，仅返回 `masked_url` |
| `/api/rotate` | 新增 `session` 参数支持单会话轮换；加锁防并发竞态 |
| 所有 API | 新增速率限制（如 60 次/分钟） |

### 4.3 代理使用方式

注册脚本通过粘性会话获取代理后，有两种使用方式：

**方式 A：直接使用代理（推荐，绕过 mihomo）**
```python
# 获取会话
session = requests.post("http://server:7892/api/session/acquire?key=KEY",
                         json={"task_id": "register_001"}).json()

# 直接用代理发请求（不经过 mihomo）
proxy = session["session"]["proxy"]
proxy_type = session["session"]["proxy_type"]
proxies = {f"{proxy_type}": f"{proxy_type}://{proxy}"}
requests.post("https://target.com/register", proxies=proxies)

# 任务完成释放
requests.post("http://server:7892/api/session/release?key=KEY",
               json={"task_id": "register_001"})
```

**方式 B：通过 mihomo 子规则路由（复杂，非首选）**
```
mihomo sub-rules 按请求特征分流到固定节点
→ 实现复杂，不推荐
```

### 4.4 WebUI 界面变更

#### 新增"粘性会话"管理卡片

| UI 元素 | 说明 |
|---|---|
| 代理池状态面板 | 显示总数/可用/占用/排队，可视化 |
| 活跃会话列表 | 表格显示 task_id、绑定 IP、获取时间、超时时间 |
| 手动释放按钮 | 可手动释放某个卡住的会话 |
| 并发配置 | 可配置 `MAX_CONCURRENCY_PER_IP`、`SESSION_TIMEOUT` |
| 开关 | 启用/关闭粘性会话模式（关闭时回退到现有 round-robin） |

---

## 五、技术方案

### 5.1 架构设计

```
注册脚本 (N 个并发任务)
    │
    ├── POST /api/session/acquire (task_id) → 分配代理
    ├── 直接用代理发请求（绕过 mihomo）
    └── POST /api/session/release (task_id) → 释放代理

Mihomo-Relay-WebUI (Flask)
    ├── 粘性会话管理模块（新增）
    │   ├── session_store: {task_id → proxy} 映射表
    │   ├── proxy_pool: 代理池 + 并发计数 + 健康检查
    │   └── queue: 排队等待队列
    │
    ├── 现有场景 A/B/C/D/E/F（保留）
    └── mihomo 容器管理（保留，非粘性场景继续用）
```

### 5.2 核心数据结构

```python
# 会话存储
session_store = {
    "register_001": {
        "task_id": "register_001",
        "proxy": "1.2.3.4:1080",
        "proxy_type": "socks5",
        "acquired_at": 1723720800,
        "expires_at": 1723721400,   # 10 分钟超时
        "status": "active"           # active / released / expired
    }
}

# 代理池
proxy_pool = {
    "1.2.3.4:1080": {
        "proxy": "1.2.3.4:1080",
        "type": "socks5",
        "in_use": 2,                # 当前并发数
        "max_concurrency": 2,       # 最大并发
        "health": "healthy",        # healthy / unhealthy / checking
        "fail_count": 0,            # 连续失败次数
        "last_check": 1723720800
    }
}

# 排队队列
task_queue = [
    {"task_id": "register_016", "created_at": 1723720900},
    {"task_id": "register_017", "created_at": 1723720910}
]
```

### 5.3 代理分配流程

```
新任务请求代理
    │
    ├── 代理池有空闲 IP（in_use < max_concurrency）？
    │   ├── 是 → 分配 IP，in_use + 1，记录到 session_store
    │   └── 否 → 进入排队队列，等待其他任务释放
    │
    └── 任务完成 / 超时
        ├── in_use - 1
        ├── 队列有等待任务？→ 分配给队列首部任务
        └── 无等待 → IP 回到空闲池
```

### 5.4 与现有系统的集成

| 现有模块 | 变更 |
|---|---|
| `load_settings()` | 新增粘性会话配置项 |
| `gen_proxy_config()` | 新增粘性模式标记，不影响现有轮换模式 |
| `gen_api_config()` | 场景 E 提取的代理同步写入 proxy_pool |
| `/api/rotate` | 加锁 + 支持 session 参数 |
| `/api/connections` | 移除明文密码 |
| `app.py` | 新增粘性会话管理路由模块 |
| Dockerfile | 无变更 |
| WebUI HTML | 新增粘性会话管理卡片 |

### 5.5 技术选型

| 组件 | 选型 | 说明 |
|---|---|---|
| 后端语言 | Python（现有 Flask） | 不引入新语言 |
| 并发控制 | `threading.Lock` + `queue.Queue` | 轻量，不引入 Redis |
| 会话存储 | 内存 + JSON 文件持久化 | 同现有 SETTINGS_PATH 模式 |
| 代理健康检查 | 复用现有 `test_proxy()` | 定时线程跑 |
| 线程安全 | 全局锁保护 session_store 和 proxy_pool | |

---

## 六、安全修复需求

在实现粘性会话的同时，修复已识别的安全问题：

| 编号 | 问题 | 修复方案 | 优先级 |
|---|---|---|---|
| S1 | `/api/connections` 明文泄露代理凭证 | 移除 `url` 字段，仅返回 `masked_url` | 高 |
| S2 | 场景 F SSRF 漏洞 | 校验 URL 协议 + 禁止内网 IP + URL 白名单 | 高 |
| S3 | `/api/rotate` 并发竞态 | 加 `threading.Lock` | 中 |
| S4 | 健康检查 HTTP 明文 | 改用 `https://ip-api.com/json` | 中 |
| S5 | API Key URL 传递泄露 | 推荐使用 Header，URL 方式保留但加日志告警 | 中 |
| S6 | 无速率限制 | Flask 增加 `flask-limiter`，API 60 次/分钟 | 中 |
| S7 | Session Cookie 配置 | 设置 `SESSION_COOKIE_HTTPONLY=True` 等 | 低 |

---

## 七、约束与限制

| 约束 | 说明 |
|---|---|
| 不引入新的外部服务 | 不依赖 Redis / 数据库，使用内存 + 文件 |
| 保持 Docker 单容器部署 | 不新增容器 |
| 向后兼容 | 现有场景 A~F 功能不受影响，粘性会话为可选模式 |
| Python 技术栈 | 不引入 Go/Rust，保持 Flask + mihomo 架构 |
| 代理协议 | 支持 SOCKS5 和 HTTP 代理的粘性 |
| 并发上限 | 单实例支持 100 并发任务，1000 个代理 |

---

## 八、验收标准

| 编号 | 验收项 | 验证方式 |
|---|---|---|
| A1 | 10 个 IP + 10 个任务，每个任务全程同一 IP | 注册脚本验证 IP 不变 |
| A2 | 10 个 IP + 15 个任务，前 10 个各分配独立 IP，后 5 个轮换复用 | 查看代理池状态 API |
| A3 | 任务完成后 IP 自动释放，排队任务自动接上 | 并发测试观察 |
| A4 | 同一 IP 并发数不超过配置上限 | 代理池状态 API 验证 |
| A5 | 会话超时自动释放 | 等待超时后检查 session_store |
| A6 | `/api/session/acquire` 和 `/api/session/release` 正常工作 | curl 测试 |
| A7 | `/api/pool/status` 返回正确的池状态 | curl 测试 |
| A8 | 强制轮换单会话 `/api/session/rotate` 正常 | curl 测试 |
| A9 | 现有场景 A~F 功能不受影响 | 回归测试 |
| A10 | `/api/connections` 不再泄露明文密码 | curl 验证 |
| A11 | API 速率限制生效 | 快速连续请求验证 429 |
| A12 | WebUI 显示粘性会话管理面板 | 浏览器访问验证 |

---

## 九、开发优先级

| 阶段 | 内容 | 说明 |
|---|---|---|
| P0 | 粘性会话核心 + 代理池 + 并发控制 | 核心功能，先跑通 |
| P0 | API 接口（acquire/release/status/rotate） | 脚本对接依赖 |
| P1 | 安全修复 S1/S2/S3 | 高危漏洞 |
| P1 | WebUI 粘性会话管理面板 | 可视化管理 |
| P2 | 安全修复 S4/S5/S6 | 中危漏洞 |
| P2 | 会话超时 + 自动清理 | 健壮性 |
| P3 | 安全修复 S7 | 低危配置 |
| P3 | 代理健康检查 + 自动剔除 | 稳定性 |

---

## 十、附录

### 10.1 参考项目

| 项目 | 地址 | 借鉴点 |
|---|---|---|
| proxyhub | https://github.com/jiusanzhou/proxyhub | Session 头粘性 + TTL 机制 |
| ZenProxy | https://github.com/Micky203/zenproxy | 端口映射 + 并发多 IP 出口 |
| ProxyMapService | https://github.com/optinsoft/ProxyMapService | 端口映射 + 自动 session 管理 |

### 10.2 现有系统文件结构

```
Mihomo-Relay-WebUI/
├── app.py                 # 主程序（988 行），需新增粘性模块
├── Dockerfile             # Docker 构建文件
├── docker-compose.yml     # Compose 配置
├── deploy.sh              # 一键部署脚本
└── README.md              # 项目文档
```

### 10.3 现有 API 接口

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/connections` | GET | 获取连接信息 |
| `/api/status` | GET | 获取状态 |
| `/api/rotate` | GET/POST | 轮换/刷新代理 |

认证方式：`?key=API_KEY` 或 `X-API-Key` 请求头
