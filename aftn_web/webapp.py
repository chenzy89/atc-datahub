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
        for rec in records:
            rec["sender_address"] = _extract_sender(rec.get("raw_text", ""))
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

    @app.route("/api/aftn_messages/<int:msg_id>")
    def api_aftn_message_get(msg_id: int):
        """获取单条 AFTN 报文"""
        record = db.get_aftn_message(msg_id)
        if record is None:
            return jsonify({"error": "not found"}), 404
        record["sender_address"] = _extract_sender(record.get("raw_text", ""))
        return jsonify(record)

    @app.route("/api/flight_plans")
    def api_flight_plans():
        """查询飞行计划列表（支持过滤+分页）"""
        callsign = _req_str("callsign")
        adep = _req_str("adep")
        adest = _req_str("adest")
        dof = _req_date("dof")
        airport = _req_str("airport")  # 关注机场：adep OR adest 匹配
        route = _req_str("route")  # 航路关键词
        source_message_type = _req_str("source_message_type")
        flight_rule = _req_str("flight_rule")
        limit = request.args.get("limit", 100, type=int)
        offset = request.args.get("offset", 0, type=int)

        records = db.query_flight_plans(
            callsign=callsign,
            adep=adep,
            adest=adest,
            dof=dof,
            airport=airport,
            route=route,
            source_message_type=source_message_type,
            flight_rule=flight_rule,
            limit=min(limit, 500),
            offset=offset,
        )
        total = db.count_flight_plans(
            callsign=callsign,
            adep=adep,
            adest=adest,
            dof=dof,
            airport=airport,
            route=route,
            source_message_type=source_message_type,
            flight_rule=flight_rule,
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

    @app.route("/api/flight_plans/export")
    def api_flight_plan_export():
        """导出飞行计划为 Excel (.xlsx)"""
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from io import BytesIO

        callsign = _req_str("callsign")
        adep = _req_str("adep")
        adest = _req_str("adest")
        dof = _req_date("dof")
        airport = _req_str("airport")
        route = _req_str("route")
        source_message_type = _req_str("source_message_type")
        flight_rule = _req_str("flight_rule")

        records = db.query_flight_plans(
            callsign=callsign,
            adep=adep,
            adest=adest,
            dof=dof,
            airport=airport,
            route=route,
            source_message_type=source_message_type,
            flight_rule=flight_rule,
            limit=10000,
            offset=0,
        )

        wb = Workbook()
        ws = wb.active
        ws.title = "飞行计划"

        headers = ["ID", "航班号", "规则与种类", "应答机", "机型", "DOF",
                   "起飞地", "ETD", "ATD", "目的地", "ETA", "ATA", "航路", "报文", "报文时间"]
        hdr_font = Font(bold=True, color="FFFFFF", size=11)
        hdr_fill = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
        thin_border = Border(
            left=Side(style="thin", color="D1D5DB"),
            right=Side(style="thin", color="D1D5DB"),
            top=Side(style="thin", color="D1D5DB"),
            bottom=Side(style="thin", color="D1D5DB"),
        )

        for col, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=h)
            cell.font = hdr_font
            cell.fill = hdr_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = thin_border

        for row_idx, rec in enumerate(records, 2):
            ws.cell(row=row_idx, column=1, value=rec.get("id", ""))
            ws.cell(row=row_idx, column=2, value=rec.get("callsign", ""))
            ws.cell(row=row_idx, column=3, value=rec.get("flight_rule", ""))
            ws.cell(row=row_idx, column=4, value=rec.get("ssr", ""))
            ws.cell(row=row_idx, column=5, value=rec.get("aircraft_type", ""))
            ws.cell(row=row_idx, column=6, value=_safe_date(rec.get("dof")))
            ws.cell(row=row_idx, column=7, value=rec.get("adep", ""))
            ws.cell(row=row_idx, column=8, value=_safe_dt(rec.get("etd")))
            ws.cell(row=row_idx, column=9, value=_safe_dt(rec.get("atd")))
            ws.cell(row=row_idx, column=10, value=rec.get("adest", ""))
            ws.cell(row=row_idx, column=11, value=_safe_dt(rec.get("eta")))
            ws.cell(row=row_idx, column=12, value=_safe_dt(rec.get("ata")))
            ws.cell(row=row_idx, column=13, value=rec.get("route", ""))
            ws.cell(row=row_idx, column=14, value=rec.get("message_types", ""))
            ws.cell(row=row_idx, column=15, value=_safe_dt(rec.get("last_message_time")))
            for col in range(1, 16):
                ws.cell(row=row_idx, column=col).border = thin_border
                ws.cell(row=row_idx, column=col).alignment = Alignment(vertical="center")

        col_widths = [6, 14, 10, 10, 12, 10, 16, 16, 10, 16, 16, 50, 10, 30, 16]
        for i, w in enumerate(col_widths, 1):
            ws.column_dimensions[chr(64 + i)].width = w

        out = BytesIO()
        wb.save(out)
        out.seek(0)
        return out.getvalue(), 200, {
            "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "Content-Disposition": f"attachment; filename=flight_plans_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.xlsx",
        }


    # ── 辅助 ──

    def _safe_date(v: Any) -> str:
        return str(v)[:10] if v else ""

    def _safe_dt(v: Any) -> str:
        return str(v)[:16] if v else ""

    def _extract_sender(raw_text: str) -> str:
        """从 AFTN 报文中提取发报地址"""
        if not raw_text:
            return ""
        parts = raw_text.split()
        for i, p in enumerate(parts):
            if p in ("FF", "GG") and i + 3 < len(parts):
                cand = parts[i + 3].upper()
                if len(cand) in (7, 8) and cand.isalpha():
                    return cand
        return ""

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

    # ── 禁用浏览器缓存 API 响应 ──────────────────────────
    @app.after_request
    def _no_cache(response):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    return app
