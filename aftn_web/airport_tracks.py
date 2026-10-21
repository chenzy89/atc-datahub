"""机场进离场航迹 — 基于雷达历史（radar_history）按空域识别

flight_tracks 表只保存深圳终端区相关航班，广州/深圳/珠海/惠州 的完整航迹
需要直接扫描 data/radar_history/radar_YYYYMMDD.jsonl.gz。

识别规则（两遍扫描）：
  第一遍：找“击中点”——航迹点同时满足『距机场中心 <= radius_km』且
          『高度 <= ceiling_m』，按呼号+时间聚类成航段（间隔 >30 分钟算新航段）。
  第二遍：把该航段前后 ±30 分钟内的所有点（高度 <= store_max_alt_m）全部收集，
          即“整条进离场航迹”，不再限制水平范围；多点命中多个机场时，
          每个点归给最近的机场，避免邻场重复。
  进/离场判定：优先看航段首尾高度趋势，其次看雷达帧 ap/ad。

结果按日期缓存到 data/airport_tracks/YYYY-MM-DD.json.gz，同一天再次打开秒开；
当天缓存 30 分钟后自动重建。
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
        "display_radius_km": 60, "color_scale_m": 6000, "alt_step_m": 300,
    },
    "ZGSZ": {
        "name": "深圳宝安机场",
        "center": {"lat": 22.637778, "lon": 113.798056},
        "radius_km": 6, "ceiling_m": 300,
        "display_radius_km": 60, "color_scale_m": 6000, "alt_step_m": 300,
    },
    "ZGSD": {
        "name": "珠海金湾机场",
        "center": {"lat": 22.006111, "lon": 113.376389},
        "radius_km": 5, "ceiling_m": 300,
        "display_radius_km": 60, "color_scale_m": 6000, "alt_step_m": 300,
    },
    "ZGHZ": {
        "name": "惠州平潭机场",
        "center": {"lat": 23.048611, "lon": 114.597500},
        "radius_km": 8, "ceiling_m": 500,
        "display_radius_km": 60, "color_scale_m": 6000, "alt_step_m": 300,
    },
}

_EARTH_R = 6371000.0
_TODAY_TTL = 1800          # 当天缓存有效期（秒）
_CACHE_VERSION = 5        # 识别/存储逻辑变更时递增，旧缓存自动失效
_MAX_DATE_SPAN = 7         # 一次查询最多跨多少天（每天都要扫一遍历史）
_SPLIT_GAP_SECONDS = 1800  # 同一呼号两次进入空域的间隔阈值（聚类新航段）
_PAD_SECONDS = 1800        # 航段前后额外收集的时间（秒）
_POINT_DECIMATE = 3        # 抽稀：每 3 个点留 1 个（约 12 秒一个点）
_MIN_POINTS_PER_TRACK = 2
_STORE_MAX_ALT_DEFAULT = 6000

_build_lock = threading.Lock()
_mem_cache: dict[str, tuple[float, dict, float]] = {}


def load_airspace() -> dict[str, dict]:
    """读取机场空域配置（每次重读，改文件后无需重启）。"""
    airports: dict[str, dict] = {}
    store_cap = float(_STORE_MAX_ALT_DEFAULT)
    try:
        raw = json.loads(AIRSPACE_FILE.read_text(encoding="utf-8"))
        try:
            store_cap = float(raw.get("store_max_alt_m", _STORE_MAX_ALT_DEFAULT))
            if store_cap <= 0:
                store_cap = float(_STORE_MAX_ALT_DEFAULT)
        except (TypeError, ValueError):
            store_cap = float(_STORE_MAX_ALT_DEFAULT)
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
                    "color_scale_m": float(cfg.get("color_scale_m", 6000)),
                    "alt_step_m": float(cfg.get("alt_step_m", 300)),
                    "store_max_alt_m": store_cap,
                }
            except (KeyError, TypeError, ValueError):
                logger.warning("机场空域配置 %s 格式有误，已跳过", code)
    except FileNotFoundError:
        logger.warning("未找到 %s，使用内置机场空域配置", AIRSPACE_FILE)
    except Exception:
        logger.exception("读取机场空域配置失败，使用内置配置")
    if not airports:
        airports = {k: json.loads(json.dumps(v)) for k, v in _AIRSPACE_DEFAULTS.items()}
        for v in airports.values():
            v["store_max_alt_m"] = store_cap
    return airports


def store_max_alt(airports: dict[str, dict]) -> float:
    return max((cfg.get("store_max_alt_m", _STORE_MAX_ALT_DEFAULT) for cfg in airports.values()),
               default=float(_STORE_MAX_ALT_DEFAULT))


def _history_path(date_str: str) -> Path:
    return HISTORY_DIR / f"radar_{date_str.replace('-', '')}.jsonl.gz"


def _cache_path(date_str: str) -> Path:
    return CACHE_DIR / f"{date_str}.json.gz"


def _ts_seconds(ts: str) -> float:
    """同一文件内都是同一天，只取时分秒即可，比 strptime 快得多。"""
    try:
        sec = int(ts[11:13]) * 3600 + int(ts[14:16]) * 60 + int(ts[17:19])
        if len(ts) >= 23 and ts[19] == '.':
            sec += int(ts[20:23]) / 1000.0
        return sec
    except (ValueError, IndexError):
        return 0.0


def _keep_main_run(pts: list[list], clat: float, clon: float) -> list[list]:
    """同一呼号可能被多架飞机/模拟机复用，窗口内的点会串。

    按时间/距离连续性切成若干段，只保留离机场最近的那一段——
    它就是该机场真正的进离场航迹。
    """
    if len(pts) < 2:
        return pts
    cos_clat = math.cos(math.radians(clat))

    def dist2(p: list) -> float:
        dy = (p[0] - clat) * 111000.0
        dx = (p[1] - clon) * 111000.0 * cos_clat
        return dx * dx + dy * dy

    runs: list[list[list]] = []
    cur = [pts[0]]
    for p in pts[1:]:
        prev = cur[-1]
        dt = abs(p[3] - prev[3])
        dy = (p[0] - prev[0]) * 111000.0
        dx = (p[1] - prev[1]) * 111000.0 * cos_clat
        d = math.sqrt(dx * dx + dy * dy)
        # 超过 5 分钟，或速度明显不合理（>350m/s）则断开
        if dt > 300 or d > max(5000.0, 350.0 * dt):
            runs.append(cur)
            cur = [p]
        else:
            cur.append(p)
    runs.append(cur)

    best_run = None
    best_d = None
    for r in runs:
        dmin = min(dist2(p) for p in r)
        if best_d is None or dmin < best_d:
            best_d = dmin
            best_run = r
    return best_run or pts


def _empty_day(airports: dict[str, dict]) -> dict[str, Any]:
    return {
        code: {"tracks": [],
               "stats": {"arrival": 0, "departure": 0, "total": 0, "scanned_points": 0}}
        for code in airports
    }


def _scan_day(date_str: str, airports: dict[str, dict]) -> dict[str, Any]:
    """扫描一天的雷达历史，返回各机场的进离场航迹（无水平范围限制）。"""
    path = _history_path(date_str)
    if not path.exists():
        return _empty_day(airports)

    # ── 第一遍：找击中点（进入空域且低高度） ──────────────────
    plist = []
    for code, cfg in airports.items():
        clat = cfg["center"]["lat"]
        clon = cfg["center"]["lon"]
        cos_clat = math.cos(math.radians(clat))
        r_km = cfg["radius_km"]
        plist.append((
            code, clat, clon, cos_clat,
            clat - r_km / 111.0 - 0.02,
            clat + r_km / 111.0 + 0.02,
            clon - r_km / (111.0 * max(0.2, cos_clat)) - 0.02,
            clon + r_km / (111.0 * max(0.2, cos_clat)) + 0.02,
            (r_km * 1000.0) ** 2,
        ))

    hits: dict[str, dict[str, list[tuple]]] = {code: {} for code in airports}
    total_points = 0

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

            for (code, clat, clon, cos_clat, lat_lo, lat_hi, lon_lo, lon_hi, r_m2) in plist:
                if lt < lat_lo or lt > lat_hi or ln < lon_lo or ln > lon_hi:
                    continue
                dy = (lt - clat) * 111000.0
                dx = (ln - clon) * 111000.0 * cos_clat
                if dx * dx + dy * dy > r_m2:
                    continue

                try:
                    i = line.find('"fl":')
                    j = line.find(",", i + 5)
                    fl = float(line[i + 5:j])
                    if fl > airports[code]["ceiling_m"]:
                        continue
                    i = line.find('"cs":"')
                    j = line.find('"', i + 6)
                    cs = line[i + 6:j].strip().upper()
                    if not cs:
                        continue
                    i = line.find('"ts":"')
                    j = line.find('"', i + 6)
                    ts = line[i + 6:j]
                    i = line.find('"ap":"')
                    j = line.find('"', i + 6)
                    ap = line[i + 6:j].strip().upper()
                    i = line.find('"ad":"')
                    j = line.find('"', i + 6)
                    ad = line[i + 6:j].strip().upper()
                except (ValueError, TypeError):
                    continue

                hits[code].setdefault(cs, []).append(
                    (ts, _ts_seconds(ts), fl, ap, ad)
                )
                total_points += 1

    # ── 聚类成航段 ──────────────────────────────────────────
    segs: list[dict] = []
    for code, by_cs in hits.items():
        for cs, hs in by_cs.items():
            hs.sort(key=lambda x: x[1])
            cur: list[tuple] = []
            last = None
            clusters: list[list[tuple]] = []
            for h in hs:
                if last is not None and h[1] - last > _SPLIT_GAP_SECONDS:
                    clusters.append(cur)
                    cur = []
                cur.append(h)
                last = h[1]
            if cur:
                clusters.append(cur)

            for cl in clusters:
                segs.append({
                    "code": code,
                    "cs": cs,
                    "lo": max(0.0, cl[0][1] - _PAD_SECONDS),
                    "hi": cl[-1][1] + _PAD_SECONDS,
                    "ap": next((h[3] for h in cl if h[3]), ""),
                    "ad": next((h[4] for h in cl if h[4]), ""),
                    "points": [],
                    "n": 0,
                })

    # ── 第二遍：收集整条进离场航迹（高度 <= store_max_alt_m） ──
    by_cs_seg: dict[str, list[dict]] = {}
    for sg in segs:
        by_cs_seg.setdefault(sg["cs"], []).append(sg)

    store_cap = store_max_alt(airports)

    if by_cs_seg:
        centers = {code: (cfg["center"]["lat"], cfg["center"]["lon"],
                          math.cos(math.radians(cfg["center"]["lat"])))
                   for code, cfg in airports.items()}
        with gzip.open(str(path), "rt", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                i = line.find('"cs":"')
                if i < 0:
                    continue
                j = line.find('"', i + 6)
                if j < 0:
                    continue
                cs = line[i + 6:j].strip().upper()
                cands = by_cs_seg.get(cs)
                if not cands:
                    continue

                ts = line[7:31] if line.startswith('{"ts":"') else ""
                if len(ts) < 19:
                    k = line.find('"ts":"')
                    if k < 0:
                        continue
                    ts = line[k + 6:k + 30]
                sec = _ts_seconds(ts)

                matched = [sg for sg in cands if sg["lo"] <= sec <= sg["hi"]]
                if not matched:
                    continue

                i = line.find('"fl":')
                j = line.find(",", i + 5)
                try:
                    fl = float(line[i + 5:j])
                except (ValueError, TypeError):
                    continue
                if fl > store_cap:
                    continue

                i = line.find('"lt":')
                j = line.find(",", i + 5)
                try:
                    lt = float(line[i + 5:j])
                    i2 = line.find('"ln":')
                    j2 = line.find(",", i2 + 5)
                    ln = float(line[i2 + 5:j2])
                except (ValueError, TypeError):
                    continue

                sg = matched[0]
                if len(matched) > 1:
                    best = None
                    for m in matched:
                        clat, clon, cos_clat = centers[m["code"]]
                        dy = (lt - clat) * 111000.0
                        dx = (ln - clon) * 111000.0 * cos_clat
                        d2 = dx * dx + dy * dy
                        if best is None or d2 < best[0]:
                            best = (d2, m)
                    sg = best[1]

                sg["n"] += 1
                # 抽稀；地面点（<150m）全部保留，保证接地/起飞点不丢
                if sg["n"] % _POINT_DECIMATE != 0 and fl >= 150.0:
                    continue
                sg["points"].append([round(lt, 5), round(ln, 5), round(fl, 1), int(sec)])

    # ── 生成航迹 ────────────────────────────────────────────
    result: dict[str, Any] = {}
    per_code: dict[str, list[dict]] = {code: [] for code in airports}
    stats: dict[str, dict] = {code: {"arrival": 0, "departure": 0, "total": 0}
                              for code in airports}

    for sg in segs:
        cfg = airports[sg["code"]]
        pts = _keep_main_run(sg["points"], cfg["center"]["lat"], cfg["center"]["lon"])
        if len(pts) < _MIN_POINTS_PER_TRACK:
            continue

        first_fl = pts[0][2]
        last_fl = pts[-1][2]
        if last_fl < first_fl - 100:
            typ = "ARRIVAL"
        elif last_fl > first_fl + 100:
            typ = "DEPARTURE"
        elif sg["ad"] == sg["code"] and sg["ap"] != sg["code"]:
            typ = "ARRIVAL"
        elif sg["ap"] == sg["code"] and sg["ad"] != sg["code"]:
            typ = "DEPARTURE"
        else:
            # 趋势不明显：看离机场最近的点在航段前段还是后段
            clat = airports[sg["code"]]["center"]["lat"]
            clon = airports[sg["code"]]["center"]["lon"]
            cos_clat = math.cos(math.radians(clat))
            min_d = None
            min_i = 0
            for idx, p in enumerate(pts):
                dy = (p[0] - clat) * 111000.0
                dx = (p[1] - clon) * 111000.0 * cos_clat
                d2 = dx * dx + dy * dy
                if min_d is None or d2 < min_d:
                    min_d = d2
                    min_i = idx
            typ = "ARRIVAL" if min_i > len(pts) / 2 else "DEPARTURE"

        per_code[sg["code"]].append({
            "callsign": sg["cs"],
            "type": typ,
            "adep": sg["ap"],
            "adest": sg["ad"],
            "start": pts[0][3],
            "end": pts[-1][3],
            "points": pts,
        })
        if typ == "ARRIVAL":
            stats[sg["code"]]["arrival"] += 1
        else:
            stats[sg["code"]]["departure"] += 1
        stats[sg["code"]]["total"] += 1

    for code, tracks in per_code.items():
        tracks.sort(key=lambda t: (t["start"], t["callsign"]))
        result[code] = {"tracks": tracks, "stats": stats[code]}

    logger.info("机场航迹扫描: %s 击中点 %d，航迹 %d 条",
                date_str, total_points, sum(len(x["tracks"]) for x in result.values()))
    return result


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
                    "alt_step_m": cfg["alt_step_m"],
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
        # 现在一天的数据较大，内存只保留最近 3 天
        if len(_mem_cache) > 3:
            for k in sorted(_mem_cache, key=lambda k: _mem_cache[k][0])[:len(_mem_cache) - 3]:
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


_TYPE_ALIASES = {
    "ARR": "ARRIVAL", "ARRIVAL": "ARRIVAL", "A": "ARRIVAL",
    "DEP": "DEPARTURE", "DEPARTURE": "DEPARTURE", "D": "DEPARTURE",
}


def parse_types(values: list[str] | None) -> set[str]:
    out: set[str] = set()
    for v in values or []:
        key = _TYPE_ALIASES.get(str(v).strip().upper())
        if key:
            out.add(key)
    return out or {"ARRIVAL", "DEPARTURE"}


def _day_window(date_str: str, start: str, end: str) -> tuple[float, float] | None:
    """把查询的 UTC 时间段换算成当天内的秒数窗口；无交集返回 None。"""
    if date_str < start[:10] or date_str > end[:10]:
        return None
    lo = 0.0
    hi = 86400.0
    if date_str == start[:10]:
        lo = _ts_seconds(start)
    if date_str == end[:10]:
        hi = _ts_seconds(end)
    if hi < lo:
        return None
    return lo, hi


def get_airports_tracks(dates: list[str], codes: list[str], types: set[str],
                        start: str, end: str, max_alt: float) -> dict[str, Any]:
    """多机场 / 多日期 / 时间段 / 进离场类型 / 高度上限 的合并查询结果。"""
    airports = load_airspace()
    codes = [c.upper() for c in codes if c.upper() in airports]
    if not codes:
        raise KeyError(",".join(codes))

    max_alt = max(0.0, float(max_alt))
    tracks: list[dict] = []
    stats = {c: {"arrival": 0, "departure": 0, "total": 0} for c in codes}

    for date_str in dates:
        window = _day_window(date_str, start, end)
        if window is None:
            continue
        lo_sec, hi_sec = window
        payload = _load_payload(date_str, airports)
        for code in codes:
            item = payload["airports"].get(code) or {}
            for trk in item.get("tracks", []):
                typ = trk.get("type")
                if typ not in types:
                    continue
                pts = [p for p in trk.get("points", [])
                       if lo_sec <= p[3] <= hi_sec and p[2] <= max_alt]
                if len(pts) < _MIN_POINTS_PER_TRACK:
                    continue
                tracks.append({
                    "airport": code,
                    "type": typ,
                    "callsign": trk.get("callsign", ""),
                    "adep": trk.get("adep", ""),
                    "adest": trk.get("adest", ""),
                    "points": pts,
                })
                stats[code]["arrival" if typ == "ARRIVAL" else "departure"] += 1
                stats[code]["total"] += 1

    tracks.sort(key=lambda t: (t["points"][0][3], t["callsign"]))
    meta = {
        c: {
            "icao": c,
            "name": airports[c]["name"],
            "center": airports[c]["center"],
            "radius_km": airports[c]["radius_km"],
            "ceiling_m": airports[c]["ceiling_m"],
            "color_scale_m": airports[c]["color_scale_m"],
            "alt_step_m": airports[c]["alt_step_m"],
        }
        for c in codes
    }
    total_arr = sum(s["arrival"] for s in stats.values())
    total_dep = sum(s["departure"] for s in stats.values())
    return {
        "date_from": dates[0] if dates else "",
        "date_to": dates[-1] if dates else "",
        "start": start,
        "end": end,
        "max_alt_m": max_alt,
        "airports": meta,
        "tracks": tracks,
        "stats": {
            "by_airport": stats,
            "arrival": total_arr,
            "departure": total_dep,
            "total": total_arr + total_dep,
        },
    }
