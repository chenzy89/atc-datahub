# -*- coding: utf-8 -*-
"""回填 flight_plans.region（属地：境内/境外）

用途：v2.4.2 新增 region 列后，把历史飞行计划按呼号三字码补上属地。
特性：幂等——只处理 region 为空的行，可反复执行（服务重启后补跑一次即可）。

用法：
    /usr/bin/python3 scripts/backfill_flight_plan_region.py [数据库路径] [--batch 50000]

判定规则（与 aftn_web/airline_region.py 一致）：
    1) 三字码在 config/airline_domestic.json 的 domestic 列表 → 境内
    2) 呼号形如 B6512 / B738 / B（中国注册号）→ 境内
    3) 其余 → 境外
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aftn_web.airline_region import _CHINA_REG_RE  # noqa: E402

DEFAULT_DB = ROOT / "data" / "aftn.db"
REGION_FILE = ROOT / "config" / "airline_domestic.json"


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    db_path = Path(args[0]) if args else DEFAULT_DB
    batch = 50000
    for a in sys.argv[1:]:
        if a.startswith("--batch"):
            batch = int(a.split("=", 1)[1]) if "=" in a else 50000

    if not db_path.exists():
        sys.exit("数据库不存在: %s" % db_path)
    if not REGION_FILE.exists():
        sys.exit("缺少映射表: %s（先跑 scripts/build_airline_region.py）" % REGION_FILE)

    raw = json.loads(REGION_FILE.read_text(encoding="utf-8"))
    domestic = sorted({str(c).strip().upper() for c in (raw.get("domestic") or []) if str(c).strip()})
    print("境内三字码 %d 个，数据库 %s" % (len(domestic), db_path))

    con = sqlite3.connect(str(db_path), timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    con.execute("PRAGMA synchronous=NORMAL")

    # 1. 列与索引（服务已启动过则已存在）
    try:
        con.execute("ALTER TABLE flight_plans ADD COLUMN region TEXT NOT NULL DEFAULT ''")
        con.commit()
        print("已新增 region 列")
    except sqlite3.OperationalError:
        pass
    try:
        con.execute("CREATE INDEX IF NOT EXISTS idx_fpl_region ON flight_plans(region)")
        con.commit()
    except sqlite3.OperationalError:
        pass

    todo = con.execute("SELECT COUNT(*) FROM flight_plans WHERE region='' OR region IS NULL").fetchone()[0]
    total = con.execute("SELECT COUNT(*) FROM flight_plans").fetchone()[0]
    print("待回填 %d / 总计 %d 行" % (todo, total))
    if todo == 0:
        print("无需回填")
        return

    in_list = ",".join("'%s'" % c for c in domestic)   # 仅内部生成，无外部输入
    case_sql = (
        "CASE "
        "WHEN UPPER(TRIM(SUBSTR(callsign,1,3))) IN (%s) THEN '境内' "
        "WHEN UPPER(TRIM(callsign)) GLOB 'B[0-9]*' THEN '境内' "
        "WHEN UPPER(TRIM(callsign)) = 'B' THEN '境内' "
        "ELSE '境外' END" % in_list
    )

    max_id = con.execute("SELECT COALESCE(MAX(id),0) FROM flight_plans").fetchone()[0]
    done = 0
    t0 = time.time()
    upper = min(batch, max_id)
    while upper <= max_id + batch:
        cur = con.execute(
            "UPDATE flight_plans SET region = %s "
            "WHERE id > ? AND id <= ? AND (region='' OR region IS NULL)" % case_sql,
            (upper - batch, upper),
        )
        con.commit()
        done += cur.rowcount
        if done and (done % (batch * 5) < batch or upper >= max_id):
            print("  已处理至 id=%d，累计回填 %d 行（%.0fs）" % (upper, done, time.time() - t0))
        upper += batch

    print("完成：回填 %d 行，耗时 %.1fs" % (done, time.time() - t0))

    # 2. 结果校验
    for rg, cnt in con.execute("SELECT region, COUNT(*) FROM flight_plans GROUP BY region ORDER BY 2 DESC"):
        print("  %-4s %d" % (rg or "(空)", cnt))
    left = con.execute("SELECT COUNT(*) FROM flight_plans WHERE region='' OR region IS NULL").fetchone()[0]
    print("仍为空的行: %d" % left)
    con.close()


if __name__ == "__main__":
    main()
