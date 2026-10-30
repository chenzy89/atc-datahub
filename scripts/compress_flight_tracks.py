"""一次性迁移：把 flight_tracks.points_json 从明文 TEXT 压缩为 gzip BLOB（v2.3.19）

- 幂等：只处理 typeof(points_json)='text' 的行，已压缩的 BLOB 自动跳过
- 在线执行：每 500 行提交一次，与运行中的 aftn_web 共存（busy_timeout 保护）
- 用法: python3 scripts/compress_flight_tracks.py [数据库路径]
- 完成后空间不会立即缩小，等每日 04:00 VACUUM 自动收缩（或手动 VACUUM）
"""
import gzip
import sqlite3
import sys
import time

DB = sys.argv[1] if len(sys.argv) > 1 else "/home/share/atc_datahub/data/aftn.db"
BATCH = 500


def main() -> int:
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA journal_mode=WAL")

    pending = con.execute(
        "SELECT COUNT(*) FROM flight_tracks WHERE typeof(points_json)='text'"
    ).fetchone()[0]
    print(f"待压缩行数: {pending}", flush=True)
    if pending == 0:
        print("无需迁移", flush=True)
        return 0

    total = 0
    skipped = 0
    t0 = time.monotonic()
    while True:
        rows = con.execute(
            "SELECT id, points_json FROM flight_tracks "
            "WHERE typeof(points_json)='text' LIMIT ?",
            (BATCH,),
        ).fetchall()
        if not rows:
            break
        updates = []
        for row_id, pts in rows:
            try:
                updates.append((gzip.compress(pts.encode("utf-8"), 6), row_id))
            except Exception:
                skipped += 1
        if updates:
            con.executemany("UPDATE flight_tracks SET points_json=? WHERE id=?", updates)
            con.commit()
        total += len(updates)
        if total % 10000 < BATCH:
            pct = (total + pending - pending) and min(100, 100 * total // max(1, con.execute(
                "SELECT COUNT(*) FROM flight_tracks").fetchone()[0]))
            print(f"已压缩 {total} 行（跳过 {skipped}），{time.monotonic()-t0:.0f}s", flush=True)

    remain = con.execute(
        "SELECT COUNT(*) FROM flight_tracks WHERE typeof(points_json)='text'"
    ).fetchone()[0]
    print(f"迁移完成: 共压缩 {total} 行，跳过 {skipped} 行，剩余明文 {remain} 行，"
          f"耗时 {time.monotonic()-t0:.0f}s", flush=True)
    print("提示: 空间将在每日 04:00 VACUUM 后真正释放（vacuum_enabled=true）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
