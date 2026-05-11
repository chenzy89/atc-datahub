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
                flight_rule TEXT NOT NULL DEFAULT '',
                message_types TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            DROP INDEX IF EXISTS idx_fpl_key;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_fpl_key
                ON flight_plans(callsign, adep, adest, dof);
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

    def get_aftn_message(self, msg_id: int) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM aftn_messages WHERE id=?", (msg_id,)
        ).fetchone()
        return dict(row) if row else None

    def count_aftn_by_type(self) -> dict[str, int]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT message_type, COUNT(*) as cnt FROM aftn_messages GROUP BY message_type"
        ).fetchall()
        return {r["message_type"]: r["cnt"] for r in rows}

    # ── 飞行计划 ──────────────────────────────────────────────

    def upsert_flight_plan(self, plan: FlightPlan) -> int:
        """由报文自动调用：按 callsign+adep+adest+dof 插入或更新已有记录"""
        now = _fmt_dt(datetime.utcnow())
        conn = self._get_conn()

        existing = conn.execute(
            "SELECT id, dof, atd, ata, flight_rule, message_types FROM flight_plans WHERE callsign=? AND adep=? AND adest=? AND dof=?",
            (plan.callsign, plan.adep, plan.adest, _fmt_date(plan.dof)),
        ).fetchone()

        if existing:
            # ── 维护 message_types（逗号分隔，去重） ──────────────
            msg_types_raw = existing["message_types"] or ""
            existing_types = [t.strip() for t in msg_types_raw.split(",") if t.strip()]
            if plan.source_message_type and plan.source_message_type not in existing_types:
                existing_types.append(plan.source_message_type)
            merged_types = ",".join(existing_types)

            updates: dict[str, Any] = {
                "source_message_type": plan.source_message_type,
                "last_message_time": _fmt_dt(plan.last_message_time),
                "raw_message_text": plan.raw_message_text or "",
                "updated_at": now,
                "message_types": merged_types,
            }
            # 维护 flight_rule（只在 FPL 时更新，避免被 DEP/ARR 覆盖）
            if plan.flight_rule and plan.source_message_type == "FPL":
                updates["flight_rule"] = plan.flight_rule

            # 以下字段只在报文中含有有效值时更新，避免被不含这些字段的报文（如 DEP/ARR）覆盖为空
            if plan.ssr:
                updates["ssr"] = plan.ssr
            if plan.aircraft_type:
                updates["aircraft_type"] = plan.aircraft_type
            if plan.route:
                updates["route"] = plan.route
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
            # ── 新建记录，初始化 message_types ──────────────
            init_types = plan.source_message_type if plan.source_message_type else ""
            conn.execute(
                """INSERT INTO flight_plans
                   (callsign, ssr, aircraft_type, dof, adep, etd, atd,
                    adest, eta, ata, route,
                    source_message_type, last_message_time, raw_message_text,
                    flight_rule, message_types, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    plan.flight_rule or "",
                    init_types,
                    now, now,
                ),
            )
            conn.commit()
            return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def find_flight_plan(self, callsign: str, adep: str, adest: str, dof: date | None = None) -> dict[str, Any] | None:
        """按 callsign+adep+adest+可选 DOF 查找飞行计划"""
        conn = self._get_conn()
        if dof:
            row = conn.execute(
                "SELECT * FROM flight_plans WHERE callsign=? AND adep=? AND adest=? AND dof=?",
                (callsign, adep, adest, _fmt_date(dof)),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM flight_plans WHERE callsign=? AND adep=? AND adest=?",
                (callsign, adep, adest),
            ).fetchone()
        return dict(row) if row else None

    def update_flight_plan_atd(self, fpl_id: int, atd: datetime, ssr: str = "", source_message_type: str = "") -> bool:
        """更新指定飞行计划的 ATD，可选同时更新 SSR（来自 DEP 报文）"""
        conn = self._get_conn()
        now = _fmt_dt(datetime.utcnow())
        existing = conn.execute(
            "SELECT message_types FROM flight_plans WHERE id=?", (fpl_id,)
        ).fetchone()
        msg_types = self._merge_message_type(existing["message_types"] if existing else "", source_message_type) if source_message_type else (existing["message_types"] if existing else "")
        if ssr:
            cur = conn.execute(
                "UPDATE flight_plans SET atd=?, ssr=?, message_types=?, updated_at=? WHERE id=?",
                (_fmt_dt(atd), ssr, msg_types, now, fpl_id),
            )
        else:
            cur = conn.execute(
                "UPDATE flight_plans SET atd=?, message_types=?, updated_at=? WHERE id=?",
                (_fmt_dt(atd), msg_types, now, fpl_id),
            )
        conn.commit()
        return cur.rowcount > 0

    def update_flight_plan_ata(self, fpl_id: int, ata: datetime, source_message_type: str = "") -> bool:
        """更新指定飞行计划的 ATA"""
        conn = self._get_conn()
        now = _fmt_dt(datetime.utcnow())
        existing = conn.execute(
            "SELECT message_types FROM flight_plans WHERE id=?", (fpl_id,)
        ).fetchone()
        msg_types = self._merge_message_type(existing["message_types"] if existing else "", source_message_type) if source_message_type else (existing["message_types"] if existing else "")
        cur = conn.execute(
            "UPDATE flight_plans SET ata=?, message_types=?, updated_at=? WHERE id=?",
            (_fmt_dt(ata), msg_types, now, fpl_id),
        )
        conn.commit()
        return cur.rowcount > 0

    def delete_by_key(self, callsign: str, adep: str, adest: str) -> bool:
        """按 callsign+adep+adest 删除飞行计划（用于 CNL 取消报）"""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT id FROM flight_plans WHERE callsign=? AND adep=? AND adest=?",
            (callsign.strip().upper(), adep.strip().upper()[:4], adest.strip().upper()[:4]),
        ).fetchone()
        if not cur:
            return False
        conn.execute("DELETE FROM flight_plans WHERE id=?", (cur["id"],))
        conn.commit()
        return True

    def query_flight_plans(
        self,
        callsign: str | None = None,
        adep: str | None = None,
        adest: str | None = None,
        dof: date | None = None,
        airport: str | None = None,  # 关注机场：adep OR adest 匹配
        route: str | None = None,  # 航路关键词
        source_message_type: str | None = None,
        flight_rule: str | None = None,
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
        if airport:
            conditions.append("(adep LIKE ? OR adest LIKE ?)")
            params.append(f"%{airport.upper()}%")
            params.append(f"%{airport.upper()}%")
        if route:
            conditions.append("route LIKE ?")
            params.append(f"%{route.upper()}%")
        if source_message_type:
            conditions.append("source_message_type = ?")
            params.append(source_message_type.upper())
        if flight_rule:
            conditions.append("flight_rule = ?")
            params.append(flight_rule.upper())

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
        airport: str | None = None,
        route: str | None = None,
        source_message_type: str | None = None,
        flight_rule: str | None = None,
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
        if airport:
            conditions.append("(adep LIKE ? OR adest LIKE ?)")
            params.append(f"%{airport.upper()}%")
            params.append(f"%{airport.upper()}%")
        if route:
            conditions.append("route LIKE ?")
            params.append(f"%{route.upper()}%")
        if source_message_type:
            conditions.append("source_message_type = ?")
            params.append(source_message_type.upper())
        if flight_rule:
            conditions.append("flight_rule = ?")
            params.append(flight_rule.upper())
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
                last_message_time, raw_message_text,
                flight_rule, message_types, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                plan.flight_rule or "",
                plan.message_types or plan.source_message_type or "",
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
               raw_message_text=?, flight_rule=?, message_types=?, updated_at=?
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
                plan.flight_rule or "",
                plan.message_types or plan.source_message_type or "",
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

    @staticmethod
    def _merge_message_type(existing: str, new_type: str) -> str:
        """向逗号分隔的 message_types 中去重追加新类型"""
        if not new_type:
            return existing
        types = [t.strip() for t in existing.split(",") if t.strip()]
        if new_type not in types:
            types.append(new_type)
        return ",".join(types)


def _fmt_dt(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def _fmt_date(d: date | None) -> str | None:
    return d.isoformat() if d else None
