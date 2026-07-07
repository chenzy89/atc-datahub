#!/bin/bash
# ATC DataHub 启动脚本
# Usage: ./start_aftn.sh

PROJECT_DIR="/home/share/atc_datahub"
PIDFILE="/tmp/aftn_web.pid"
PORT=5000
PYTHON="/usr/bin/python3"

cd "$PROJECT_DIR" || { echo "❌ 目录不存在: $PROJECT_DIR"; exit 1; }

# 检查端口是否已占用
if ss -tlnp | grep -q ":$PORT "; then
    echo "✅ 端口 $PORT 已在监听，服务运行中，无需重启"
    ss -tlnp | grep ":$PORT "
    exit 0
fi

# 清理残留 PID 文件
if [ -f "$PIDFILE" ]; then
    echo "⚠️  发现残留 PID 文件，清理中..."
    rm -f "$PIDFILE"
fi

# 启动服务
echo "🚀 启动 aftn_web..."
nohup $PYTHON -m aftn_web \
    -c "$PROJECT_DIR/config.json" \
    --log-dir "$PROJECT_DIR/logs" \
    > /dev/null 2>&1 &

# 等待并验证
sleep 2
if ss -tlnp | grep -q ":$PORT "; then
    echo "✅ 启动成功！端口 $PORT 已监听"
    ss -tlnp | grep ":$PORT "
else
    echo "❌ 启动失败，请检查日志: $PROJECT_DIR/logs/"
    exit 1
fi
