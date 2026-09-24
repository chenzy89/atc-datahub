"""气象云图云量处理模块

从 /mnt/WXMap/<MMDD>/ 读取 PNG 气象云图，
裁剪指定像素区域(X:550~780, Y:280~380)，
记录裁剪后文件大小(KB)作为云量指标，
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
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from PIL import Image

logger = logging.getLogger("aftn_web.wx_cloud")

WXMAP_DIR = Path("/mnt/WXMap")

# 补全缓存：{(year, month): 上次补全时间戳}，短时间内不重复补全
_BACKFILL_CACHE: dict[tuple[int, int], float] = {}
_BACKFILL_CACHE_TTL = 600.0  # 10 分钟
_backfill_lock = threading.Lock()

# 每小时最多取多少张云图参与平均（0=全部）。用于历史补扫提速，见 config.cloud_cover.sample_per_hour
SAMPLE_PER_HOUR = 0


def _refresh_keys() -> set:
    """需要保持刷新的 (UTC日期, UTC小时)：当前小时 + 上一小时

    云图每 ~6 分钟一张，整点后仍会陆续补入，这两个小时不跳过。
    """
    now = datetime.utcnow()
    prev = now - timedelta(hours=1)
    return {
        (now.strftime("%Y-%m-%d"), now.hour),
        (prev.strftime("%Y-%m-%d"), prev.hour),
    }


def day_dir_target_hours(mmdd_str: str) -> set | None:
    """给定北京时 MMDD 目录名，返回该目录覆盖的 24 个 (UTC日期, UTC小时)

    纯名称换算，不访问共享盘，可用于秒级判断某天是否还需要处理。
    """
    parsed = _parse_mmdd_year(mmdd_str)
    if not parsed:
        return None
    year, month, day = parsed
    return {_beijing_to_utc_date_hour(year, month, day, h) for h in range(24)}


def _safe_is_dir(p: Path) -> bool:
    """安全判断目录是否存在。

    Python 3.8 的 Path.is_dir() 对 ENODEV(19, No such device) 会重新抛出 OSError
    （挂载盘设备失效但挂载点仍在时），这里统一按“不存在”处理，避免刷屏。
    """
    try:
        return p.is_dir()
    except OSError:
        return False


def is_wxmap_mounted() -> bool:
    """判断天气图挂载盘是否已挂载且可读。

    - 目录不存在 / 设备失效（stale mount，os.stat 抛 ENODEV）→ False
    - 是挂载点且可访问，或目录存在可读 → True
    """
    try:
        if not WXMAP_DIR.exists():
            return False
        if os.path.ismount(str(WXMAP_DIR)):
            return True
        # 非挂载点但目录可读（本地目录/测试环境也算可用）
        os.listdir(str(WXMAP_DIR))
        return True
    except OSError:
        return False

# 云量等级阈值（KB）—— 裁剪区域 X550~780, Y280~380，范围约 0.16~8 KB
CLOUD_LEVELS = [
    (0, 0),       # 等级0: 0 KB
    (1, 0.18),    # 等级1: <=0.18 KB  晴/无云
    (2, 0.25),    # 等级2: 0.18-0.25 KB
    (3, 0.5),     # 等级3: 0.25-0.5 KB
    (4, 1.0),     # 等级4: 0.5-1.0 KB
    (5, 1.5),     # 等级5: 1.0-1.5 KB
    (6, 2.5),     # 等级6: 1.5-2.5 KB
    (7, 4.0),     # 等级7: 2.5-4.0 KB
    (8, 6.0),     # 等级8: 4.0-6.0 KB
    (9, 8.0),     # 等级9: 6.0-8.0 KB
    (10, 12.0),   # 等级10: 8.0-12.0 KB
    (11, 99999),  # 等级11: >12 KB
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


def crop_region_size_bytes(image_path: str | Path) -> int | None:
    """裁剪图片指定像素区域（X: 550~780, Y: 280~380），返回裁剪后文件大小(字节)

    图片尺寸 990×959，原点(0,0)在左上角，正X向右，正Y向下。
    用该区域的像素大小来表达云量。
    若失败返回 None。
    """
    try:
        img = Image.open(str(image_path))
        # (left, upper, right, lower)
        cropped = img.crop((550, 280, 780, 380))
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        return buf.tell()
    except Exception as e:
        logger.warning("裁剪失败 %s: %s", image_path, e)
        return None


# 别名，兼容旧引用
def crop_top_half_size_bytes(image_path: str | Path) -> int | None:
    return crop_region_size_bytes(image_path)


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


def process_date(mmdd_str: str, skip_hours: set | None = None,
                 sample_per_hour: int = 0) -> dict[str, dict[int, dict[str, Any]]] | None:
    """处理指定 MMDD 目录下所有云图，返回 {UTC日期: {UTC小时: {avg_kb, count}}}

    MMDD 目录名和 HHmm 文件名均为北京时，函数自动转换为 UTC。

    skip_hours: 已入库的 (UTC日期, UTC小时) 集合，命中则不下载、不裁剪
    sample_per_hour: 每小时最多取前 N 张图参与平均（0=全部）
    """
    parsed = _parse_mmdd_year(mmdd_str)
    if not parsed:
        logger.warning("无效的MMDD目录名: %s", mmdd_str)
        return None
    year, month, day = parsed

    dir_path = WXMAP_DIR / mmdd_str
    if not _safe_is_dir(dir_path):
        logger.info("目录不存在或不可用: %s", dir_path)
        return None

    # 按 UTC 日期+小时收集所有裁剪后的大小
    # result[UTC日期][UTC小时] = [size_kb, ...]
    result: dict[str, dict[int, list[float]]] = {}
    skipped = 0

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

        # 北京时 → UTC（先算出来，命中跳过就不用读文件了）
        utc_date, utc_hour = _beijing_to_utc_date_hour(year, month, day, bj_hour)
        if skip_hours and (utc_date, utc_hour) in skip_hours:
            skipped += 1
            continue
        if sample_per_hour and len(result.get(utc_date, {}).get(utc_hour, [])) >= sample_per_hour:
            continue

        file_path = dir_path / fname
        size_bytes = crop_top_half_size_bytes(file_path)
        if size_bytes is None or size_bytes <= 0:
            continue

        size_kb = size_bytes / 1024.0
        result.setdefault(utc_date, {}).setdefault(utc_hour, []).append(size_kb)

    if skipped:
        logger.debug("目录 %s：跳过已入库云图 %d 张（免下载）", mmdd_str, skipped)

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


def process_and_store_day(db, mmdd_str: str, skip_hours: set | None = None,
                          sample_per_hour: int = 0) -> int:
    """处理指定MMDD目录并存入数据库，返回存储的小时数

    目录名和文件名均为北京时，内部自动转换为 UTC 后存储。
    skip_hours: 已入库的 (UTC日期, UTC小时)，命中则不再重算
    """
    from .database import Database  # noqa: F811

    result = process_date(mmdd_str, skip_hours=skip_hours, sample_per_hour=sample_per_hour)
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


def process_day_if_needed(db, mmdd_str: str, existing: set | None = None,
                          sample_per_hour: int = 0) -> tuple[int, bool]:
    """按需处理一个北京时 MMDD 目录，返回 (新增/更新小时数, 是否访问了共享盘)

    先做「名称→UTC」换算，若该目录 24 个目标小时都已在库（除当前/上一小时），
    直接返回，连目录都不列 —— 这是月视图补全从分钟级降到毫秒级的关键。
    """
    targets = day_dir_target_hours(mmdd_str)
    if not targets:
        return 0, False

    refresh = _refresh_keys()
    if existing is None:
        dmin = min(t[0] for t in targets)
        dmax = max(t[0] for t in targets)
        existing = db.get_cloud_cover_hours(dmin, dmax)

    # 需要刷新的小时不算「已齐全」
    if all((t in existing and t not in refresh) for t in targets):
        return 0, False

    if not _safe_is_dir(WXMAP_DIR / mmdd_str):
        return 0, False

    skip = {t for t in existing if t not in refresh}
    stored = process_and_store_day(db, mmdd_str, skip_hours=skip, sample_per_hour=sample_per_hour)
    if stored > 0:
        logger.info("已处理 %s: %d 小时云量数据", mmdd_str, stored)
    return stored, True


def scan_all(db) -> int:
    """扫描所有 MMDD 目录并处理，返回处理的总小时数

    已齐全的目录会直接跳过（不访问共享盘），因此启动扫描很快。
    """
    total = 0
    if not _safe_is_dir(WXMAP_DIR):
        logger.info("WXMap目录不存在或不可用: %s", WXMAP_DIR)
        return 0

    scanned_dirs = 0
    skipped_dirs = 0
    for entry in sorted(os.listdir(str(WXMAP_DIR))):
        if not re.match(r"^\d{4}$", entry):
            continue

        stored, accessed = process_day_if_needed(db, entry, sample_per_hour=SAMPLE_PER_HOUR)
        total += stored
        if accessed:
            scanned_dirs += 1
        else:
            skipped_dirs += 1

    logger.info("云量数据扫描完成：处理 %d 小时（扫描 %d 个目录，跳过 %d 个已齐全目录）",
                total, scanned_dirs, skipped_dirs)
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
    if not _safe_is_dir(dir_path):
        logger.info("北京时目录不存在或不可用: %s", dir_path)
        return 0

    # 当前小时持续刷新：云图每 ~6 分钟一张，整点内还会陆续到达，
    # 每次重算并用 INSERT OR REPLACE 覆盖（代价仅 ~10 张图）
    utc_date = now_utc.strftime("%Y-%m-%d")
    utc_hour = now_utc.hour

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


def backfill_month(db, year: int, month: int) -> dict[str, Any]:
    """检查挂载盘并补全指定 UTC 年月的云量数据。

    云图目录按北京时命名（MMDD），UTC 月份 YYYY-MM 对应的北京时日期范围是
    YYYY-MM-01 ～ YYYY-(MM+1)-01，全部遍历一遍。

    ⚡ 已入库的小时不会重新下载/裁剪：
      - 目录 24 个小时都已入库 → 连列目录都省掉（零共享盘访问，毫秒级）
      - 部分缺失 → 只处理缺的小时；当前/上一小时始终刷新

    返回:
        mounted: 挂载盘是否已挂载
        processed_days / skipped_days: 实际访问共享盘 / 直接跳过的目录数
        stored_hours: 新写入（或刷新）的小时数
        cached: 是否命中补全缓存（10分钟内不重复补全）
    """
    if not is_wxmap_mounted():
        return {
            "mounted": False,
            "processed_days": 0,
            "skipped_days": 0,
            "stored_hours": 0,
            "cached": False,
            "message": "天气图挂载盘未挂载，仅显示已入库数据",
        }

    key = (year, month)
    now_ts = datetime.utcnow().timestamp()
    with _backfill_lock:
        last = _BACKFILL_CACHE.get(key, 0.0)
        if now_ts - last < _BACKFILL_CACHE_TTL:
            return {
                "mounted": True,
                "processed_days": 0,
                "skipped_days": 0,
                "stored_hours": 0,
                "cached": True,
                "message": "该月数据刚刚已补全",
            }
        _BACKFILL_CACHE[key] = now_ts

        # UTC 月对应的北京时日期范围（含边界）：YYYY-MM-01 ～ YYYY-(MM+1)-01
        if month == 12:
            next_first = datetime(year + 1, 1, 1)
        else:
            next_first = datetime(year, month + 1, 1)
        start_bj = datetime(year, month, 1) + timedelta(hours=8)
        end_bj = next_first + timedelta(hours=8)

        processed_days = 0
        skipped_days = 0
        stored_hours = 0
        d = start_bj
        while d <= end_bj:
            mmdd = f"{d.month:02d}{d.day:02d}"
            stored, accessed = process_day_if_needed(
                db, mmdd, sample_per_hour=SAMPLE_PER_HOUR
            )
            if accessed:
                processed_days += 1
            else:
                skipped_days += 1
            stored_hours += stored
            d += timedelta(days=1)

        logger.info("云量补全 %04d-%02d 完成：扫描 %d 个目录，跳过 %d 个（已齐全），写入 %d 小时",
                    year, month, processed_days, skipped_days, stored_hours)
        return {
            "mounted": True,
            "processed_days": processed_days,
            "skipped_days": skipped_days,
            "stored_hours": stored_hours,
            "cached": False,
            "message": f"补全完成：扫描 {processed_days} 天目录，写入 {stored_hours} 小时数据",
        }
