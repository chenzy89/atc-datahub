"""
飞行数据记录 (FDR) 存储 — 内存中的航迹快照列表

CAT062 解析后的每条航迹数据写入 FDR 列表，
由定时器每 4 秒检查一次，执行：
- 跑道/飞行程序更新（来自 CAT062 RDS/SID/STAR）
- 终端区进出检测（多边形 + 高度）

TTL: 超过 8 秒未更新的记录自动清理。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from pathlib import Path

from .terminal_area import is_in_terminal, load_terminal_config

logger = logging.getLogger("aftn_web.fdr")

# TTL: 超过此时间未更新的 FDR 将被移除
FDR_TTL_SECONDS = 60

# 定期检查间隔
PROCESS_INTERVAL_SECONDS = 4

_RUNWAY_PATTERN = re.compile(r'^\d{2}[LCR]?$')

def _validate_runway(val: str) -> str:
    """校验跑道值（两位数字 + 可选 L/C/R），非法返回 'ERROR'"""
    val = val.strip().replace("\x00", "")
    if not val:
        return val
    if _RUNWAY_PATTERN.match(val):
        return val
    return "ERROR"


_FP_PATTERN = re.compile(r'^[A-Z0-9]{6,7}$')

def _validate_flight_procedure(val: str) -> str:
    """校验飞行程序值（6~7 位大写字母数字），非法返回 'ERROR'"""
    val = val.strip().replace("\x00", "")
    if not val:
        return val
    if _FP_PATTERN.match(val):
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

    # ── 终端区检测状态 ──────────────────────────────────
    in_terminal: bool = False               # 上次检查时是否在终端区内
    terminal_entry_ts: str = ""             # 进终端时间（UTC ISO）
    terminal_entry_lat: float = 0.0         # 进终端纬度
    terminal_entry_lon: float = 0.0         # 进终端经度
    terminal_exit_ts: str = ""              # 出终端时间（UTC ISO）
    terminal_exit_lat: float = 0.0          # 出终端纬度
    terminal_exit_lon: float = 0.0          # 出终端经度
    terminal_accum_seconds: int = 0         # 终端内累计飞行秒数
    _last_terminal_check_time: float = 0.0  # 上次终端检查的 monotonic 时间
    _landed: bool = False  # 是否已落地（高度低于终端区下限），落地后停止终端检测

    # ── 扇区跟踪 ──
    sector_index: int = 0
    _sector_recorded: set[str] = field(default_factory=set)  # 已记录的扇区代码

    @property
    def is_associated(self) -> bool:
        """是否已相关：有起降地信息才可能与飞行计划匹配"""
        return bool(self.adep) and bool(self.adest)

    def check_terminal_transition(self, lat: float, lon: float, alt_m: float,
                                   check_time: float, utc_iso: str) -> None:
        """检查终端区进出变化并更新状态

        已落地航班（_landed=True）直接标记不在终端区并跳过一切检测，
        避免落地后高度噪声导致再次误判进终端。
        """
        if self._landed:
            self.in_terminal = False
            return

        # 检测是否落地：高度低于终端区底部，标记为已落地
        cfg = load_terminal_config()
        if alt_m > 0 and alt_m < cfg["floor_m"]:
            self._landed = True
            self.in_terminal = False
            logger.debug("[TERM] %s 已落地，停止终端检测", self.callsign)
            return

        prev = self.in_terminal
        now_in = is_in_terminal(lat, lon, alt_m)
        self.in_terminal = now_in

        dt = check_time - self._last_terminal_check_time if self._last_terminal_check_time > 0 else PROCESS_INTERVAL_SECONDS
        self._last_terminal_check_time = check_time

        if now_in and not prev:
            # 进终端
            self.terminal_entry_ts = utc_iso
            self.terminal_entry_lat = lat
            self.terminal_entry_lon = lon
            logger.debug("[TERM] %s 进终端区 @ %s (%.4f, %.4f) %.0fm",
                         self.callsign, utc_iso, lat, lon, alt_m)
        elif not now_in and prev:
            # 出终端
            self.terminal_exit_ts = utc_iso
            self.terminal_exit_lat = lat
            self.terminal_exit_lon = lon
            logger.debug("[TERM] %s 出终端区 @ %s (%.4f, %.4f) %.0fm",
                         self.callsign, utc_iso, lat, lon, alt_m)
        elif now_in and prev:
            # 在终端内持续飞行：累加时间
            self.terminal_accum_seconds += max(0, int(dt))


class FDRStore:
    """
    内存 FDR 列表，线程安全
    以 callsign 为键 (无呼号时用 SSR)
    """
    # ── 扇区映射（CAT062序号 → 扇区名 → 终端代码） ──
    _SECTOR_MAP: list[tuple[str, str]] = []  # [(扇区名, 终端代码), ...]，索引0对应CAT062序号1
    _SECTOR_BY_CODE: dict[str, str] = {}     # {终端代码: 扇区名}
    _INTERESTED_TERMINALS: set[str] = set()

    @classmethod
    def load_sector_map(cls, path: str | Path = "/home/share/atc_aftn_web/config/SectorInfo.txt") -> None:
        """从 SectorInfo.txt 加载扇区映射"""
        cls._SECTOR_MAP.clear()
        cls._SECTOR_BY_CODE.clear()
        cls._INTERESTED_TERMINALS.clear()
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("//"):
                        continue
                    # 格式：CAT062序号\t扇区名\t终端代码
                    parts = line.split("\t")
                    if len(parts) >= 3:
                        idx_str, sector_name, terminal = parts[0], parts[1], parts[2]
                        terminal = terminal.strip()
                        if terminal.upper() == "NULL":
                            cls._SECTOR_MAP.append((sector_name.strip(), ""))
                        else:
                            term = terminal.upper()
                            cls._SECTOR_MAP.append((sector_name.strip(), term))
                            cls._SECTOR_BY_CODE[term] = sector_name.strip()
            # 统计需关注的对空终端
            cls._INTERESTED_TERMINALS = {t for t in cls._SECTOR_BY_CODE
                                         if t.startswith("ZGJDTM")}
            logger.info("扇区映射已加载: %d 个扇区, %d 个对空终端",
                        len(cls._SECTOR_MAP), len(cls._INTERESTED_TERMINALS))
        except Exception as exc:
            logger.warning("SectorInfo.txt 加载失败: %s", exc)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, FDRRecord] = {}
        self._last_process = 0.0
        self._last_cleanup = 0.0
        # 待写入 DB 的扇区记录（callsign+dof+sector_code → hour）
        self._pending_sectors: dict[tuple[str, str, str], int] = {}
        self.load_sector_map()

    # ── 公开接口 ──────────────────────────────────────────

    def update_from_radar(self, parsed: dict[str, Any],
                             received_at: datetime | None = None) -> None:
        """从 CAT062 解析结果更新或插入一条 FDR"""
        callsign = parsed.get("callsign", "").strip()
        ssr = parsed.get("ssr", "").strip()
        key = callsign or ssr
        if not key:
            return

        now = time.monotonic()
        utc_iso = (received_at or datetime.utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")

        with self._lock:
            rec = self._records.get(key)
            if rec is None:
                rec = FDRRecord()
                self._records[key] = rec

            rec.callsign = callsign
            rec.ssr = ssr
            rec.last_update = now

            # 位置信息
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

            # 尾迹
            if lat or lon:
                last_pt = rec.trail[-1] if rec.trail else None
                trail_dt = now - rec._last_trail_time
                moved = False
                if last_pt:
                    dlat = abs(lat - last_pt[0])
                    dlon = abs(lon - last_pt[1])
                    moved = dlat > 0.02 or dlon > 0.02
                if trail_dt >= 2.5 or moved or not last_pt:
                    rec.trail.append((lat, lon))
                    if len(rec.trail) > 5:
                        rec.trail.pop(0)
                    rec._last_trail_time = now

            # 覆盖非空字段
            if runway := parsed.get("runway", "").strip():
                rec.runway = _validate_runway(runway)
            if fp := parsed.get("flight_procedure", "").strip():
                rec.flight_procedure = _validate_flight_procedure(fp)
            if adep := parsed.get("adep", "").strip():
                rec.adep = adep.upper()
            if adest := parsed.get("adest", "").strip():
                rec.adest = adest.upper()

            # ── 扇区跟踪 ────────────────────────────────
            sector_idx = parsed.get("sector_index", 0)
            if sector_idx and rec.callsign and received_at:
                self._track_sector(rec, sector_idx, received_at)

            # ── 终端区进出检测 ──────────────────────────
            # 有有效位置且高度 > 0 时才检查
            if lat and lon and new_fl > 0:
                rec.check_terminal_transition(lat, lon, new_fl, now, utc_iso)

    def process_updates(self, db: Any) -> None:
        """
        周期性处理：
        1. 清理超时记录
        2. 对有效 FDR 执行飞行计划更新：
           a. 跑道/飞行程序
           b. 终端区进出时间
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

            # 2. 收集需要处理的记录快照
            candidates = [r for r in self._records.values() if r.callsign]

        # 3. 逐条更新飞行计划
        radar_updated = 0
        terminal_updated = 0
        error_count = 0

        # 4. 扇区记录批量刷入 DB
        sector_flushed = self._flush_sector_records(db)

        for rec in candidates:
            try:
                # 3a. 跑道/程序更新
                if rec.runway or rec.flight_procedure:
                    affected = db.update_radar_data(
                        rec.callsign,
                        rec.runway,
                        rec.flight_procedure,
                        adep=rec.adep,
                        adest=rec.adest,
                    )
                    if affected > 0:
                        radar_updated += 1

                # 3b. 终端区数据更新
                if rec.terminal_entry_ts or rec.terminal_exit_ts:
                    affected = db.update_terminal_data(
                        rec.callsign,
                        rec.terminal_entry_ts,
                        rec.terminal_exit_ts,
                        rec.terminal_accum_seconds,
                        adep=rec.adep,
                        adest=rec.adest,
                    )
                    if affected > 0:
                        terminal_updated += 1

            except Exception:
                error_count += 1
                if error_count <= 3:
                    logger.warning("[FDR] %s 更新失败（已累计 %d 次）",
                                   rec.callsign, error_count)

        if radar_updated:
            logger.info("[FDR] 更新 %d 条跑道/程序", radar_updated)
        if terminal_updated:
            logger.info("[FDR] 更新 %d 条终端区数据", terminal_updated)
        if sector_flushed > 0:
            logger.debug("[SECTOR] 刷入 %d 条扇区记录", sector_flushed)
        if error_count > 10:
            logger.info("[FDR] 累计 %d 次更新失败，等待下个周期重试", error_count)

    def get_tracks(self) -> list[dict[str, Any]]:
        """返回所有有效航迹（带位置和终端区信息）"""
        now = time.monotonic()
        with self._lock:
            tracks = []
            for rec in self._records.values():
                if now - rec.last_update > FDR_TTL_SECONDS:
                    continue
                fl_diff = rec.flight_level - rec.prev_flight_level
                level_trend = 'c' if abs(fl_diff) > 1.0 and fl_diff > 0 else \
                              'd' if abs(fl_diff) > 1.0 else 'm'
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
                    "in_terminal": rec.in_terminal,
                })
            return tracks

    # ── 扇区跟踪方法 ─────────────────────────────────────

    def _track_sector(self, rec: FDRRecord, sector_idx: int,
                       received_at: datetime) -> None:
        """跟踪航班首次进入扇区的时间，跨时段只在首时段计数
        注意：调用方已持有 self._lock"""
        if sector_idx < 1 or sector_idx > len(self._SECTOR_MAP):
            return
        _, terminal = self._SECTOR_MAP[sector_idx - 1]
        if not terminal or terminal not in self._INTERESTED_TERMINALS:
            return

        # 已为此航班记录过此扇区，跳过
        if terminal in rec._sector_recorded:
            return

        # received_at 为 UTC
        # sector_flights（小时粒度统计）仍用北京时
        from datetime import timedelta
        bj = received_at + timedelta(hours=8)
        dof = bj.strftime("%Y-%m-%d")
        hour = bj.hour
        slot_bj = (bj.hour * 60 + bj.minute) // 10
        # sector_traffic_10min（语音页折线图）使用 UTC
        utc_dof = received_at.strftime("%Y-%m-%d")
        utc_slot = (received_at.hour * 60 + received_at.minute) // 10
        key = (rec.callsign, dof, terminal)

        if key not in self._pending_sectors:
            # 存储 (hour, slot_bj, utc_dof, utc_slot)
            self._pending_sectors[key] = (hour, slot_bj, utc_dof, utc_slot)
        rec._sector_recorded.add(terminal)

    def _flush_sector_records(self, db: Any) -> int:
        with self._lock:
            pending = dict(self._pending_sectors)
            self._pending_sectors.clear()

        if not pending:
            return 0

        flushed = 0
        for (callsign, dof, terminal_code), (hour, slot_bj, utc_dof, utc_slot) in pending.items():
            try:
                if db.record_sector_flight(callsign, dof, terminal_code, hour):
                    flushed += 1
                # 10 分钟粒度使用 UTC
                try:
                    db.record_sector_flight_10min(utc_dof, terminal_code, utc_slot)
                except Exception:
                    pass
            except Exception:
                logger.debug("[SECTOR] 写入失败 %s/%s/%s", callsign, dof, terminal_code)
        return flushed

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
