#!/bin/bash
# mihomo 代理中转终端 - 一键部署脚本（免 build，docker pull + run）
# 用法：上传整个文件夹到服务器，执行 bash deploy.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "========================================="
echo "  mihomo 代理中转终端 - 一键部署"
echo "========================================="

# 1. 检查 Docker
echo ""
echo "[1/5] 检查 Docker..."
if ! command -v docker &>/dev/null; then
    echo "错误：未安装 Docker，请先安装 Docker"
    exit 1
fi
echo "Docker 版本：$(docker --version)"

# 2. 拉取镜像
echo ""
echo "[2/5] 拉取镜像..."
docker pull python:3.11-slim
docker pull metacubex/mihomo:latest

# 3. 清理旧容器
echo ""
echo "[3/5] 清理旧容器..."
docker rm -f mihomo-web 2>/dev/null || true
docker rm -f mihomo 2>/dev/null || true

# 4. 启动 mihomo-web（挂载 app.py，免 build）
echo ""
echo "[4/5] 启动 mihomo-web（首次启动约 30 秒安装依赖）..."
docker run -d \
    --name mihomo-web \
    --restart always \
    --add-host host.docker.internal:host-gateway \
    -p 7892:7892 \
    -e UI_PASSWORD="${UI_PASSWORD:-mihomo123}" \
    -e SECRET_KEY="$(openssl rand -hex 32)" \
    -e MIHOMO_HOST=host.docker.internal \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v /tmp:/tmp \
    -v "${SCRIPT_DIR}/app.py:/app/app.py:ro" \
    -w /app \
    python:3.11-slim \
    bash -c "apt-get update -qq && apt-get install -y -qq --no-install-recommends curl cron >/dev/null 2>&1 && pip install --no-cache-dir -q flask pyyaml docker && service cron start && python app.py"

# 5. 放行端口
echo ""
echo "[5/5] 放行防火墙端口..."
if command -v ufw &>/dev/null; then
    ufw allow 7890/tcp 2>/dev/null || true
    ufw allow 7891/tcp 2>/dev/null || true
    ufw allow 7892/tcp 2>/dev/null || true
    echo "ufw 已放行 7890/7891/7892"
elif command -v firewall-cmd &>/dev/null; then
    firewall-cmd --permanent --add-port=7890/tcp 2>/dev/null || true
    firewall-cmd --permanent --add-port=7891/tcp 2>/dev/null || true
    firewall-cmd --permanent --add-port=7892/tcp 2>/dev/null || true
    firewall-cmd --reload 2>/dev/null || true
    echo "firewalld 已放行 7890/7891/7892"
else
    echo "未检测到防火墙工具，请手动放行端口：7890 7891 7892"
    echo "宝塔面板：安全 -> 放行端口 7890 7891 7892"
fi

# 等待启动
echo ""
echo "等待启动..."
STARTED=false
for i in $(seq 1 30); do
    if docker logs mihomo-web 2>&1 | grep -q "Running on"; then
        echo "mihomo-web 已启动"
        STARTED=true
        break
    fi
    sleep 2
done

if [ "$STARTED" = "false" ]; then
    echo "警告：mihomo-web 未在 60 秒内启动，查看日志："
    docker logs --tail 30 mihomo-web 2>&1
    echo ""
    echo "如果正在安装依赖，请等待 1-2 分钟后执行："
    echo "  docker logs -f mihomo-web"
    exit 1
fi

SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -z "$SERVER_IP" ]; then
    SERVER_IP="<服务器IP>"
fi

echo ""
echo "========================================="
echo "  部署完成"
echo "========================================="
echo ""
echo "管理页面:  http://${SERVER_IP}:7892"
echo "管理密码:  ${UI_PASSWORD:-mihomo123}"
echo ""
echo "SOCKS5 端口: 7890"
echo "HTTP 端口:   7891"
echo "API 端口:    7892"
echo ""
echo "首次使用："
echo "  1. 打开管理页面登录"
echo "  2. 选择场景（直连/挂代理/API提取/Clash订阅）"
echo "  3. 点击应用，mihomo 会自动启动"
echo "  4. 在对外连接设置账号密码和端口"
echo "========================================="
echo ""
echo "常用命令："
echo "  查看日志:   docker logs -f mihomo-web"
echo "  重启:       docker restart mihomo-web"
echo "  更新代码:   替换 app.py 后 docker restart mihomo-web"
echo "  完全卸载:   docker rm -f mihomo-web mihomo"
echo "========================================="
