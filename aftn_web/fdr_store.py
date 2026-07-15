"""
飞行数据记录 (FDR) 存储 — 内存中的航迹快照列表

CAT062 解析后的每条航迹数据写入 FDR 列表，
由定时器每 4 秒检查一次，执行：
- 跑道/飞行程序更新（来自 CAT062 RDS/SID/STAR）
- 终端区进出检测（多边形 + 高度）

TTL: 超过 8 秒未更新的记录自动清理。

v2.1.x 新增：
- 全量航迹点累计（_full_track）
- 航迹保存到 DB（进港落地后 / 出港离区时）
- 自定义保存区域+机场配置
"""

from __future__ import annotations

import json
import logging
import math
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
FDR_TTL_SECONDS = 16

# 定期检查间隔
PROCESS_INTERVAL_SECONDS = 4

# 航迹采样间隔（秒）：每 N 秒采集一个全量航迹点
TRACK_SAMPLE_INTERVAL = 5

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


def _point_in_rect(lat: float, lon: float,
                   tl_lat: float, tl_lon: float,
                   br_lat: float, br_lon: float) -> bool:
    """判断点是否在矩形区域内"""
    min_lat = min(tl_lat, br_lat)
    max_lat = max(tl_lat, br_lat)
    min_lon = min(tl_lon, br_lon)
    max_lon = max(tl_lon, br_lon)
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


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
    qnh_height: float = 0.0           # QNH 修正高度 (米)，来自 CAT062 I135
    qnh_applied: bool = False         # I135 QNH 修正标志
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
    _landed_at: float = 0.0  # 落地时的 monotonic 时间戳

    # ── 扇区跟踪 ──
    sector_index: int = 0
    _sector_recorded: set[str] = field(default_factory=set)  # 已记录的扇区代码

    # ── 移交点（雷达探测） ──────────────────────────────
    handover_pt: str = ""                                     # 雷达探测的移交点（TransPt 匹配）
    _handover_pt_updated: bool = False                        # 移交点是否已写回 DB

    # ── 航迹保存 ──────────────────────────────────────────
    _full_track: list[dict] = field(default_factory=list)     # 全量航迹 [{ts,lt,ln,fl,hd,sp,...}]
    _track_saved: bool = False                                # 是否已保存到 DB
    _last_track_ts: float = 0.0                               # 上次采集全量点的时间（monotonic）
    _was_in_save_area: bool = False                           # 上次检查时是否在保存区域内
    _area_departed: bool = False                              # 是否已触发离区保存

    @property
    def is_associated(self) -> bool:
        """是否已相关：有起降地信息才可能与飞行计划匹配"""
        return bool(self.adep) and bool(self.adest)

    def record_full_track_point(self, lat: float, lon: float, fl: float,
                                 heading: float, speed: float,
                                 runway: str, flight_procedure: str,
                                 adep: str, adest: str,
                                 utc_iso: str, now_mono: float) -> None:
        """采集一个全量航迹点（按采样间隔）"""
        dt = now_mono - self._last_track_ts
        if dt < TRACK_SAMPLE_INTERVAL and self._full_track:
            return
        self._full_track.append({
            "ts": utc_iso,
            "lt": lat,
            "ln": lon,
            "fl": fl,
            "hd": heading,
            "sp": speed,
            "rw": runway,
            "fp": flight_procedure,
            "ap": adep,
            "ad": adest,
        })
        self._last_track_ts = now_mono

    def get_full_track_json(self) -> str:
        """返回全量航迹的 JSON 字符串"""
        return json.dumps(self._full_track, ensure_ascii=False, separators=(",", ":"))

    def is_arrival_to_airport(self, airports: set[str]) -> bool:
        """检查航班是否进港到指定机场"""
        return self.adest.upper() in airports

    def is_departure_from_airport(self, airports: set[str]) -> bool:
        """检查航班是否从指定机场出港"""
        return self.adep.upper() in airports

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
            self._landed_at = time.monotonic()
            self.in_terminal = False
            # 落地时立即赋值退出时间（UTC），无需等ATA
            self.terminal_exit_ts = utc_iso
            logger.debug("[TERM] %s 已落地(exit=%s)，停止终端检测", self.callsign, utc_iso)
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
    def load_sector_map(cls, path: str | Path = "/home/share/atc_datahub/config/SectorInfo.txt") -> None:
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

    @classmethod
    def load_trans_pt_files(cls) -> None:
        """加载进/出港移交点坐标文件（TransPt_inbound.txt / TransPt_outbound.txt）"""
        base = Path("/home/share/atc_datahub/config")
        files = {
            "inbound": base / "TransPt_inbound.txt",
            "outbound": base / "TransPt_outbound.txt",
        }
        for direction, path in files.items():
            result: dict[str, tuple[float, float]] = {}
            try:
                if not path.exists():
                    logger.debug("TransPt_%s.txt 不存在: %s", direction, path)
                    continue
                for line in path.read_text("utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    name = parts[0].strip().upper()
                    lat_str = parts[1].strip()
                    lon_str = parts[2].strip()
                    lat = cls._dms_to_decimal(lat_str)
                    lon = cls._dms_to_decimal(lon_str)
                    if lat is not None and lon is not None:
                        result[name] = (lat, lon)
                if direction == "inbound":
                    cls._INBOUND_PTS = result
                else:
                    cls._OUTBOUND_PTS = result
                if result:
                    logger.info(
                        "TransPt_%s.txt 已加载: %s", direction,
                        list(result.keys()),
                    )
            except Exception as exc:
                logger.warning("TransPt_%s.txt 加载失败: %s", direction, exc)

    # 移交点坐标缓存（类变量）
    _INBOUND_PTS: dict[str, tuple[float, float]] = {}  # 进港移交点 {名称: (lat, lon)}
    _OUTBOUND_PTS: dict[str, tuple[float, float]] = {}  # 出港移交点
    _TRANS_PT_LOADED = False

    @staticmethod
    def _dms_to_decimal(dms: str) -> float | None:
        """将 DMS 格式 "22,29,24N" 转为十进制度数"""
        try:
            dms = dms.strip()
            if not dms:
                return None
            direction = dms[-1]
            nums = dms[:-1].strip()
            parts = nums.split(",")
            if len(parts) != 3:
                return None
            d = float(parts[0])
            m = float(parts[1])
            s = float(parts[2])
            decimal = d + m / 60.0 + s / 3600.0
            if direction in ("S", "W"):
                decimal = -decimal
            return decimal
        except (ValueError, IndexError, TypeError):
            return None

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """计算两点间的大圆距离（千米）"""
        R = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) ** 2 +
             math.cos(math.radians(lat1)) *
             math.cos(math.radians(lat2)) *
             math.sin(dlon / 2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c

    def _detect_handover_pt(self, lat: float, lon: float, adep: str, adest: str,
                             airports: set[str]) -> str:
        """根据当前位置和航班类型，检测最近的移交点（<10km 即采用）

        进港航班（adest 在 airports 中）→ 对照 TransPt_inbound.txt
        出港航班（adep 在 airports 中）→ 对照 TransPt_outbound.txt
        返回匹配的移交点名称（大写），未匹配返回空字符串
        """
        if not lat or not lon:
            return ""
        if adest.upper() in airports:
            pts = self._INBOUND_PTS
        elif adep.upper() in airports:
            pts = self._OUTBOUND_PTS
        else:
            return ""

        # 记录所有点位的距离（方便调试）
        for name, (pt_lat, pt_lon) in pts.items():
            dist = self._haversine_km(lat, lon, pt_lat, pt_lon)
            if dist < 5.0:
                logger.info(
                    "[HANDOVER] %s 距 %s %.1fkm (<5km @ %.4f,%.4f)，采用移交点 %s",
                    self._callsign_hint, name, dist, lat, lon, name,
                )
                return name
            if dist < 20.0:
                logger.debug(
                    "[HANDOVER] %s 距 %s %.1fkm (>5km但<20km @ %.4f,%.4f)",
                    self._callsign_hint, name, dist, lat, lon,
                )
        return ""

    _callsign_hint = ""  # 临时调试用

    def __init__(self, track_config: dict | None = None) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, FDRRecord] = {}
        self._last_process = 0.0
        self._last_cleanup = 0.0
        # 待写入 DB 的扇区记录（callsign+dof+sector_code → hour）
        self._pending_sectors: dict[tuple[str, str, str], int] = {}
        self.load_sector_map()

        # ── 移交点坐标加载（类变量，仅加载一次） ──────────
        if not FDRStore._TRANS_PT_LOADED:
            FDRStore._TRANS_PT_LOADED = True
            self.load_trans_pt_files()

        # ── 航迹保存配置 ──────────────────────────────────
        self._track_enabled = False
        self._track_airports: set[str] = {"ZGSZ", "ZGSD", "VMMC"}
        self._track_tl_lat = 23.5
        self._track_tl_lon = 112.0
        self._track_br_lat = 21.0
        self._track_br_lon = 115.5

        if track_config:
            self._track_enabled = bool(track_config.get("enabled", False))
            airports = track_config.get("airports", ["ZGSZ", "ZGSD", "VMMC"])
            self._track_airports = {a.upper() for a in airports}
            tl = track_config.get("area_top_left", {})
            br = track_config.get("area_bottom_right", {})
            self._track_tl_lat = float(tl.get("lat", 23.5))
            self._track_tl_lon = float(tl.get("lon", 112.0))
            self._track_br_lat = float(br.get("lat", 21.0))
            self._track_br_lon = float(br.get("lon", 115.5))
            if self._track_enabled:
                logger.info(
                    "航迹保存已启用: 机场=%s, 区域=(%.1f,%.1f)-(%.1f,%.1f)",
                    self._track_airports,
                    self._track_tl_lat, self._track_tl_lon,
                    self._track_br_lat, self._track_br_lon,
                )

    def _point_in_save_area(self, lat: float, lon: float) -> bool:
        """判断点是否在自定义保存区域内"""
        return _point_in_rect(
            lat, lon,
            self._track_tl_lat, self._track_tl_lon,
            self._track_br_lat, self._track_br_lon,
        )

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

            # QNH 修正高度：优先用于落地检测和轨迹记录
            rec.qnh_height = parsed.get("qnh_height_m", 0.0)
            rec.qnh_applied = parsed.get("qnh_applied", False)
            # 有效高度：QNH 修正 >=0 且标记已修正时用 QNH，否则用标准气压高度
            eff_fl = rec.qnh_height if (rec.qnh_applied and rec.qnh_height >= 0) else new_fl

            rec.speed = parsed.get("speed", 0.0)

            # 尾迹（仅显示用，保留最近5个）
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
            rw = parsed.get("runway", "").strip()
            fp = parsed.get("flight_procedure", "").strip()
            adep_raw = parsed.get("adep", "").strip()
            adest_raw = parsed.get("adest", "").strip()

            if rw:
                rec.runway = _validate_runway(rw)
            if fp:
                rec.flight_procedure = _validate_flight_procedure(fp)
            if adep_raw:
                rec.adep = adep_raw.upper()
            if adest_raw:
                rec.adest = adest_raw.upper()

            # ── 全量航迹点采集（所有航班都采集，仅在配置启用时保存） ──
            if lat or lon:
                rec.record_full_track_point(
                    lat, lon, eff_fl,
                    rec.heading, rec.speed,
                    rec.runway, rec.flight_procedure,
                    rec.adep, rec.adest,
                    utc_iso, now,
                )

            # ── 移交点检测（雷达位置 vs TransPt 文件） ──
            # 只在有有效位置且有起降地信息时才检测
            if lat and lon and (rec.adep or rec.adest):
                self._callsign_hint = rec.callsign
                detected_hp = self._detect_handover_pt(
                    lat, lon, rec.adep, rec.adest,
                    self._track_airports,
                )
                if detected_hp and detected_hp != rec.handover_pt:
                    logger.info(
                        "[HANDOVER] %s 雷达探测到移交点: %s (航段 %s->%s)，替换原 %s",
                        rec.callsign, detected_hp, rec.adep, rec.adest,
                        rec.handover_pt or '(空)',
                    )
                    rec.handover_pt = detected_hp
                    rec._handover_pt_updated = False

            # ── 出港离区检测（仅在航迹保存启用时） ──
            if self._track_enabled and (rec.adep or rec.adest):
                # 已有起降地信息且配置启用时才检测
                in_area = self._point_in_save_area(lat, lon)
                if rec._was_in_save_area and not in_area and not rec._area_departed:
                    # 从区内到区外 → 触发离区保存
                    rec._area_departed = True
                    logger.info(
                        "[TRACK] %s %s->%s 离开保存区域，标记出港",
                        rec.callsign, rec.adep, rec.adest,
                    )
                rec._was_in_save_area = in_area

            # ── 扇区跟踪 ────────────────────────────────
            sector_idx = parsed.get("sector_index", 0)
            if sector_idx and rec.callsign and received_at:
                self._track_sector(rec, sector_idx, received_at)

            # ── 终端区进出检测 ──────────────────────────
            # 有有效位置且高度 > 0 时才检查
            if lat and lon and eff_fl > 0:
                rec.check_terminal_transition(lat, lon, eff_fl, now, utc_iso)

    def _save_track(self, db: Any, rec: FDRRecord, track_type: str) -> bool:
        """保存航迹到数据库
        
        返回 True 表示成功保存
        """
        if rec._track_saved:
            return False
        if not rec.callsign:
            return False
        if len(rec._full_track) < 2:
            logger.debug("[TRACK] %s 航迹点不足，跳过保存", rec.callsign)
            return False

        points_json = rec.get_full_track_json()
        dof = datetime.utcnow().strftime("%Y-%m-%d")
        start_time = rec._full_track[0].get("ts", "")
        end_time = rec._full_track[-1].get("ts", "")

        try:
            from .database import save_flight_track
            track_id = save_flight_track(
                db, rec.callsign, track_type,
                rec.adep, rec.adest, dof,
                points_json, start_time, end_time,
            )
            rec._track_saved = True
            logger.info(
                "[TRACK] %s %s 航迹已保存 (id=%d, 点=%d, %s->%s)",
                rec.callsign, track_type, track_id,
                len(rec._full_track), start_time, end_time,
            )
            return True
        except Exception as exc:
            logger.error("[TRACK] %s 航迹保存失败: %s", rec.callsign, exc)
            return False

    def process_updates(self, db: Any) -> None:
        """
        周期性处理：
        1. 清理超时记录
        2. 对有效 FDR 执行飞行计划更新：
           a. 跑道/飞行程序
           b. 终端区进出时间
        3. 航迹保存：
           a. 进港：落地后标牌消失（过期）时保存
           b. 出港：离开自定义区域时保存
        """
        now = time.monotonic()
        with self._lock:
            # 1. 清理超时
            expired = [k for k, r in self._records.items()
                       if now - r.last_update > FDR_TTL_SECONDS]
            # 1a. 已进终端区且未离区的暂不删，避免短时雷达间隙丢失终端状态
            keep_keys = []
            for k in expired:
                r = self._records[k]
                if r.terminal_entry_ts and not r.terminal_exit_ts and r.callsign:
                    if r.terminal_accum_seconds >= 60:
                        keep_keys.append(k)
            for k in expired:
                if k not in keep_keys:
                    del self._records[k]
            if expired:
                logger.debug(
                    "FDR cleanup: removed %d expired records (%d 保留)",
                    len(expired) - len(keep_keys), len(keep_keys),
                )

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
                # 3a. 跑道/程序/移交点更新
                if rec.runway or rec.flight_procedure or (rec.handover_pt and not rec._handover_pt_updated):
                    affected = db.update_radar_data(
                        rec.callsign,
                        rec.runway,
                        rec.flight_procedure,
                        adep=rec.adep,
                        adest=rec.adest,
                        handover_pt=rec.handover_pt,
                    )
                    if affected > 0:
                        radar_updated += 1
                        if rec.handover_pt:
                            rec._handover_pt_updated = True

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

        # ── 5. 航迹保存 ──────────────────────────────────────
        if self._track_enabled:
            self._process_track_save(db, candidates)

        # 6. 删除已保留的过期记录（处理完这一轮才真正清理）
        if keep_keys:
            with self._lock:
                for k in keep_keys:
                    self._records.pop(k, None)

        if radar_updated:
            logger.info("[FDR] 更新 %d 条跑道/程序", radar_updated)
        if terminal_updated:
            logger.info("[FDR] 更新 %d 条终端区数据", terminal_updated)
        if sector_flushed > 0:
            logger.debug("[SECTOR] 刷入 %d 条扇区记录", sector_flushed)
        if error_count > 10:
            logger.info("[FDR] 累计 %d 次更新失败，等待下个周期重试", error_count)

    def _process_track_save(self, db: Any, candidates: list[FDRRecord]) -> None:
        """处理航迹保存逻辑
        
        在 process_updates() 中调用，需在清理超时记录后进行。
        """
        saved_count = 0
        for rec in candidates:
            if rec._track_saved:
                continue
            if len(rec._full_track) < 2:
                continue

            # A) 进港航班保存条件：已落地 + 到港目的地
            #    落地后等待 10 秒防误判，然后立即保存，不等记录过期
            if rec._landed and rec.is_arrival_to_airport(self._track_airports):
                now = time.monotonic()
                landed_grace = (now - rec._landed_at) > 10.0  # 落地后等 10 秒
                is_expiring = (now - rec.last_update) > (FDR_TTL_SECONDS * 0.8)
                if landed_grace or is_expiring:
                    if self._save_track(db, rec, "ARRIVAL"):
                        saved_count += 1
                    # 保存后标记，避免下次再处理

            # B) 出港航班保存条件：离开自定义区域
            elif rec._area_departed and rec.is_departure_from_airport(self._track_airports):
                if self._save_track(db, rec, "DEPARTURE"):
                    saved_count += 1

        if saved_count > 0:
            logger.info("[TRACK] 本轮保存 %d 条航迹", saved_count)

    def get_tracks(self) -> list[dict[str, Any]]:
        """返回所有有效航迹（带位置和终端区信息）"""
        now = time.monotonic()
        with self._lock:
            tracks = []
            for rec in self._records.values():
                if now - rec.last_update > FDR_TTL_SECONDS:
                    continue
                # 对外暴露有效高度（QNH 修正优先）
                disp_fl = rec.qnh_height if (rec.qnh_applied and rec.qnh_height >= 0) else rec.flight_level
                fl_diff = rec.flight_level - rec.prev_flight_level
                level_trend = 'c' if abs(fl_diff) > 1.0 and fl_diff > 0 else \
                              'd' if abs(fl_diff) > 1.0 else 'm'
                tracks.append({
                    "callsign": rec.callsign,
                    "ssr": rec.ssr,
                    "latitude": rec.latitude,
                    "longitude": rec.longitude,
                    "heading": rec.heading,
                    "flight_level": disp_fl,
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
                    # 记录 callsign 详情（用于扇区合并去重）
                    db.record_sector_flight_10min_detail(
                        utc_dof, terminal_code, utc_slot, callsign, dof
                    )
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
