"""
飞行数据记录 (FDR) 存储 — 内存中的航迹快照列表

CAT062 解析后的每条航迹数据写入 FDR 列表，
由定时器每 4 秒检查一次，仅对"已相关"（有起降地）的记录
执行飞行计划跑道/飞行程序更新，避免高频 DB 写入。

TTL: 超过 8 秒未更新的记录自动清理。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("aftn_web.fdr")

# TTL: 超过此时间未更新的 FDR 将被移除
FDR_TTL_SECONDS = 8

# 定期检查间隔
PROCESS_INTERVAL_SECONDS = 4

# 跑道有效长度：2~3（如 "16", "34L", "07R"）
_RUNWAY_LEN_MIN = 2
_RUNWAY_LEN_MAX = 3
# 飞行程序有效长度：6~7（如 "SAREX31", "OVGOT3"）
_FP_LEN_MIN = 6
_FP_LEN_MAX = 7


def _validate_runway(val: str) -> str:
    """校验跑道值，不合法返回 'ERROR'
    - 去掉 null 字节和不可见字符
    - 长度 2~3（如 "16", "34L", "07R"）
    """
    val = val.strip().replace("\x00", "")
    if val and _RUNWAY_LEN_MIN <= len(val) <= _RUNWAY_LEN_MAX:
        return val
    return "ERROR"


def _validate_flight_procedure(val: str) -> str:
    """校验飞行程序值，不合法返回 'ERROR'
    - 去掉 null 字节和不可见字符
    - 长度 6~7（如 "SAREX31", "OVGOT3"）
    """
    val = val.strip().replace("\x00", "")
    if val and _FP_LEN_MIN <= len(val) <= _FP_LEN_MAX:
        return val
    return "ERROR"


@dataclass
class FDRRecord:
    """单条飞行数据记录"""
    callsign: str = ""
    ssr: str = ""
    adep: str = ""
    adest: str = ""
    runway: str = ""
    flight_procedure: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    heading: float = 0.0
    flight_level: float = 0.0
    prev_flight_level: float = 0.0
    speed: float = 0.0
    trail: list[tuple[float, float]] = field(default_factory=list)  # 历史尾迹 [(lat,lon),...]
    last_update: float = 0.0  # time.monotonic()
    _last_trail_time: float = 0.0  # 上次添加尾迹时间

    @property
    def is_associated(self) -> bool:
        """是否已相关：有起降地信息才可能与飞行计划匹配"""
        return bool(self.adep) and bool(self.adest)


class FDRStore:
    """
    内存 FDR 列表，线程安全
    以 callsign 为键 (无呼号时用 SSR)
    """
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, FDRRecord] = {}
        self._last_process = 0.0  # 上次处理时间 (monotonic)
        self._last_cleanup = 0.0

    # ── 公开接口 ──────────────────────────────────────────

    def update_from_radar(self, parsed: dict[str, Any]) -> None:
        """从 CAT062 解析结果更新或插入一条 FDR"""
        callsign = parsed.get("callsign", "").strip()
        ssr = parsed.get("ssr", "").strip()
        key = callsign or ssr
        if not key:
            return

        now = time.monotonic()
        with self._lock:
            rec = self._records.get(key)
            if rec is None:
                rec = FDRRecord()
                self._records[key] = rec

            rec.callsign = callsign
            rec.ssr = ssr
            rec.last_update = now
            # 位置信息（每个数据包都更新）
            lat = parsed.get("latitude", 0.0)
            lon = parsed.get("longitude", 0.0)
            rec.latitude = lat
            rec.longitude = lon
            rec.heading = parsed.get("heading", 0.0)
            new_fl = parsed.get("flight_level", 0.0)
            if abs(new_fl - rec.flight_level) > 0.5:
                rec.prev_flight_level = rec.flight_level
            rec.flight_level = new_fl
            rec.speed = parsed.get("speed", 0.0)
            # 尾迹: 每 ≥2.5 秒或位置变化 ≥0.02° 才加一个点
            if lat or lon:
                last_pt = rec.trail[-1] if rec.trail else None
                dt = now - rec._last_trail_time
                moved = False
                if last_pt:
                    dlat = abs(lat - last_pt[0])
                    dlon = abs(lon - last_pt[1])
                    moved = dlat > 0.02 or dlon > 0.02
                if dt >= 2.5 or moved or not last_pt:
                    rec.trail.append((lat, lon))
                    if len(rec.trail) > 5:
                        rec.trail.pop(0)
                    rec._last_trail_time = now
            # 仅当新数据非空时才覆盖（避免空值覆盖已有信息）
            if runway := parsed.get("runway", "").strip():
                rec.runway = _validate_runway(runway)
            if fp := parsed.get("flight_procedure", "").strip():
                rec.flight_procedure = _validate_flight_procedure(fp)
            if adep := parsed.get("adep", "").strip():
                rec.adep = adep.upper()
            if adest := parsed.get("adest", "").strip():
                rec.adest = adest.upper()

    def process_updates(self, db: Any) -> None:
        """
        周期性处理：
        1. 清理超时记录
        2. 对有呼号的 FDR 执行飞行计划更新（跑道/飞行程序）
           优先匹配已相关记录（有起降地），其次是仅有呼号的记录。
        """
        now = time.monotonic()
        with self._lock:
            # 1. 清理超时
            expired = [k for k, r in self._records.items()
                       if now - r.last_update > FDR_TTL_SECONDS]
            for k in expired:
                del self._records[k]
            if expired:
                logger.debug("FDR cleanup: removed %d expired records", len(expired))

            # 2. 收集记录快照：有呼号且可能有跑道/程序信息的记录
            #    优先处理已相关的（有起降地），但也处理仅有呼号的
            #    （雷达数据可能没有 ADEP/ADEST 但有跑道/程序信息）
            candidates = [r for r in self._records.values()
                          if r.callsign and (r.runway or r.flight_procedure)]

        # 3. 逐条更新飞行计划（在锁外执行 DB 操作，异常不会互相影响）
        updated_count = 0
        error_count = 0
        for rec in candidates:
            try:
                affected = db.update_radar_data(
                    rec.callsign,
                    rec.runway,
                    rec.flight_procedure,
                    adep=rec.adep,
                    adest=rec.adest,
                )
                if affected > 0:
                    updated_count += 1
            except Exception:
                error_count += 1
                if error_count <= 3:
                    logger.warning("[FDR] %s 更新失败（已累计 %d 次）",
                                   rec.callsign, error_count)
        if updated_count:
            logger.info("[FDR] 更新 %d 条飞行计划", updated_count)
        if error_count > 10:
            logger.info("[FDR] 累计 %d 次更新失败，等待下个周期重试", error_count)

    def get_tracks(self) -> list[dict[str, Any]]:
        """返回所有有效航迹（带位置信息）"""
        now = time.monotonic()
        with self._lock:
            tracks = []
            for rec in self._records.values():
                if now - rec.last_update > FDR_TTL_SECONDS:
                    continue
                # 高度变化趋势
                fl_diff = rec.flight_level - rec.prev_flight_level
                if abs(fl_diff) > 1.0:
                    level_trend = 'c' if fl_diff > 0 else 'd'
                else:
                    level_trend = 'm'
                tracks.append({
                    "callsign": rec.callsign,
                    "ssr": rec.ssr,
                    "latitude": rec.latitude,
                    "longitude": rec.longitude,
                    "heading": rec.heading,
                    "flight_level": rec.flight_level,
                    "speed": rec.speed,
                    "level_trend": level_trend,
                    "trail": rec.trail[-5:],
                    "adep": rec.adep,
                    "adest": rec.adest,
                    "runway": rec.runway,
                    "flight_procedure": rec.flight_procedure,
                })
            return tracks

    def get_stats(self) -> dict[str, Any]:
        """返回当前 FDR 列表统计"""
        with self._lock:
            total = len(self._records)
            associated = sum(1 for r in self._records.values() if r.is_associated)
            return {
                "total": total,
                "associated": associated,
                "age_seconds": int(time.monotonic()) if total else 0,
            }
