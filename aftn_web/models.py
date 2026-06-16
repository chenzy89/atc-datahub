"""数据模型"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass
class AftnMessage:
    """原始 AFTN 报文记录"""

    id: int = 0
    raw_text: str = ""
    message_type: str = ""
    message_text: str = ""
    utc_time: datetime | None = None
    received_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "raw_text": self.raw_text,
            "message_type": self.message_type,
            "message_text": self.message_text,
            "utc_time": self.utc_time.isoformat() if self.utc_time else None,
            "received_at": self.received_at.isoformat() if self.received_at else None,
        }


@dataclass
class FlightPlan:
    """飞行计划"""

    id: int = 0
    callsign: str = ""
    ssr: str = ""
    aircraft_type: str = ""
    dof: date | None = None
    adep: str = ""
    etd: datetime | None = None
    atd: datetime | None = None
    adest: str = ""
    eta: datetime | None = None
    ata: datetime | None = None
    runway: str = ""          # 使用跑道（来自雷达 CAT062）
    flight_procedure: str = "" # 飞行程序 SID/STAR（来自雷达 CAT062）
    entry_time: str = ""       # 进终端区时间（UTC ISO格式，来自雷达轨迹）
    exit_time: str = ""        # 出终端区时间（UTC ISO格式，来自雷达轨迹）
    terminal_flight_time: int = 0  # 终端飞行时间（秒）
    route: str = ""
    handover_pt: str = ""   # 移交点，从航路自动解析
    source_message_type: str = ""
    last_message_time: datetime | None = None
    raw_message_text: str = ""
    flight_rule: str = ""   # 编组8，如 IS/IN/IG/IM/IX/IB
    message_types: str = ""  # 所有收到的报文种类，逗号分隔，如 FPL,DEP,ARR
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "callsign": self.callsign,
            "ssr": self.ssr,
            "aircraft_type": self.aircraft_type,
            "dof": self.dof.isoformat() if self.dof else None,
            "adep": self.adep,
            "etd": self.etd.isoformat() if self.etd else None,
            "atd": self.atd.isoformat() if self.atd else None,
            "adest": self.adest,
            "eta": self.eta.isoformat() if self.eta else None,
            "ata": self.ata.isoformat() if self.ata else None,
            "runway": self.runway,
            "flight_procedure": self.flight_procedure,
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "terminal_flight_time": self.terminal_flight_time,
            "route": self.route,
            "handover_pt": self.handover_pt,
            "source_message_type": self.source_message_type,
            "last_message_time": self.last_message_time.isoformat() if self.last_message_time else None,
            "raw_message_text": self.raw_message_text,
            "flight_rule": self.flight_rule,
            "message_types": self.message_types,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass
class FlightTrack:
    """航迹记录"""
    id: int = 0
    callsign: str = ""
    track_type: str = ""       # ARRIVAL 或 DEPARTURE
    adep: str = ""
    adest: str = ""
    dof: str = ""              # YYYY-MM-DD
    points_json: str = ""      # JSON 序列化的航迹点列表
    start_time: str = ""       # 首点时间
    end_time: str = ""         # 末点时间
    created_at: str = ""

    def get_points(self) -> list[dict]:
        import json
        try:
            return json.loads(self.points_json)
        except (json.JSONDecodeError, TypeError):
            return []
