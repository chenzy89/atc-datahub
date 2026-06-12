#!/bin/bash
# 回填全部历史扇区 callsign 数据到 sector_callsigns_10min
# 从 sector_flights 表重建（一次性操作，后续启动会自动回填当日数据）
#
# 用法: ./scripts/backfill_sector_callsigns.sh [config.json 路径]

set -e
cd "$(dirname "$0")/.."
CONFIG="${1:-config.json}"
echo "开始回填历史扇区 callsign 数据..."
echo "配置文件: $CONFIG"
echo ""
/usr/bin/python3 -m aftn_web -c "$CONFIG" --backfill-sector-all
