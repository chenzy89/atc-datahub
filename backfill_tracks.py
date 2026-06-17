#!/usr/bin/env python3
"""
一次性航迹回填脚本：扫描 radar_history gzip 文件，将 DB 中缺失的航迹补入 flight_tracks 表。

用法:  python3 backfill_tracks.py --date 2026-06-17

会跳过已经在 flight_tracks 表中有记录的呼号+DOF 组合。
"""
import argparse
import json
import logging
import sqlite3
import sys
import gzip
import os
from datetime import datetime, date
from pathlib import Path
from typing import List

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("backfill")


def query_radar_history(archive_dir: str, date_str: str, callsign: str = "") -> List[dict]:
    """从 radar_history gzip 中查询轨迹点"""
    fpath = os.path.join(archive_dir, f"radar_{date_str.replace('-', '')}.jsonl.gz")
    if not os.path.exists(fpath):
        return []

    results = []
    try:
        with gzip.open(fpath, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    pt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cs = (pt.get("cs") or "").strip().upper()
                if callsign and cs != callsign:
                    continue
                results.append(pt)
    except Exception as exc:
        logger.warning("读取 %s 失败: %s", fpath, exc)
    return results


def get_flight_plan(conn, callsign: str, dof: str):
    """查飞行计划的 adep/adest"""
    cur = conn.execute(
        "SELECT adep, adest FROM flight_plans WHERE callsign=? AND dof=? LIMIT 1",
        [callsign, dof],
    )
    return cur.fetchone()


def backfill(date_str: str, archive_dir: str, db_path: str):
    logger.info("回填日期: %s", date_str)
    logger.info("档案目录: %s", archive_dir)
    logger.info("数据库: %s", db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 1. 收集 gzip 中所有呼号及点数
    fpath = os.path.join(archive_dir, f"radar_{date_str.replace('-', '')}.jsonl.gz")
    if not os.path.exists(fpath):
        logger.error("未找到档案文件: %s", fpath)
        return

    logger.info("扫描档案文件: %s", fpath)
    pts_by_cs = {}
    line_count = 0
    with gzip.open(fpath, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            line_count += 1
            try:
                pt = json.loads(line)
            except json.JSONDecodeError:
                continue
            cs = (pt.get("cs") or "").strip().upper()
            if not cs:
                continue
            if cs not in pts_by_cs:
                pts_by_cs[cs] = []
            pts_by_cs[cs].append(pt)

    logger.info("共 %d 行, %d 个呼号", line_count, len(pts_by_cs))

    # 2. 查询已存在的航迹（去重依据）
    existing = set()
    cur = conn.execute(
        "SELECT callsign FROM flight_tracks WHERE dof=?",
        [date_str],
    )
    for r in cur.fetchall():
        existing.add(r["callsign"].upper())

    logger.info("DB 已有 %d 个呼号有航迹", len(existing))

    # 3. 跳过已有，回填缺失
    saved = 0
    skipped_no_fp = 0
    skipped_short = 0
    skipped_exists = 0

    for cs, pts in sorted(pts_by_cs.items()):
        if cs in existing:
            skipped_exists += 1
            continue

        if len(pts) < 2:
            skipped_short += 1
            continue

        # 按时间排序
        pts_sorted = sorted(pts, key=lambda p: p.get("ts", ""))

        # 尝试从飞行计划获取 adep/adest，先试当天，再试前/后一天
        fp = get_flight_plan(conn, cs, date_str)
        fp_dof = date_str
        if not fp:
            # 试前一天（跨午夜）
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            prev = (dt.replace(day=1) - __import__("datetime").timedelta(days=1)).strftime("%Y-%m-%d")
            # 更精确的昨天
            from datetime import timedelta
            prev = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
            fp = get_flight_plan(conn, cs, prev)
            if fp:
                fp_dof = prev

        if not fp:
            skipped_no_fp += 1
            logger.debug("跳过 %s: 未找到飞行计划", cs)
            continue

        adep = fp["adep"] or ""
        adest = fp["adest"] or ""

        # 判断进/出港
        if adest.upper() == "ZGSZ":
            track_type = "ARRIVAL"
        elif adep.upper() == "ZGSZ":
            track_type = "DEPARTURE"
        else:
            track_type = "ARRIVAL"

        points_json = json.dumps(pts_sorted)
        start_time = pts_sorted[0].get("ts", "")
        end_time = pts_sorted[-1].get("ts", "")

        try:
            conn.execute(
                """INSERT INTO flight_tracks
                   (callsign, track_type, adep, adest, dof, points_json, start_time, end_time, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                [cs, track_type, adep, adest, fp_dof, points_json, start_time, end_time],
            )
            conn.commit()
            saved += 1
            logger.info(
                "[保存] %s %s %s->%s (%d点, dof=%s)",
                cs, track_type, adep, adest, len(pts_sorted), fp_dof,
            )
        except Exception as exc:
            logger.error("保存 %s 失败: %s", cs, exc)

    logger.info(
        "\n=== 回填完成 ===\n"
        "  已存在跳过: %d\n"
        "  点数不足跳过: %d\n"
        "  无飞行计划跳过: %d\n"
        "  新保存: %d",
        skipped_exists, skipped_short, skipped_no_fp, saved,
    )

    conn.close()


def main():
    parser = argparse.ArgumentParser(description="航迹回填脚本")
    parser.add_argument("--date", default=datetime.utcnow().strftime("%Y-%m-%d"),
                        help="回填日期 (YYYY-MM-DD)")
    parser.add_argument("--archive-dir", default="data/radar_history",
                        help="radar_history 目录")
    parser.add_argument("--db", default="data/aftn.db",
                        help="数据库路径")
    args = parser.parse_args()

    backfill(args.date, args.archive_dir, args.db)


if __name__ == "__main__":
    main()
