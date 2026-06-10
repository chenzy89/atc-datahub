#!/usr/bin/env python3
"""回填脚本：为已落地但终端时长为 0 的航班补算终端时长（entry -> exit/ata）"""
import sys, sqlite3
from datetime import datetime, timezone

DB_PATH = "/home/share/atc_aftn_web/data/aftn.db"

def parse_iso(val):
    if not val:
        return None
    try:
        s = val.replace("Z", "+00:00").replace("T", " ")
        if "+" not in s and s.count("-") >= 2:
            dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    rows = c.execute("""
        SELECT id, callsign, dof, entry_time, exit_time, ata, terminal_flight_time
        FROM flight_plans
        WHERE entry_time != ''
          AND terminal_flight_time = 0
          AND (exit_time != '' OR (ata IS NOT NULL AND ata != ''))
        ORDER BY dof DESC
    """).fetchall()
    print("待回填记录数: %d" % len(rows))

    updated = 0
    for r in rows:
        entry = parse_iso(r["entry_time"])
        end = parse_iso(r["exit_time"] or r["ata"] or "")
        if not entry or not end:
            continue
        diff = int((end - entry).total_seconds())
        if diff <= 0:
            continue
        if diff > 86400:
            # 超过 24 小时，大概率数据有问题，跳过
            continue
        c.execute(
            "UPDATE flight_plans SET terminal_flight_time=? WHERE id=?",
            (diff, r["id"]),
        )
        conn.commit()
        updated += 1
        print("  #%d %s dof=%s entry=%s -> %s: 0 -> %ds (%dm%ds)" % (
            r["id"], r["callsign"].ljust(10), r["dof"],
            r["entry_time"][:19], str(end)[:19],
            diff, diff // 60, diff % 60,
        ))

    print("\n共更新 %d/%d 条记录" % (updated, len(rows)))
    conn.close()

if __name__ == "__main__":
    main()
