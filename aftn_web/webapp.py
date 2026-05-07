"""Flask Web 应用 — API + 前端页面"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from flask import Flask, jsonify, render_template, request

from .config import AppConfig
from .database import Database
from .models import FlightPlan

logger = logging.getLogger("aftn_web.webapp")


def create_app(config: AppConfig, db: Database) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
    )

    # ── 页面 ──────────────────────────────────────────────────

    @app.route("/")
    def index():
        return render_template("index.html")

    # ── 统计 ──────────────────────────────────────────────────

    @app.route("/api/stats")
    def api_stats():
        total_fpl = db.count_flight_plans()
        fpl_by_type: dict[str, int] = {}
        for t in ("FPL", "DEP", "ARR", "DLA"):
            fpl_by_type[t] = db.count_flight_plans(source_message_type=t)
        total_aftn = db.count_aftn_messages()
        aftn_by_type = db.count_aftn_by_type()
        return jsonify({
            "total_flight_plans": total_fpl,
            "flight_plans_by_type": fpl_by_type,
            "total_aftn_messages": total_aftn,
            "aftn_messages_by_type": aftn_by_type,
            "db_path": str(config.db_path),
        })

    # ══════════════════════════════════════════════════════════
    # 模块一：AFTN 报文
    # ══════════════════════════════════════════════════════════

    @app.route("/api/aftn_messages")
    def api_aftn_messages():
        msg_type = _req_str("message_type")
        keyword = _req_str("keyword")
        date_from = _req_str("date_from")
        date_to = _req_str("date_to")
        limit = request.args.get("limit", 100, type=int)
        offset = request.args.get("offset", 0, type=int)

        records = db.query_aftn_messages(
            message_type=msg_type,
            keyword=keyword,
            date_from=date_from,
            date_to=date_to,
            limit=min(limit, 500),
            offset=offset,
        )
        total = db.count_aftn_messages(
            message_type=msg_type,
            keyword=keyword,
            date_from=date_from,
            date_to=date_to,
        )
        return jsonify({"total": total, "records": records})

    # ══════════════════════════════════════════════════════════
    # 模块二：飞行计划
    # ══════════════════════════════════════════════════════════

    @app.route("/api/flight_plans")
    def api_flight_plans():
        """查询飞行计划列表（支持过滤+分页）"""
        callsign = _req_str("callsign")
        adep = _req_str("adep")
        adest = _req_str("adest")
        dof = _req_date("dof")
        ssr = _req_str("ssr")
        aircraft_type = _req_str("aircraft_type")
        source_message_type = _req_str("source_message_type")
        limit = request.args.get("limit", 100, type=int)
        offset = request.args.get("offset", 0, type=int)

        records = db.query_flight_plans(
            callsign=callsign,
            adep=adep,
            adest=adest,
            dof=dof,
            ssr=ssr,
            aircraft_type=aircraft_type,
            source_message_type=source_message_type,
            limit=min(limit, 500),
            offset=offset,
        )
        total = db.count_flight_plans(
            callsign=callsign,
            adep=adep,
            adest=adest,
            dof=dof,
            ssr=ssr,
            aircraft_type=aircraft_type,
            source_message_type=source_message_type,
        )
        return jsonify({"total": total, "records": records})

    @app.route("/api/flight_plans/<int:fpl_id>")
    def api_flight_plan_get(fpl_id: int):
        """获取单条飞行计划"""
        record = db.get_flight_plan(fpl_id)
        if record is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(record)

    @app.route("/api/flight_plans", methods=["POST"])
    def api_flight_plan_create():
        """手工新增飞行计划"""
        data = request.get_json(silent=True) or {}
        plan = _parse_plan_from_json(data)
        plan.id = 0
        try:
            new_id = db.create_flight_plan(plan)
            return jsonify({"id": new_id, "ok": True}), 201
        except Exception as exc:
            logger.exception("create flight plan failed")
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/flight_plans/<int:fpl_id>", methods=["PUT"])
    def api_flight_plan_update(fpl_id: int):
        """手工更新飞行计划"""
        data = request.get_json(silent=True) or {}
        plan = _parse_plan_from_json(data)
        plan.id = fpl_id
        ok = db.update_flight_plan(fpl_id, plan)
        if not ok:
            return jsonify({"error": "not found"}), 404
        return jsonify({"ok": True})

    @app.route("/api/flight_plans/<int:fpl_id>", methods=["DELETE"])
    def api_flight_plan_delete(fpl_id: int):
        """删除飞行计划"""
        ok = db.delete_flight_plan(fpl_id)
        if not ok:
            return jsonify({"error": "not found"}), 404
        return jsonify({"ok": True})

    # ── 辅助 ──────────────────────────────────────────────────

    def _req_str(key: str) -> Optional[str]:
        v = request.args.get(key, "").strip()
        return v if v else None

    def _req_date(key: str) -> Optional[date]:
        v = request.args.get(key, "").strip()
        if not v:
            return None
        try:
            return date.fromisoformat(v)
        except ValueError:
            return None

    def _parse_plan_from_json(data: dict[str, Any]) -> FlightPlan:
        def _dt(v: Any) -> datetime | None:
            if not v:
                return None
            if isinstance(v, datetime):
                return v
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(str(v)[:19], fmt)
                except ValueError:
                    continue
            return None

        def _d(v: Any) -> date | None:
            if not v:
                return None
            if isinstance(v, date):
                return v
            for fmt in ("%Y-%m-%d",):
                try:
                    return datetime.strptime(str(v)[:10], fmt).date()
                except ValueError:
                    continue
            return None

        return FlightPlan(
            id=int(data.get("id") or 0),
            callsign=str(data.get("callsign") or "").strip().upper(),
            ssr=str(data.get("ssr") or "").strip().upper(),
            aircraft_type=str(data.get("aircraft_type") or "").strip().upper(),
            dof=_d(data.get("dof")),
            adep=str(data.get("adep") or "").strip().upper(),
            etd=_dt(data.get("etd")),
            atd=_dt(data.get("atd")),
            adest=str(data.get("adest") or "").strip().upper(),
            eta=_dt(data.get("eta")),
            ata=_dt(data.get("ata")),
            route=str(data.get("route") or "").strip().upper(),
            source_message_type=str(data.get("source_message_type") or "MANUAL"),
            last_message_time=_dt(data.get("last_message_time")),
            raw_message_text=str(data.get("raw_message_text") or ""),
        )

    return app
