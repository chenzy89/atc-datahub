"""AFTN 电报解析器 — 适配 Python 3.8"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from .models import AftnMessage, FlightPlan

_UTC_PLUS_8 = timezone(timedelta(hours=8))
SUPPORTED_TYPES = {"FPL", "DEP", "ARR", "DLA"}


def _utc_to_beijing(utc_dt: datetime) -> datetime:
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(_UTC_PLUS_8).replace(tzinfo=None)


def _beijing_date_from_utc(utc_dt: datetime) -> date:
    return _utc_to_beijing(utc_dt).date()


class AftnParseError(ValueError):
    pass


class AftnParseResult:
    def __init__(
        self,
        raw_text: str = "",
        message: Optional[AftnMessage] = None,
        action: str = "",
        flight_plan: Optional[FlightPlan] = None,
        accepted: bool = False,
        errors: list[str] | None = None,
    ):
        self.raw_text = raw_text
        self.message = message or AftnMessage()
        self.action = action
        self.flight_plan = flight_plan
        self.accepted = accepted
        self.errors = errors or []


class AftnParser:
    """AFTN 报文解析器"""

    def parse(
        self,
        payload: bytes | str | dict[str, Any],
        received_at: Optional[datetime] = None,
    ) -> AftnParseResult:
        received_at = received_at or datetime.utcnow()
        wrapper = self._coerce_wrapper(payload)
        raw_text = wrapper.get("raw_text", "")
        message_time = self._parse_iso_time(wrapper.get("utc_time")) or received_at
        raw_type = str(wrapper.get("message_type", "")).strip().upper()
        core_text = self._extract_core_message(raw_text)
        detected_type = self._detect_message_type(core_text) or raw_type

        message = AftnMessage(
            raw_text=raw_text,
            message_type=detected_type,
            message_text=core_text or raw_text,
            utc_time=message_time,
            received_at=received_at,
        )

        result = AftnParseResult(
            raw_text=raw_text,
            message=message,
            action=detected_type,
        )

        if detected_type not in SUPPORTED_TYPES:
            result.errors.append(f"不支持的报文类型: {detected_type}")
            return result

        try:
            if detected_type == "FPL":
                plan = self._parse_fpl(core_text, message_time)
            elif detected_type == "DEP":
                plan = self._parse_dep(core_text, message_time)
            elif detected_type == "DLA":
                plan = self._parse_dla(core_text, message_time)
            elif detected_type == "ARR":
                plan = self._parse_arr(core_text, message_time)
            else:
                result.errors.append(f"不支持的报文类型: {detected_type}")
                return result

            plan.source_message_type = detected_type
            plan.last_message_time = message_time
            plan.raw_message_text = raw_text
            result.flight_plan = plan
            result.accepted = True
        except AftnParseError as exc:
            result.errors.append(str(exc))

        return result

    def _coerce_wrapper(self, payload: bytes | str | dict[str, Any]) -> dict[str, str]:
        if isinstance(payload, dict):
            return {
                "raw_text": str(payload.get("MessageText", payload.get("message_text", ""))),
                "message_type": str(payload.get("MessageType", payload.get("message_type", ""))),
                "utc_time": str(payload.get("UtcTime", payload.get("utc_time", ""))),
            }
        if isinstance(payload, bytes):
            text = payload.decode("utf-8", errors="replace")
        else:
            text = payload
        stripped = text.strip()
        if stripped.startswith("{") and "MessageText" in stripped:
            try:
                data = json.loads(stripped)
                return self._coerce_wrapper(data)
            except Exception:
                pass
        return {"raw_text": stripped, "message_type": "", "utc_time": ""}

    def _extract_core_message(self, text: str) -> str:
        if not text:
            return ""
        flat = text.replace("\r", "").replace("\n", "")
        start = flat.find("(")
        if start < 0:
            return flat.strip()
        end = flat.find(")", start)
        if end < 0:
            return flat[start:].strip()
        return flat[start: end + 1].strip()

    def _detect_message_type(self, text: str) -> str:
        if not text or not text.startswith("("):
            return ""
        prefix = text[1:4].upper()
        return prefix if prefix in SUPPORTED_TYPES else ""

    def _parse_fpl(self, core_text: str, message_time: datetime) -> FlightPlan:
        fields = self._split_fields(core_text)
        if len(fields) < 9:
            raise AftnParseError(f"FPL 报文段数不足: {len(fields)}")

        callsign, ssr = self._parse_callsign_and_ssr(fields[1])
        if not callsign:
            raise AftnParseError("FPL 缺少呼号")

        departure = fields[5].strip().upper()
        arrival = fields[7].strip().upper()
        route_field = fields[6].strip().upper()
        route = route_field.split(" ", 1)[1].strip() if " " in route_field else route_field
        eet_minutes = self._hhmm_to_minutes(arrival[4:8])

        base_day = _beijing_date_from_utc(message_time)
        etd_hhmm = departure[4:8]

        dof_utc_day: Optional[date] = None
        for field in fields:
            marker = field.upper().find("DOF/")
            if marker >= 0:
                digits = field[marker + 4: marker + 10]
                if len(digits) == 6 and digits.isdigit():
                    try:
                        dof_utc_day = datetime.strptime("20" + digits, "%Y%m%d").date()
                    except ValueError:
                        pass
                break

        if dof_utc_day is not None:
            etd_utc = self._combine_day_hhmm(dof_utc_day, etd_hhmm)
            dof = _beijing_date_from_utc(etd_utc)
        else:
            etd_hour = int(etd_hhmm[:2])
            etd_min = int(etd_hhmm[2:4])
            if etd_hour > 16 or (etd_hour == 16 and etd_min > 0):
                etd_utc = self._combine_day_hhmm(base_day - timedelta(days=1), etd_hhmm)
                dof = base_day
            else:
                etd_utc = self._combine_day_hhmm(base_day, etd_hhmm)
                dof = _beijing_date_from_utc(etd_utc)

        plan = FlightPlan(
            callsign=callsign,
            adep=departure[:4],
            adest=arrival[:4],
            ssr=ssr,
            aircraft_type=fields[3].strip().upper(),
            flight_rules=fields[2].strip().upper(),
            route=route,
            dof=dof,
            etd=etd_utc,
            eet_minutes=eet_minutes,
            eta=etd_utc + timedelta(minutes=eet_minutes) if etd_utc else None,
        )
        return plan

    def _parse_dep(self, core_text: str, message_time: datetime) -> FlightPlan:
        fields = self._split_fields(core_text)
        if len(fields) < 4:
            raise AftnParseError(f"DEP 报文段数不足: {len(fields)}")
        callsign, ssr = self._parse_callsign_and_ssr(fields[1])
        if not callsign:
            raise AftnParseError("DEP 缺少呼号")

        departure = fields[2].strip().upper()
        hhmm = departure[4:8]
        base_day = _beijing_date_from_utc(message_time)
        h, m = int(hhmm[:2]), int(hhmm[2:4])
        if h > 16 or (h == 16 and m > 0):
            time_utc = self._combine_day_hhmm(base_day - timedelta(days=1), hhmm)
        else:
            time_utc = self._combine_day_hhmm(base_day, hhmm)

        plan = FlightPlan(
            callsign=callsign,
            adep=departure[:4],
            adest=fields[3].strip().upper()[:4],
            ssr=ssr,
            dof=base_day,
            atd=time_utc,
        )
        return plan

    def _parse_dla(self, core_text: str, message_time: datetime) -> FlightPlan:
        fields = self._split_fields(core_text)
        if len(fields) < 4:
            raise AftnParseError(f"DLA 报文段数不足: {len(fields)}")
        callsign, ssr = self._parse_callsign_and_ssr(fields[1])
        if not callsign:
            raise AftnParseError("DLA 缺少呼号")

        departure = fields[2].strip().upper()
        hhmm = departure[4:8]
        base_day = _beijing_date_from_utc(message_time)
        h, m = int(hhmm[:2]), int(hhmm[2:4])
        if h > 16 or (h == 16 and m > 0):
            time_utc = self._combine_day_hhmm(base_day - timedelta(days=1), hhmm)
        else:
            time_utc = self._combine_day_hhmm(base_day, hhmm)

        plan = FlightPlan(
            callsign=callsign,
            adep=departure[:4],
            adest=fields[3].strip().upper()[:4],
            ssr=ssr,
            dof=base_day,
            etd=time_utc,
        )
        return plan

    def _parse_arr(self, core_text: str, message_time: datetime) -> FlightPlan:
        fields = self._split_fields(core_text)
        if len(fields) not in {4, 5}:
            raise AftnParseError(f"ARR 报文段数异常: {len(fields)}")
        callsign, _ = self._parse_callsign_and_ssr(fields[1])
        if not callsign:
            raise AftnParseError("ARR 缺少呼号")

        arrival = fields[-1].strip().upper()
        ata_hhmm = arrival[4:8]
        base_day = _beijing_date_from_utc(message_time)

        dof_utc_day: Optional[date] = None
        for field in fields:
            marker = field.upper().find("DOF/")
            if marker >= 0:
                digits = field[marker + 4: marker + 10]
                if len(digits) == 6 and digits.isdigit():
                    try:
                        dof_utc_day = datetime.strptime("20" + digits, "%Y%m%d").date()
                    except ValueError:
                        pass
                break

        if dof_utc_day is not None:
            ata_utc = self._combine_day_hhmm(dof_utc_day, ata_hhmm)
            dof = _beijing_date_from_utc(ata_utc)
        else:
            ata_hour = int(ata_hhmm[:2])
            ata_min = int(ata_hhmm[2:4])
            if ata_hour > 16 or (ata_hour == 16 and ata_min > 0):
                ata_utc = self._combine_day_hhmm(base_day - timedelta(days=1), ata_hhmm)
                dof = base_day
            else:
                ata_utc = self._combine_day_hhmm(base_day, ata_hhmm)
                dof = _beijing_date_from_utc(ata_utc)

        plan = FlightPlan(
            callsign=callsign,
            adep=fields[2].strip().upper()[:4] if len(fields) >= 4 else "",
            adest=arrival[:4],
            dof=dof,
            ata=ata_utc,
        )
        return plan

    def _split_fields(self, core_text: str) -> list[str]:
        if not core_text:
            raise AftnParseError("AFTN 报文为空")
        if not core_text.startswith("("):
            raise AftnParseError("AFTN 报文缺少起始括号")
        body = core_text[1:-1] if core_text.endswith(")") else core_text[1:]
        return [field.strip() for field in body.split("-")]

    @staticmethod
    def _parse_callsign_and_ssr(field: str) -> tuple[str, str]:
        text = field.strip().upper()
        if "/A" not in text:
            return text, ""
        callsign, suffix = text.split("/A", 1)
        suffix = suffix.strip()
        if not suffix:
            return callsign.strip(), ""
        return callsign.strip(), f"A{suffix[:4]}"

    @staticmethod
    def _combine_day_hhmm(day: date, hhmm: str) -> datetime:
        if len(hhmm) != 4 or not hhmm.isdigit():
            raise AftnParseError(f"时间字段非法: {hhmm!r}")
        return datetime(day.year, day.month, day.day, int(hhmm[:2]), int(hhmm[2:4]))

    @staticmethod
    def _hhmm_to_minutes(hhmm: str) -> int:
        if len(hhmm) != 4 or not hhmm.isdigit():
            raise AftnParseError(f"EET 字段非法: {hhmm!r}")
        return int(hhmm[:2]) * 60 + int(hhmm[2:4])

    @staticmethod
    def _parse_iso_time(value: str) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y%m%d%H%M%S"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
        return None
