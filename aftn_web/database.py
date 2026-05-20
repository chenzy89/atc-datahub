"""SQLite 数据库操作"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .handover import get_resolver
from .models import AftnMessage, FlightPlan

# 匹配阈值：DEP/ARR 找 ETD/ETA 最近计划时的最大允许差值（秒）
MAX_ETD_DIFF_SECONDS = 12 * 3600  # 12 小时


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
                handover_pt TEXT NOT NULL DEFAULT '',
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
                ON flight_plans(callsign, adep, adest, dof, etd);
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
        # 迁移：新增 handover_pt 列（v26.5.15），兼容新旧数据库
        try:
            conn.execute("ALTER TABLE flight_plans ADD COLUMN handover_pt TEXT NOT NULL DEFAULT ''")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # 列已存在
        conn.commit()

    # ── AFTN 报文 ──────────────────────────────────────────────

    def save_aftn_message(self, msg: AftnMessage) -> int:
        conn = self._get_conn()
        conn.execute(
            """INSERT OR IGNORE INTO aftn_messages (raw_text, message_type, message_text, utc_time, received_at)
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
        if conn.total_changes == 0:
            return 0  # 重复报文，忽略
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

        if message_type == "__OTHER__":
            conditions.append("message_type NOT IN ('FPL','DEP','ARR','DLA','CNL','EST','HQ','METAR','CPL','AOC','ACP','TOC','CHG')")
        elif message_type:
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
        if message_type == "__OTHER__":
            conditions.append("message_type NOT IN ('FPL','DEP','ARR','DLA','CNL','EST','HQ','METAR','CPL','AOC','ACP','TOC','CHG')")
        elif message_type:
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

    def _row_to_dict(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        """sqlite3.Row → dict，兼容 Python 3.8（Row 没有 .get()）"""
        return dict(row) if row else None

    def upsert_flight_plan(self, plan: FlightPlan) -> int:
        """由报文自动调用：按 callsign+adep+adest+dof+etd 插入或更新已有记录
        同 DOF 不同 ETD → 视为不同计划（各自独立更新）
        DLA 是延误报，ETD 是延误后的新值，故按 callsign+adep+adest+dof 匹配（忽略 ETD）
        """
        now = _fmt_dt(datetime.utcnow())
        conn = self._get_conn()

        sel_cols = "id, dof, atd, ata, etd, eta, flight_rule, source_message_type, message_types"

        if plan.source_message_type == "DLA":
            # DLA：按 callsign+adep+adest+dof 匹配，忽略 etd（ETD 是 DLA 将更新的新值）
            # 当有多个同呼号起降地同 DOF 的计划时，选择已有 ETD 距 DLA 收报时间最近的那份
            candidates = conn.execute(
                f"SELECT {sel_cols} FROM flight_plans "
                "WHERE callsign=? AND adep=? AND adest=? AND dof=?",
                (plan.callsign, plan.adep, plan.adest, _fmt_date(plan.dof)),
            ).fetchall()
            if len(candidates) > 1:
                # 多计划中选 ETD 最接近 DLA 收报时间的那个
                ref_time = plan.last_message_time or plan.etd or datetime.utcnow()
                best = None
                best_diff = float("inf")
                for c in candidates:
                    etd_val = _parse_dt_stored(c["etd"])
                    if etd_val is None:
                        continue
                    diff = abs((ref_time - etd_val).total_seconds())
                    if diff < best_diff:
                        best_diff = diff
                        best = c
                existing = self._row_to_dict(best) if best else self._row_to_dict(candidates[0])
            else:
                existing = self._row_to_dict(candidates[0]) if candidates else None
        elif plan.etd:
            existing = self._row_to_dict(conn.execute(
                f"SELECT {sel_cols} FROM flight_plans "
                "WHERE callsign=? AND adep=? AND adest=? AND dof=? AND etd=?",
                (plan.callsign, plan.adep, plan.adest, _fmt_date(plan.dof), _fmt_dt(plan.etd)),
            ).fetchone())
        else:
            # DEP/ARR/CNL/CHG 等无 ETD 的报文：先按 callsign+adep+adest+dof 找已有记录
            # （包括由 FPL 创建的带 ETD 的记录），找不到再回退到 ETD 为空的记录
            existing = self._row_to_dict(conn.execute(
                f"SELECT {sel_cols} FROM flight_plans "
                "WHERE callsign=? AND adep=? AND adest=? AND dof=?",
                (plan.callsign, plan.adep, plan.adest, _fmt_date(plan.dof)),
            ).fetchone())
            if not existing:
                existing = self._row_to_dict(conn.execute(
                    f"SELECT {sel_cols} FROM flight_plans "
                    "WHERE callsign=? AND adep=? AND adest=? AND dof=? AND (etd IS NULL OR etd='')",
                    (plan.callsign, plan.adep, plan.adest, _fmt_date(plan.dof)),
                ).fetchone())

        if existing:
            # ── 维护 message_types（逗号分隔，去重） ──────────────
            msg_types_raw = existing["message_types"] or ""
            existing_types = [t.strip() for t in msg_types_raw.split(",") if t.strip()]
            if plan.source_message_type and plan.source_message_type not in existing_types:
                existing_types.append(plan.source_message_type)
            merged_types = ",".join(existing_types)

            updates: dict[str, Any] = {
                "last_message_time": _fmt_dt(plan.last_message_time),
                "raw_message_text": plan.raw_message_text or "",
                "updated_at": now,
                "message_types": merged_types,
            }
            # source_message_type 保留首次创建时的值（通常是 FPL），不因后续 DLA/CHG 覆盖
            if plan.source_message_type == "FPL" or not existing.get("source_message_type"):
                updates["source_message_type"] = plan.source_message_type
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
                updates["handover_pt"] = get_resolver().resolve(plan.route)
            if plan.source_message_type not in ("DEP", "ARR") or not existing["dof"]:
                if plan.dof:
                    updates["dof"] = _fmt_date(plan.dof)
            if plan.etd:
                updates["etd"] = _fmt_dt(plan.etd)
                # DLA 修改 ETD 后，自动重算 ETA = 新 ETD + 原飞行时长
                if plan.source_message_type == "DLA" and existing.get("etd") and existing.get("eta"):
                    try:
                        old_etd = datetime.fromisoformat(existing["etd"])
                        old_eta = datetime.fromisoformat(existing["eta"])
                        flight_duration = old_eta - old_etd
                        new_eta = plan.etd + flight_duration
                        updates["eta"] = _fmt_dt(new_eta)
                    except (ValueError, TypeError):
                        pass
            if plan.atd:
                updates["atd"] = _fmt_dt(plan.atd)
                # DEP 设置 ATD 后，自动重算 ETA = ATD + 原飞行时长
                if plan.source_message_type == "DEP" and existing.get("etd") and existing.get("eta"):
                    try:
                        old_etd = datetime.fromisoformat(existing["etd"])
                        old_eta = datetime.fromisoformat(existing["eta"])
                        flight_duration = old_eta - old_etd
                        new_eta = plan.atd + flight_duration
                        updates["eta"] = _fmt_dt(new_eta)
                    except (ValueError, TypeError):
                        pass
            if plan.eta and (plan.source_message_type != "DEP" or not updates.get("eta")):
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
            # ── 精确 DOF 未匹配 → ARR/DEP/DLA 尝试无 DOF 查找 ──────
            if plan.source_message_type in ("ARR", "DEP", "DLA"):
                alt_candidates = conn.execute(
                    "SELECT id, dof, atd, ata, etd, eta, source_message_type, message_types FROM flight_plans WHERE callsign=? AND adep=? AND adest=?",
                    (plan.callsign, plan.adep, plan.adest),
                ).fetchall()
                alt = None
                if len(alt_candidates) > 1:
                    # 多计划：DLA 选 ETD 最接近收报时间的，其他选第一个
                    if plan.source_message_type == "DLA":
                        ref_time = plan.last_message_time or plan.etd or datetime.utcnow()
                        best = None
                        best_diff = float("inf")
                        for c in alt_candidates:
                            etd_val = _parse_dt_stored(c["etd"])
                            if etd_val is None:
                                continue
                            diff = abs((ref_time - etd_val).total_seconds())
                            if diff < best_diff:
                                best_diff = diff
                                best = c
                        alt = self._row_to_dict(best) if best else None
                    else:
                        alt = self._row_to_dict(alt_candidates[0])
                elif alt_candidates:
                    alt = self._row_to_dict(alt_candidates[0])
                if alt:
                    msg_types_raw = alt["message_types"] or ""
                    existing_types = [t.strip() for t in msg_types_raw.split(",") if t.strip()]
                    if plan.source_message_type and plan.source_message_type not in existing_types:
                        existing_types.append(plan.source_message_type)
                    updates: dict[str, Any] = {
                        "last_message_time": _fmt_dt(plan.last_message_time),
                        "raw_message_text": plan.raw_message_text or "",
                        "updated_at": now,
                        "message_types": ",".join(existing_types),
                    }
                    # 保留首次创建的 source_message_type，不被 DLA 等覆盖
                    if plan.source_message_type == "FPL" or not alt.get("source_message_type"):
                        updates["source_message_type"] = plan.source_message_type
                    if plan.source_message_type == "DLA":
                        # DLA fallback 也更新 ETD/SSR
                        if plan.etd:
                            updates["etd"] = _fmt_dt(plan.etd)
                            # 重算 ETA
                            if alt.get("etd") and alt.get("eta"):
                                try:
                                    old_etd = datetime.fromisoformat(alt["etd"])
                                    old_eta = datetime.fromisoformat(alt["eta"])
                                    flight_duration = old_eta - old_etd
                                    new_eta = plan.etd + flight_duration
                                    updates["eta"] = _fmt_dt(new_eta)
                                except (ValueError, TypeError):
                                    pass
                        if plan.ssr:
                            updates["ssr"] = plan.ssr
                    if plan.atd:
                        updates["atd"] = _fmt_dt(plan.atd)
                        # DEP 设置 ATD 后，自动重算 ETA = ATD + 原飞行时长
                        if plan.source_message_type == "DEP" and alt.get("etd") and alt.get("eta"):
                            try:
                                old_etd = datetime.fromisoformat(alt["etd"])
                                old_eta = datetime.fromisoformat(alt["eta"])
                                flight_duration = old_eta - old_etd
                                new_eta = plan.atd + flight_duration
                                updates["eta"] = _fmt_dt(new_eta)
                            except (ValueError, TypeError):
                                pass
                    if plan.eta and plan.source_message_type not in ("DLA", "DEP"):
                        updates["eta"] = _fmt_dt(plan.eta)
                    if plan.ata:
                        updates["ata"] = _fmt_dt(plan.ata)
                    set_clause = ", ".join(f"{k}=?" for k in updates)
                    conn.execute(
                        f"UPDATE flight_plans SET {set_clause} WHERE id=?",
                        list(updates.values()) + [alt["id"]],
                    )
                    conn.commit()
                    return alt["id"]

            # ── 新建记录，初始化 message_types ──────────────
            init_types = plan.source_message_type if plan.source_message_type else ""
            handover_pt = plan.handover_pt or (get_resolver().resolve(plan.route) if plan.route else "")
            conn.execute(
                """INSERT INTO flight_plans
                   (callsign, ssr, aircraft_type, dof, adep, etd, atd,
                    adest, eta, ata, route, handover_pt,
                    source_message_type, last_message_time, raw_message_text,
                    flight_rule, message_types, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan.callsign, plan.ssr, plan.aircraft_type,
                    _fmt_date(plan.dof), plan.adep,
                    _fmt_dt(plan.etd), _fmt_dt(plan.atd),
                    plan.adest,
                    _fmt_dt(plan.eta), _fmt_dt(plan.ata),
                    plan.route, handover_pt,
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

    def find_flight_plans_by_key(self, callsign: str, adep: str, adest: str, dof: date | None = None,
                                  exclude_cancelled: bool = False) -> list[dict[str, Any]]:
        """返回所有匹配 (callsign, adep, adest, 可选 dof) 的飞行计划
        exclude_cancelled=True 时排除 message_types 含 CNL 的已取消计划"""
        conn = self._get_conn()
        cancelled_clause = " AND message_types NOT LIKE '%CNL%'" if exclude_cancelled else ""
        if dof:
            rows = conn.execute(
                f"SELECT * FROM flight_plans WHERE callsign=? AND adep=? AND adest=? AND dof=?{cancelled_clause}",
                (callsign, adep, adest, _fmt_date(dof)),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM flight_plans WHERE callsign=? AND adep=? AND adest=?{cancelled_clause}",
                (callsign, adep, adest),
            ).fetchall()
        return [dict(r) for r in rows]

    def find_flight_plan(self, callsign: str, adep: str, adest: str, dof: date | None = None,
                          exclude_cancelled: bool = False) -> dict[str, Any] | None:
        """按 callsign+adep+adest+可选 DOF 查找飞行计划（返回第一条）
        exclude_cancelled=True 时排除已取消计划"""
        conn = self._get_conn()
        cancelled_clause = " AND message_types NOT LIKE '%CNL%'" if exclude_cancelled else ""
        if dof:
            row = conn.execute(
                f"SELECT * FROM flight_plans WHERE callsign=? AND adep=? AND adest=? AND dof=?{cancelled_clause}",
                (callsign, adep, adest, _fmt_date(dof)),
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT * FROM flight_plans WHERE callsign=? AND adep=? AND adest=?{cancelled_clause}",
                (callsign, adep, adest),
            ).fetchone()
        return dict(row) if row else None

    def find_closest_plan_by_etd(self, callsign: str, adep: str, adest: str, dof: date, target_time: datetime,
                                 max_diff_seconds: int = MAX_ETD_DIFF_SECONDS,
                                 exclude_cancelled: bool = False) -> dict[str, Any] | None:
        """找 ETD 离 target_time 最近的计划（DEP 用），差值超过阈值则返回 None"""
        if not target_time:
            return None
        plans = self.find_flight_plans_by_key(callsign, adep, adest, dof, exclude_cancelled=exclude_cancelled)
        best, _ = _pick_closest_datetime(plans, "etd", target_time, max_diff_seconds)
        return best

    def find_closest_plan_by_eta(self, callsign: str, adep: str, adest: str, dof: date, target_time: datetime,
                                 max_diff_seconds: int = MAX_ETD_DIFF_SECONDS,
                                 exclude_cancelled: bool = False) -> dict[str, Any] | None:
        """找 ETA（从 eta 字段）离 target_time 最近的计划（ARR 用），差值超过阈值则返回 None"""
        if not target_time:
            return None
        plans = self.find_flight_plans_by_key(callsign, adep, adest, dof, exclude_cancelled=exclude_cancelled)
        best, _ = _pick_closest_datetime(plans, "eta", target_time, max_diff_seconds)
        return best

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

    def update_chg_route(self, callsign: str, adep: str, adest: str,
                          dof: date, route: str) -> bool:
        """按 callsign+adep+adest+dof 查找飞行计划，更新航路（CHG 编组15）"""
        conn = self._get_conn()
        existing = conn.execute(
            "SELECT id, message_types FROM flight_plans "
            "WHERE callsign=? AND adep=? AND adest=? AND dof=?",
            (callsign.upper(), adep.upper()[:4], adest.upper()[:4], _fmt_date(dof)),
        ).fetchone()
        if not existing:
            return False

        msg_types = self._merge_message_type(existing["message_types"] or "", "CHG")
        now = _fmt_dt(datetime.utcnow())
        new_handover = get_resolver().resolve(route) if route else ""
        conn.execute(
            "UPDATE flight_plans SET route=?, handover_pt=?, message_types=?, updated_at=? WHERE id=?",
            (route, new_handover, msg_types, now, existing["id"]),
        )
        conn.commit()
        return True

    def update_chg_route_by_id(self, fpl_id: int, route: str) -> bool:
        """按 ID 更新航路（CHG fallback 用）"""
        conn = self._get_conn()
        existing = conn.execute(
            "SELECT message_types FROM flight_plans WHERE id=?", (fpl_id,)
        ).fetchone()
        if not existing:
            return False

        msg_types = self._merge_message_type(existing["message_types"] or "", "CHG")
        now = _fmt_dt(datetime.utcnow())
        new_handover = get_resolver().resolve(route) if route else ""
        cur = conn.execute(
            "UPDATE flight_plans SET route=?, handover_pt=?, message_types=?, updated_at=? WHERE id=?",
            (route, new_handover, msg_types, now, fpl_id),
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

    def mark_cancelled(self, callsign: str, adep: str, adest: str, dof: date | None) -> bool:
        """CNL：在已有计划的 message_types 中添加 CNL 标签"""
        conn = self._get_conn()
        cs = callsign.strip().upper()
        ad = adep.strip().upper()[:4]
        ae = adest.strip().upper()[:4]

        if dof:
            row = conn.execute(
                "SELECT id, message_types FROM flight_plans WHERE callsign=? AND adep=? AND adest=? AND dof=?",
                (cs, ad, ae, _fmt_date(dof)),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, message_types FROM flight_plans WHERE callsign=? AND adep=? AND adest=?",
                (cs, ad, ae),
            ).fetchone()
        if not row:
            return False

        new_types = self._merge_message_type(row["message_types"], "CNL")
        now = _fmt_dt(datetime.utcnow())
        conn.execute(
            "UPDATE flight_plans SET message_types=?, updated_at=? WHERE id=?",
            (new_types, now, row["id"]),
        )
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
        handover_pt: str | None = None,  # 移交点关键词
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conn = self._get_conn()
        params, conditions = _build_fpl_conditions(
            callsign=callsign, adep=adep, adest=adest, dof=dof,
            airport=airport, route=route,
            source_message_type=source_message_type,
            flight_rule=flight_rule, handover_pt=handover_pt,
        )
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
        handover_pt: str | None = None,
    ) -> int:
        conn = self._get_conn()
        params, conditions = _build_fpl_conditions(
            callsign=callsign, adep=adep, adest=adest, dof=dof,
            airport=airport, route=route,
            source_message_type=source_message_type,
            flight_rule=flight_rule, handover_pt=handover_pt,
        )
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        row = conn.execute(f"SELECT COUNT(*) FROM flight_plans {where}", params).fetchone()
        return row[0]

    def query_traffic_statistics(
        self,
        airports: list[str],
        date_from: str,
        date_to: str,
    ) -> dict[str, Any]:
        """
        查询流量统计：
        - dep_hourly / arr_hourly: 各小时出港/进港总数（24元数组）
        - dep_count / arr_count: 出港/进港总数
        - dep_handover / arr_handover: dict[移交点] = 总数
        - peak_hour, peak_hour_dep, peak_hour_arr, peak_day, peak_day_count
        - days: 统计天数
        """
        result: dict[str, Any] = {
            "dep_count": 0, "arr_count": 0,
            "dep_hourly": [0]*24, "arr_hourly": [0]*24,
            "dep_handover": {}, "arr_handover": {},
            "peak_hour": 0, "peak_hour_dep": 0, "peak_hour_arr": 0,
            "peak_day": "", "peak_day_count": 0,
            "days": 1,
        }
        if not airports:
            return result

        conn = self._get_conn()
        placeholders = ",".join("?" for _ in airports)
        date_to_end = date_to + " 23:59:59"

        # ── 出港统计 ──
        dep_rows = conn.execute(
            f"""SELECT atd, handover_pt FROM flight_plans
               WHERE adep IN ({placeholders})
                 AND atd IS NOT NULL AND atd >= ? AND atd <= ?""",
            [*airports, date_from, date_to_end],
        ).fetchall()

        for row in dep_rows:
            h = _hour_from_dt_str(row["atd"])
            if h is not None:
                result["dep_hourly"][h] += 1
            result["dep_count"] += 1
            hp = row["handover_pt"] or ""
            if hp:
                result["dep_handover"][hp] = result["dep_handover"].get(hp, 0) + 1

        # ── 进港统计 ──
        arr_rows = conn.execute(
            f"""SELECT ata, handover_pt FROM flight_plans
               WHERE adest IN ({placeholders})
                 AND ata IS NOT NULL AND ata >= ? AND ata <= ?""",
            [*airports, date_from, date_to_end],
        ).fetchall()

        for row in arr_rows:
            h = _hour_from_dt_str(row["ata"])
            if h is not None:
                result["arr_hourly"][h] += 1
            result["arr_count"] += 1
            hp = row["handover_pt"] or ""
            if hp:
                result["arr_handover"][hp] = result["arr_handover"].get(hp, 0) + 1

        # ── 天数 ──
        try:
            d1 = datetime.strptime(date_from, "%Y-%m-%d")
            d2 = datetime.strptime(date_to, "%Y-%m-%d")
            result["days"] = max((d2 - d1).days + 1, 1)
        except ValueError:
            result["days"] = 1

        # ── 高峰小时 ──
        peak_total = 0
        peak_dep = 0
        peak_arr = 0
        peak_h = 0
        for h in range(24):
            total_h = result["dep_hourly"][h] + result["arr_hourly"][h]
            if total_h > peak_total:
                peak_total = total_h
                peak_dep = result["dep_hourly"][h]
                peak_arr = result["arr_hourly"][h]
                peak_h = h
        result["peak_hour"] = peak_h
        result["peak_hour_dep"] = peak_dep
        result["peak_hour_arr"] = peak_arr

        # ── 高峰日 ──
        # 按日聚合 atd/ata
        day_counts: dict[str, int] = {}
        for row in dep_rows:
            d = _day_from_dt_str(row["atd"])
            if d:
                day_counts[d] = day_counts.get(d, 0) + 1
        for row in arr_rows:
            d = _day_from_dt_str(row["ata"])
            if d:
                day_counts[d] = day_counts.get(d, 0) + 1
        if day_counts:
            peak_day = max(day_counts, key=day_counts.get)
            result["peak_day"] = peak_day
            result["peak_day_count"] = day_counts[peak_day]

        return result

    def create_flight_plan(self, plan: FlightPlan) -> int:
        """手动新增飞行计划"""
        now = _fmt_dt(datetime.utcnow())
        conn = self._get_conn()
        handover_pt = plan.handover_pt or (get_resolver().resolve(plan.route) if plan.route else "")
        conn.execute(
            """INSERT INTO flight_plans
               (callsign, ssr, aircraft_type, dof, adep, etd, atd,
                adest, eta, ata, route, handover_pt, source_message_type,
                last_message_time, raw_message_text,
                flight_rule, message_types, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                plan.callsign, plan.ssr, plan.aircraft_type,
                _fmt_date(plan.dof), plan.adep,
                _fmt_dt(plan.etd), _fmt_dt(plan.atd),
                plan.adest,
                _fmt_dt(plan.eta), _fmt_dt(plan.ata),
                plan.route, handover_pt,
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
        handover_pt = plan.handover_pt or (get_resolver().resolve(plan.route) if plan.route else "")
        conn.execute(
            """UPDATE flight_plans SET
               callsign=?, ssr=?, aircraft_type=?, dof=?, adep=?,
               etd=?, atd=?, adest=?, eta=?, ata=?, route=?, handover_pt=?,
               source_message_type=?, last_message_time=?,
               raw_message_text=?, flight_rule=?, message_types=?, updated_at=?
               WHERE id=?""",
            (
                plan.callsign, plan.ssr, plan.aircraft_type,
                _fmt_date(plan.dof), plan.adep,
                _fmt_dt(plan.etd), _fmt_dt(plan.atd),
                plan.adest,
                _fmt_dt(plan.eta), _fmt_dt(plan.ata),
                plan.route, handover_pt,
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


def _build_fpl_conditions(
    callsign: str | None = None,
    adep: str | None = None,
    adest: str | None = None,
    dof: date | None = None,
    airport: str | None = None,
    route: str | None = None,
    source_message_type: str | None = None,
    flight_rule: str | None = None,
    handover_pt: str | None = None,
) -> tuple[list[Any], list[str]]:
    """构建飞行计划查询条件，返回 (params, conditions)"""
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
        p = f"%{airport.upper()}%"
        params.extend([p, p])
    if route:
        conditions.append("route LIKE ?")
        params.append(f"%{route.upper()}%")
    if source_message_type:
        conditions.append("source_message_type = ?")
        params.append(source_message_type.upper())
    if flight_rule == "__OTHER__":
        conditions.append("flight_rule NOT IN ('IS','IN','IG','IM','IX','IB','VS','VN','VG','VX')")
    elif flight_rule:
        conditions.append("flight_rule = ?")
        params.append(flight_rule.upper())
    if handover_pt == "__OTHER__":
        conditions.append("(handover_pt = '' OR handover_pt IS NULL)")
    elif handover_pt:
        conditions.append("handover_pt LIKE ?")
        params.append(f"%{handover_pt.upper()}%")
    return params, conditions


def _fmt_dt(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def _fmt_date(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _parse_dt_stored(value: str | None) -> datetime | None:
    """解析库中存储的日期时间字符串（YYYY-MM-DD HH:MM:SS 或 ISO 格式）"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        pass
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _pick_closest_datetime(
    plans: list[dict],
    field: str,
    target: datetime,
    max_diff_seconds: int,
) -> tuple[dict | None, float]:
    """从计划列表中选出 field 时间字段离 target 最近的记录，不超阈值"""
    if not target or not plans:
        return None, float("inf")
    best = None
    best_diff = float("inf")
    for p in plans:
        val = _parse_dt_stored(p.get(field))
        if val is None:
            continue
        diff = abs((target - val).total_seconds())
        if diff < best_diff:
            best_diff = diff
            best = p
    if best and best_diff <= max_diff_seconds:
        return best, best_diff
    return None, float("inf")


def _hour_from_dt_str(dt_str: str | None) -> int | None:
    """从 'YYYY-MM-DD HH:MM:SS' 提取小时"""
    if not dt_str or len(dt_str) < 14:
        return None
    try:
        return int(dt_str[11:13])
    except (ValueError, IndexError):
        return None


def _day_from_dt_str(dt_str: str | None) -> str | None:
    """从 'YYYY-MM-DD HH:MM:SS' 提取日期"""
    if not dt_str or len(dt_str) < 10:
        return None
    return dt_str[:10]
