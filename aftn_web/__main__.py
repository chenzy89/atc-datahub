"""启动入口"""

from __future__ import annotations

import logging
import signal
import sys
from argparse import ArgumentParser
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Thread

from .config import load_config
from .database import Database, _fmt_dt, _pick_closest_datetime
import json

from .parser import AftnParser, split_multi_aftn
from .receiver import UdpReceiver
from .webapp import create_app

logger = logging.getLogger("aftn_web")


def setup_logging(log_dir: str | Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_dir:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(
            log_path / f"aftn-web-{datetime.now():%Y%m%d}.log",
            encoding="utf-8",
        )
        handlers.append(fh)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description="ATC AFTN WebHub — AFTN 报文接收与查询系统")
    parser.add_argument(
        "-c", "--config",
        default="config.json",
        help="配置文件路径 (默认: config.json)",
    )
    parser.add_argument(
        "--log-dir",
        default=None,
        help="日志目录 (默认: 不写文件日志)",
    )
    args = parser.parse_args(argv)

    setup_logging(args.log_dir)
    config = load_config(args.config)
    logger.info("starting %s", config.system_name)
    logger.info("config: %s", config.config_file)

    # 数据库
    db = Database(config.db_path)
    logger.info("database: %s", config.db_path)

    # 迁移：新字段兼容旧库（若不存在则 ALTER）
    try:
        conn = db._get_conn()
        for col, typ in [("flight_rule", "TEXT NOT NULL DEFAULT ''"), ("message_types", "TEXT NOT NULL DEFAULT ''")]:
            try:
                conn.execute(f"ALTER TABLE flight_plans ADD COLUMN {col} {typ}")
                conn.commit()
                logger.info("migrated: added column %s", col)
            except Exception:
                pass  # 列已存在
    except Exception:
        pass

    # AFTN 解析器
    parser_aftn = AftnParser()

    # UDP 接收器
    total_received = [0]
    total_parsed = [0]

    def on_aftn_message(payload: bytes, addr: str, port: int, received_at: datetime) -> None:
        nonlocal total_received, total_parsed
        total_received[0] += 1

        # ── 提取原始文本 ─────────────────────────────────────
        raw_text = ""
        if isinstance(payload, bytes):
            raw_text = payload.decode("utf-8", errors="replace")
        elif isinstance(payload, str):
            raw_text = payload
        elif isinstance(payload, dict):
            raw_text = str(payload.get("MessageText", payload.get("message_text", payload.get("raw_text", ""))))

        # ── 检测多报文粘连 ──────────────────────────────────
        # 优先判断是否为 JSON 包装格式，若是则解包后对 MessageText 做拆分
        _unwrapped_text = raw_text
        if raw_text.strip().startswith("{") and "MessageText" in raw_text:
            try:
                _parsed = json.loads(raw_text)
                _unwrapped_text = _parsed.get("MessageText", raw_text)
                logger.debug("JSON 报文解包: len=%d", len(_unwrapped_text))
            except json.JSONDecodeError:
                pass  # 非标准 JSON，按原始文本处理

        sub_messages = split_multi_aftn(_unwrapped_text)
        if len(sub_messages) > 1:
            logger.info("多报文粘连: %d 份子报文 <- %s:%d", len(sub_messages), addr, port)
            _iter_payloads: list = sub_messages
        else:
            _iter_payloads = [payload]

        for _sub_payload in _iter_payloads:
            try:
                result = parser_aftn.parse(_sub_payload, received_at=received_at)
            except Exception:
                logger.exception("parse error from %s:%d", addr, port)
                continue

            # LAM 报文：不记录、不解析
            if result.message.message_type == "LAM":
                logger.debug("AFTN LAM ignored: not recorded")
                continue

            # 保存原始报文
            db.save_aftn_message(result.message)

            # EST：只记录报文，不解析、不生成 FlightPlan、不更新飞行计划
            if result.message.message_type == "EST":
                total_parsed[0] += 1
                logger.info(
                    "[EST] recorded only (total: recv=%d, parsed=%d)",
                    total_received[0], total_parsed[0],
                )
                continue

            if not result.accepted:
                if result.errors:
                    logger.debug("AFTN ignored: %s", "; ".join(result.errors))
                continue

            plan = result.flight_plan
            action = result.action

            if action == "CNL":
                # CNL：标记取消，不删除
                marked = db.mark_cancelled(
                    plan.callsign, plan.adep, plan.adest, plan.dof,
                )
                total_parsed[0] += 1
                if marked:
                    logger.info(
                        "[CNL] %s %s->%s DOF=%s 已标记取消 (total: recv=%d, parsed=%d)",
                        plan.callsign, plan.adep, plan.adest, plan.dof or "?",
                        total_received[0], total_parsed[0],
                    )
                else:
                    logger.info(
                        "[CNL] %s %s->%s 无关联计划 (total: recv=%d, parsed=%d)",
                        plan.callsign, plan.adep, plan.adest,
                        total_received[0], total_parsed[0],
                    )
            elif action == "FPL":
                # FPL：同 DOF 已存在则 upsert（更新 flight_rule、message_types），否则新建
                existing = db.find_flight_plan(plan.callsign, plan.adep, plan.adest, plan.dof)
                if existing:
                    db.upsert_flight_plan(plan)
                    total_parsed[0] += 1
                    logger.info(
                        "[FPL] %s %s->%s DOF=%s 已存在，upsert (total: recv=%d, parsed=%d)",
                        plan.callsign, plan.adep, plan.adest, plan.dof,
                        total_received[0], total_parsed[0],
                    )
                else:
                    db.create_flight_plan(plan)
                    total_parsed[0] += 1
                    logger.info(
                        "[FPL] %s %s->%s DOF=%s 新建 (total: recv=%d, parsed=%d)",
                        plan.callsign, plan.adep, plan.adest, plan.dof,
                        total_received[0], total_parsed[0],
                    )
            elif action == "DEP":
                # DEP：在同 DOF 中找 ETD 最接近 ATD 的计划
                # 若差值 > 12h 则新建；若 DOF 匹配不到，降级搜索全部 DOF 防跨日
                _matched_dep = db.find_closest_plan_by_etd(
                    plan.callsign, plan.adep, plan.adest, plan.dof, plan.atd,
                )
                if not _matched_dep:
                    # DOF 匹配不到 → 尝试无视 DOF 找 ETD 最接近的计划（处理跨日 ARR 场景）
                    all_plans = db.find_flight_plans_by_key(plan.callsign, plan.adep, plan.adest)
                    _matched_dep, _ = _pick_closest_datetime(all_plans, "etd", plan.atd, 12 * 3600)
                    # ETD 仍匹配不到 → 尝试按 ATD 匹配防重复（DEP 先到后 FPL 迟到场景）
                    if not _matched_dep:
                        _matched_dep, _ = _pick_closest_datetime(all_plans, "atd", plan.atd, 12 * 3600)
                    # 所有时间字段匹配均失败但同 key 有计划 → 直接匹配到它（防跨报文类型重复）
                    if not _matched_dep and all_plans:
                        _matched_dep = all_plans[0]
                if _matched_dep:
                    db.update_flight_plan_atd(_matched_dep["id"], plan.atd, ssr=plan.ssr, source_message_type="DEP")
                    total_parsed[0] += 1
                    logger.info(
                        "[DEP] %s %s->%s 匹配计划 (id=%d, ETD=%s)，更新 ATD=%s (total: recv=%d, parsed=%d)",
                        plan.callsign, plan.adep, plan.adest,
                        _matched_dep["id"], _matched_dep.get("etd", ""), _fmt_dt(plan.atd),
                        total_received[0], total_parsed[0],
                    )
                else:
                    db.create_flight_plan(plan)
                    total_parsed[0] += 1
                    logger.info(
                        "[DEP] %s %s->%s DOF=%s 无匹配计划，新建 ATD=%s (total: recv=%d, parsed=%d)",
                        plan.callsign, plan.adep, plan.adest, plan.dof, plan.atd,
                        total_received[0], total_parsed[0],
                    )
            elif action == "ARR":
                # ARR：在同 DOF 中找 ETA 最接近 ATA 的计划
                # 若差值 > 12h 则新建；若 DOF 匹配不到，降级搜索全部 DOF 防跨日
                _matched_arr = db.find_closest_plan_by_eta(
                    plan.callsign, plan.adep, plan.adest, plan.dof, plan.ata,
                )
                if not _matched_arr:
                    # DOF 匹配不到 → 尝试无视 DOF 找 ETA 最接近的计划（处理跨日落地场景）
                    all_plans = db.find_flight_plans_by_key(plan.callsign, plan.adep, plan.adest)
                    _matched_arr, _ = _pick_closest_datetime(all_plans, "eta", plan.ata, 12 * 3600)
                    # ETA 仍匹配不到 → 尝试按 ATA 匹配防重复（ARR 先到后 FPL 迟到场景）
                    if not _matched_arr:
                        _matched_arr, _ = _pick_closest_datetime(all_plans, "ata", plan.ata, 12 * 3600)
                    # 所有时间字段匹配均失败但同 key 有计划 → 直接匹配到它（防跨报文类型重复）
                    if not _matched_arr and all_plans:
                        _matched_arr = all_plans[0]
                if _matched_arr:
                    db.update_flight_plan_ata(_matched_arr["id"], plan.ata, source_message_type="ARR")
                    total_parsed[0] += 1
                    logger.info(
                        "[ARR] %s %s->%s 匹配计划 (id=%d, ETA=%s)，更新 ATA=%s (total: recv=%d, parsed=%d)",
                        plan.callsign, plan.adep, plan.adest,
                        _matched_arr["id"], _matched_arr.get("eta", ""), _fmt_dt(plan.ata),
                        total_received[0], total_parsed[0],
                    )
                else:
                    db.create_flight_plan(plan)
                    total_parsed[0] += 1
                    logger.info(
                        "[ARR] %s %s->%s DOF=%s 无匹配计划，新建 ATA=%s (total: recv=%d, parsed=%d)",
                        plan.callsign, plan.adep, plan.adest, plan.dof, plan.ata,
                        total_received[0], total_parsed[0],
                    )
            elif action == "CHG":
                # CHG（更正报）：只处理编组 15（航路），忽略不含编组 15 的 CHG
                if not plan.route:
                    total_parsed[0] += 1
                    logger.info(
                        "[CHG] %s %s->%s DOF=%s 无编组15，忽略 (total: recv=%d, parsed=%d)",
                        plan.callsign, plan.adep, plan.adest, plan.dof,
                        total_received[0], total_parsed[0],
                    )
                else:
                    updated = db.update_chg_route(
                        plan.callsign, plan.adep, plan.adest, plan.dof, plan.route,
                    )
                    total_parsed[0] += 1
                    if updated:
                        logger.info(
                            "[CHG] %s %s->%s DOF=%s 更新航路 (total: recv=%d, parsed=%d)",
                            plan.callsign, plan.adep, plan.adest, plan.dof,
                            total_received[0], total_parsed[0],
                        )
                    else:
                        # DOF 精确匹配不到，降级到无视 DOF 查找
                        fallback = db.find_flight_plans_by_key(plan.callsign, plan.adep, plan.adest)
                        if fallback:
                            db.update_chg_route_by_id(fallback[0]["id"], plan.route)
                            logger.info(
                                "[CHG] %s %s->%s DOF=%s 未精确匹配，fallback 更新航路 (id=%d, total: recv=%d, parsed=%d)",
                                plan.callsign, plan.adep, plan.adest, plan.dof,
                                fallback[0]["id"], total_received[0], total_parsed[0],
                            )
                        else:
                            logger.info(
                                "[CHG] %s %s->%s DOF=%s 无匹配计划，忽略 (total: recv=%d, parsed=%d)",
                                plan.callsign, plan.adep, plan.adest, plan.dof,
                                total_received[0], total_parsed[0],
                            )
            else:
                # DLA：upsert
                db.upsert_flight_plan(plan)
                total_parsed[0] += 1
                if total_parsed[0] <= 5 or total_parsed[0] % 10 == 0:
                    logger.info(
                        "[%s] %s %s->%s (total: recv=%d, parsed=%d)",
                        action, plan.callsign, plan.adep, plan.adest,
                        total_received[0], total_parsed[0],
                    )

        # 每 10 条打印一次统计
        if total_received[0] % 10 == 0:
            logger.info(
                "recv=%d parsed=%d",
                total_received[0],
                total_parsed[0],
            )

    receiver = UdpReceiver(config.aftn, on_aftn_message)
    receiver.start()

    # Flask web 服务
    app = create_app(config, db)
    web_host = config.web.host
    web_port = config.web.port

    def run_web() -> None:
        logger.info("web server starting on http://%s:%d", web_host, web_port)
        app.run(host=web_host, port=web_port, debug=config.web.debug, use_reloader=False)

    web_thread = Thread(target=run_web, daemon=True, name="web-server")
    web_thread.start()

    # 信号处理
    stop_requested = [False]

    def signal_handler(signum: int, _frame: object) -> None:
        if stop_requested[0]:
            logger.warning("forced exit")
            sys.exit(1)
        logger.info("received signal %d, shutting down...", signum)
        stop_requested[0] = True
        receiver.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print(f"\n{'='*50}")
    print(f"  {config.system_name}")
    print(f"  UDP 接收: {config.aftn.bind_host}:{config.aftn.port}")
    if config.aftn.multicast_group:
        print(f"  组播组:   {config.aftn.multicast_group}")
    print(f"  Web 页面: http://{web_host}:{web_port}")
    print(f"  数据库:   {config.db_path}")
    print(f"{'='*50}\n")

    # 保持主线程
    try:
        while not stop_requested[0]:
            import time
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        receiver.stop()
        logger.info("shutdown complete")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
