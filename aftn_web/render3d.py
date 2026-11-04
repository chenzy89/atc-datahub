# -*- coding: utf-8 -*-
"""3D 轨迹服务端预渲染（v3.2.12）

用与前端兼容模式一致的投影数学，在服务器端离线渲染 3D 轨迹动画并编码为 MP4
（无 libx264 时回退 mpeg4 / GIF）。浏览器端只需 <video> 播放，任何电脑都流畅，
彻底规避客户端 GPU/性能/渲染正确性问题。

job 管理：内存字典 + 后台线程；进度经 /api/render3d_status 轮询。
"""
from __future__ import annotations

import math
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = BASE_DIR / "data" / "renders"

VERT_EXAG = 5
TMA_TOP = 4800 * VERT_EXAG
OUT_W, OUT_H = 1280, 720
FPS = 12
TARGET_SEC = 60          # 成片时长（秒，不包含片尾斜视段）
TAIL_SEC = 5             # 片尾镜头扫掠到 45° 斜视的时长
ZMIN = 120               # 近平面裁剪阈值（与前端一致）

_JOBS = {}               # job_id -> dict
_LOCK = threading.Lock()

_FONT_PATHS = [
    "/usr/share/fonts/cesi/CESI_HT_GB18030.TTF",
    "/usr/share/fonts/wps-office/FZFSK.TTF",
    "/usr/share/fonts/wps-office/FZWBK.TTF",
]


def _load_font(size):
    for p in _FONT_PATHS:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _job(job_id):
    with _LOCK:
        return _JOBS.get(job_id)


def _set_job(job_id, **kw):
    with _LOCK:
        if job_id in _JOBS:
            _JOBS[job_id].update(kw)


def start_render(pts, callsign, date_str):
    """pts: 航迹点列表（ts/lt/ln/fl/qh/hd/sp 字段，已按 ts 排序）。返回 job_id。"""
    job_id = uuid.uuid4().hex[:12]
    with _LOCK:
        _JOBS[job_id] = {
            "status": "pending", "progress": 0, "total": 0,
            "callsign": callsign, "date": date_str, "file": None, "error": None,
            "ts": time.time(),
        }
    t = threading.Thread(target=_render_worker, args=(job_id, pts, callsign, date_str), daemon=True)
    t.start()
    return job_id


def job_status(job_id):
    with _LOCK:
        j = _JOBS.get(job_id)
        if not j:
            return None
        d = dict(j)
        if d.get("file"):
            d["file_url"] = "/renders/" + Path(d["file"]).name
        return d


# ────────────────────────── 投影与绘制 ──────────────────────────

class _Cam:
    """与前端 SW 模式一致的投影（相机位于南上方向）"""

    def __init__(self, w, h, yaw, pitch, dist, cy):
        self.w, self.h = w, h
        self.yaw, self.pitch, self.dist, self.cy = yaw, pitch, dist, cy
        self.cyw = math.cos(yaw)
        self.syw = math.sin(yaw)
        self.cp = math.cos(pitch)
        self.sp = math.sin(pitch)
        self.k0 = h * 0.9

    def project(self, x, y, z):
        dy = y - self.cy
        x1 = x * self.cyw + z * self.syw
        z1 = -x * self.syw + z * self.cyw
        y2 = dy * self.cp - z1 * self.sp
        z3 = -dy * self.sp - z1 * self.cp + self.dist
        k = self.k0 / (z3 if z3 > 30 else 30)
        return (self.w / 2 + x1 * k, self.h / 2 - y2 * k, z3, k)


def _clip_stroke(cam, pts, closed, zmin=ZMIN):
    """世界坐标折线 → 可见段列表（近平面裁剪插值），pts: [(x,y,z),...]"""
    n = len(pts)
    if n < 2:
        return []
    segs, cur = [], []
    prev = pts[0]
    pa = cam.project(*prev)
    last = n if closed else n - 1
    for i in range(1, last + 1):
        cur3 = pts[i % n]
        pb = cam.project(*cur3)
        aok, bok = pa[2] >= zmin, pb[2] >= zmin
        if aok and bok:
            cur.append(pb)
        elif aok != bok:
            t = (zmin - pa[2]) / (pb[2] - pa[2])
            pm = cam.project(prev[0] + (cur3[0] - prev[0]) * t,
                             prev[1] + (cur3[1] - prev[1]) * t,
                             prev[2] + (cur3[2] - prev[2]) * t)
            if aok:
                cur.append(pm)
                if len(cur) > 1:
                    segs.append(cur)
                cur = []
            else:
                cur.append(pm)
                cur.append(pb)
        prev = cur3
        pa = pb
    if len(cur) > 1:
        segs.append(cur)
    return segs


def _draw_segs(dr, segs, color, width):
    for seg in segs:
        dr.line([(p[0], p[1]) for p in seg], fill=color, width=width, joint="curve")


def _ease(t):
    return t * t * (3 - 2 * t)


def _render_worker(job_id, pts, callsign, date_str):
    try:
        _set_job(job_id, status="rendering")
        path = _render_video(job_id, pts, callsign, date_str)
        _set_job(job_id, status="done", file=str(path), progress=1, total=1)
    except Exception as e:  # noqa: BLE001
        _set_job(job_id, status="error", error=str(e)[:300])


def _render_video(job_id, pts, callsign, date_str):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT_DIR / ("tmp_" + job_id)
    tmp.mkdir(parents=True, exist_ok=True)

    # ── 数据准备（与前端一致：优先 QNH 高度）──
    import datetime as _dt

    def parse_t(s):
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

    pts = [p for p in pts if p.get("lt") and p.get("ln") and p.get("ts")]
    pts.sort(key=lambda p: p["ts"])
    rec = []
    has_qh = False
    for p in pts:
        qh = float(p.get("qh") or 0)
        alt = qh if qh > 0 else float(p.get("fl") or 0)
        if qh > 0:
            has_qh = True
        rec.append({
            "t": parse_t(p["ts"]), "lon": float(p["ln"]), "lat": float(p["lt"]),
            "alt": alt, "hd": float(p.get("hd") or 0), "sp": float(p.get("sp") or 0),
        })
    if len(rec) < 2:
        raise RuntimeError("有效航迹点不足")
    dur = rec[-1]["t"] - rec[0]["t"]
    if dur <= 0:
        raise RuntimeError("航迹时间跨度异常")

    lon0 = sum(r["lon"] for r in rec) / len(rec)
    lat0 = sum(r["lat"] for r in rec) / len(rec)
    m_lat = 110540.0
    m_lon = 111320.0 * math.cos(lat0 * math.pi / 180)
    xyz = [((r["lon"] - lon0) * m_lon, max(r["alt"] * VERT_EXAG, 0), -(r["lat"] - lat0) * m_lat)
           for r in rec]
    max_alt = max(p[1] for p in xyz)
    span_e = max(abs(p[0]) for p in xyz)
    span_n = max(abs(p[2]) for p in xyz)
    span = max(span_e, span_n, 1500)
    ground = span * 2.1 + 4000
    gh = ground / 2
    s_mark = max(ground / 260, 55)

    # 边界 / 海岸线 / 陆地
    import json
    boundary = []
    try:
        fdr = json.loads((BASE_DIR / "config" / "FDRG.json").read_text(encoding="utf-8"))
        boundary = fdr.get("vertices") or []
    except Exception:
        boundary = []
    bxyz = [((v[1] - lon0) * m_lon, 0, -(v[0] - lat0) * m_lat) for v in boundary] if boundary else []
    land = []
    try:
        cjson = json.loads((BASE_DIR / "aftn_web" / "static" / "coastline_prd.json").read_text(encoding="utf-8"))
        for poly in cjson.get("land") or []:
            land.append([((q[1] - lon0) * m_lon, 0, -(q[0] - lat0) * m_lat) for q in poly])
    except Exception:
        land = []

    n_total = int(FPS * (TARGET_SEC + TAIL_SEC))
    _set_job(job_id, total=n_total)

    font_big = _load_font(24)
    font_mid = _load_font(18)

    dist_top = max(max_alt * 2.0, span * 1.55, 3500)
    dist_obl = span * 1.75
    cy = max_alt * 0.35

    # ── 逐帧渲染 ──
    for fi in range(n_total):
        vt = fi / float(FPS)                               # 视频时间轴（秒）
        # 航迹时间（片尾斜视段暂停推进，聚焦空域体展示）
        play_frac = min(vt / TARGET_SEC, 1.0)
        tt = rec[0]["t"] + play_frac * dur
        # 相机：主段正上方俯视（北朝上）；片尾 5 秒缓动到东南斜视
        if vt < TARGET_SEC:
            yaw, pitch, dist = 0.0, 1.45, dist_top
        else:
            e = _ease(min(1.0, (vt - TARGET_SEC) / TAIL_SEC))
            yaw = 0.62 * e
            pitch = 1.45 - (1.45 - 0.62) * e
            dist = dist_top - (dist_top - dist_obl) * e

        cam = _Cam(OUT_W, OUT_H, yaw, pitch, dist, cy)
        img = Image.new("RGB", (OUT_W, OUT_H), (10, 22, 40))
        dr = ImageDraw.Draw(img)

        # 地面
        c_pts = [(-gh, 0, -gh), (gh, 0, -gh), (gh, 0, gh), (-gh, 0, gh)]
        cscr = [cam.project(*p) for p in c_pts]
        if all(p[2] > 40 for p in cscr):
            dr.polygon([(p[0], p[1]) for p in cscr], fill=(13, 31, 56))

        # 陆地填充（全部顶点可见才画）
        for poly in land:
            scr = [cam.project(*p) for p in poly]
            if any(p[2] < ZMIN for p in scr):
                continue
            dr.polygon([(p[0], p[1]) for p in scr], fill=(214, 218, 224))

        # 网格
        grid_c = (22, 40, 63)
        for gi in range(21):
            t = -gh + ground * gi / 20
            _draw_segs(dr, _clip_stroke(cam, [(t, 0, -gh), (t, 0, gh)], False), grid_c, 1)
            _draw_segs(dr, _clip_stroke(cam, [(-gh, 0, t), (gh, 0, t)], False), grid_c, 1)

        # 岸线
        for poly in land:
            _draw_segs(dr, _clip_stroke(cam, poly, True), (56, 189, 248), 1)

        # 终端区空域体（侧壁仅斜视时绘制；俯视时所有墙面叠在边界线上会糊成实心带）
        if bxyz:
            _draw_segs(dr, _clip_stroke(cam, bxyz, True), (255, 140, 66), 2)
            if pitch < 1.2:
                ov = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
                od = ImageDraw.Draw(ov)
                light_az = math.radians(315)
                lx, lz = math.sin(light_az), -math.cos(light_az)
                cen_x = sum(p[0] for p in bxyz) / len(bxyz)
                cen_z = sum(p[2] for p in bxyz) / len(bxyz)
                walls = []
                nb = len(bxyz)
                for wi in range(nb):
                    wj = (wi + 1) % nb
                    ax, _, az = bxyz[wi]
                    bx, _, bz = bxyz[wj]
                    nx, nz = bz - az, -(bx - ax)
                    mx2, mz2 = (ax + bx) / 2 - cen_x, (az + bz) / 2 - cen_z
                    if nx * mx2 + nz * mz2 < 0:
                        nx, nz = -nx, -nz
                    nl = math.hypot(nx, nz) or 1
                    f = 0.35 + 0.65 * max(0, (nx / nl) * lx + (nz / nl) * lz)
                    ga, gb = cam.project(ax, 0, az), cam.project(bx, 0, bz)
                    ta, tb = cam.project(ax, TMA_TOP, az), cam.project(bx, TMA_TOP, bz)
                    if ga[2] <= 45 or gb[2] <= 45 or ta[2] <= 45 or tb[2] <= 45:
                        continue
                    walls.append(((ga[2] + gb[2] + ta[2] + tb[2]) / 4, ga, gb, ta, tb, f))
                walls.sort(key=lambda u: -u[0])
                for _, ga, gb, ta, tb, f in walls:
                    col = (int(255 * f), int(140 * f), int(66 * f), 31)   # 12% 不透明
                    od.polygon([(ga[0], ga[1]), (gb[0], gb[1]), (tb[0], tb[1]), (ta[0], ta[1])],
                               fill=col, outline=(255, 140, 66, 170))
                img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
                dr = ImageDraw.Draw(img)
            _draw_segs(dr, _clip_stroke(cam, [(p[0], TMA_TOP, p[2]) for p in bxyz], True),
                       (255, 179, 71), 2)

        # 指北针
        z_n = -gh * 0.72
        tip = cam.project(0, 0, z_n)
        back = cam.project(0, 0, z_n + ground / 12)
        if tip[2] > ZMIN and back[2] > ZMIN:
            dr.line([(back[0], back[1]), (tip[0], tip[1])], fill=(74, 222, 128), width=3)
            ang = math.atan2(tip[1] - back[1], tip[0] - back[0])
            wing = max(6, tip[3] * s_mark * 0.7)
            for s in (-0.45, 0.45):
                dr.line([(tip[0], tip[1]),
                         (tip[0] - wing * math.cos(ang + s), tip[1] - wing * math.sin(ang + s))],
                        fill=(74, 222, 128), width=3)
            if font_mid:
                nx2 = tip[0] + (tip[0] - back[0]) * 0.18
                ny2 = tip[1] + (tip[1] - back[1]) * 0.18
                dr.text((nx2 - 8, ny2 - 12), "N", fill=(74, 222, 128), font=font_mid)

        # 全轨迹（暗黄）+ 已飞（亮青）
        target_t = tt
        _draw_segs(dr, _clip_stroke(cam, [(p[0], p[1], p[2]) for p in xyz], False), (150, 128, 55), 2)

        # 已飞段
        flown = []
        for r3, p3 in zip(rec, xyz):
            if r3["t"] <= target_t:
                flown.append(p3)
            else:
                break
        if len(flown) >= 2:
            _draw_segs(dr, _clip_stroke(cam, flown, False), (56, 232, 255), 3)

        # 起终点
        if flown:
            sp0 = cam.project(*flown[0])
            if sp0[2] > ZMIN:
                rad = max(3, sp0[3] * s_mark * 0.5)
                dr.ellipse([sp0[0] - rad, sp0[1] - rad, sp0[0] + rad, sp0[1] + rad], fill=(74, 222, 128))
            spl = cam.project(*flown[-1])
            if spl[2] > ZMIN:
                rad = max(3, spl[3] * s_mark * 0.5)
                dr.ellipse([spl[0] - rad, spl[1] - rad, spl[0] + rad, spl[1] + rad], fill=(248, 113, 113))

        # 飞机（插值状态）
        st_state = _state_at(rec, xyz, tt)
        if st_state:
            px, py, pz = st_state["x"], st_state["y"], st_state["z"]
            hd_rad = st_state["hd"] * math.pi / 180
            dx, dz = math.sin(hd_rad), -math.cos(hd_rad)
            p_nose = cam.project(px + dx * s_mark * 2.2, py, pz + dz * s_mark * 2.2)
            p_tail = cam.project(px - dx * s_mark * 1.4, py, pz - dz * s_mark * 1.4)
            p_pos = cam.project(px, py, pz)
            if p_pos[2] > ZMIN:
                lead = cam.project(px + dx * s_mark * 4.6, py, pz + dz * s_mark * 4.6)
                if lead[2] > ZMIN:
                    dr.line([(p_pos[0], p_pos[1]), (lead[0], lead[1])], fill=(56, 232, 255), width=2)
                ang = math.atan2(p_nose[1] - p_tail[1], p_nose[0] - p_tail[0])
                s2 = max(5, p_pos[3] * s_mark)
                ca, sa = math.cos(ang), math.sin(ang)
                body = [(s2 * 2.2, 0), (-s2 * 1.2, s2 * 1.8), (-s2 * 1.7, 0), (-s2 * 1.2, -s2 * 1.8)]
                pts2 = [(p_pos[0] + bx * ca - by * sa, p_pos[1] + bx * sa + by * ca) for bx, by in body]
                dr.polygon(pts2, fill=(232, 238, 247))

        # HUD
        if font_big and font_mid:
            dr.rectangle([10, 8, 620, 64], fill=(10, 22, 40))
            tm = _dt.datetime.fromtimestamp(tt, _dt.timezone.utc).strftime("%H:%M:%S")
            alt_lbl = "高度(QNH)" if has_qh else "高度"
            line1 = "%s  %s   3D轨迹回放（服务端渲染）" % (callsign, date_str)
            line2 = "时刻 %s   %s %.0fm   航向 %.0f°   速度 %.0fkm/h" % (
                tm, alt_lbl, st_state["alt"] if st_state else 0,
                st_state["hd"] if st_state else 0, st_state["sp"] if st_state else 0)
            dr.text((18, 12), line1, fill=(127, 216, 143), font=font_big)
            dr.text((18, 38), line2, fill=(226, 232, 240), font=font_mid)

        img.save(tmp / ("%05d.png" % fi), compress_level=3)
        if fi % 24 == 0:
            _set_job(job_id, progress=fi + 1)
            time.sleep(0.01)   # 让出 GIL，避免拖垮 Web 服务

    _set_job(job_id, status="encoding")

    # ── 编码 MP4（ffmpeg），失败回退 GIF ──
    base_name = "render3d_%s_%s_%s" % (callsign, date_str, job_id)
    mp4 = OUT_DIR / (base_name + ".mp4")
    ok = False
    if shutil.which("ffmpeg"):
        for codec_args in (
            ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"],
            ["-c:v", "mpeg4", "-q:v", "4"],
        ):
            try:
                r = subprocess.run(
                    ["ffmpeg", "-y", "-framerate", str(FPS), "-i", str(tmp / "%05d.png")]
                    + codec_args + [str(mp4)],
                    capture_output=True, timeout=600)
                if r.returncode == 0 and mp4.exists() and mp4.stat().st_size > 10000:
                    ok = True
                    break
            except Exception:
                continue
    if not ok:
        gif = OUT_DIR / (base_name + ".gif")
        frames = [Image.open(tmp / ("%05d.png" % i)).convert("P", palette=Image.ADAPTIVE)
                  for i in range(0, n_total, 2)]
        frames[0].save(gif, save_all=True, append_images=frames[1:],
                       duration=int(2000 / FPS), loop=0)
        mp4 = gif
    shutil.rmtree(tmp, ignore_errors=True)
    return mp4


def _state_at(rec, xyz, tt):
    """二分 + 线性插值（与前端 _stateAt 一致）"""
    if tt <= rec[0]["t"]:
        return {"x": xyz[0][0], "y": xyz[0][1], "z": xyz[0][2],
                "alt": rec[0]["alt"], "hd": rec[0]["hd"], "sp": rec[0]["sp"]}
    if tt >= rec[-1]["t"]:
        return {"x": xyz[-1][0], "y": xyz[-1][1], "z": xyz[-1][2],
                "alt": rec[-1]["alt"], "hd": rec[-1]["hd"], "sp": rec[-1]["sp"]}
    lo, hi = 0, len(rec) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if rec[mid]["t"] <= tt:
            lo = mid
        else:
            hi = mid
    a, b = rec[lo], rec[hi]
    f = (tt - a["t"]) / (b["t"] - a["t"]) if b["t"] > a["t"] else 0
    return {
        "x": xyz[lo][0] + (xyz[hi][0] - xyz[lo][0]) * f,
        "y": xyz[lo][1] + (xyz[hi][1] - xyz[lo][1]) * f,
        "z": xyz[lo][2] + (xyz[hi][2] - xyz[lo][2]) * f,
        "alt": a["alt"] + (b["alt"] - a["alt"]) * f,
        "hd": a["hd"] + (b["hd"] - a["hd"]) * f,
        "sp": a["sp"] + (b["sp"] - a["sp"]) * f,
    }
