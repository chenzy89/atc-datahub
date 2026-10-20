"""机场进离场航迹 — 基于雷达历史（radar_history）按空域识别

flight_tracks 表只保存深圳终端区相关航班，广州/惠州机场的航班不在其中；
因此本模块直接扫描 data/radar_history/radar_YYYYMMDD.jsonl.gz。

识别规则：航班轨迹任一点同时满足
    距机场中心 <= radius_km  且  高度 <= ceiling_m
即视为该机场的进离场航班。进/离场优先用雷达帧里的 ap/ad（起降地）判断，
没有起降地时按进入空域前后的高度变化/最接近机场的位置推断。

扫描一天约 10-20 秒，结果按日期缓存到 data/airport_tracks/YYYY-MM-DD.json.gz，
同一天再次打开秒开；当天的缓存 5 分钟后自动重建。
"""

from __future__ import annotations

import gzip
import json
import logging
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("aftn_web.airport_tracks")

AIRSPACE_FILE = Path("/home/share/atc_datahub/config/airport_airspace.json")
HISTORY_DIR = Path("/home/share/atc_datahub/data/radar_history")
CACHE_DIR = Path("/home/share/atc_datahub/data/airport_tracks")

_AIRSPACE_DEFAULTS = {
    "ZGGG": {
        "name": "广州白云机场",
        "center": {"lat": 23.390278, "lon": 113.298889},
        "radius_km": 6, "ceiling_m": 300,
        "display_radius_km": 60, "color_scale_m": 3000,
    },
    "ZGHZ": {
        "name": "惠州平潭机场",
        "center": {"lat": 23.048611, "lon": 114.597500},
        "radius_km": 8, "ceiling_m": 500,
        "display_radius_km": 60, "color_scale_m": 3000,
    },
}

_EARTH_R = 6371000.0
_TODAY_TTL = 1800          # 当天缓存有效期（秒）
_CACHE_VERSION = 2        # 识别逻辑变更时递增，旧缓存自动失效
_SPLIT_GAP_SECONDS = 1800  # 同一呼号两段航迹的时间间隔阈值
_MIN_POINTS_PER_TRACK = 2

_build_lock = threading.Lock()
_mem_cache: dict[str, tuple[float, dict]] = {}


def load_airspace() -> dict[str, dict]:
    """读取机场空域配置（每次重读，改文件后无需重启）。"""
    airports: dict[str, dict] = {}
    try:
        raw = json.loads(AIRSPACE_FILE.read_text(encoding="utf-8"))
        for code, cfg in (raw.get("airports") or {}).items():
            if not isinstance(cfg, dict):
                continue
            center = cfg.get("center") or {}
            try:
                airports[str(code).upper()] = {
                    "name": str(cfg.get("name") or code),
                    "center": {"lat": float(center["lat"]), "lon": float(center["lon"])},
                    "radius_km": float(cfg.get("radius_km", 6)),
                    "ceiling_m": float(cfg.get("ceiling_m", 300)),
                    "display_radius_km": float(cfg.get("display_radius_km", 60)),
                    "color_scale_m": float(cfg.get("color_scale_m", 3000)),
                }
            except (KeyError, TypeError, ValueError):
                logger.warning("机场空域配置 %s 格式有误，已跳过", code)
    except FileNotFoundError:
        logger.warning("未找到 %s，使用内置机场空域配置", AIRSPACE_FILE)
    except Exception:
        logger.exception("读取机场空域配置失败，使用内置配置")
    if not airports:
        airports = {k: json.loads(json.dumps(v)) for k, v in _AIRSPACE_DEFAULTS.items()}
    return airports


def _history_path(date_str: str) -> Path:
    return HISTORY_DIR / f"radar_{date_str.replace('-', '')}.jsonl.gz"


def _cache_path(date_str: str) -> Path:
    return CACHE_DIR / f"{date_str}.json.gz"


def _dist_m(lat: float, lon: float, clat: float, clon: float, cos_clat: float) -> float:
    dlat = math.radians(lat - clat)
    dlon = math.radians(lon - clon)
    a = (math.sin(dlat / 2) ** 2
         + cos_clat * math.cos(math.radians(lat)) * math.sin(dlon / 2) ** 2)
    return 2 * _EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def _scan_day(date_str: str, airports: dict[str, dict]) -> dict[str, Any]:
    """扫描一天的雷达历史，返回各机场的进离场航迹。"""
    path = _history_path(date_str)
    if not path.exists():
        return {
            code: {"tracks": [],
                   "stats": {"arrival": 0, "departure": 0, "total": 0, "scanned_points": 0}}
            for code in airports
        }

    # 预计算各机场参数（元组比字典查找快）
    plist = []
    for code, cfg in airports.items():
        clat = cfg["center"]["lat"]
        clon = cfg["center"]["lon"]
        cos_clat = math.cos(math.radians(clat))
        disp_km = cfg["display_radius_km"]
        plist.append((
            code, clat, clon, cos_clat,
            clat - disp_km / 111.0 - 0.02,
            clat + disp_km / 111.0 + 0.02,
            clon - disp_km / (111.0 * max(0.2, cos_clat)) - 0.02,
            clon + disp_km / (111.0 * max(0.2, cos_clat)) + 0.02,
            (disp_km * 1000.0) ** 2,
        ))

    # code -> callsign -> [(ts, lt, ln, fl, ap, ad)]
    groups: dict[str, dict[str, list[tuple]]] = {code: {} for code in airports}
    total_points = 0
    # 配色上限以上再留 1000m 余量，更高高度的点不进图（只影响显示，不影响识别）
    fl_cap = max(cfg["color_scale_m"] for cfg in airports.values()) + 1000.0

    with gzip.open(str(path), "rt", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            i = line.find('"lt":')
            if i < 0:
                continue
            j = line.find(",", i + 5)
            if j < 0:
                continue
            try:
                lt = float(line[i + 5:j])
                i2 = line.find('"ln":')
                j2 = line.find(",", i2 + 5)
                ln = float(line[i2 + 5:j2])
            except (ValueError, TypeError):
                continue

            for (code, clat, clon, cos_clat, lat_lo, lat_hi, lon_lo, lon_hi, disp_m2) in plist:
                if lt < lat_lo or lt > lat_hi or ln < lon_lo or ln > lon_hi:
                    continue
                # 等距近似平方距离（60km 量级足够精确，比 haversine 快很多）
                dy = (lt - clat) * 111000.0
                dx = (ln - clon) * 111000.0 * cos_clat
                if dx * dx + dy * dy > disp_m2:
                    continue

                # 只对命中范围的点解析其余字段
                try:
                    i = line.find('"fl":')
                    j = line.find(",", i + 5)
                    fl = float(line[i + 5:j])
                    if fl > fl_cap:
                        continue
                    i = line.find('"cs":"')
                    j = line.find('"', i + 6)
                    cs = line[i + 6:j].strip().upper()
                    if not cs:
                        continue
                    i = line.find('"ts":"') + 6
                    j = line.find('"', i)
                    ts = line[i:j]
                    # ap/ad 只在低高度点提取（高空点几乎不会带计划信息，省时间）
                    if fl < 2000.0:
                        i = line.find('"ap":"')
                        j = line.find('"', i + 6)
                        ap = line[i + 6:j].strip().upper()
                        i = line.find('"ad":"')
                        j = line.find('"', i + 6)
                        ad = line[i + 6:j].strip().upper()
                    else:
                        ap = ad = ""
                except (ValueError, TypeError):
                    continue

                groups[code].setdefault(cs, []).append(
                    (ts, round(lt, 6), round(ln, 6), round(fl, 1), ap, ad)
                )
                total_points += 1
                break  # 同一时刻只可能属于一个机场（两机场相距约 130km）

    result: dict[str, Any] = {}
    for code, cfg in airports.items():
        result[code] = _build_airport_tracks(code, cfg, groups.get(code, {}),
                                             total_points)
    return result


def _ts_seconds(ts: str) -> float:
    """同一文件内都是同一天，只取时分秒即可，比 strptime 快得多。"""
    try:
        sec = int(ts[11:13]) * 3600 + int(ts[14:16]) * 60 + int(ts[17:19])
        if len(ts) >= 23 and ts[19] == '.':
            sec += int(ts[20:23]) / 1000.0
        return sec
    except (ValueError, IndexError):
        return 0.0


def _build_airport_tracks(code: str, cfg: dict, by_cs: dict[str, list[tuple]],
                          total_scanned: int) -> dict[str, Any]:
    radius_m = cfg["radius_km"] * 1000.0
    ceiling_m = cfg["ceiling_m"]
    clat = cfg["center"]["lat"]
    clon = cfg["center"]["lon"]
    cos_clat = math.cos(math.radians(clat))
    radius_m2 = radius_m * radius_m
    color_cap = cfg["color_scale_m"]
    tracks: list[dict] = []
    n_arr = 0
    n_dep = 0

    for cs, pts in by_cs.items():
        pts.sort(key=lambda x: x[0])
        # 按时间间隔切分为独立航段（同一呼号一天可能飞多段）
        segments: list[list[tuple]] = []
        cur: list[tuple] = []
        last_sec = None
        for pt in pts:
            sec = _ts_seconds(pt[0])
            if last_sec is not None and sec - last_sec > _SPLIT_GAP_SECONDS:
                if len(cur) >= _MIN_POINTS_PER_TRACK:
                    segments.append(cur)
                cur = []
            cur.append(pt)
            last_sec = sec
        if len(cur) >= _MIN_POINTS_PER_TRACK:
            segments.append(cur)

        for seg in segments:
            qual = []
            min_d = None
            min_idx = 0
            for idx, pt in enumerate(seg):
                dy = (pt[1] - clat) * 111000.0
                dx = (pt[2] - clon) * 111000.0 * cos_clat
                d2 = dx * dx + dy * dy
                if min_d is None or d2 < min_d:
                    min_d = d2
                    min_idx = idx
                if d2 <= radius_m2 and pt[3] <= ceiling_m:
                    qual.append((idx, pt[3]))
            if not qual:
                continue

            # 进/离场判定：优先看高度变化（雷达里的 ap/ad 偶尔会串计划，
            # 例如把落地航班标成从本场起飞）；趋势不明显时再看 ap/ad
            has_arr = any(pt[5] == code for pt in seg)
            has_dep = any(pt[4] == code for pt in seg)
            first_fl = seg[0][3]
            last_fl = seg[-1][3]
            if last_fl < first_fl - 100:
                typ = "ARRIVAL"
            elif last_fl > first_fl + 100:
                typ = "DEPARTURE"
            elif has_arr and not has_dep:
                typ = "ARRIVAL"
            elif has_dep and not has_arr:
                typ = "DEPARTURE"
            else:
                typ = "ARRIVAL" if min_idx > len(seg) / 2 else "DEPARTURE"

            if typ == "ARRIVAL":
                n_arr += 1
            else:
                n_dep += 1

            # 抽稀：隔点保留，首尾必留（原始约 4 秒一个点）；
            # 只画到配色上限高度（默认 3000m）附近，边界点保留
            n = len(seg)
            out_pts = []
            for idx, pt in enumerate(seg):
                if idx % 2 != 0 and idx != n - 1:
                    continue
                if pt[3] <= color_cap or idx == 0 or idx == n - 1 \
                        or (idx > 0 and seg[idx - 1][3] <= color_cap) \
                        or (idx + 1 < n and seg[idx + 1][3] <= color_cap):
                    out_pts.append({"lt": pt[1], "ln": pt[2], "fl": pt[3], "ts": pt[0]})
            if len(out_pts) < _MIN_POINTS_PER_TRACK:
                continue
            tracks.append({
                "callsign": cs,
                "type": typ,
                "adep": next((pt[4] for pt in seg if pt[4]), ""),
                "adest": next((pt[5] for pt in seg if pt[5]), ""),
                "points": out_pts,
            })

    tracks.sort(key=lambda t: (t["points"][0].get("ts", ""), t["callsign"]))
    return {
        "stats": {
            "arrival": n_arr,
            "departure": n_dep,
            "total": n_arr + n_dep,
            "scanned_points": total_scanned,
        },
        "tracks": tracks,
    }


def _build_payload(date_str: str, airports: dict[str, dict]) -> dict[str, Any]:
    raw = _scan_day(date_str, airports)
    return {
        "v": _CACHE_VERSION,
        "date": date_str,
        "airports": {
            code: {
                **raw.get(code, {"tracks": [], "stats": {}}),
                "airport": {
                    "icao": code,
                    "name": cfg["name"],
                    "center": cfg["center"],
                    "radius_km": cfg["radius_km"],
                    "ceiling_m": cfg["ceiling_m"],
                    "display_radius_km": cfg["display_radius_km"],
                    "color_scale_m": cfg["color_scale_m"],
                },
            }
            for code, cfg in airports.items()
        },
    }


def _load_payload(date_str: str, airports: dict[str, dict]) -> dict[str, Any]:
    """带内存 + 磁盘缓存的读取。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ttl = _TODAY_TTL if date_str == today else None
    try:
        cfg_mtime = AIRSPACE_FILE.stat().st_mtime
    except OSError:
        cfg_mtime = 0.0

    now = time.time()
    hit = _mem_cache.get(date_str)
    if hit and hit[2] == cfg_mtime and (ttl is None or now - hit[0] < ttl):
        return hit[1]

    cpath = _cache_path(date_str)
    if ttl is None and cpath.exists():
        try:
            if cpath.stat().st_mtime >= cfg_mtime:
                with gzip.open(str(cpath), "rt", encoding="utf-8") as f:
                    payload = json.load(f)
                if (payload.get("v") == _CACHE_VERSION
                        and all(code in payload.get("airports", {}) for code in airports)):
                    _mem_cache[date_str] = (now, payload, cfg_mtime)
                    return payload
        except Exception:
            logger.exception("读取机场航迹缓存失败: %s", cpath)

    with _build_lock:
        # 二次检查（等锁期间可能已被其它请求生成）
        hit = _mem_cache.get(date_str)
        if hit and hit[2] == cfg_mtime and (ttl is None or time.time() - hit[0] < ttl):
            return hit[1]

        t0 = time.time()
        payload = _build_payload(date_str, airports)
        logger.info("机场航迹扫描完成: %s（%.1fs）", date_str, time.time() - t0)

        _mem_cache[date_str] = (time.time(), payload, cfg_mtime)
        if len(_mem_cache) > 8:
            for k in sorted(_mem_cache, key=lambda k: _mem_cache[k][0])[:5]:
                _mem_cache.pop(k, None)

        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = cpath.with_suffix(".tmp")
            with gzip.open(str(tmp), "wt", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            tmp.replace(cpath)
        except Exception:
            logger.exception("写入机场航迹缓存失败: %s", cpath)
        return payload


def get_airport_tracks(date_str: str, code: str) -> dict[str, Any]:
    """返回指定机场当天的进离场航迹（含空域配置）。"""
    airports = load_airspace()
    code = code.upper()
    if code not in airports:
        raise KeyError(code)
    payload = _load_payload(date_str, airports)
    item = payload["airports"].get(code) or {"tracks": [], "stats": {}}
    return {
        "airport": item["airport"],
        "date": date_str,
        "stats": item.get("stats", {}),
        "tracks": item.get("tracks", []),
    }
