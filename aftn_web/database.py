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
        self._lock = threading.Lock()
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
                adep TEXT NOT NULL DEFAULT '',
                adest TEXT NOT NULL DEFAULT '',
                ssr TEXT NOT NULL DEFAULT '',
                aircraft_type TEXT NOT NULL DEFAULT '',
                flight_rules TEXT NOT NULL DEFAULT '',
                route TEXT NOT NULL DEFAULT '',
                dof TEXT,
                etd TEXT,
                eet_minutes INTEGER NOT NULL DEFAULT 0,
                atd TEXT,
                eta TEXT,
                ata TEXT,
                source_message_type TEXT NOT NULL DEFAULT '',
                last_message_time TEXT,
                raw_message_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_fpl_key
                ON flight_plans(callsign, adep, adest);

            CREATE INDEX IF NOT EXISTS idx_fpl_dof
                ON flight_plans(dof);

            CREATE INDEX IF NOT EXISTS idx_fpl_adep
                ON flight_plans(adep);

            CREATE INDEX IF NOT EXISTS idx_fpl_adest
                ON flight_plans(adest);

            CREATE INDEX IF NOT EXISTS idx_aftn_type
                ON aftn_messages(message_type);

            CREATE INDEX IF NOT EXISTS idx_aftn_time
                ON aftn_messages(received_at);
        """)
        conn.commit()

    # ── AFTN 报文 ──

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
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conn = self._get_conn()
        where = ""
        params: list[str] = []
        if message_type:
            where = "WHERE message_type = ?"
            params.append(message_type)
        rows = conn.execute(
            f"SELECT * FROM aftn_messages {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return [dict(r) for r in rows]

    # ── 飞行计划 ──

    def upsert_flight_plan(self, plan: FlightPlan) -> int:
        """插入或更新飞行计划（按 callsign+adep+adest 唯一键）"""
        now = _fmt_dt(datetime.utcnow())
        conn = self._get_conn()

        existing = conn.execute(
            "SELECT id, dof, atd, ata FROM flight_plans WHERE callsign=? AND adep=? AND adest=?",
            (plan.callsign, plan.adep, plan.adest),
        ).fetchone()

        if existing:
            # 更新已有记录
            updates = {
                "ssr": plan.ssr,
                "aircraft_type": plan.aircraft_type,
                "flight_rules": plan.flight_rules,
                "route": plan.route,
                "eet_minutes": plan.eet_minutes,
                "source_message_type": plan.source_message_type,
                "last_message_time": _fmt_dt(plan.last_message_time),
                "raw_message_text": plan.raw_message_text or "",
                "updated_at": now,
            }
            # 不直接用 DEP/ARR 覆盖已有 dof
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
            # 插入新记录
            conn.execute(
                """INSERT INTO flight_plans
                   (callsign, adep, adest, ssr, aircraft_type, flight_rules, route,
                    dof, etd, eet_minutes, atd, eta, ata,
                    source_message_type, last_message_time, raw_message_text,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan.callsign,
                    plan.adep,
                    plan.adest,
                    plan.ssr,
                    plan.aircraft_type,
                    plan.flight_rules,
                    plan.route,
                    _fmt_date(plan.dof),
                    _fmt_dt(plan.etd),
                    plan.eet_minutes,
                    _fmt_dt(plan.atd),
                    _fmt_dt(plan.eta),
                    _fmt_dt(plan.ata),
                    plan.source_message_type,
                    _fmt_dt(plan.last_message_time),
                    plan.raw_message_text or "",
                    now,
                    now,
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
        keyword: str | None = None,
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
        if keyword:
            conditions.append("(callsign LIKE ? OR adep LIKE ? OR adest LIKE ? OR route LIKE ?)")
            kw = f"%{keyword.upper()}%"
            params.extend([kw, kw, kw, kw])

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"SELECT * FROM flight_plans {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        rows = conn.execute(sql, params + [limit, offset]).fetchall()
        return [dict(r) for r in rows]

    def count_flight_plans(
        self,
        callsign: str | None = None,
        adep: str | None = None,
        adest: str | None = None,
        dof: date | None = None,
        ssr: str | None = None,
        aircraft_type: str | None = None,
        source_message_type: str | None = None,
        keyword: str | None = None,
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
        if keyword:
            conditions.append("(callsign LIKE ? OR adep LIKE ? OR adest LIKE ? OR route LIKE ?)")
            kw = f"%{keyword.upper()}%"
            params.extend([kw, kw, kw, kw])

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        row = conn.execute(f"SELECT COUNT(*) FROM flight_plans {where}", params).fetchone()
        return row[0]


def _fmt_dt(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def _fmt_date(d: date | None) -> str | None:
    return d.isoformat() if d else None
