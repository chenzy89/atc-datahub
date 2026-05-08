"""批量修改历史报文：发报地址为 ZGSDYMYX / ZGSZYMYX 的报文，类型改为 METAR"""
from __future__ import annotations
import sys
sys.path.insert(0, "/home/share/atc_aftn_web")

from aftn_web.parser import _extract_sender

DB = "/home/share/atc_aftn_web/data/aftn.db"
import sqlite3

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT id, raw_text, message_type FROM aftn_messages ORDER BY id"
).fetchall()

changed = 0
sender_set = {"ZGSDYMYX", "ZGSZYMYX"}
for r in rows:
    raw = r["raw_text"] or ""
    sender = _extract_sender(raw)
    current_type = r["message_type"] or ""
    if sender in sender_set and current_type != "METAR":
        conn.execute("UPDATE aftn_messages SET message_type='METAR' WHERE id=?", (r["id"],))
        changed += 1
        print(f"  id={r['id']:>6}  {current_type:6s} → METAR  ({raw[:60]})")

conn.commit()
conn.close()
print(f"\n共修改 {changed} 条记录")
