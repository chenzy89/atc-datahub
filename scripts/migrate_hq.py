"""批量修改历史报文：发报地址为 ZBBBZGZX 的报文，类型改为 HQ"""
from __future__ import annotations
import sys
sys.path.insert(0, "/home/share/atc_aftn_web")

from aftn_web.parser import _extract_sender

DB = "/home/share/atc_aftn_web/data/aftn.db"
import sqlite3

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# 找出所有 message_type 不是 HQ 但发报地址为 ZBBBZGZX 的记录
rows = conn.execute(
    "SELECT id, raw_text, message_type FROM aftn_messages ORDER BY id"
).fetchall()

changed = 0
for r in rows:
    raw = r["raw_text"] or ""
    sender = _extract_sender(raw)
    current_type = r["message_type"] or ""
    if sender == "ZBBBZGZX" and current_type != "HQ":
        conn.execute("UPDATE aftn_messages SET message_type='HQ' WHERE id=?", (r["id"],))
        changed += 1
        print(f"  id={r['id']:>6}  {current_type:6s} → HQ    ({raw[:60]})")

conn.commit()
conn.close()
print(f"\n共修改 {changed} 条记录")
