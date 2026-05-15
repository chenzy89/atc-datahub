#!/usr/bin/env python3
"""回填已有飞行计划的移交点字段（handover_pt）"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aftn_web.database import Database
from aftn_web.handover import HandoverResolver

DB_PATH = "/home/share/atc_aftn_web/data/aftn.db"


def main():
    db = Database(DB_PATH)
    resolver = HandoverResolver()

    conn = db._get_conn()
    rows = conn.execute(
        "SELECT id, route, handover_pt FROM flight_plans WHERE route != ''"
    ).fetchall()

    updated = 0
    no_change = 0
    for row in rows:
        fpl_id = row["id"]
        route = row["route"]
        current_hp = row["handover_pt"] or ""
        computed_hp = resolver.resolve(route)
        if computed_hp and computed_hp != current_hp:
            conn.execute(
                "UPDATE flight_plans SET handover_pt=? WHERE id=?",
                (computed_hp, fpl_id),
            )
            updated += 1
            if updated <= 5:
                print(f"  #{fpl_id}: {current_hp or '(空)'} → {computed_hp}  ({route[:50]})")
        elif not computed_hp and current_hp:
            # Clear stale handover point if route no longer matches
            conn.execute(
                "UPDATE flight_plans SET handover_pt='' WHERE id=?",
                (fpl_id,),
            )
            updated += 1
        else:
            no_change += 1

    conn.commit()
    print(f"\n✅ 完成：更新 {updated} 条，无需改动 {no_change} 条")


if __name__ == "__main__":
    main()
