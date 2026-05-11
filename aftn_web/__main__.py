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
from .database import Database
from .parser import AftnParser
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
    _prev_raw_text: str = ""   # 去重：记录上一条原始报文正文

    # UDP 接收器
    total_received = [0]
    total_parsed = [0]

    def on_aftn_message(payload: bytes, addr: str, port: int, received_at: datetime) -> None:
        nonlocal total_received, total_parsed, _prev_raw_text
        total_received[0] += 1
        try:
            result = parser_aftn.parse(payload, received_at=received_at)
        except Exception:
            logger.exception("parse error from %s:%d", addr, port)
            return

        raw_text = result.message.raw_text or ""

        # ── 去重：与上一条报文正文相同则忽略 ──────────────────
        if raw_text == _prev_raw_text:
            logger.debug("duplicate message ignored: %.40s...", raw_text[:40])
            return
        _prev_raw_text = raw_text

        # LAM 报文：不记录、不解析
        if result.message.message_type == "LAM":
            logger.debug("AFTN LAM ignored: not recorded")
            return

        # 保存原始报文
        db.save_aftn_message(result.message)

        # EST：只记录报文，不解析、不生成 FlightPlan、不更新飞行计划
        if result.message.message_type == "EST":
            total_parsed[0] += 1
            logger.info(
                "[EST] recorded only (total: recv=%d, parsed=%d)",
                total_received[0], total_parsed[0],
            )
            return

        if not result.accepted:
            if result.errors:
                logger.debug("AFTN ignored: %s", "; ".join(result.errors))
            return

        plan = result.flight_plan
        action = result.action

        if action == "CNL":
            # CNL：查找并删除关联的飞行计划
            deleted = db.delete_by_key(plan.callsign, plan.adep, plan.adest)
            total_parsed[0] += 1
            if deleted:
                logger.info(
                    "[CNL] %s %s->%s 已取消删除 (total: recv=%d, parsed=%d)",
                    plan.callsign, plan.adep, plan.adest,
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
            # DEP：检查报文中是否包含 DOF 字段
            raw_has_dof = "DOF/" in (plan.raw_message_text or "").upper()
            if not raw_has_dof:
                today = datetime.utcnow().date()
                # 先查当日计划
                existing = db.find_flight_plan(plan.callsign, plan.adep, plan.adest, today)
                if existing:
                    # 当日有计划 → upsert 更新
                    db.upsert_flight_plan(plan)
                    total_parsed[0] += 1
                    logger.info(
                        "[DEP] %s %s->%s 当日已有计划，更新 (total: recv=%d, parsed=%d)",
                        plan.callsign, plan.adep, plan.adest,
                        total_received[0], total_parsed[0],
                    )
                else:
                    yesterday = today - timedelta(days=1)
                    existing_yd = db.find_flight_plan(plan.callsign, plan.adep, plan.adest, yesterday)
                    if existing_yd and not existing_yd.get("atd"):
                        # 昨日有计划且 ATD 为空 → 跨日延误
                        db.update_flight_plan_atd(existing_yd["id"], plan.atd, ssr=plan.ssr, source_message_type="DEP")
                        total_parsed[0] += 1
                        logger.info(
                            "[DEP] %s %s->%s 跨日延误，ATD=%s 赋给昨日计划 (id=%d) (total: recv=%d, parsed=%d)",
                            plan.callsign, plan.adep, plan.adest, plan.atd, existing_yd["id"],
                            total_received[0], total_parsed[0],
                        )
                    else:
                        # 今日昨日均无计划 → 新建
                        db.create_flight_plan(plan)
                        total_parsed[0] += 1
                        logger.info(
                            "[DEP] %s %s->%s 无计划，新建 (total: recv=%d, parsed=%d)",
                            plan.callsign, plan.adep, plan.adest,
                            total_received[0], total_parsed[0],
                        )
            else:
                # 包含 DOF 字段 → 查同 DOF 计划，有则更新 ATD+SSR，无则新建
                existing = db.find_flight_plan(plan.callsign, plan.adep, plan.adest, plan.dof)
                if existing:
                    db.update_flight_plan_atd(existing["id"], plan.atd, ssr=plan.ssr, source_message_type="DEP")
                    total_parsed[0] += 1
                    logger.info(
                        "[DEP] %s %s->%s DOF=%s 已有计划，更新 ATD+SSR (total: recv=%d, parsed=%d)",
                        plan.callsign, plan.adep, plan.adest, plan.dof,
                        total_received[0], total_parsed[0],
                    )
                else:
                    db.create_flight_plan(plan)
                    total_parsed[0] += 1
                    logger.info(
                        "[DEP] %s %s->%s DOF=%s 新建 (total: recv=%d, parsed=%d)",
                        plan.callsign, plan.adep, plan.adest, plan.dof,
                        total_received[0], total_parsed[0],
                    )
        elif action == "ARR":
            # ARR：优先查找未落地计划（ATA 为空），再按 DOF 回退
            raw_has_dof = "DOF/" in (plan.raw_message_text or "").upper()
            if not raw_has_dof:
                # 无 DOF → 搜索 (callsign+adep+adest) 匹配的未落地计划
                candidates = db.query_flight_plans(
                    callsign=plan.callsign,
                    adep=plan.adep,
                    adest=plan.adest,
                    limit=10,
                )
                pending = [p for p in candidates if not p.get("ata")]
                if pending:
                    # 取最近一版未落地计划（按 DOF 倒序）
                    pending.sort(key=lambda p: p.get("dof", "") or "", reverse=True)
                    target = pending[0]
                    db.update_flight_plan_ata(target["id"], plan.ata, source_message_type="ARR")
                    total_parsed[0] += 1
                    logger.info(
                        "[ARR] %s %s->%s 未落地计划 (id=%d, dof=%s)，赋 ATA=%s (total: recv=%d, parsed=%d)",
                        plan.callsign, plan.adep, plan.adest,
                        target["id"], target.get("dof", ""), plan.ata,
                        total_received[0], total_parsed[0],
                    )
                else:
                    # 找不到未落地计划，回退到当日/昨日 DOF 匹配逻辑
                    today = datetime.utcnow().date()
                    existing = db.find_flight_plan(plan.callsign, plan.adep, plan.adest, today)
                    if existing:
                        db.upsert_flight_plan(plan)
                        total_parsed[0] += 1
                        logger.info(
                            "[ARR] %s %s->%s 当日已有计划 (已落地)，upsert (total: recv=%d, parsed=%d)",
                            plan.callsign, plan.adep, plan.adest,
                            total_received[0], total_parsed[0],
                        )
                    else:
                        yesterday = today - timedelta(days=1)
                        existing_yd = db.find_flight_plan(plan.callsign, plan.adep, plan.adest, yesterday)
                        if existing_yd and not existing_yd.get("ata"):
                            db.update_flight_plan_ata(existing_yd["id"], plan.ata, source_message_type="ARR")
                            total_parsed[0] += 1
                            logger.info(
                                "[ARR] %s %s->%s 跨日期落地，ATA=%s 赋给昨日计划 (id=%d) (total: recv=%d, parsed=%d)",
                                plan.callsign, plan.adep, plan.adest, plan.ata, existing_yd["id"],
                                total_received[0], total_parsed[0],
                            )
                        else:
                            db.create_flight_plan(plan)
                            total_parsed[0] += 1
                            logger.info(
                                "[ARR] %s %s->%s 无计划，新建 (total: recv=%d, parsed=%d)",
                                plan.callsign, plan.adep, plan.adest,
                                total_received[0], total_parsed[0],
                            )
            else:
                # 含有 DOF → 查同 DOF 计划，有则更新 ATA，无则新建
                existing = db.find_flight_plan(plan.callsign, plan.adep, plan.adest, plan.dof)
                if existing:
                    db.update_flight_plan_ata(existing["id"], plan.ata, source_message_type="ARR")
                    total_parsed[0] += 1
                    logger.info(
                        "[ARR] %s %s->%s DOF=%s 已有计划，更新 ATA (total: recv=%d, parsed=%d)",
                        plan.callsign, plan.adep, plan.adest, plan.dof,
                        total_received[0], total_parsed[0],
                    )
                else:
                    db.create_flight_plan(plan)
                    total_parsed[0] += 1
                    logger.info(
                        "[ARR] %s %s->%s DOF=%s 新建 (total: recv=%d, parsed=%d)",
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
