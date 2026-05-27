"""珠海终端管制区空域判断工具

从 FDRG.json 加载终端区定义：
- 多边形顶点 (lat, lon)
- ceiling_m / floor_m
- 机场列表

提供点是否在终端区内的判断函数。
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("aftn_web.terminal")

_TERMINAL_CACHE: Optional[dict[str, Any]] = None
_FDRG_PATH = Path("/home/share/atc_aftn_web/config/FDRG.json")


def _point_in_polygon(lat: float, lon: float, vertices: list[list[float]]) -> bool:
    """射线法判断点是否在多边形内（经纬度坐标）"""
    n = len(vertices)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        yi, xi = vertices[i]
        yj, xj = vertices[j]
        # 水平射线法：从左向右射出的射线与边的交点个数
        if ((yi > lat) != (yj > lat)) and \
           (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def load_terminal_config() -> dict[str, Any]:
    """加载 FDRG.json，返回终端区配置"""
    global _TERMINAL_CACHE
    if _TERMINAL_CACHE is not None:
        return _TERMINAL_CACHE
    try:
        raw = json.loads(_FDRG_PATH.read_text(encoding="utf-8"))
        _TERMINAL_CACHE = {
            "name": raw.get("name", ""),
            "ceiling_m": float(raw.get("ceiling_m", 4800)),
            "floor_m": float(raw.get("floor_m", 30)),
            "airports": [a.upper() for a in raw.get("airports", [])],
            "vertices": raw.get("vertices", []),
        }
        logger.info(
            "终端区定义已加载: %s, %d机场, %d顶点, 高度%.0f-%.0fm",
            _TERMINAL_CACHE["name"],
            len(_TERMINAL_CACHE["airports"]),
            len(_TERMINAL_CACHE["vertices"]),
            _TERMINAL_CACHE["floor_m"],
            _TERMINAL_CACHE["ceiling_m"],
        )
        return _TERMINAL_CACHE
    except Exception as exc:
        logger.warning("FDRG.json 加载失败: %s", exc)
        return {"name": "", "ceiling_m": 0, "floor_m": 0, "airports": [], "vertices": []}


def reload_terminal_config() -> dict[str, Any]:
    """重新加载（清除缓存）"""
    global _TERMINAL_CACHE
    _TERMINAL_CACHE = None
    return load_terminal_config()


def is_in_terminal(lat: float, lon: float, altitude_m: float) -> bool:
    """判断一个点是否在终端区空域内（多边形内 + 高度范围）"""
    cfg = load_terminal_config()
    if not cfg["vertices"]:
        return False
    if not _point_in_polygon(lat, lon, cfg["vertices"]):
        return False
    if altitude_m < cfg["floor_m"] or altitude_m > cfg["ceiling_m"]:
        return False
    return True


def is_terminal_airport(icao: str) -> bool:
    """判断机场是否终端区机场"""
    cfg = load_terminal_config()
    return icao.upper() in cfg["airports"]


def get_terminal_config_safe() -> dict[str, Any]:
    """返回前端可用的终端区配置（不含敏感信息）"""
    cfg = load_terminal_config()
    return {
        "name": cfg["name"],
        "ceiling_m": cfg["ceiling_m"],
        "floor_m": cfg["floor_m"],
        "airports": cfg["airports"],
        "vertices": cfg["vertices"],
    }
