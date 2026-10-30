#!/bin/bash
# restart_aftn_web.sh - 一键重启 aftn_web 进程（停旧 + 启新 + 验证）
# 用法: bash restart_aftn_web.sh

REPO_DIR="/home/share/atc_datahub"
PID_FILE="/tmp/aftn_web.pid"
PYTHON="/usr/bin/python3"
LOG_DIR="$REPO_DIR/logs"
PORT=5000

echo "==> 1/4 停止旧进程..."

# 优先从 PID 文件取进程号，取不到就用 pgrep 找
OLD_PID=""
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null)
fi
if [ -z "$OLD_PID" ] || ! kill -0 "$OLD_PID" 2>/dev/null; then
    OLD_PID=$(pgrep -f "python3 -m aftn_web" | head -1)
fi

if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    kill "$OLD_PID"
    # 最多等 10 秒让进程优雅退出
    for i in $(seq 1 20); do
        kill -0 "$OLD_PID" 2>/dev/null || break
        sleep 0.5
    done
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "   进程 $OLD_PID 未响应，强制 kill..."
        kill -9 "$OLD_PID"
        sleep 1
    fi
    echo "   旧进程 $OLD_PID 已停止"
else
    echo "   没有找到运行中的旧进程"
fi

# 清理残留的 PID 锁文件（防止新进程启动被拒）
rm -f "$PID_FILE"

# 验证端口已释放（防止旧进程未被杀掉时新进程启动失败却误报成功）
if ss -tln | grep -q ":$PORT "; then
    echo "❌ 端口 $PORT 仍被占用（旧进程未停止，可能权限不足或命名空间隔离），中止启动"
    exit 1
fi

echo "==> 2/4 启动新进程..."
cd "$REPO_DIR" || { echo "错误: 无法进入 $REPO_DIR"; exit 1; }
nohup "$PYTHON" -m aftn_web -c "$REPO_DIR/config.json" --log-dir "$LOG_DIR" > /dev/null 2>&1 &

echo "==> 3/4 等待端口 $PORT 就绪..."
for i in $(seq 1 20); do
    ss -tln | grep -q ":$PORT " && break
    sleep 0.5
done

echo "==> 4/4 验证..."
if ss -tln | grep -q ":$PORT "; then
    NEW_PID=$(pgrep -f "python3 -m aftn_web" | head -1)
    echo "✅ aftn_web 重启成功！新进程 PID=$NEW_PID，端口 $PORT 正常监听"
else
    echo "❌ 端口 $PORT 未监听，启动可能失败，请查看日志: $LOG_DIR"
    exit 1
fi
