"""启动入口"""

from __future__ import annotations

import logging
import signal
import sys
from argparse import ArgumentParser
from datetime import datetime
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

    # AFTN 解析器
    parser_aftn = AftnParser()

    # UDP 接收器
    total_received = [0]
    total_parsed = [0]

    def on_aftn_message(payload: bytes, addr: str, port: int, received_at: datetime) -> None:
        nonlocal total_received, total_parsed
        total_received[0] += 1
        try:
            result = parser_aftn.parse(payload, received_at=received_at)
        except Exception:
            logger.exception("parse error from %s:%d", addr, port)
            return

        # 保存原始报文
        db.save_aftn_message(result.message)

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
        else:
            # FPL / DEP / ARR / DLA：upsert（找不到则新建，找到了则更新对应字段）
            # source_message_type 由 upsert 统一更新为最新收到的报文类型
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
