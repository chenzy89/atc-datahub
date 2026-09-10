"""气象雷达回波叠加模块

回波图片由外部程序从中央气象台下载并已处理为透明回波层，目录结构按北京时组织：

    <cloud_map>/
        <MMDD>/          北京时日期，如 0909 表示 9 月 9 日
            <HHmm>.PNG   北京时时刻，如 0006.PNG 表示 00:06

本模块只做"按时间检索回波文件 + 提供地理配准参数"，不再做任何图像处理。

时间约定：目录名与文件名均为**北京时（UTC+8）**，而系统内部统一使用 **UTC**；
对外接口一律接收/返回 UTC epoch 秒，模块内部完成换算。

检索规则：给定目标时刻 T（北京时），在 T 之前 lookback 分钟内（含 T 当分钟）
从近到远逐分钟回找，命中第一个存在的文件即返回；找不到则视为无可用回波。

API（见 webapp.py）：
  GET /api/cloud_mosaic/latest           实时模式：返回当前可用回波元数据
  GET /api/cloud_mosaic/at?ts=<epoch>    回放模式：返回指定 UTC 时刻可用回波元数据
  GET /api/cloud_mosaic/image?ts=<epoch> 回波 PNG（ts 缺省时取最新）
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from PIL import Image

logger = logging.getLogger("aftn_web.cloud_mosaic")

# 北京时区（UTC+8）
TZ_BEIJING = timezone(timedelta(hours=8))

# 回找窗口（分钟）：目标时刻往前多少分钟内允许借用较早的回波图。
# 实时模式用它判断"最近 20 分钟内是否有回波图"；回放模式同样适用。
DEFAULT_LOOKBACK_MIN = 20

# 主图有效区域（用于裁剪掉底部标题/色标条）：原产品 990×959，主图区高 838。
# 可用 geo.json 的 "crop": [x, y, w, h] 覆盖。
DEFAULT_CROP = (0, 0, 990, 838)

# 内置默认配准（image px → lon/lat 仿射），可用 geo.json 覆盖
DEFAULT_AFFINE: dict[str, float] = {
    "lon_px": 0.013840681, "lon_py": -0.000242942, "lon_c": 104.098622,
    "lat_px": 0.000008547, "lat_py": -0.013128268, "lat_c": 26.699521,
}


# ── 目录与配准 ────────────────────────────────────────────────

def resolve_cloud_dir(config_file: Path | None = None, configured: str | None = None) -> Path:
    """解析云图根目录：显式配置 > 配置文件同级的 cloud_map > 部署默认路径"""
    candidates: list[Path] = []
    if configured:
        p = Path(configured)
        candidates.append(p if p.is_absolute() else ((config_file.parent / p) if config_file else p))
    if config_file:
        candidates.append(config_file.parent / "cloud_map")
    candidates.append(Path("/home/share/atc_datahub/cloud_map"))
    for p in candidates:
        try:
            if p.is_dir():
                return p
        except OSError:
            continue
    return candidates[0]


def load_geo(cloud_dir: Path) -> tuple[dict[str, float], tuple[int, int, int, int]]:
    """读取 geo.json → (仿射参数, 主图裁剪矩形)。缺失/损坏时用内置默认值"""
    affine = dict(DEFAULT_AFFINE)
    crop = DEFAULT_CROP
    geo_file = cloud_dir / "geo.json"
    if not geo_file.exists():
        return affine, crop
    try:
        data = json.loads(geo_file.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("读取 geo.json 失败(%s)，使用内置配准", e)
        return affine, crop

    keys = ("lon_px", "lon_py", "lon_c", "lat_px", "lat_py", "lat_c")
    if all(k in data for k in keys):
        affine = {k: float(data[k]) for k in keys}
    else:
        logger.warning("geo.json 配准参数不全，使用内置配准: %s", geo_file)

    c = data.get("crop")
    if isinstance(c, (list, tuple)) and len(c) == 4:
        try:
            crop = (int(c[0]), int(c[1]), int(c[2]), int(c[3]))
        except (TypeError, ValueError):
            logger.warning("geo.json crop 参数非法，使用默认 %s", DEFAULT_CROP)
    return affine, crop


# ── 时间换算与文件检索 ────────────────────────────────────────

def bt_now() -> datetime:
    """当前北京时"""
    return datetime.now(TZ_BEIJING)


def as_beijing(dt: datetime) -> datetime:
    """任意 datetime → 北京时（naive 视为 UTC）"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ_BEIJING)


def dir_name_of(bt: datetime) -> str:
    return f"{bt.month:02d}{bt.day:02d}"


def file_stem_of(bt: datetime) -> str:
    return f"{bt.hour:02d}{bt.minute:02d}"


def _candidates(cloud_dir: Path, bt: datetime) -> list[Path]:
    d = cloud_dir / dir_name_of(bt)
    stem = file_stem_of(bt)
    return [d / f"{stem}.PNG", d / f"{stem}.png"]


def find_echo_file(
    cloud_dir: Path,
    bt_target: datetime,
    lookback_min: int = DEFAULT_LOOKBACK_MIN,
) -> tuple[Optional[Path], Optional[datetime]]:
    """自 bt_target 起往前 lookback_min 分钟内查找最新的回波文件

    返回 (文件路径, 该文件对应的北京时)；未命中返回 (None, None)。
    """
    for off in range(0, max(0, lookback_min) + 1):
        t = bt_target - timedelta(minutes=off)
        for p in _candidates(cloud_dir, t):
            try:
                if p.is_file():
                    return p, t
            except OSError:
                continue
    return None, None


def file_bt_time(path: Path) -> Optional[datetime]:
    """从路径解析回波时间（北京时）：目录名 MMDD + 文件名 HHmm

    目录名不含年份，按当前北京时推断；跨年（1 月读到 12 月目录）时回退一年。
    """
    try:
        mmdd, stem = path.parent.name, path.stem
        if len(mmdd) != 4 or len(stem) < 4:
            return None
        month, day = int(mmdd[:2]), int(mmdd[2:4])
        hour, minute = int(stem[:2]), int(stem[2:4])
        now = bt_now()
        dt = datetime(now.year, month, day, hour, minute, tzinfo=TZ_BEIJING)
        if (dt - now).total_seconds() > 2 * 86400:
            dt = dt.replace(year=now.year - 1)
        return dt
    except (ValueError, OSError):
        return None


def _image_size(path: Path) -> Optional[tuple[int, int]]:
    try:
        with Image.open(str(path)) as im:
            return im.size
    except Exception as e:
        logger.warning("读取回波图尺寸失败 %s: %s", path, e)
        return None


# ── 元数据 ────────────────────────────────────────────────────

def _meta_for(cloud_dir: Path, ts_utc: Optional[float], lookback_min: int) -> dict[str, Any]:
    """构造某 UTC 时刻的回波元数据"""
    meta: dict[str, Any] = {"available": False, "dir": str(cloud_dir)}

    if not cloud_dir.is_dir():
        meta["error"] = "cloud_map 目录不存在"
        return meta

    if ts_utc is None:
        bt_target = bt_now()
    else:
        try:
            bt_target = as_beijing(datetime.fromtimestamp(float(ts_utc), tz=timezone.utc))
        except (OverflowError, OSError, ValueError):
            meta["error"] = "ts 参数非法"
            return meta

    src, bt = find_echo_file(cloud_dir, bt_target, lookback_min)
    if src is None:
        meta["error"] = f"{lookback_min} 分钟内无可用回波图"
        meta["target"] = bt_target.strftime("%Y-%m-%d %H:%M")
        return meta

    affine, crop = load_geo(cloud_dir)
    size = _image_size(src)
    epoch = int(bt.timestamp())

    meta.update({
        "available": True,
        "file": f"{src.parent.name}/{src.name}",
        "ts": epoch,                                   # 回波时刻（UTC epoch 秒）
        "time": bt.strftime("%Y-%m-%d %H:%M"),         # 北京时（显示用）
        "time_utc": bt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "image_url": f"/api/cloud_mosaic/image?ts={epoch}",
        "geo": affine,
        "crop": {"x": crop[0], "y": crop[1], "w": crop[2], "h": crop[3]},
        "width": size[0] if size else crop[0] + crop[2],
        "height": size[1] if size else crop[1] + crop[3],
        "lookback_min": lookback_min,
    })
    return meta


def latest_meta(cloud_dir: Path, lookback_min: int = DEFAULT_LOOKBACK_MIN) -> dict[str, Any]:
    """实时模式：当前（北京时）最近 lookback 分钟内可用的最新回波"""
    return _meta_for(cloud_dir, None, lookback_min)


def meta_for_time(
    cloud_dir: Path,
    ts_utc: float,
    lookback_min: int = DEFAULT_LOOKBACK_MIN,
) -> dict[str, Any]:
    """回放模式：指定 UTC 时刻可用的最新回波"""
    return _meta_for(cloud_dir, ts_utc, lookback_min)


def exact_file(cloud_dir: Path, ts_utc: float) -> Optional[Path]:
    """按 UTC 时刻精确命中回波文件（不做时间回找），供图片接口使用"""
    try:
        bt = as_beijing(datetime.fromtimestamp(float(ts_utc), tz=timezone.utc))
    except (OverflowError, OSError, ValueError):
        return None
    for p in _candidates(cloud_dir, bt):
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None
