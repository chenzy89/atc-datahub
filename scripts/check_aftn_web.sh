#!/bin/bash
# 检查 aftn_web 端口 5000 是否活着，没活着就自动重启
# 用法：./scripts/check_aftn_web.sh

PORT=5000
WORK_DIR="/home/share/atc_aftn_web"
LOG_DIR="$WORK_DIR/logs"
PID_FILE="/tmp/aftn_web.pid"

if ss -tlnp | grep -q ":$PORT "; then
    # 端口正常
    exit 0
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 端口 $PORT 未监听，重启 aftn_web..."

# 清理残留 PID 文件
rm -f "$PID_FILE"

# 启动
cd "$WORK_DIR" || exit 1
nohup /usr/bin/python3 -m aftn_web -c config.json --log-dir "$LOG_DIR" > /dev/null 2>&1 &

# 等几秒确认启动成功
sleep 3
if ss -tlnp | grep -q ":$PORT "; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ aftn_web 已重启成功"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ❌ aftn_web 启动失败，请手动检查"
fi
