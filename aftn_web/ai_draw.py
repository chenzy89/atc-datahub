"""AI 作图工具 — 外部 LLM API（OpenAI 兼容）+ 坐标解析 + 地图图层文本生成

v3.1.1 新增。设计要点：
- 纯标准库（urllib），生产环境零新增依赖；
- AI 返回严格 JSON 图形列表，后端校验后转换为与 map/*.txt 同格式的图层文本，
  前端零改动复用 parseMapFile + drawElement 绘制管线；
- AI 未配置或调用失败时，退回内置坐标解析器（把文件中的点连成多边形）；
- 覆盖层保存为 data/user_overlays/<名称>.map。
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import time as _time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple

logger = logging.getLogger("aftn_web.ai_draw")

_BASE_DIR = Path("/home/share/atc_datahub")
CONFIG_PATH = _BASE_DIR / "config" / "ai_config.json"
OVERLAY_DIR = _BASE_DIR / "data" / "user_overlays"

_DEFAULT_CONFIG = {
    "api_base": "",
    "api_key": "",
    "model": "",
    "app_id": "",
    "admin_password": "",    # 管理员密码；为空时使用默认密码
    # 注意：AI 启用状态不落盘 —— v3.1.3 起为 10 分钟内存会话授权
    # （勾选「启用 AI 工具」保存后生效，到期自动关闭，需重新验证管理员密码）
}
DEFAULT_ADMIN_PASSWORD = "atc123"

AI_SESSION_TTL = 600                    # 启用有效期：10 分钟
_AI_SESSION = {"until": 0.0}            # 进程内存，重启即失效


def grant_ai_session(seconds: int = AI_SESSION_TTL) -> int:
    """启用 AI 通道 seconds 秒，返回到期时间戳"""
    until = _time.time() + seconds
    _AI_SESSION["until"] = until
    return until


def revoke_ai_session() -> None:
    _AI_SESSION["until"] = 0.0


def ai_session_remaining() -> int:
    """剩余有效秒数（向下取整，过期为 0）"""
    return max(0, int(_AI_SESSION["until"] - _time.time()))


def ai_session_active() -> bool:
    return ai_session_remaining() > 0

MAX_FILE_BYTES = 512 * 1024          # 上传文件上限 512KB
MAX_SHAPES = 50                      # 单次作图图形数上限
MAX_POINTS_PER_SHAPE = 5000          # 单图形顶点数上限
LLM_TIMEOUT = 120                    # LLM 调用超时（秒）

VALID_TYPES = ("polygon", "polyline", "point", "text")


class AIDrawError(Exception):
    """作图业务错误（消息可直接展示给用户）"""


# ── 配置读写 ──────────────────────────────────────────────────

def load_config() -> dict:
    cfg = dict(_DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k in _DEFAULT_CONFIG:
                if k in data and data[k] is not None:
                    cfg[k] = str(data[k]).strip()
    except (OSError, ValueError) as exc:
        logger.warning("ai_draw: 读取配置失败: %s", exc)
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {k: str(cfg.get(k, "") or "").strip() for k in _DEFAULT_CONFIG}
    CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    try:
        CONFIG_PATH.chmod(0o600)  # 含密钥，仅属主可读
    except OSError:
        pass


def mask_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:3] + "****" + key[-4:]


def is_configured(cfg: dict) -> bool:
    return bool(cfg.get("api_base") and cfg.get("api_key") and cfg.get("model"))


def api_ready(cfg: dict) -> bool:
    """AI API 可被调用：会话授权在有效期内 且 三项配置齐全"""
    return ai_session_active() and is_configured(cfg)


def verify_admin_password(cfg: dict, password: str) -> bool:
    expected = (cfg.get("admin_password") or "").strip() or DEFAULT_ADMIN_PASSWORD
    return (password or "").strip() == expected


def set_admin_password(cfg: dict, new_password: str) -> None:
    cfg["admin_password"] = (new_password or "").strip()


# ── LLM 调用（OpenAI 兼容 /chat/completions）─────────────────

def _endpoint(api_base: str) -> str:
    base = (api_base or "").strip().rstrip("/")
    if not base:
        raise AIDrawError("未配置 API 地址")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def call_llm(cfg: dict, system: str, user: str,
             timeout: int = LLM_TIMEOUT) -> str:
    """调用 OpenAI 兼容接口，返回助手回复文本。失败抛 AIDrawError。"""
    url = _endpoint(cfg.get("api_base", ""))
    payload: dict[str, Any] = {
        "model": cfg.get("model", ""),
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + (cfg.get("api_key") or ""),
    }
    if cfg.get("app_id"):
        headers["X-App-Id"] = cfg["app_id"]

    last_err: Optional[str] = None
    # 先带 response_format（json 模式），部分服务商不支持 400 时去掉重试
    for attempt in (1, 2):
        if attempt == 2:
            payload.pop("response_format", None)
            body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            choices = data.get("choices") or []
            if not choices:
                raise AIDrawError("AI 返回为空（无 choices）")
            msg = choices[0].get("message") or {}
            content = (msg.get("content") or "").strip()
            if not content:
                raise AIDrawError("AI 返回内容为空")
            return content
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            last_err = "HTTP %s %s" % (exc.code, detail)
            if exc.code != 400 or attempt == 2:
                break
        except urllib.error.URLError as exc:
            last_err = "连接失败: %s" % (exc.reason,)
            break
        except (TimeoutError, OSError) as exc:
            last_err = "请求超时或网络错误: %s" % exc
            break
        except ValueError as exc:
            last_err = "AI 返回非 JSON: %s" % exc
            break
    raise AIDrawError("AI 调用失败: %s" % last_err)


# ── AI 作图 ──────────────────────────────────────────────────

_SYSTEM_PROMPT = """你是空管雷达地图作图助手。用户会上传一个包含坐标点的文件和一段作图要求。
你的任务：解析文件中的坐标（可能为十进制度、度分秒 DMS 如 22,36,00N、KML 等任意格式），
按用户的作图要求输出图形列表。

只输出 JSON（不要 markdown 代码块），格式：
{
  "name": "可选的图层保存名（仅当用户在要求中指定了保存名/文件名时给出）",
  "shapes": [
    {"type": "polygon",  "color": 30, "label": "图形名称(可选)", "points": [[经度, 纬度], ...]},
    {"type": "polyline", "color": 26, "label": "线名称(可选)",   "points": [[经度, 纬度], ...]},
    {"type": "point",    "color": 21, "label": "点名",           "points": [[经度, 纬度]]},
    {"type": "text",     "color": 20, "label": "要显示的文字",   "points": [[经度, 纬度]]}
  ]
}

规则：
1. points 中每点为 [经度lon, 纬度lat]（十进制度，WGS84）。北纬/东经为正。
2. type 只能是 polygon（多边形，自动闭合）/ polyline（折线）/ point（点标）/ text（文字标注）。
3. color 为 0-30 的整数颜色号，深蓝背景下醒目的常用色：30黄 26青 13绿 21红 25紫 4蓝 27橙。
4. 用户要求"连接为多边形/围成区域"用 polygon；"连线/航路"用 polyline；顶点带名称时给 point。
5. 文件中若有多组点（如多个空域），拆成多个 shapes，每组给 label。
6. 坐标明显超出中国 region（纬度 0~60，经度 70~150）时检查是否经纬度颠倒，能修正则修正。
7. 不要编造文件中不存在的坐标点。"""


def ai_generate_shapes(cfg: dict, file_text: str, prompt: str) -> Tuple[List[dict], Optional[str]]:
    """调用 LLM 解析文件并生成图形。返回 (shapes, ai_name)。失败抛 AIDrawError。"""
    content = call_llm(
        cfg,
        _SYSTEM_PROMPT,
        "【作图要求】\n%s\n\n【文件内容】\n%s" % (prompt or "把文件中的点连接为多边形", file_text[:MAX_FILE_BYTES]),
    )
    # 容忍模型输出 ```json ... ``` 包裹
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise AIDrawError("AI 返回中未找到 JSON")
    try:
        data = json.loads(m.group(0))
    except ValueError as exc:
        raise AIDrawError("AI 返回 JSON 解析失败: %s" % exc)
    shapes = _sanitize_shapes(data.get("shapes") or [])
    if not shapes:
        raise AIDrawError("AI 未解析出有效图形（请检查文件坐标格式）")
    name = data.get("name")
    return shapes, (str(name).strip() if name else None)


def _sanitize_shapes(raw: Any) -> List[dict]:
    """校验并规范化 AI 返回的图形列表。"""
    out: List[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw[:MAX_SHAPES]:
        if not isinstance(item, dict):
            continue
        stype = str(item.get("type", "")).lower().strip()
        if stype not in VALID_TYPES:
            if stype in ("circle", "round"):
                stype = "polygon"  # 圆形由 AI 直接给多边形近似点，这里兜底
            else:
                continue
        pts = _sanitize_points(item.get("points") or [])
        min_pts = {"polygon": 3, "polyline": 2, "point": 1, "text": 1}[stype]
        if len(pts) < min_pts:
            continue
        try:
            color = int(item.get("color", 30))
        except (TypeError, ValueError):
            color = 30
        color = max(0, min(30, color))
        out.append({
            "type": stype,
            "color": color,
            "label": str(item.get("label", "") or item.get("name", "")).strip()[:60],
            "points": pts[:MAX_POINTS_PER_SHAPE],
        })
    return out


def _sanitize_points(raw: Any) -> List[Tuple[float, float]]:
    """校验坐标点：修正经纬颠倒、剔除越界点。"""
    pts: List[Tuple[float, float]] = []
    if not isinstance(raw, list):
        return pts
    for p in raw:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            continue
        try:
            lon, lat = float(p[0]), float(p[1])
        except (TypeError, ValueError):
            continue
        # 经纬颠倒修正：纬度应在 [-90,90]
        if abs(lat) > 90 and abs(lon) <= 90:
            lon, lat = lat, lon
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        pts.append((round(lon, 6), round(lat, 6)))
    return pts


# ── 内置坐标解析器（AI 不可用时的兜底）─────────────────────

_DMS_TOKEN = re.compile(
    r"(\d{1,3}),(\d{1,2}),(?:([\d.]+))?\s*([NSEWnsew])"
)
_NUM_TOKEN = re.compile(r"(-?\d+(?:\.\d+)?)")


def _dms_to_deg(d: str, m: str, s: str, hemi: str) -> float:
    deg = float(d) + float(m) / 60 + (float(s) if s else 0.0) / 3600
    if hemi.upper() in ("S", "W"):
        deg = -deg
    return deg


def fallback_parse_points(text: str) -> List[Tuple[float, float]]:
    """从文本中提取坐标点序列。支持 DMS、十进制、KML(lon,lat[,alt])。"""
    pts: List[Tuple[float, float]] = []
    kml_mode = bool(re.search(r"<coordinate", text, re.I))

    lines = text.replace("\r", "").split("\n")
    for line in lines:
        line = line.strip()
        if not line or line.startswith(("//", "#", "<?", "<?xml")):
            continue
        # 1) DMS 对：如 22,36,00N 113,54,00E
        dms = _DMS_TOKEN.findall(line)
        if len(dms) >= 2:
            i = 0
            while i + 1 < len(dms):
                a, b = dms[i], dms[i + 1] if i + 1 < len(dms) else None
                if b is None:
                    break
                ah, bh = a[3].upper(), b[3].upper()
                if ah in ("N", "S") and bh in ("E", "W"):
                    pts.append((_dms_to_deg(*b[:3], bh), _dms_to_deg(*a[:3], ah)))
                    i += 2
                elif ah in ("E", "W") and bh in ("N", "S"):
                    pts.append((_dms_to_deg(*a[:3], ah), _dms_to_deg(*b[:3], bh)))
                    i += 2
                else:
                    i += 1
            continue
        # 2) KML：<coordinates> 中 lon,lat[,alt] 序列（经度在前）
        if kml_mode:
            nums = _NUM_TOKEN.findall(line)
            # 三元组（lon,lat,alt）时步长 3，否则 2
            step = 3 if len(nums) >= 3 and len(nums) % 3 == 0 else 2
            i = 0
            while i + 1 < len(nums):
                lon, lat = float(nums[i]), float(nums[i + 1])
                if abs(lat) > 90 and abs(lon) <= 90:
                    lon, lat = lat, lon
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    pts.append((lon, lat))
                i += step
            continue
        # 3) 十进制：每行取前两个数，>90 的是经度（中国区域纬度<60）
        nums = _NUM_TOKEN.findall(line)
        if len(nums) >= 2:
            a, b = float(nums[0]), float(nums[1])
            if abs(a) > 90 and abs(b) <= 90:
                a, b = b, a  # 第一个数是经度 → 交换，使 a=纬度 b=经度
            if -90 <= a <= 90 and -180 <= b <= 180 and abs(b) > 0.1:
                pts.append((b, a))
    return pts


def fallback_shapes(file_text: str) -> List[dict]:
    pts = fallback_parse_points(file_text)
    if len(pts) < 3:
        raise AIDrawError(
            "AI 未配置或调用失败，且内置解析器未能从文件中识别出至少 3 个坐标点。"
            "请在配置中填入正确的 AI API 信息后重试。"
        )
    return [{"type": "polygon", "color": 30, "label": "", "points": pts[:MAX_POINTS_PER_SHAPE]}]


# ── 图层文本生成（与 map/*.txt 同格式）─────────────────────

def _dms_parts(val: float) -> Tuple[int, int, float]:
    """度→(度,分,秒)，处理浮点进位（避免出现 60.0 秒/分）。"""
    val = abs(val)
    d = int(val)
    m_f = (val - d) * 60
    m = int(m_f)
    s = round((m_f - m) * 60, 1)
    if s >= 60.0:
        s = 0.0
        m += 1
    if m >= 60:
        m = 0
        d += 1
    return d, m, s


def _dms_lat(lat: float) -> str:
    d, m, s = _dms_parts(lat)
    return "%d,%02d,%04.1f%s" % (d, m, s, "N" if lat >= 0 else "S")


def _dms_lon(lon: float) -> str:
    d, m, s = _dms_parts(lon)
    return "%03d,%02d,%04.1f%s" % (d, m, s, "E" if lon >= 0 else "W")


def shapes_to_map_text(shapes: List[dict], layer_name: str = "AI绘图") -> str:
    """把图形列表转换为与 map/*.txt 同格式的图层文本（TAB 分隔）。

    polygon → GPxx 多边形（0.12 半透明填充）；polyline → GR 航路多点连线；
    point → GST 点标；text → GTXT 文字。
    注意：.map 格式以空白分隔字段，名称/标签中的空格会挤错后续字段，
    必须替换为下划线（与 CUSTOM_AREA.txt 等现有图层惯例一致）。
    """
    lines: List[str] = []
    for sh in shapes:
        color = sh.get("color", 30)
        label = (sh.get("label") or layer_name or "AI").replace("\t", " ")
        label = re.sub(r"\s+", "_", label.strip()) or "AI"
        pts = sh["points"]
        if sh["type"] == "polygon":
            lines.append("GP%d\t%s\t0\t0.12\t%d\t2" % (color, label, len(pts)))
            for lon, lat in pts:
                lines.append("GPP\t%s\t%s\t0" % (_dms_lat(lat), _dms_lon(lon)))
        elif sh["type"] == "polyline":
            lines.append("GR\t%s\t0\t2\t%d" % (label, len(pts)))
            for lon, lat in pts:
                lines.append("GRR\t%s\t%s\t" % (_dms_lat(lat), _dms_lon(lon)))
        elif sh["type"] == "point":
            lon, lat = pts[0]
            lines.append("GST%d\t%s\t%s\t%s\t%s\t0\t1"
                         % (color, _dms_lat(lat), _dms_lon(lon), label or "PT", label or "PT"))
        elif sh["type"] == "text":
            lon, lat = pts[0]
            lines.append("GTXT%d\t%s\t%s\t%s\t%s"
                         % (color, _dms_lat(lat), _dms_lon(lon), label, label))
    return "\n".join(lines) + "\n"


# ── 覆盖层文件管理 ──────────────────────────────────────────

def safe_overlay_name(name: str) -> str:
    """清洗图层保存名：去路径分隔与控制字符，限 40 字。"""
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "", (name or "").strip())
    name = name.replace("..", "").strip(". ")
    return name[:40]


def default_overlay_name() -> str:
    return datetime.now().strftime("%y%m%d%H%M")


def save_overlay(name: str, map_text: str) -> str:
    """保存覆盖层，返回最终名称。同名覆盖。"""
    OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
    final = safe_overlay_name(name) or default_overlay_name()
    path = OVERLAY_DIR / (final + ".map")
    path.write_text(map_text, encoding="utf-8")
    return final


def list_overlays() -> List[dict]:
    if not OVERLAY_DIR.exists():
        return []
    out = []
    for p in sorted(OVERLAY_DIR.glob("*.map")):
        try:
            out.append({
                "name": p.stem,
                "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
        except OSError:
            continue
    return out


def read_overlay(name: str) -> Optional[str]:
    final = safe_overlay_name(name)
    path = OVERLAY_DIR / (final + ".map")
    if not final or not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def delete_overlay(name: str) -> bool:
    final = safe_overlay_name(name)
    path = OVERLAY_DIR / (final + ".map")
    if final and path.exists():
        path.unlink()
        return True
    return False


# ── 作图入口 ────────────────────────────────────────────────

def draw(cfg: dict, file_text: str, prompt: str, name: str) -> dict:
    """执行一次作图。返回 {name, shapes, engine, note}。"""
    engine, note, ai_name = "ai", "", None
    shapes: List[dict] = []
    if api_ready(cfg):
        try:
            shapes, ai_name = ai_generate_shapes(cfg, file_text, prompt)
        except AIDrawError as exc:
            logger.warning("ai_draw: AI 作图失败，使用内置解析器: %s", exc)
            note = "AI 调用失败已回退内置解析: %s" % exc
            engine = "fallback"
            shapes = fallback_shapes(file_text)
    else:
        engine = "fallback"
        if not ai_session_active():
            note = ("AI 通道未启用或已过有效期（勾选启用后 10 分钟自动关闭），"
                    "已使用内置解析器，未调用外部 API；"
                    "如需继续使用请点⚙重新验证管理员密码并勾选「启用 AI 工具」")
        else:
            note = "AI 未配置完整（API 地址/Key/模型），已使用内置解析器"
        shapes = fallback_shapes(file_text)

    final_name = safe_overlay_name(name) or safe_overlay_name(ai_name or "") or default_overlay_name()
    map_text = shapes_to_map_text(shapes, final_name)
    saved = save_overlay(final_name, map_text)
    return {"name": saved, "shape_count": len(shapes), "engine": engine,
            "note": note, "map_text": map_text}
