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


def _parse_mmdd_year(mmdd_str: str) -> tuple[int, int, int] | None:
    """解析 MMDD 目录名，返回 (year, month, day)

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
    return (year, month, day)


def _beijing_to_utc_date_hour(year: int, month: int, day: int,
                               beijing_hour: int) -> tuple[str, int]:
    """北京时 → UTC 日期+小时

    北京时 = UTC + 8。返回 (UTC日期 YYYY-MM-DD, UTC小时)
    """
    from datetime import timedelta
    bj = datetime(year, month, day, beijing_hour, 0, 0)
    utc = bj - timedelta(hours=8)
    utc_date = utc.strftime("%Y-%m-%d")
    return utc_date, utc.hour


def process_date(mmdd_str: str) -> dict[str, dict[int, dict[str, Any]]] | None:
    """处理指定 MMDD 目录下所有云图，返回 {UTC日期: {UTC小时: {avg_kb, count}}}

    MMDD 目录名和 HHmm 文件名均为北京时，函数自动转换为 UTC。
    """
    parsed = _parse_mmdd_year(mmdd_str)
    if not parsed:
        logger.warning("无效的MMDD目录名: %s", mmdd_str)
        return None
    year, month, day = parsed

    dir_path = WXMAP_DIR / mmdd_str
    if not dir_path.is_dir():
        logger.warning("目录不存在: %s", dir_path)
        return None

    # 按 UTC 日期+小时收集所有裁剪后的大小
    # result[UTC日期][UTC小时] = [size_kb, ...]
    result: dict[str, dict[int, list[float]]] = {}

    for fname in sorted(os.listdir(str(dir_path))):
        if not fname.upper().endswith(".PNG"):
            continue
        hhmm = fname.replace(".PNG", "").replace(".png", "")
        m = re.match(r"^(\d{2})(\d{2})$", hhmm)
        if not m:
            continue
        bj_hour = int(m.group(1))
        if bj_hour < 0 or bj_hour > 23:
            continue

        file_path = dir_path / fname
        size_bytes = crop_top_half_size_bytes(file_path)
        if size_bytes is None or size_bytes <= 0:
            continue

        # 北京时 → UTC
        utc_date, utc_hour = _beijing_to_utc_date_hour(year, month, day, bj_hour)

        size_kb = size_bytes / 1024.0
        result.setdefault(utc_date, {}).setdefault(utc_hour, []).append(size_kb)

    if not result:
        logger.info("目录 %s 中无有效云图", mmdd_str)
        return None

    # 聚合为最终格式
    final: dict[str, dict[int, dict[str, Any]]] = {}
    for utc_date, hours in result.items():
        final[utc_date] = {}
        for h, sizes in hours.items():
            avg_kb = sum(sizes) / len(sizes)
            final[utc_date][h] = {
                "date": utc_date,
                "hour": h,
                "avg_kb": round(avg_kb, 2),
                "count": len(sizes),
                "level": get_cloud_level(avg_kb),
            }

    return final


def process_and_store_day(db, mmdd_str: str) -> int:
    """处理指定MMDD目录并存入数据库，返回存储的小时数

    目录名和文件名均为北京时，内部自动转换为 UTC 后存储。
    """
    from .database import Database  # noqa: F811

    result = process_date(mmdd_str)
    if not result:
        return 0

    stored = 0
    for utc_date, hours in result.items():
        for utc_hour, data in hours.items():
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
            logger.info("已处理 %s: %d 小时云量数据", entry, stored)

    logger.info("云量数据扫描完成，共处理 %d 小时", total)
    return total


def process_today_hourly(db) -> int:
    """处理最新一小时的云图（目录和文件名均为北京时，自动转UTC存储）

    以当前 UTC 小时为基准，找到对应的北京时小时，
    处理该小时的所有云图。
    返回新存储的小时数。
    """
    from datetime import timedelta
    now_utc = datetime.utcnow()

    # 北京时 = UTC + 8
    bj_hour = (now_utc.hour + 8) % 24

    # 计算北京时的日期
    bj_dt = now_utc + timedelta(hours=8)
    mmdd_str = f"{bj_dt.month:02d}{bj_dt.day:02d}"
    dir_path = WXMAP_DIR / mmdd_str
    if not dir_path.is_dir():
        logger.warning("北京时目录不存在: %s", dir_path)
        return 0

    # 检查这个 UTC 小时是否已处理
    utc_date = now_utc.strftime("%Y-%m-%d")
    utc_hour = now_utc.hour
    existing = db.get_cloud_cover(utc_date, utc_hour)
    if existing is not None:
        logger.debug("当前小时 %s/%d 已有云量数据，跳过", utc_date, utc_hour)
        return 0

    # 找对应北京时的图片（如 UTC 06:00 → 北京时 14:00 → 文件名 14xx.PNG）
    prefix = f"{bj_hour:02d}"
    sizes: list[float] = []

    for fname in sorted(os.listdir(str(dir_path))):
        if not fname.upper().endswith(".PNG"):
            continue
        if not fname.startswith(prefix):
            continue

        file_path = dir_path / fname
        size_bytes = crop_top_half_size_bytes(file_path)
        if size_bytes is None or size_bytes <= 0:
            continue
        sizes.append(size_bytes / 1024.0)

    if not sizes:
        logger.info("UTC %s/%d (北京时 %s %02d:xx) 无云图",
                     utc_date, utc_hour, mmdd_str, bj_hour)
        return 0

    avg_kb = sum(sizes) / len(sizes)
    db.insert_or_update_cloud_cover(
        date=utc_date,
        hour=utc_hour,
        avg_kb=round(avg_kb, 2),
        count=len(sizes),
    )
    logger.info("已记录云量 UTC %s/%d (北京时 %s %02d:xx): avg=%.1fKB (%d张)",
                 utc_date, utc_hour, mmdd_str, bj_hour, avg_kb, len(sizes))
    return 1
