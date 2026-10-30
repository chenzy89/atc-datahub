"""航迹历史存储 — 每天一个多成员 gzip JSONL 文件

存储解析后的单条航迹点 (parsed CAT062 records)，而非 FDR 快照。
最小存储单元：时间戳 + 呼号 + 位置 + 状态信息。

保留 90 天，自动清理过期文件。

写入策略：缓冲 5 秒或 200 条后，组装完整的独立 gzip member 写入文件。
读取时跳过损坏的最后一个 member，保证 crash-safe。

CFL 处理：CAT062 的计划块 CFL 并非每帧都带，原样存储会让回放里绝大多数帧显示 0000。
因此写入时按呼号做「前向保持」（有值才覆盖，否则沿用最后已知值），与 fdr_store 的实时
行为一致；同一呼号超过 _CFL_LATCH_TTL_SECONDS 无更新则丢弃保持值（防呼号复用串值）。
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import struct
import threading
import time
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("aftn_web.radar_history")

# 缓冲参数
_FLUSH_INTERVAL_SECONDS = 5
_FLUSH_BATCH_SIZE = 200

# CFL 前向保持：同一呼号超过该秒数无更新则丢弃已保持的 CFL
_CFL_LATCH_TTL_SECONDS = 1800

# 呼号索引侧车文件后缀（radar_YYYYMMDD.jsonl.gz.idx.json.gz）
_IDX_SUFFIX = ".idx.json.gz"
# 内存中最多缓存的日索引个数（每个约几十 MB）
_INDEX_CACHE_MAX = 4


def _iter_gzip_members_with_offsets(filepath: Path):
    """迭代 gzip 多成员文件，产出 (成员起始字节偏移, 解压数据)。
    基于 zlib.decompressobj(wbits=31) 逐成员解压，尾部损坏 member 自动终止。"""
    try:
        raw = open(str(filepath), "rb")
    except Exception:
        return
    with raw:
        while True:
            start = raw.tell()
            magic = raw.read(2)
            if len(magic) < 2 or magic != b"\x1f\x8b":
                return
            raw.seek(start)
            d = zlib.decompressobj(31)
            buf = bytearray()
            while True:
                chunk = raw.read(262144)
                if not chunk:
                    yield start, bytes(buf)  # 尾部不完整 member
                    return
                try:
                    buf += d.decompress(chunk)
                except zlib.error:
                    return
                if d.eof:
                    buf += d.flush()
                    # 回退未消费的下一 member 字节
                    raw.seek(-len(d.unused_data), 1)
                    yield start, bytes(buf)
                    break


def _write_gzip_member(filepath: Path, data: bytes) -> bool:
    """追加一个完整的独立 gzip member 到文件
    返回 True 表示写入成功
    """
    buf = io.BytesIO()
    try:
        with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
            gz.write(data)
        member = buf.getvalue()
        with open(str(filepath), "ab") as f:
            f.write(member)
        return True
    except Exception as exc:
        logger.error("radar history: write gzip member failed: %s", exc)
        return False


def _read_gzip_members(filepath: Path) -> list[bytes]:
    """读取 gzip 多成员文件，返回所有完整 member 的解压数据
    跳过尾部不完整 member（crash-safe）
    """
    members: list[bytes] = []
    try:
        with open(str(filepath), "rb") as raw:
            data = raw.read()
    except Exception:
        return members

    offset = 0
    while offset < len(data):
        # 检查 gzip 头部魔术字
        if data[offset:offset+2] != b'\x1f\x8b':
            break
        # 定位下一个 member 的起始位置
        # gzip header: 10 bytes fixed + optional FEXTRA/NAME/COMMENT + 8 bytes CRC
        # 简化：尝试解压当前 member，如果有错误则终止
        try:
            buf = io.BytesIO(data[offset:])
            with gzip.GzipFile(fileobj=buf) as gz:
                member_data = gz.read()
            members.append(member_data)
            offset += buf.tell()
        except Exception:
            # 最后一个 member 不完整，跳过
            if members:
                logger.debug("radar history: truncated member at offset %d, skipping", offset)
            break

    return members


class RadarHistoryStore:
    """航迹历史存储，线程安全，crash-safe"""

    def __init__(self, data_dir: str | Path, retention_days: int = 90):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._today: str | None = None
        self._buffer: list[str] = []      # JSONL lines waiting to flush
        self._last_flush = time.monotonic()
        self._current_path: Path | None = None
        self._last_cleanup = time.monotonic()
        self._cfl_latch: dict[str, tuple[float, float]] = {}   # 呼号 → (CFL 米, 最后更新时间)
        # 呼号索引缓存: {文件路径: {"members": [offset...], "cs": {呼号: [member_idx...]}}}
        self._index_cache: dict[str, dict] = {}
        self._index_lock = threading.Lock()

    # ── 公开接口 ──────────────────────────────────────────

    def record(self, parsed: dict[str, Any], received_at: datetime) -> None:
        """记录一条解析后的航迹点（缓冲写入）"""
        now = datetime.utcnow()
        today = now.strftime("%Y%m%d")

        ts = received_at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{received_at.microsecond // 1000:03d}Z"
        point = {
            "ts": ts,
            "cs": (parsed.get("callsign") or "").strip(),
            "ss": (parsed.get("ssr") or "").strip(),
            "lt": parsed.get("latitude", 0.0),
            "ln": parsed.get("longitude", 0.0),
            "fl": parsed.get("flight_level", 0.0),
            "hd": parsed.get("heading", 0.0),
            "sp": parsed.get("speed", 0.0),
            "ap": (parsed.get("adep") or "").strip().upper(),
            "ad": (parsed.get("adest") or "").strip().upper(),
            "rw": (parsed.get("runway") or "").strip(),
            "fp": (parsed.get("flight_procedure") or "").strip(),
            "cf": 0.0,
            "si": parsed.get("sector_index", 0),
        }

        cs_key = point["cs"].upper()
        try:
            cfl_now = float(parsed.get("cfl", 0.0) or 0.0)
        except (TypeError, ValueError):
            cfl_now = 0.0

        line = None

        with self._lock:
            # CFL 前向保持（与 fdr_store 实时行为一致）
            now_mono = time.monotonic()
            if cs_key:
                if cfl_now > 0:
                    point["cf"] = cfl_now
                    self._cfl_latch[cs_key] = (cfl_now, now_mono)
                else:
                    latched = self._cfl_latch.get(cs_key)
                    if latched is not None:
                        if now_mono - latched[1] <= _CFL_LATCH_TTL_SECONDS:
                            point["cf"] = latched[0]
                        else:
                            del self._cfl_latch[cs_key]
            elif cfl_now > 0:
                point["cf"] = cfl_now

            line = json.dumps(point, ensure_ascii=False, separators=(",", ":")) + "\n"

            if today != self._today:
                self._rotate(today)

            self._buffer.append(line)

            # 达到阈值 | 超过间隔 → 刷盘
            now_ts = time.monotonic()
            if (len(self._buffer) >= _FLUSH_BATCH_SIZE
                    or now_ts - self._last_flush >= _FLUSH_INTERVAL_SECONDS):
                self._flush_nolock()

    def query(
        self,
        ts_from: str,
        ts_to: str,
        callsign: str | None = None,
    ) -> list[dict[str, Any]]:
        """查询时间范围内的航迹点"""
        from_d = ts_from[:10].replace("-", "")
        to_d = ts_to[:10].replace("-", "")
        results: list[dict[str, Any]] = []

        files = sorted(self.data_dir.glob("radar_*.jsonl.gz"))
        for f in files:
            name = f.stem  # radar_20260526 or radar_20260502.jsonl
            date_part = name.split("_", 1)[1].split(".")[0] if "_" in name else ""
            if date_part < from_d or date_part > to_d:
                continue
            if callsign and f != self._current_path:
                # 历史文件（非当日写入中）→ 索引快速查询；失败回退全扫描
                if self._query_indexed(f, ts_from, ts_to, callsign, results):
                    continue
            if date_part == from_d:
                self._scan_file(f, ts_from, ts_to, callsign, results)
            elif date_part == to_d:
                self._scan_file(f, ts_from, ts_to, callsign, results)
            else:
                self._scan_file(f, None, None, callsign, results)

        return results

    def get_time_range(self) -> tuple[Optional[str], Optional[str]]:
        """获取可用历史数据的时间范围"""
        files = sorted(self.data_dir.glob("radar_*.jsonl.gz"))
        if not files:
            return None, None

        first_ts: str | None = None
        last_ts: str | None = None

        # 第一个 member 的第一条有效记录
        try:
            members = _read_gzip_members(files[0])
            if members:
                for line in members[0].decode("utf-8").split("\n"):
                    line = line.strip()
                    if line:
                        try:
                            first_ts = json.loads(line).get("ts", "")
                        except json.JSONDecodeError:
                            pass
                        break
        except Exception:
            pass

        # 最后一个 member 的最后一条有效记录
        try:
            members = _read_gzip_members(files[-1])
            if members:
                last_member = members[-1]
                last_line = ""
                for ln in last_member.decode("utf-8").split("\n"):
                    ln = ln.strip()
                    if ln:
                        last_line = ln
                if last_line:
                    try:
                        last_ts = json.loads(last_line).get("ts", "")
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass

        return first_ts, last_ts

    def flush(self) -> None:
        """强制刷盘"""
        with self._lock:
            self._flush_nolock()

    # ── 呼号索引（v2.3.19）────────────────────────────────
    # 目的：按呼号查询历史航迹时不再全日解压扫描（~25-30s），
    # 而是按索引只解压该呼号出现的 member（秒级）。
    # 索引侧车文件与数据文件同目录，构建一次永久复用；
    # 只对非当日（已写完）的文件启用，当日文件仍走全扫描。

    def _index_path(self, filepath: Path) -> Path:
        return filepath.with_name(filepath.name + _IDX_SUFFIX)

    def _load_or_build_index(self, filepath: Path) -> Optional[dict]:
        """加载（或首次构建）单日呼号索引；失败返回 None（调用方回退全扫描）"""
        key = str(filepath)
        with self._index_lock:
            cached = self._index_cache.get(key)
        if cached is not None:
            return cached

        sidecar = self._index_path(filepath)
        idx: dict | None = None
        # 1) 侧车文件有效（比数据文件新）→ 直接加载
        try:
            if sidecar.is_file() and sidecar.stat().st_mtime >= filepath.stat().st_mtime:
                with gzip.open(str(sidecar), "rb") as fh:
                    loaded = json.loads(fh.read().decode("utf-8"))
                if isinstance(loaded, dict) and "members" in loaded and "cs" in loaded:
                    idx = loaded
        except Exception as exc:
            logger.warning("radar history: 读取索引失败 %s: %s", sidecar.name, exc)

        # 2) 无有效侧车 → 全量扫描构建并落盘
        if idx is None:
            t0 = time.monotonic()
            members: list[int] = []
            cs_map: dict[str, list[int]] = {}
            try:
                mi = 0
                for start, data in _iter_gzip_members_with_offsets(filepath):
                    members.append(start)
                    for line in data.decode("utf-8", "replace").split("\n"):
                        if not line:
                            continue
                        try:
                            cs = json.loads(line).get("cs") or ""
                        except Exception:
                            continue
                        if cs:
                            cs_map.setdefault(cs.upper(), []).append(mi)
                    mi += 1
                idx = {"members": members, "cs": cs_map}
            except Exception as exc:
                logger.warning("radar history: 构建索引失败 %s: %s", filepath.name, exc)
                return None
            try:
                tmp = sidecar.with_name(sidecar.name + ".tmp")
                with gzip.open(str(tmp), "wb", compresslevel=6) as fh:
                    fh.write(json.dumps(idx, separators=(",", ":")).encode("utf-8"))
                tmp.replace(sidecar)
                logger.info(
                    "radar history: 构建索引完成 %s（%d members / %d 呼号, %.1fs）",
                    filepath.name, len(members), len(cs_map), time.monotonic() - t0,
                )
            except Exception as exc:
                logger.warning("radar history: 索引落盘失败 %s: %s", sidecar.name, exc)

        with self._index_lock:
            if len(self._index_cache) >= _INDEX_CACHE_MAX:
                self._index_cache.clear()
            self._index_cache[key] = idx
        return idx

    def _query_indexed(self, filepath: Path, ts_from: str | None, ts_to: str | None,
                       callsign: str, results: list[dict[str, Any]]) -> bool:
        """按索引只解压目标呼号出现的 member。成功返回 True（无论命中多少点）。"""
        idx = self._load_or_build_index(filepath)
        if idx is None:
            return False
        member_ids = (idx.get("cs") or {}).get(callsign.upper())
        if not member_ids:
            return True
        members = idx.get("members") or []
        # 同一 member 可能被同一呼号的多条记录引用，必须去重
        try:
            with open(str(filepath), "rb") as raw:
                for mid in sorted(set(member_ids)):
                    if mid >= len(members):
                        continue
                    raw.seek(members[mid])
                    d = zlib.decompressobj(31)
                    buf = b""
                    while True:
                        chunk = raw.read(262144)
                        if not chunk:
                            break
                        buf += d.decompress(chunk)
                        if d.eof:
                            buf += d.flush()
                            break
                    self._filter_lines(buf.decode("utf-8", "replace"),
                                       ts_from, ts_to, callsign, results)
        except Exception as exc:
            logger.warning("radar history: 索引查询失败 %s: %s", filepath.name, exc)
            return False
        return True

    @staticmethod
    def _filter_lines(text: str, ts_from: str | None, ts_to: str | None,
                      callsign: str, results: list[dict[str, Any]]) -> None:
        """按时间/呼号过滤 JSONL 文本行并追加到 results"""
        cs_upper = callsign.upper()
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                pt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (pt.get("cs") or "").upper() != cs_upper:
                continue
            ts = pt.get("ts", "")
            if ts_from and ts < ts_from:
                continue
            if ts_to and ts > ts_to:
                continue
            results.append(pt)

    def close(self) -> None:
        """关闭并刷盘"""
        with self._lock:
            self._flush_nolock()
            self._buffer = []
            self._today = None
            self._current_path = None

    # ── 内部 ──────────────────────────────────────────────

    def _rotate(self, today: str) -> None:
        """切换日期文件"""
        self._flush_nolock()
        self._current_path = self.data_dir / f"radar_{today}.jsonl.gz"
        self._today = today
        # 跨日清空 CFL 保持表（避免跨天串值，同时限制内存）
        self._cfl_latch.clear()
        logger.info("radar history: rotated to %s", self._current_path.name)

        # 每小时检查过期清理
        now_ts = time.monotonic()
        if now_ts - self._last_cleanup > 3600:
            self._last_cleanup = now_ts
            self._cleanup_old()

    def _flush_nolock(self) -> None:
        """将缓冲写入文件（持有锁时调用）"""
        if not self._buffer or not self._current_path:
            return
        data = "".join(self._buffer).encode("utf-8")
        if _write_gzip_member(self._current_path, data):
            self._buffer = []
            self._last_flush = time.monotonic()
        else:
            logger.warning("radar history: flush failed, buffer kept (%d lines)", len(self._buffer))

    def _cleanup_old(self) -> None:
        """删除过期文件"""
        cutoff = datetime.utcnow() - timedelta(days=self.retention_days)
        cutoff_str = cutoff.strftime("%Y%m%d")
        removed = 0
        for f in self.data_dir.glob("radar_*.jsonl.gz"):
            try:
                name = f.stem
                date_part = name.split("_", 1)[1].split(".")[0] if "_" in name else ""
                if date_part < cutoff_str and date_part.isdigit():
                    f.unlink()
                    removed += 1
            except Exception:
                pass
        if removed:
            logger.info("radar history: cleaned %d old files (retention=%dd)", removed, self.retention_days)

    def _scan_file(
        self,
        path: Path,
        ts_from: str | None,
        ts_to: str | None,
        callsign: str | None,
        results: list[dict[str, Any]],
    ) -> None:
        """扫描单个文件的所有完整 member"""
        try:
            members = _read_gzip_members(path)
        except Exception as exc:
            logger.warning("radar history: scan %s failed: %s", path.name, exc)
            return

        for member_data in members:
            for line in member_data.decode("utf-8").split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    pt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = pt.get("ts", "")
                if ts_from and ts < ts_from:
                    continue
                if ts_to and ts > ts_to:
                    continue
                if callsign and pt.get("cs", "").upper() != callsign.upper():
                    continue
                results.append(pt)
