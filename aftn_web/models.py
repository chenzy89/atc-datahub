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
    """解析后的飞行计划（由 AFTN 报文更新/合并）"""

    id: int = 0
    callsign: str = ""
    adep: str = ""
    adest: str = ""
    ssr: str = ""
    aircraft_type: str = ""
    flight_rules: str = ""
    route: str = ""
    dof: date | None = None
    etd: datetime | None = None
    eet_minutes: int = 0
    atd: datetime | None = None
    eta: datetime | None = None
    ata: datetime | None = None
    source_message_type: str = ""
    last_message_time: datetime | None = None
    raw_message_text: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "callsign": self.callsign,
            "adep": self.adep,
            "adest": self.adest,
            "ssr": self.ssr,
            "aircraft_type": self.aircraft_type,
            "flight_rules": self.flight_rules,
            "route": self.route,
            "dof": self.dof.isoformat() if self.dof else None,
            "etd": self.etd.isoformat() if self.etd else None,
            "eet_minutes": self.eet_minutes,
            "atd": self.atd.isoformat() if self.atd else None,
            "eta": self.eta.isoformat() if self.eta else None,
            "ata": self.ata.isoformat() if self.ata else None,
            "source_message_type": self.source_message_type,
            "last_message_time": self.last_message_time.isoformat() if self.last_message_time else None,
            "raw_message_text": self.raw_message_text,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
