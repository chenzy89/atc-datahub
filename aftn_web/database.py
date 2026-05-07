"""SQLite 数据库操作"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .models import AftnMessage, FlightPlan


class Database:
    """SQLite 数据库封装，线程安全"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
        return self._local.conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS aftn_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_text TEXT NOT NULL DEFAULT '',
                message_type TEXT NOT NULL DEFAULT '',
                message_text TEXT NOT NULL DEFAULT '',
                utc_time TEXT,
                received_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS flight_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                callsign TEXT NOT NULL DEFAULT '',
                ssr TEXT NOT NULL DEFAULT '',
                aircraft_type TEXT NOT NULL DEFAULT '',
                dof TEXT,
                adep TEXT NOT NULL DEFAULT '',
                etd TEXT,
                atd TEXT,
                adest TEXT NOT NULL DEFAULT '',
                eta TEXT,
                ata TEXT,
                route TEXT NOT NULL DEFAULT '',
                source_message_type TEXT NOT NULL DEFAULT '',
                last_message_time TEXT,
                raw_message_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_fpl_dof
                ON flight_plans(dof);
            CREATE INDEX IF NOT EXISTS idx_fpl_adep
                ON flight_plans(adep);
            CREATE INDEX IF NOT EXISTS idx_fpl_adest
                ON flight_plans(adest);
            CREATE INDEX IF NOT EXISTS idx_fpl_callsign
                ON flight_plans(callsign);
            CREATE INDEX IF NOT EXISTS idx_aftn_type
                ON aftn_messages(message_type);
            CREATE INDEX IF NOT EXISTS idx_aftn_time
                ON aftn_messages(received_at);
        """)
        conn.commit()

    # ── AFTN 报文 ──────────────────────────────────────────────

    def save_aftn_message(self, msg: AftnMessage) -> int:
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO aftn_messages (raw_text, message_type, message_text, utc_time, received_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                msg.raw_text,
                msg.message_type,
                msg.message_text,
                _fmt_dt(msg.utc_time),
                _fmt_dt(msg.received_at),
            ),
        )
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def query_aftn_messages(
        self,
        message_type: str | None = None,
        keyword: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conn = self._get_conn()
        conditions: list[str] = []
        params: list[Any] = []

        if message_type:
            conditions.append("message_type = ?")
            params.append(message_type)
        if keyword:
            conditions.append("(raw_text LIKE ? OR message_text LIKE ?)")
            kw = f"%{keyword}%"
            params.extend([kw, kw])
        if date_from:
            conditions.append("received_at >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("received_at <= ?")
            params.append(date_to + " 23:59:59")

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = conn.execute(
            f"SELECT * FROM aftn_messages {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return [dict(r) for r in rows]

    def count_aftn_messages(
        self,
        message_type: str | None = None,
        keyword: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> int:
        conn = self._get_conn()
        conditions: list[str] = []
        params: list[Any] = []
        if message_type:
            conditions.append("message_type = ?")
            params.append(message_type)
        if keyword:
            conditions.append("(raw_text LIKE ? OR message_text LIKE ?)")
            kw = f"%{keyword}%"
            params.extend([kw, kw])
        if date_from:
            conditions.append("received_at >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("received_at <= ?")
            params.append(date_to + " 23:59:59")
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        row = conn.execute(f"SELECT COUNT(*) FROM aftn_messages {where}", params).fetchone()
        return row[0]

    def count_aftn_by_type(self) -> dict[str, int]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT message_type, COUNT(*) as cnt FROM aftn_messages GROUP BY message_type"
        ).fetchall()
        return {r["message_type"]: r["cnt"] for r in rows}

    # ── 飞行计划 ──────────────────────────────────────────────

    def upsert_flight_plan(self, plan: FlightPlan) -> int:
        """由报文自动调用：插入或按 callsign+adep+adest 更新已有记录"""
        now = _fmt_dt(datetime.utcnow())
        conn = self._get_conn()

        existing = conn.execute(
            "SELECT id, dof, atd, ata FROM flight_plans WHERE callsign=? AND adep=? AND adest=?",
            (plan.callsign, plan.adep, plan.adest),
        ).fetchone()

        if existing:
            updates: dict[str, Any] = {
                "ssr": plan.ssr,
                "aircraft_type": plan.aircraft_type,
                "route": plan.route,
                "source_message_type": plan.source_message_type,
                "last_message_time": _fmt_dt(plan.last_message_time),
                "raw_message_text": plan.raw_message_text or "",
                "updated_at": now,
            }
            # DEP/ARR 不轻易覆盖已有 dof；其他报文允许更新
            if plan.source_message_type not in ("DEP", "ARR") or not existing["dof"]:
                if plan.dof:
                    updates["dof"] = _fmt_date(plan.dof)
            if plan.etd:
                updates["etd"] = _fmt_dt(plan.etd)
            if plan.atd:
                updates["atd"] = _fmt_dt(plan.atd)
            if plan.eta:
                updates["eta"] = _fmt_dt(plan.eta)
            if plan.ata:
                updates["ata"] = _fmt_dt(plan.ata)

            set_clause = ", ".join(f"{k}=?" for k in updates)
            conn.execute(
                f"UPDATE flight_plans SET {set_clause} WHERE id=?",
                list(updates.values()) + [existing["id"]],
            )
            conn.commit()
            return existing["id"]
        else:
            conn.execute(
                """INSERT INTO flight_plans
                   (callsign, ssr, aircraft_type, dof, adep, etd, atd,
                    adest, eta, ata, route,
                    source_message_type, last_message_time, raw_message_text,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan.callsign, plan.ssr, plan.aircraft_type,
                    _fmt_date(plan.dof), plan.adep,
                    _fmt_dt(plan.etd), _fmt_dt(plan.atd),
                    plan.adest,
                    _fmt_dt(plan.eta), _fmt_dt(plan.ata),
                    plan.route,
                    plan.source_message_type,
                    _fmt_dt(plan.last_message_time),
                    plan.raw_message_text or "",
                    now, now,
                ),
            )
            conn.commit()
            return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def query_flight_plans(
        self,
        callsign: str | None = None,
        adep: str | None = None,
        adest: str | None = None,
        dof: date | None = None,
        ssr: str | None = None,
        aircraft_type: str | None = None,
        source_message_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conn = self._get_conn()
        conditions: list[str] = []
        params: list[Any] = []

        if callsign:
            conditions.append("callsign LIKE ?")
            params.append(f"%{callsign.upper()}%")
        if adep:
            conditions.append("adep LIKE ?")
            params.append(f"%{adep.upper()}%")
        if adest:
            conditions.append("adest LIKE ?")
            params.append(f"%{adest.upper()}%")
        if dof:
            conditions.append("dof = ?")
            params.append(_fmt_date(dof))
        if ssr:
            conditions.append("ssr LIKE ?")
            params.append(f"%{ssr.upper()}%")
        if aircraft_type:
            conditions.append("aircraft_type LIKE ?")
            params.append(f"%{aircraft_type.upper()}%")
        if source_message_type:
            conditions.append("source_message_type = ?")
            params.append(source_message_type.upper())

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"SELECT * FROM flight_plans {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        rows = conn.execute(sql, params + [limit, offset]).fetchall()
        return [dict(r) for r in rows]

    def get_flight_plan(self, fpl_id: int) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM flight_plans WHERE id=?", (fpl_id,)
        ).fetchone()
        return dict(row) if row else None

    def count_flight_plans(
        self,
        callsign: str | None = None,
        adep: str | None = None,
        adest: str | None = None,
        dof: date | None = None,
        ssr: str | None = None,
        aircraft_type: str | None = None,
        source_message_type: str | None = None,
    ) -> int:
        conn = self._get_conn()
        conditions: list[str] = []
        params: list[Any] = []
        if callsign:
            conditions.append("callsign LIKE ?")
            params.append(f"%{callsign.upper()}%")
        if adep:
            conditions.append("adep LIKE ?")
            params.append(f"%{adep.upper()}%")
        if adest:
            conditions.append("adest LIKE ?")
            params.append(f"%{adest.upper()}%")
        if dof:
            conditions.append("dof = ?")
            params.append(_fmt_date(dof))
        if ssr:
            conditions.append("ssr LIKE ?")
            params.append(f"%{ssr.upper()}%")
        if aircraft_type:
            conditions.append("aircraft_type LIKE ?")
            params.append(f"%{aircraft_type.upper()}%")
        if source_message_type:
            conditions.append("source_message_type = ?")
            params.append(source_message_type.upper())
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        row = conn.execute(f"SELECT COUNT(*) FROM flight_plans {where}", params).fetchone()
        return row[0]

    def create_flight_plan(self, plan: FlightPlan) -> int:
        """手动新增飞行计划"""
        now = _fmt_dt(datetime.utcnow())
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO flight_plans
               (callsign, ssr, aircraft_type, dof, adep, etd, atd,
                adest, eta, ata, route, source_message_type,
                last_message_time, raw_message_text, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                plan.callsign, plan.ssr, plan.aircraft_type,
                _fmt_date(plan.dof), plan.adep,
                _fmt_dt(plan.etd), _fmt_dt(plan.atd),
                plan.adest,
                _fmt_dt(plan.eta), _fmt_dt(plan.ata),
                plan.route,
                plan.source_message_type or "MANUAL",
                _fmt_dt(plan.last_message_time),
                plan.raw_message_text or "",
                now, now,
            ),
        )
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def update_flight_plan(self, fpl_id: int, plan: FlightPlan) -> bool:
        """手动更新飞行计划"""
        now = _fmt_dt(datetime.utcnow())
        conn = self._get_conn()
        existing = conn.execute(
            "SELECT id FROM flight_plans WHERE id=?", (fpl_id,)
        ).fetchone()
        if not existing:
            return False
        conn.execute(
            """UPDATE flight_plans SET
               callsign=?, ssr=?, aircraft_type=?, dof=?, adep=?,
               etd=?, atd=?, adest=?, eta=?, ata=?, route=?,
               source_message_type=?, last_message_time=?,
               raw_message_text=?, updated_at=?
               WHERE id=?""",
            (
                plan.callsign, plan.ssr, plan.aircraft_type,
                _fmt_date(plan.dof), plan.adep,
                _fmt_dt(plan.etd), _fmt_dt(plan.atd),
                plan.adest,
                _fmt_dt(plan.eta), _fmt_dt(plan.ata),
                plan.route,
                plan.source_message_type or "MANUAL",
                _fmt_dt(plan.last_message_time),
                plan.raw_message_text or "",
                now,
                fpl_id,
            ),
        )
        conn.commit()
        return True

    def delete_flight_plan(self, fpl_id: int) -> bool:
        """删除飞行计划"""
        conn = self._get_conn()
        existing = conn.execute(
            "SELECT id FROM flight_plans WHERE id=?", (fpl_id,)
        ).fetchone()
        if not existing:
            return False
        conn.execute("DELETE FROM flight_plans WHERE id=?", (fpl_id,))
        conn.commit()
        return True


def _fmt_dt(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def _fmt_date(d: date | None) -> str | None:
    return d.isoformat() if d else None
