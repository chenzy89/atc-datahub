#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3.2.22 构建 3D 轨迹图陆地数据（底图 + 补充多边形）

背景：aftn_web/static/coastline_prd.json 由 map/COASTLINE.txt 一次性提取而来，没有生成脚本。
本脚本改为可重复构建：
  base  = aftn_web/static/coastline_prd_base.json   （原始底图，首次运行自动从 coastline_prd.json 备份）
  extra = config/extra_land_polys.json              （人工补充区域，如机场填海区，ddmmss 坐标）
  out   = aftn_web/static/coastline_prd.json        （= base + extra，幂等）

用法：python3 scripts/build_coastline_extra.py
"""
import json
import shutil
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC = BASE_DIR / "aftn_web" / "static"
OUT_FILE = STATIC / "coastline_prd.json"
BASE_FILE = STATIC / "coastline_prd_base.json"
EXTRA_FILE = BASE_DIR / "config" / "extra_land_polys.json"


def dms_to_dec(s):
    """223935N -> 22.659722 ; 1134630E -> 113.775 ; 支持 ddmmss / dddmmss"""
    s = s.strip().upper()
    if not s:
        raise ValueError("空坐标")
    hemi = s[-1]
    body = s[:-1]
    if not body.isdigit():
        raise ValueError("非法坐标: %s" % s)
    if len(body) == 6:                  # 纬度 ddmmss
        dd, mm, ss = int(body[0:2]), int(body[2:4]), int(body[4:6])
    elif len(body) == 7:                # 经度 dddmmss
        dd, mm, ss = int(body[0:3]), int(body[3:5]), int(body[5:7])
    else:
        raise ValueError("坐标长度不对: %s" % s)
    v = dd + mm / 60.0 + ss / 3600.0
    if hemi in ("S", "W"):
        v = -v
    elif hemi not in ("N", "E"):
        raise ValueError("缺少半球标识: %s" % s)
    return round(v, 6)


def main():
    if not OUT_FILE.exists():
        print("[x] 缺少 %s" % OUT_FILE)
        return 1
    # 首次运行：把当前数据备份为 base（之后不再覆盖，保证幂等可重复构建）
    if not BASE_FILE.exists():
        shutil.copy2(OUT_FILE, BASE_FILE)
        print("[+] 已备份底图 -> %s" % BASE_FILE.name)

    base = json.loads(BASE_FILE.read_text(encoding="utf-8"))
    land = [list(p) for p in (base.get("land") or [])]
    n_base = len(land)

    extras = []
    if EXTRA_FILE.exists():
        cfg = json.loads(EXTRA_FILE.read_text(encoding="utf-8"))
        for item in cfg.get("polys") or []:
            coords = item.get("coords") or []
            if len(coords) < 3:
                print("[!] 跳过点数不足的多边形: %s" % item.get("name"))
                continue
            ring = []
            for c in coords:
                parts = str(c).split()
                if len(parts) != 2:
                    raise ValueError("坐标格式应为 '223935N 1134630E': %s" % c)
                lat = dms_to_dec(parts[0])          # 纬度在前
                lon = dms_to_dec(parts[1])          # 经度在后
                ring.append([lat, lon])
            extras.append(ring)
            lats = [q[0] for q in ring]
            lons = [q[1] for q in ring]
            print("[+] %s: %d 点, lat %.4f~%.4f, lon %.4f~%.4f"
                  % (item.get("name", "unnamed"), len(ring),
                     min(lats), max(lats), min(lons), max(lons)))

    out = {"land": land + extras}
    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
    size_kb = OUT_FILE.stat().st_size / 1024.0
    print("[=] 写出 %s：底图 %d + 补充 %d = %d 个多边形, %.0f KB"
          % (OUT_FILE.name, n_base, len(extras), len(out["land"]), size_kb))
    return 0


if __name__ == "__main__":
    sys.exit(main())
