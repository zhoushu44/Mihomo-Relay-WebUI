# Mihomo Relay WebUI

**中文名称：Mihomo 代理中转管理终端**

基于 mihomo（Clash.Meta）的 Web 代理中转管理面板，支持直连、SOCKS5/HTTP 上游代理、API 按需提取和 Clash 订阅轮换，并向外提供带独立认证的 SOCKS5、HTTP 与 API 入口，支持粘性会话（每个调用方独立端口，端口即身份）。

- **前端**：React 单页应用（首页 / 连接配置 / API Key 三页 + 公开 API 文档页）
- **登录**：API Key（一套 Key 管全部：代理提取 + 连接配置 + 管理面板）
- **API**：`/api/v1/proxy` 一个接口覆盖共享出口 / 粘性会话 / 消耗式提取

## 功能特性

- **React WebUI**（2026-08 重写）：状态概览、连接配置、API Key 管理、公开 API 文档页
- **API Key 认证**：URL 参数 / `X-API-Key` 头 / JSON body 三种传法，恒定时间比较
- **统一取代理 API**：`GET/POST /api/v1/proxy`（共享出口 / 粘性会话 / consume 烧号 / txt 输出）
- **连接配置 API**：`GET/POST /api/v1/config`（入口模式 / 场景 / 更新 Key，部分更新）
- **SOCKS5 入口**（默认 7890）与 **HTTP 入口**（默认 7891）：独立账号密码，可复制连接链接
- **混合单端口**（mixed 7899，SOCKS5+HTTP 同端口）、**双入口**（socks+http 同时开）模式
- **场景 A**：直连；**场景 B/C/D**：挂代理（轮换）；**场景 E**：API 提取（懒加载）；**场景 F**：Clash 订阅
- **粘性会话（v2）**：每个 task_id 独立动态端口（40001-40999），端口固定绑定同一上游，幂等、故障切换端口不变
- **消耗式（consume）**：注册成功即烧号，池子自动补新；粘性会话 10 分钟过期自动清理
- **端口安全**：会话端口 SOCKS5 账号密码认证，无凭据/错凭据一律拒连
- **测速 / 测试代理 / 重启 / 刷新**：WebUI 一键操作
- **CI 自动发布**：GitHub Actions 构建同一镜像并推送 `1.0` + `latest` 双标签，本地无需推送

## 快速开始

### 1. 拉取镜像

```bash
docker pull zhoushu1/mihomo-relay-webui:latest
```

> 也可固定版本：`docker pull zhoushu1/mihomo-relay-webui:1.0`

### 2. 运行容器

```bash
docker run -d \
    --name mihomo-web \
    --restart always \
    --add-host host.docker.internal:host-gateway \
    -p 7892:7892 \
    -e UI_PASSWORD=mihomo123 \
    -e "SECRET_KEY=$(openssl rand -hex 32)" \
    -e MIHOMO_HOST=host.docker.internal \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v /tmp:/tmp \
    zhoushu1/mihomo-relay-webui:latest
```

`mihomo-web` 会通过 Docker SDK 自动拉取并管理 `metacubex/mihomo:latest`，不需要单独部署 mihomo。

### 3. 登录使用

打开 `http://服务器IP:7892`，输入 **API Key**（见下方"环境变量 `API_KEY`"；未设置时管理接口禁止访问，请先设置）。

## Docker Compose（可选）

仓库提供 `docker-compose.yml`（基础）与 `docker-compose.prod.yml`（生产）两种编排，使用前请按需修改环境变量与端口映射：

```bash
docker compose up -d
```

## 更新镜像

```bash
docker pull zhoushu1/mihomo-relay-webui:latest
docker restart mihomo-web
```

`/tmp:/tmp` 会保留已保存场景、账号密码、API Key 和 mihomo 配置。

## 需要开放的端口

| 端口/段 | 协议 | 用途 | 什么时候开放 |
|---------|------|------|--------------|
| `7890` | TCP | 共享 SOCKS5 入口 | 客户端需要使用 SOCKS5 时必须开放 |
| `7891` | TCP | 共享 HTTP 入口 | 启用 HTTP 入口后必须开放（默认未启用） |
| `7899` | TCP | 混合单端口（mixed） | 切换到 mixed 模式后必须开放（默认未启用） |
| `7892` | TCP | WebUI 管理页面和 API | 需要远程管理或调用 API 时必须开放 |
| `40001-40999` | TCP | 粘性会话动态端口段（每个会话从 40001 起逐个分配独立端口） | 使用粘性会话功能时必须开放 |

一次性放行（云安全组 / 服务器防火墙同时放行）：

```text
TCP 7890-7899   # 覆盖 SOCKS5 / HTTP / mixed 所有入口模式
TCP 40001-40999 # 覆盖粘性会话全部动态端口
TCP 7892        # 管理面板 + API
```

Ubuntu/UFW：

```bash
ufw allow 7890/tcp
ufw allow 7891/tcp
ufw allow 7899/tcp
ufw allow 7892/tcp
ufw allow 40001:40999/tcp
ufw status
```

宝塔面板：进入 **安全**，放行 `7890`、`7891`、`7899`、`7892`，以及端口段 `40001-40999`。

> 实测确认：共享入口（7890）、管理面板（7892）与动态会话端口（40001、40002…）均已从外部直连验证开放。
> 7891 / 7899 未通仅因对应模式当前未启用（无监听），放行规则加上后启用即可用。
> 安全建议：代理入口必须设置账号密码；7892 建议限制为自己的固定 IP，或通过 HTTPS 反向代理访问。
> mihomo 访问上游代理和订阅只需要出站网络，不需要额外开放入站端口。

## 首次使用

1. 打开 `http://服务器IP:7892`，输入 **API Key** 登录
2. 在「连接配置」页设置 SOCKS5/HTTP 入口账号密码，选择场景，点「保存应用」
3. 复制连接链接使用（`socks5://用户名:密码@服务器IP:7890`）
4. 客户接入：调 `/api/v1/proxy?key=KEY&session=客户ID` 获取独立端口

### 保存应用 vs 切换

| 按钮 | 作用 |
|------|------|
| **保存应用** | 保存当前填写参数 + 立即应用（部分更新，未填的保留原值） |
| **切换** | 用上次保存的参数直接切换，不用重填（只在已保存过配置时出现） |

当前场景显示绿色 `● 当前` 标记，已保存参数自动填回输入框。

## 对外连接格式

```
# SOCKS5（有密码，推荐）
socks5://用户名:密码@服务器IP:7890

# HTTP（有密码，推荐）
http://用户名:密码@服务器IP:7891

# 混合单端口（mixed 模式）
socks5://用户名:密码@服务器IP:7899 / http://用户名:密码@服务器IP:7899
```

密码中有特殊字符（如 `@`）需要 URL 编码：`@` -> `%40`，例：`socks5://sockstest:socks-pass%401@192.6.121.16:7890`。

## 统一 API（摘要）

> 全部接口走 `http://服务器IP:7892`，认证：`?key=KEY` 或 `X-API-Key: KEY` 头或 JSON body `{"key": ...}`。
> **完整接口文档（含参数表/curl 示例/错误码）在网页「API 文档」页签，未登录也可查看：`http://IP:7892/#/docs`**

```bash
# ① 共享出口
curl "http://IP:7892/api/v1/proxy?key=KEY"

# ② 粘性会话（同 session 永远同一端口同一代理）
curl "http://IP:7892/api/v1/proxy?key=KEY&session=user_001"

# ③ 消耗式（取走即烧号，池子自动补新）
curl "http://IP:7892/api/v1/proxy?key=KEY&session=user_001&consume=1"

# ④ 销毁会话（注册失败/放弃时）
curl "http://IP:7892/api/v1/proxy/destroy?key=KEY&session=user_001"

# ⑤ 查看当前连接配置 / 用 Key 控制配置（入口/场景/更新 Key）
curl "http://IP:7892/api/v1/config?key=KEY"
curl -X POST "http://IP:7892/api/v1/config?key=KEY" -H 'Content-Type: application/json' \
  -d '{"entry_mode":"mixed","entry_port":"7899","entry_username":"u","entry_password":"p"}'
```

- 未设置 Key：提取开放；管理接口（config 等）403 禁止
- 错误码：401 / 403 / 400（缺参）/ 404（会话不存在）/ 409（过期 / 池耗尽）
- 旧接口（`/api/session/*`、`/api/connections`、`/api/status` 等）兼容保留

## 场景说明

### 场景 A：直连
不经过上游代理，直接用服务器 IP 出口，不需要白名单。

### 场景 B/C/D：挂代理
- 每行一个代理：`ip:port` 或 `ip:port:user:pass`，可选 SOCKS5/HTTP，支持轮换或固定第一个
- 粘性开启后：每个任务按轮询分配一个代理并绑定独立端口，故障自动切换（端口不变）

### 场景 E：API 提取
- 服务器 IP 必须在上游平台加白；每 2 分钟检测一次，代理过期才提取（不浪费额度）
- 粘性开启后（1请求1IP）：每次 acquire 懒加载提取 1 个节点绑定独立端口，10 分钟过期清理，失败不切换

### 场景 F：Clash 订阅
- 自动识别 YAML/Base64，支持 VLESS/Hysteria2/VMess/SS/Trojan，轮换 + 60s 健康检查 + 10min 订阅刷新
- 粘性支持直连（绑第一可用节点、不过期）与轮询（每任务一节点、10min 过期、坏节点跳过）两种模式

## 粘性会话（v2 核心）

- 每个任务获得独立动态端口（40001-40999），端口固定绑定同一上游代理，重复 acquire 幂等、绑定不漂移
- 代理故障自动切换（热重载 listener 的 proxy 字段），**端口保持不变**
- 多任务互不干扰（隔离验证：session A→40001、session B→40002 各自独立）；10 分钟过期自动清理
- 消耗式提取：consume=1 取走即烧号；手动轮换（sticky-rotate）只换后端、端口不变
- 会话端口带 SOCKS5 账号密码认证：无凭据/错误凭据一律拒连；端口 URL 仅通过带 Key 的 API 发放

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `API_KEY` | 随机生成 | **平台统一密钥**：前端登录 + API 鉴权（提取/管理） |
| `UI_PASSWORD` | `mihomo123` | 旧版 Web 密码（兼容旧模板登录） |
| `SECRET_KEY` | 随机生成 | Flask 会话密钥 |
| `MIHOMO_HOST` | `host.docker.internal` | mihomo 容器地址 |

> 配置目录 `/tmp` 中保存场景、账号密码与 API Key；更换容器需挂载同一 `/tmp` 保留配置。

## 常用命令

```bash
# 查看日志 / 重启 Web / 重启 mihomo
docker logs -f mihomo-web
docker restart mihomo-web
docker restart mihomo

# 部署脚本（备用，源码部署方式）
scp -r . root@服务器IP:/root/mihomo-web && ssh root@服务器IP 'cd /root/mihomo-web && bash deploy.sh'

# 完全卸载
docker rm -f mihomo-web mihomo
```

## 文件结构

```
Mihomo-Relay-WebUI/
├── app.py                  # Flask 主程序（后端 + 静态托管 + 全部 API）
├── Dockerfile              # 镜像构建（由 GitHub Actions 自动构建推送）
├── docker-compose.yml      # Compose 基础编排
├── docker-compose.prod.yml # Compose 生产编排
├── deploy.sh               # 源码一键部署脚本（备用）
├── .github/workflows/      # CI：构建镜像并推送 1.0 + latest 双标签
├── frontend/               # React 19 + Vite + TypeScript 前端源码
│   ├── src/                # App.tsx / api.ts / components.tsx / pages(Home/Connect/ApiKey/ApiDocs/Login)
│   └── public/             # favicon.svg / icons.svg
├── static/                 # 前端构建产物（Flask 托管，勿手改）
│   ├── index.html
│   └── assets/             # 哈希命名的 JS/CSS
└── test_full.py 等          # 回归测试脚本（API 21 项 / 粘性隔离 / 轮换）
```

## 验证状态（2026-08-18）

- **需求文档 v2 A1-A20** 全部实测 PASS；多用户并发粘性 PASS
- **API 全面回归 21/21 PASS**：鉴权（401/403/200）、共享/粘性/消耗提取、会话生命周期、config 控制、管理接口
- **React 前端**上线验证 PASS：登录（Key）、三页交互、公开文档页、登出
- **粘性专项**：不同 session 隔离、同 session 幂等、轮换后端切换端口不变、消耗式烧号、销毁回收
- **安全实测**：无 Key 无法获取会话端口（401）、无凭据直连被 SOCKS5 认证拒绝；接口畸形 JSON 崩溃已修复并部署
- **端口开放**：7890 / 7892 / 40001-40999 外部直连验证通过
- **代码一致性**：容器内 app.py 与前端产物 md5 与本地完全一致

## 架构与工作原理

```
用户/程序
    │
    ├── SOCKS5 :7890 ──┐
    ├── HTTP   :7891 ──┤
    ├── 粘性   40001-40999
    └── API/Web :7892 ──┘
                        │
                   mihomo-web（Flask + React）
                        │ Docker SDK
                        ▼
                     mihomo 容器（host 网络）
                        │
            ┌───────────┼───────────┐
            │ 直连      │ 代理列表   │ 订阅
            │ (DIRECT)  │ (轮换)    │ (Provider)
            └───────────┴───────────┘
```

- **配置热重载**：页面/API 操作 → 生成完整 mihomo 配置（`/tmp/mihomo_config.yaml`）→ 向 mihomo 发 SIGHUP 重载，**不重启容器**；重载后校验入口端口，失败自动回退重启
- **粘性实现**：acquire → 按场景选上游 → 分配独立端口 → 生成 `sticky-{port}` listener → 热重载 → 端口实测可达才返回
- **并发与排队**：会话/池状态线程锁保护；所有代理不可用时新请求排队，30 秒超时返回失败