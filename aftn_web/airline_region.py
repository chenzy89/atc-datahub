"""航司属地（境内/境外）判定

数据来源：config/航空公司代码.xls 的「是否国内」列
  Checked   → 境内
  Unchecked → 境外

该 xls 由 `scripts/build_airline_region.py` 预先转换为
config/airline_domestic.json（含 domestic / foreign 两个三字码列表），
运行时只读 JSON，不依赖 xlrd，也不需要生产环境装 Excel 解析库。

未收录的三字码兜底规则：
  - 形如 B6512 / B738 / B 的中国注册号（民航局注册号前缀为 B）→ 境内
  - 其余（N88 / T7C / STE 等外籍注册号或未知）→ 境外
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

logger = logging.getLogger("aftn_web.airline_region")

IN_DOMAIN = "境内"
FOREIGN = "境外"

# 中国民航航空器注册号：B + 数字（如 B6512 / B-7389 / 仅有 B）
_CHINA_REG_RE = re.compile(r"^B\d*$")

_lock = threading.Lock()
_domestic: set[str] | None = None
_source_path: Path | None = None


def _resolve_path() -> Path:
    """按优先级查找映射文件：环境变量 → 项目 config/ 目录"""
    import os

    env = os.environ.get("AIRLINE_REGION_FILE")
    if env:
        return Path(env)
    # 本文件位于 <proj>/aftn_web/airline_region.py，config 在同级目录
    return Path(__file__).resolve().parent.parent / "config" / "airline_domestic.json"


def _load() -> set[str]:
    global _domestic, _source_path
    if _domestic is not None:
        return _domestic
    with _lock:
        if _domestic is not None:
            return _domestic
        path = _resolve_path()
        _source_path = path
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            codes = raw.get("domestic") or []
            _domestic = {str(c).strip().upper() for c in codes if str(c).strip()}
            logger.info("航司属地表已加载：%s（境内 %d 个三字码）", path.name, len(_domestic))
        except FileNotFoundError:
            logger.warning("航司属地表缺失: %s，全部按兜底规则判定", path)
            _domestic = set()
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("航司属地表读取失败(%s): %s，按兜底规则判定", path, exc)
            _domestic = set()
        return _domestic


def reload() -> int:
    """强制重新加载映射表（改表后无需重启），返回境内代码数"""
    global _domestic
    with _lock:
        _domestic = None
    return len(_load())


def airline_code(callsign: str) -> str:
    """取呼号的三字码（ICAO 航司代码），做去空格/大写处理"""
    return (callsign or "").strip().upper()[:3]


def resolve(callsign: str) -> str:
    """返回 '境内' / '境外'；呼号为空时返回 ''（未知，前端显示 -）"""
    cs = (callsign or "").strip().upper()
    if not cs:
        return ""
    code = cs[:3]
    if code in _load():
        return IN_DOMAIN
    # 表里未收录：中国注册号（B + 数字）视为境内，其余境外
    if _CHINA_REG_RE.match(cs):
        return IN_DOMAIN
    return FOREIGN


def airline_name(callsign: str) -> str:
    """返回三字码对应的中文名称（查不到返回空串）"""
    path = _source_path or _resolve_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return (raw.get("names") or {}).get(airline_code(callsign), "")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""
