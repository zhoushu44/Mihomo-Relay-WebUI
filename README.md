# Mihomo Relay WebUI

**中文名称：Mihomo 代理中转管理终端**

基于 mihomo（Clash.Meta）的 Web 代理中转管理面板，支持直连、SOCKS5/HTTP 上游代理、API 按需提取和 Clash 订阅轮换，并向外提供带独立认证的 SOCKS5、HTTP 与 API 入口。

## 功能

- **SOCKS5 入口**（默认端口 7890）：独立账号密码，可复制连接链接
- **HTTP 入口**（默认端口 7891）：独立账号密码，可复制连接链接
- **直连入口**：不经过上游代理，直接用服务器 IP 出口
- **API 控制接口**（默认端口 7892）：获取连接信息、状态、轮换代理
- **场景 A**：直连（无上游代理）
- **场景 B/C/D**：挂代理（SOCKS5/HTTP，支持轮换）
- **场景 E**：API 提取（用到才提取，不浪费额度）
- **场景 F**：Clash 订阅链接（支持 Base64/VLESS/Hysteria2）
- **保存应用 + 切换**：每个场景两个按钮，保存参数后可一键切换
- **当前状态标记**：页面显示当前使用的场景（绿色 ● 当前）
- **表单预填**：已保存的参数自动填回输入框
- **测速**：Cloudflare 下载测速
- **可折叠卡片**：所有功能区域可折叠
- **白名单说明**：每种场景标注是否需要白名单

## 部署

### 方式一：一键脚本（推荐，免 build）

```bash
# 1. 上传整个 mihomo-web 文件夹到服务器
scp -r mihomo-web root@服务器IP:/root/

# 2. SSH 登录后执行
cd /root/mihomo-web
bash deploy.sh
```

脚本使用 `docker pull` + `docker run`，不需要 `docker build`。
首次启动约 30 秒（容器内自动安装依赖），之后重启秒启。

### 方式二：手动 docker pull + run

```bash
docker pull python:3.11-slim
docker pull metacubex/mihomo:latest

docker rm -f mihomo-web mihomo 2>/dev/null

docker run -d \
    --name mihomo-web \
    --restart always \
    --add-host host.docker.internal:host-gateway \
    -p 7892:7892 \
    -e UI_PASSWORD=mihomo123 \
    -e SECRET_KEY=$(openssl rand -hex 32) \
    -e MIHOMO_HOST=host.docker.internal \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v /tmp:/tmp \
    -v /root/mihomo-web/app.py:/app/app.py:ro \
    -w /app \
    python:3.11-slim \
    bash -c "apt-get update -qq && apt-get install -y -qq --no-install-recommends curl cron >/dev/null 2>&1 && pip install --no-cache-dir -q flask pyyaml docker && service cron start && python app.py"
```

### 方式三：docker build + run（启动更快）

```bash
docker build -t mihomo-web .
docker run -d \
    --name mihomo-web \
    --restart always \
    --add-host host.docker.internal:host-gateway \
    -p 7892:7892 \
    -e UI_PASSWORD=mihomo123 \
    -e SECRET_KEY=$(openssl rand -hex 32) \
    -e MIHOMO_HOST=host.docker.internal \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v /tmp:/tmp \
    mihomo-web
```

### 方式四：docker-compose

```bash
docker-compose up -d --build
```

## 端口说明

| 端口 | 用途 | 是否需要放行 |
|------|------|-------------|
| 7890 | SOCKS5 代理 | 用代理就放行 |
| 7891 | HTTP 代理 | 用代理就放行 |
| 7892 | Web 管理 + API | 需要管理页面就放行 |

宝塔面板：安全 -> 放行端口 7890 7891 7892

## 首次使用

1. 打开 `http://服务器IP:7892`
2. 输入密码 `mihomo123`
3. 先在"对外连接"设置 SOCKS5/HTTP 账号密码
4. 选择场景，点击"保存应用"
5. 复制 SOCKS5/HTTP 链接使用

### 保存应用 vs 切换

每个场景有两个按钮：

| 按钮 | 作用 |
|------|------|
| **保存应用** | 保存当前填写的参数 + 立即应用 |
| **切换** | 用上次保存的参数直接切换，不用重填 |

使用流程：
```
第一次：
  场景 E -> 填 API 地址 -> 点"保存应用"
  场景 F -> 填订阅链接 -> 点"保存应用"

之后切换：
  想用 E -> 点"切换"（不用重填 API 地址）
  想用 F -> 点"切换"（不用重填订阅链接）
```

- "切换"按钮只在已保存过配置的场景才出现
- 当前使用的场景会显示绿色 `● 当前` 标记
- 已保存的参数会自动填回输入框

## 对外连接格式

```
# SOCKS5（无密码）
socks5://服务器IP:7890

# SOCKS5（有密码）
socks5://用户名:密码@服务器IP:7890

# HTTP（无密码）
http://服务器IP:7891

# HTTP（有密码）
http://用户名:密码@服务器IP:7891
```

密码中有特殊字符（如 @）需要 URL 编码：`@` -> `%40`

```
# 例：密码 socks-pass@1
socks5://sockstest:socks-pass%401@192.6.121.16:7890
```

## API 接口

```bash
# 获取连接信息
curl http://服务器IP:7892/api/connections?key=API_KEY

# 获取状态
curl http://服务器IP:7892/api/status?key=API_KEY

# 轮换/刷新代理
curl -X POST http://服务器IP:7892/api/rotate?key=API_KEY
```

也支持 Header 认证：`X-API-Key: API_KEY`

未授权访问返回 `401`。

## 场景说明

### 场景 A：直连
- 不经过上游代理，直接用服务器 IP 出口
- 不需要白名单
- 入口仍使用 SOCKS5 / HTTP，但不经过上游

### 场景 B/C/D：挂代理
- 每行一个代理：`ip:port` 或 `ip:port:user:pass`
- 支持轮换（round-robin）或只用第一个
- 不需要白名单（用账号密码认证）
- 代理类型可选 SOCKS5 或 HTTP

### 场景 E：API 提取
- 服务器 IP 必须在上游代理平台加白
- 每 2 分钟检测一次，代理过期才提取，不浪费额度
- 提取数量默认 1，可自行调整
- 页面填的"提取数量"会自动替换 API URL 中的 `num` 参数
- 可手动立即刷新

提取机制：
```
代理活着 -> 不提取（不浪费额度）
代理过期 -> 自动提取 1 个新的 -> mihomo 自动加载
```

### 场景 F：Clash 订阅
- 填入 Clash 订阅链接
- 自动识别 Clash YAML、Base64 编码
- 支持 VLESS、Hysteria2、VMess、SS、Trojan
- 不需要白名单（取决于订阅内容）

运行机制：
```
模式：轮换（round-robin）
节点：订阅中所有节点全部加载
轮换：每次请求轮流使用下一个节点
健康检查：每 60 秒自动检测节点可用性
订阅更新：每 10 分钟自动重新拉取订阅
```

坏节点处理：

| 情况 | 处理方式 |
|------|----------|
| 节点健康检查失败 | 标记为不可用，轮换时跳过 |
| 节点在两次检查之间挂了 | 该次请求可能失败，下次请求自动换到下一个节点 |
| 节点恢复 | 下次健康检查通过后重新加入轮换 |
| 订阅更新 | 每 600 秒自动重新拉取订阅 |

## 常用命令

```bash
# 查看日志
docker logs -f mihomo-web

# 重启 Web
docker restart mihomo-web

# 重启 mihomo
docker restart mihomo

# 更新代码（替换 app.py 后）
docker restart mihomo-web

# 完全卸载
docker rm -f mihomo-web mihomo
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `UI_PASSWORD` | `mihomo123` | Web 管理密码 |
| `SECRET_KEY` | 随机生成 | Flask 会话密钥 |
| `MIHOMO_HOST` | `host.docker.internal` | mihomo 容器地址 |
| `API_KEY` | 随机生成 | API 接口密钥 |

## 文件结构

```
mihomo-web/
├── app.py              # 主程序
├── Dockerfile          # Docker 构建文件（可选，用于 build 方式）
├── docker-compose.yml  # Compose 配置（可选）
├── deploy.sh           # 一键部署脚本（推荐）
└── README.md           # 本文档
```

## 架构

```
用户/程序
    │
    ├── SOCKS5 :7890 ──┐
    ├── HTTP   :7891 ──┤
    └── API    :7892 ──┘
                        │
                   mihomo-web（Flask）
                        │ Docker SDK
                        ▼
                     mihomo 容器
                        │
            ┌───────────┼───────────┐
            │ 直连      │ 代理列表   │ 订阅
            │ (DIRECT)  │ (轮换)    │ (Provider)
            └───────────┴───────────┘
```

mihomo-web 通过 Docker socket 控制 mihomo 容器，用户在页面操作时自动创建/重启 mihomo。
