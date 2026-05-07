"""Flask Web 应用 — API + 前端页面"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from flask import Flask, jsonify, render_template, request

from .config import AppConfig
from .database import Database

logger = logging.getLogger("aftn_web.webapp")


def create_app(config: AppConfig, db: Database) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
    )

    @app.route("/")
    def index():
        return render_template("index.html")

    # ── API: 查询飞行计划 ──

    @app.route("/api/flight_plans")
    def api_flight_plans():
        callsign = _opt_str("callsign")
        adep = _opt_str("adep")
        adest = _opt_str("adest")
        dof = _opt_date("dof")
        ssr = _opt_str("ssr")
        aircraft_type = _opt_str("aircraft_type")
        source_message_type = _opt_str("source_message_type")
        keyword = _opt_str("keyword")
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
            keyword=keyword,
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
            keyword=keyword,
        )
        return jsonify({"total": total, "records": records})

    @app.route("/api/flight_plans/<int:fpl_id>")
    def api_flight_plan_detail(fpl_id: int):
        records = db.query_flight_plans(limit=1, offset=0)
        found = [r for r in records if r["id"] == fpl_id]
        if not found:
            # 用 callsign 精确查找
            results = db.query_flight_plans(limit=500)
            found = [r for r in results if r["id"] == fpl_id]
        if found:
            return jsonify(found[0])
        return jsonify({"error": "not found"}), 404

    # ── API: 查询原始 AFTN 报文 ──

    @app.route("/api/aftn_messages")
    def api_aftn_messages():
        msg_type = _opt_str("message_type")
        limit = request.args.get("limit", 100, type=int)
        offset = request.args.get("offset", 0, type=int)
        records = db.query_aftn_messages(
            message_type=msg_type, limit=min(limit, 500), offset=offset
        )
        return jsonify({"records": records})

    # ── API: 统计信息 ──

    @app.route("/api/stats")
    def api_stats():
        total_fpl = db.count_flight_plans()
        type_counts: dict[str, int] = {}
        for t in ("FPL", "DEP", "ARR", "DLA"):
            type_counts[t] = db.count_flight_plans(source_message_type=t)
        return jsonify({
            "total_flight_plans": total_fpl,
            "by_type": type_counts,
            "db_path": str(config.db_path),
        })

    def _opt_str(key: str) -> Optional[str]:
        v = request.args.get(key, "").strip()
        return v if v else None

    def _opt_date(key: str) -> Optional[date]:
        v = request.args.get(key, "").strip()
        if not v:
            return None
        try:
            return date.fromisoformat(v)
        except ValueError:
            return None

    return app
