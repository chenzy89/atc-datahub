"""
MCP Server for ATC综合数据中心
提供 SSE transport 的 MCP 协议实现，供 OpenClaw 调用查询数据
独立线程运行，不依赖第三方 MCP SDK
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Callable
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger("aftn_web.mcp")

# ── JSON-RPC 响应构建 ──────────────────────────────────────

def jsonrpc_error(code: int, message: str, req_id: Any = None) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }

def jsonrpc_result(result: Any, req_id: Any = None) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": result,
    }

# ── MCP 工具定义 ───────────────────────────────────────────

def _tool_def(name: str, description: str, input_schema: dict) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": input_schema,
    }

TOOL_DEFINITIONS = [
    _tool_def(
        "get_system_stats",
        "获取系统概览统计信息（飞行计划数、AFTN报文数、ASR状态等）",
        {
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    _tool_def(
        "search_flight_plans",
        "搜索飞行计划，支持按呼号、起降机场、DOF等过滤",
        {
            "type": "object",
            "properties": {
                "callsign": {"type": "string", "description": "航班呼号（支持模糊）"},
                "adep": {"type": "string", "description": "起飞机场"},
                "adest": {"type": "string", "description": "目的地机场"},
                "dof": {"type": "string", "description": "执行日期 YYYY-MM-DD"},
                "airport": {"type": "string", "description": "关注机场（adep OR adest 匹配）"},
                "route": {"type": "string", "description": "航路关键词"},
                "source_message_type": {"type": "string", "description": "报文类型 FPL/DEP/ARR/CHG/DLA"},
                "handover_pt": {"type": "string", "description": "移交点"},
                "limit": {"type": "integer", "description": "最大返回条数（默认100，最多500）"},
            },
            "required": [],
        },
    ),
    _tool_def(
        "get_flight_info",
        "获取指定航班号的详细飞行计划信息（含跑道、飞行程序、移交点等）",
        {
            "type": "object",
            "properties": {
                "callsign": {"type": "string", "description": "航班呼号"},
                "adep": {"type": "string", "description": "起飞机场（可选，缩小范围）"},
                "adest": {"type": "string", "description": "目的地机场（可选，缩小范围）"},
            },
            "required": ["callsign"],
        },
    ),
    _tool_def(
        "search_aftn_messages",
        "搜索AFTN报文历史记录",
        {
            "type": "object",
            "properties": {
                "message_type": {"type": "string", "description": "报文类型过滤"},
                "keyword": {"type": "string", "description": "关键词搜索"},
                "date_from": {"type": "string", "description": "开始日期 YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
                "limit": {"type": "integer", "description": "最大返回条数（默认100，最多500）"},
            },
            "required": [],
        },
    ),
    _tool_def(
        "get_current_tracks",
        "获取当前雷达航迹列表（FDR实时数据）",
        {
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    _tool_def(
        "get_flight_trail",
        "获取指定航班号的当天历史航迹点（用于轨迹回放/轨迹图）",
        {
            "type": "object",
            "properties": {
                "callsign": {"type": "string", "description": "航班呼号"},
                "date": {"type": "string", "description": "日期 YYYY-MM-DD（默认今天）"},
            },
            "required": ["callsign"],
        },
    ),
    _tool_def(
        "search_asr_records",
        "搜索ASR语音识别文本记录（扇区/时段/航班号过滤）",
        {
            "type": "object",
            "properties": {
                "sector": {"type": "string", "description": "扇区代码过滤"},
                "date_from": {"type": "string", "description": "开始日期 YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
                "callsign": {"type": "string", "description": "航班呼号过滤"},
                "limit": {"type": "integer", "description": "最大返回条数（默认100，最多1000）"},
            },
            "required": [],
        },
    ),
    _tool_def(
        "get_voice_status",
        "获取语音通道状态（各通道实时能量、活动状态、信道号）",
        {
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    _tool_def(
        "get_asr_status",
        "获取ASR接收器状态和最新识别文本",
        {
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    _tool_def(
        "get_terminal_config",
        "获取终端区配置（机场列表、多边形、高度范围）",
        {
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
]

# ── SSE 连接管理 ───────────────────────────────────────────

class SSEConnection:
    """单个 SSE 连接，持有消息队列用于向客户端推送"""
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.q: queue.Queue = queue.Queue()
        self._closed = False

    def send_event(self, event: str, data: str) -> None:
        if not self._closed:
            self.q.put((event, data))

    def close(self) -> None:
        self._closed = True


class MCPRequestHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理：SSE + JSON-RPC"""

    # 类级别引用 — 由 MCPServer 注入
    server_ref: "MCPServer" = None  # type: ignore

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("MCP HTTP: " + fmt, *args)

    # ── SSE GET 端点 ──

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/mcp":
            self._handle_sse()
        elif parsed.path == "/health":
            self._send_json({"status": "ok", "time": datetime.utcnow().isoformat() + "Z"})
        else:
            self._send_error(404, "Not found")

    def _handle_sse(self) -> None:
        session_id = str(uuid.uuid4())
        conn = SSEConnection(session_id)
        mcp_server = self.server_ref

        with mcp_server._lock:
            mcp_server._sse_connections[session_id] = conn

        logger.info("MCP SSE connected: session=%s", session_id)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        try:
            # 发送 endpoint 事件，告诉客户端 POST URL
            endpoint_data = f"/mcp/message?sessionId={session_id}"
            self.wfile.write(f"event: endpoint\ndata: {endpoint_data}\n\n".encode("utf-8"))
            self.wfile.flush()

            # 持续推送事件
            while True:
                try:
                    event, data = conn.q.get(timeout=30)
                    if event == "message":
                        self.wfile.write(f"event: message\ndata: {data}\n\n".encode("utf-8"))
                    elif event == "error":
                        self.wfile.write(f"event: error\ndata: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    # 心跳 keepalive
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with mcp_server._lock:
                mcp_server._sse_connections.pop(session_id, None)
            logger.info("MCP SSE disconnected: session=%s", session_id)

    # ── POST JSON-RPC 端点 ──

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/mcp/message":
            self._handle_message(parsed)
        else:
            self._send_error(404, "Not found")

    def _handle_message(self, parsed: urlparse) -> None:
        qs = parse_qs(parsed.query)
        session_id = qs.get("sessionId", [None])[0]

        if not session_id:
            self._send_json(jsonrpc_error(-32000, "missing sessionId"))
            return

        mcp_server = self.server_ref
        with mcp_server._lock:
            conn = mcp_server._sse_connections.get(session_id)

        if conn is None:
            self._send_json(jsonrpc_error(-32000, "invalid sessionId"))
            return

        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len)

        try:
            req = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(jsonrpc_error(-32700, "Parse error"))
            return

        method = req.get("method")
        req_id = req.get("id")
        params = req.get("params", {})

        try:
            result = mcp_server.dispatch(method, params)
            if result is not None:
                conn.send_event("message", json.dumps(jsonrpc_result(result, req_id), ensure_ascii=False))
            self._send_json({"jsonrpc": "2.0", "id": req_id, "result": {"ok": True}})
        except Exception as exc:
            logger.exception("MCP dispatch error: method=%s", method)
            conn.send_event("message", json.dumps(jsonrpc_error(-32603, str(exc), req_id), ensure_ascii=False))
            self._send_json(jsonrpc_error(-32603, str(exc), req_id))

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, msg: str) -> None:
        self._send_json({"error": msg}, status)

    # 支持 CORS 预检
    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ── MCP Server ─────────────────────────────────────────────

class MCPServer:
    """MCP 协议服务器 — SSE transport"""

    def __init__(self, host: str = "127.0.0.1", port: int = 18766):
        self.host = host
        self.port = port
        self._sse_connections: Dict[str, SSEConnection] = {}
        self._lock = threading.Lock()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

        # 数据源引用 — 由 register_data_sources 注入
        self.db = None
        self.fdr_store = None
        self.radar_history_store = None
        self.voice_receiver = None
        self.asr_receiver = None
        self.config = None

    def register_data_sources(
        self,
        db: Any,
        fdr_store: Any = None,
        radar_history_store: Any = None,
        voice_receiver: Any = None,
        asr_receiver: Any = None,
        config: Any = None,
    ) -> None:
        self.db = db
        self.fdr_store = fdr_store
        self.radar_history_store = radar_history_store
        self.voice_receiver = voice_receiver
        self.asr_receiver = asr_receiver
        self.config = config
        logger.info("MCP Server data sources registered")

    def start(self) -> None:
        if self._httpd is not None:
            logger.warning("MCP Server already running")
            return

        MCPRequestHandler.server_ref = self  # type: ignore
        self._httpd = ThreadingHTTPServer((self.host, self.port), MCPRequestHandler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True, name="mcp-server")
        self._thread.start()
        logger.info("MCP Server started on http://%s:%d", self.host, self.port)

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd = None
            logger.info("MCP Server stopped")

    def dispatch(self, method: str, params: dict) -> Any:
        """JSON-RPC 方法分发"""
        if method == "initialize":
            return {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {},
                },
                "serverInfo": {
                    "name": "atc-datahub",
                    "version": "2.1.62",
                },
            }
        elif method == "notifications/initialized":
            return None  # 无需回复
        elif method == "tools/list":
            return {"tools": TOOL_DEFINITIONS}
        elif method == "tools/call":
            return self._handle_tool_call(params.get("name", ""), params.get("arguments", {}))
        else:
            raise ValueError(f"Unknown method: {method}")

    def _handle_tool_call(self, name: str, args: dict) -> dict:
        handler = {
            "get_system_stats": self._get_system_stats,
            "search_flight_plans": self._search_flight_plans,
            "get_flight_info": self._get_flight_info,
            "search_aftn_messages": self._search_aftn_messages,
            "get_current_tracks": self._get_current_tracks,
            "get_flight_trail": self._get_flight_trail,
            "search_asr_records": self._search_asr_records,
            "get_voice_status": self._get_voice_status,
            "get_asr_status": self._get_asr_status,
            "get_terminal_config": self._get_terminal_config,
        }.get(name)

        if handler is None:
            raise ValueError(f"Unknown tool: {name}")

        data = handler(**args)
        return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, default=str)}]}

    # ═══════════════════════════════════════════════════════════
    # 各工具实现
    # ═══════════════════════════════════════════════════════════

    def _get_system_stats(self) -> dict:
        db = self.db
        if not db:
            return {"error": "database not available"}
        total_fpl = db.count_flight_plans()
        fpl_by_type: dict = {}
        for t in ("FPL", "DEP", "ARR", "DLA"):
            fpl_by_type[t] = db.count_flight_plans(source_message_type=t)
        total_aftn = db.count_aftn_messages()
        aftn_by_type = db.count_aftn_by_type()
        asr_enabled = self.asr_receiver is not None
        voice_enabled = self.voice_receiver is not None
        radar_enabled = self.fdr_store is not None

        return {
            "total_flight_plans": total_fpl,
            "flight_plans_by_type": fpl_by_type,
            "total_aftn_messages": total_aftn,
            "aftn_messages_by_type": aftn_by_type,
            "asr_enabled": asr_enabled,
            "voice_enabled": voice_enabled,
            "radar_enabled": radar_enabled,
            "db_path": str(db.db_path) if hasattr(db, "db_path") else "",
        }

    def _search_flight_plans(
        self,
        callsign: str = "",
        adep: str = "",
        adest: str = "",
        dof: str = "",
        airport: str = "",
        route: str = "",
        source_message_type: str = "",
        handover_pt: str = "",
        limit: int = 100,
    ) -> dict:
        db = self.db
        if not db:
            return {"error": "database not available", "records": []}
        limit = min(max(limit, 1), 500)
        records = db.query_flight_plans(
            callsign=callsign or None,
            adep=adep or None,
            adest=adest or None,
            dof=dof or None,
            airport=airport or None,
            route=route or None,
            source_message_type=source_message_type or None,
            handover_pt=handover_pt or None,
            limit=limit,
            offset=0,
        )
        total = db.count_flight_plans(
            callsign=callsign or None,
            adep=adep or None,
            adest=adest or None,
            dof=dof or None,
            airport=airport or None,
            route=route or None,
            source_message_type=source_message_type or None,
            handover_pt=handover_pt or None,
        )
        return {"total": total, "records": records}

    def _get_flight_info(self, callsign: str, adep: str = "", adest: str = "") -> dict:
        db = self.db
        if not db:
            return {"error": "database not available"}
        records = db.query_flight_plans(
            callsign=callsign,
            adep=adep or None,
            adest=adest or None,
            limit=5,
            offset=0,
        )
        if not records:
            records = db.query_flight_plans(callsign=callsign, limit=5, offset=0)
        if not records:
            return {"error": "not found"}

        best = max(records, key=lambda r: r.get("last_message_time") or "")
        return {
            "id": best["id"],
            "callsign": best.get("callsign", ""),
            "adep": best.get("adep", ""),
            "adest": best.get("adest", ""),
            "dof": best.get("dof", ""),
            "etd": best.get("etd", ""),
            "atd": best.get("atd", ""),
            "eta": best.get("eta", ""),
            "ata": best.get("ata", ""),
            "aircraft_type": best.get("aircraft_type", ""),
            "route": best.get("route", ""),
            "handover_pt": best.get("handover_pt", ""),
            "runway": best.get("runway", ""),
            "flight_procedure": best.get("flight_procedure", ""),
            "ssr": best.get("ssr", ""),
            "entry_time": best.get("entry_time", ""),
            "exit_time": best.get("exit_time", ""),
            "terminal_flight_time": best.get("terminal_flight_time", 0),
        }

    def _search_aftn_messages(
        self,
        message_type: str = "",
        keyword: str = "",
        date_from: str = "",
        date_to: str = "",
        limit: int = 100,
    ) -> dict:
        db = self.db
        if not db:
            return {"error": "database not available", "records": []}
        limit = min(max(limit, 1), 500)
        records = db.query_aftn_messages(
            message_type=message_type or None,
            keyword=keyword or None,
            date_from=date_from or None,
            date_to=date_to or None,
            limit=limit,
            offset=0,
        )
        total = db.count_aftn_messages(
            message_type=message_type or None,
            keyword=keyword or None,
            date_from=date_from or None,
            date_to=date_to or None,
        )
        return {"total": total, "records": records}

    def _get_current_tracks(self) -> dict:
        if self.fdr_store is None:
            return {"enabled": False, "tracks": [], "count": 0}
        stats = self.fdr_store.get_stats()
        tracks = self.fdr_store.get_tracks()
        return {
            "enabled": True,
            "count": len(tracks),
            "tracks": tracks[:200],  # 最多200条
            "stats": stats,
        }

    def _get_flight_trail(self, callsign: str, date: str = "") -> dict:
        if self.radar_history_store is None:
            return {"error": "radar history not available", "points": []}
        if not date:
            date = datetime.utcnow().strftime("%Y-%m-%d")
        ts_from = f"{date}T00:00:00.000Z"
        ts_to = f"{date}T23:59:59.000Z"
        pts = self.radar_history_store.query(ts_from, ts_to, callsign)
        return {"callsign": callsign, "date": date, "points": pts}

    def _search_asr_records(
        self,
        sector: str = "",
        date_from: str = "",
        date_to: str = "",
        callsign: str = "",
        limit: int = 100,
    ) -> dict:
        db = self.db
        if not db:
            return {"error": "database not available", "records": []}
        limit = min(max(limit, 1), 1000)
        records = db.query_asr_text(
            sector=sector or None,
            date_from=date_from or None,
            date_to=date_to or None,
            callsign=callsign or None,
            limit=limit,
            offset=0,
        )
        total = db.count_asr_text(
            sector=sector or None,
            date_from=date_from or None,
            date_to=date_to or None,
            callsign=callsign or None,
        )
        return {"total": total, "records": records}

    def _get_voice_status(self) -> dict:
        if self.voice_receiver is None:
            return {"enabled": False}
        # 获取各通道状态
        from .voice_receiver import SECTOR_CHANNELS
        channels = []
        for sector_code, channel in SECTOR_CHANNELS.items():
            ch_info = {
                "sector": sector_code,
                "channel": channel,
            }
            # 尝试获取当前能量
            try:
                eng = getattr(self.voice_receiver, "_current_energy", {}).get(channel, 0)
                vad = getattr(self.voice_receiver, "_vad_state", {}).get(channel, False)
                ch_info["energy"] = round(eng, 4)
                ch_info["active"] = bool(vad)
            except Exception:
                ch_info["energy"] = 0
                ch_info["active"] = False
            channels.append(ch_info)
        return {"enabled": True, "channels": channels}

    def _get_asr_status(self) -> dict:
        if self.asr_receiver is None:
            return {"enabled": False}
        latest = self.asr_receiver.get_latest_asr_all()
        stats = self.asr_receiver.get_stats()
        return {
            "enabled": True,
            "latest_records": latest,
            "stats": stats,
        }

    def _get_terminal_config(self) -> dict:
        try:
            from .terminal_area import get_terminal_config_safe
            return get_terminal_config_safe()
        except Exception as exc:
            return {"error": str(exc)}


# ── 便捷启动函数 ───────────────────────────────────────────

def start_mcp_server(
    db: Any,
    fdr_store: Any = None,
    radar_history_store: Any = None,
    voice_receiver: Any = None,
    asr_receiver: Any = None,
    config: Any = None,
    host: str = "127.0.0.1",
    port: int = 18766,
) -> MCPServer:
    """创建并启动 MCP Server（后台线程）"""
    server = MCPServer(host=host, port=port)
    server.register_data_sources(
        db=db,
        fdr_store=fdr_store,
        radar_history_store=radar_history_store,
        voice_receiver=voice_receiver,
        asr_receiver=asr_receiver,
        config=config,
    )
    server.start()
    return server
