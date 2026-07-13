"""气象云图云量处理模块

从 /mnt/WXMap/<MMDD>/ 读取 PNG 气象云图，
裁剪上半部分(高度/2)，记录裁剪后文件大小(KB)作为云量指标，
按小时聚合计算平均云量，存入数据库。

云量等级划分（12级，基于文件大小 KB）：
  等级0:   0 KB (无云图)
  等级1:   1-20 KB
  等级2:  21-40 KB
  等级3:  41-60 KB
  等级4:  61-80 KB
  等级5:  81-100 KB
  等级6: 101-120 KB
  等级7: 121-140 KB
  等级8: 141-160 KB
  等级9: 161-180 KB
  等级10: 181-200 KB
  等级11: >200 KB

云量等级背景色（12色，从浅到深）：
  等级0:  transparent
  等级1:  #e6f7ff (极低)
  等级2:  #bae7ff
  等级3:  #91d5ff
  等级4:  #69c0ff
  等级5:  #40a9ff
  等级6:  #1890ff
  等级7:  #096dd9
  等级8:  #0050b3
  等级9:  #003a8c
  等级10: #002766
  等级11: #001529 (极高)
"""

from __future__ import annotations

import io
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from PIL import Image

logger = logging.getLogger("aftn_web.wx_cloud")

WXMAP_DIR = Path("/mnt/WXMap")

# 云量等级阈值（KB）
CLOUD_LEVELS = [
    (0, 0),      # 等级0: 0 KB
    (1, 20),     # 等级1: 1-20 KB
    (2, 40),     # 等级2: 21-40 KB
    (3, 60),     # 等级3: 41-60 KB
    (4, 80),     # 等级4: 61-80 KB
    (5, 100),    # 等级5: 81-100 KB
    (6, 120),    # 等级6: 101-120 KB
    (7, 140),    # 等级7: 121-140 KB
    (8, 160),    # 等级8: 141-160 KB
    (9, 180),    # 等级9: 161-180 KB
    (10, 200),   # 等级10: 181-200 KB
    (11, 99999), # 等级11: >200 KB
]

# 云量等级背景色（12色，蓝色系从浅到深）
CLOUD_COLORS = [
    "transparent",    # 等级0: 无数据
    "#e6f7ff",        # 等级1: 极低
    "#bae7ff",        # 等级2: 低
    "#91d5ff",        # 等级3: 较低
    "#69c0ff",        # 等级4: 中等偏低
    "#40a9ff",        # 等级5: 中等
    "#1890ff",        # 等级6: 中等偏高
    "#096dd9",        # 等级7: 较高
    "#0050b3",        # 等级8: 高
    "#003a8c",        # 等级9: 很高
    "#002766",        # 等级10: 极高
    "#001529",        # 等级11: 极重
]


def get_cloud_level(kb: float) -> int:
    """根据文件大小(KB)返回云量等级 (0-11)"""
    for level, threshold in CLOUD_LEVELS:
        if kb <= threshold:
            return level
    return 11


def get_cloud_color(kb: float) -> str:
    """根据文件大小(KB)返回云量等级背景色"""
    level = get_cloud_level(kb)
    return CLOUD_COLORS[level]


def crop_top_half_size_bytes(image_path: str | Path) -> int | None:
    """裁剪PNG图片的上半部分，返回裁剪后图片的文件大小(字节)

    若失败返回 None。
    """
    try:
        img = Image.open(str(image_path))
        w, h = img.size
        # 裁剪上半部分 (高度/2)
        crop_h = h // 2
        cropped = img.crop((0, 0, w, crop_h))
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        return buf.tell()
    except Exception as e:
        logger.warning("裁剪失败 %s: %s", image_path, e)
        return None


def _parse_mmdd_date(mmdd_str: str) -> str | None:
    """将 MMDD 格式转为 YYYY-MM-DD

    假设目录是最近的年份（不跨年），
    如果 MMDD > 当前月日，使用去年。
    """
    m = re.match(r"^(\d{2})(\d{2})$", mmdd_str)
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    if month < 1 or month > 12 or day < 1 or day > 31:
        return None
    now = datetime.now()
    year = now.year
    # 猜测年份：如果 MMDD > 当前月日，使用去年
    curr_mmdd = now.month * 100 + now.day
    if month * 100 + day > curr_mmdd + 10:  # 容忍10天偏差
        year -= 1
    return f"{year:04d}-{month:02d}-{day:02d}"


def _parse_hhmm(hhmm_str: str) -> int | None:
    """将 HHmm 字符串转为小时数 (0-23)"""
    m = re.match(r"^(\d{2})(\d{2})$", hhmm_str)
    if not m:
        return None
    hour = int(m.group(1))
    if hour < 0 or hour > 23:
        return None
    return hour


def process_date(mmdd_str: str) -> dict[int, dict[str, Any]] | None:
    """处理指定 MMDD 目录下所有云图，返回 {hour: {avg_kb, count}}"""
    date_str = _parse_mmdd_date(mmdd_str)
    if not date_str:
        logger.warning("无效的MMDD目录名: %s", mmdd_str)
        return None

    dir_path = WXMAP_DIR / mmdd_str
    if not dir_path.is_dir():
        logger.warning("目录不存在: %s", dir_path)
        return None

    # 按小时收集所有裁剪后的大小
    hour_sizes: dict[int, list[float]] = {}

    for fname in sorted(os.listdir(str(dir_path))):
        if not fname.upper().endswith(".PNG"):
            continue
        hhmm = fname.replace(".PNG", "").replace(".png", "")
        hour = _parse_hhmm(hhmm)
        if hour is None:
            continue

        file_path = dir_path / fname
        size_bytes = crop_top_half_size_bytes(file_path)
        if size_bytes is None or size_bytes <= 0:
            continue

        size_kb = size_bytes / 1024.0
        hour_sizes.setdefault(hour, []).append(size_kb)

    if not hour_sizes:
        logger.info("目录 %s 中无有效云图", mmdd_str)
        return None

    result: dict[int, dict[str, Any]] = {}
    for hour, sizes in hour_sizes.items():
        avg_kb = sum(sizes) / len(sizes)
        result[hour] = {
            "date": date_str,
            "hour": hour,
            "avg_kb": round(avg_kb, 2),
            "count": len(sizes),
            "level": get_cloud_level(avg_kb),
        }

    return result


def process_and_store_day(db, mmdd_str: str) -> int:
    """处理指定MMDD目录并存入数据库，返回存储的小时数"""
    from .database import Database  # noqa: F811

    result = process_date(mmdd_str)
    if not result:
        return 0

    stored = 0
    for hour, data in result.items():
        try:
            db.insert_or_update_cloud_cover(
                date=data["date"],
                hour=data["hour"],
                avg_kb=data["avg_kb"],
                count=data["count"],
            )
            stored += 1
        except Exception as e:
            logger.error("存储云量数据失败 %s/%d: %s", data["date"], data["hour"], e)

    return stored


def scan_all(db) -> int:
    """扫描所有 MMDD 目录并处理，返回处理的总小时数"""
    total = 0
    if not WXMAP_DIR.is_dir():
        logger.warning("WXMap目录不存在: %s", WXMAP_DIR)
        return 0

    for entry in sorted(os.listdir(str(WXMAP_DIR))):
        if not re.match(r"^\d{4}$", entry):
            continue
        if not (WXMAP_DIR / entry).is_dir():
            continue

        stored = process_and_store_day(db, entry)
        if stored > 0:
            total += stored
            date_str = _parse_mmdd_date(entry)
            logger.info("已处理 %s (%s): %d 小时云量数据", entry, date_str, stored)

    logger.info("云量数据扫描完成，共处理 %d 小时", total)
    return total


def process_today_hourly(db) -> int:
    """处理今天（当前MMDD目录）的最新一小时云图

    以当前小时的整点为基准，只处理当前小时尚未处理的数据。
    返回新存储的小时数。
    """
    now = datetime.now()
    mmdd_str = f"{now.month:02d}{now.day:02d}"
    dir_path = WXMAP_DIR / mmdd_str
    if not dir_path.is_dir():
        logger.warning("今天目录不存在: %s", dir_path)
        return 0

    date_str = f"{now.year:04d}-{now.month:02d}-{now.day:02d}"

    # 检查当前小时是否已处理
    current_hour = now.hour

    # 先检查DB中是否已有当前小时数据
    existing = db.get_cloud_cover(date_str, current_hour)
    if existing is not None:
        logger.debug("当前小时 %s/%d 已有云量数据，跳过", date_str, current_hour)
        return 0

    # 处理当前小时的云图
    result = process_date(mmdd_str)
    if not result or current_hour not in result:
        logger.info("当前小时 %s/%d 无云图数据", date_str, current_hour)
        return 0

    data = result[current_hour]
    db.insert_or_update_cloud_cover(
        date=data["date"],
        hour=data["hour"],
        avg_kb=data["avg_kb"],
        count=data["count"],
    )
    logger.info("已记录云量 %s/%d: avg=%.1fKB (%d张)", date_str, current_hour, data["avg_kb"], data["count"])
    return 1
