"""AFTN 电报解析器 — 适配 Python 3.8"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Optional

from .models import AftnMessage, FlightPlan

PARSED_TYPES = {"FPL", "DEP", "ARR", "DLA", "CNL", "CHG"}
LABEL_ONLY_TYPES = {"AOC", "ACP", "TOC", "EST", "HQ", "METAR"}
SKIP_TYPES = {"LAM"}
RECOGNIZED_TYPES = PARSED_TYPES | LABEL_ONLY_TYPES | SKIP_TYPES


# RDX4位数字+空格+6位时间+空格+FF/GG → 子报文起始标记
_RE_MULTI_MSG = re.compile(r'(?=RDX\d{4} \d{6} (?:FF|GG) )')


def split_multi_aftn(raw_text: str) -> list[str]:
    """将可能拼接了多份 AFTN 报文的 raw_text 拆分为单条报文列表。
    依据：每条 AFTN 报以 RDX+4位流水号 + 时间 + FF/GG 开头。
    """
    if not raw_text:
        return []
    parts = _RE_MULTI_MSG.split(raw_text.strip())
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) <= 1:
        return parts  # 单条报文
    return parts


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

        # 特殊类型判断依据：发报地址
        if detected_type not in ("HQ", "METAR"):
            sender = _extract_sender(raw_text)
            if sender in ("ZBBBZGZX", "ZBBBOGXX"):
                detected_type = "HQ"
            elif sender in ("ZGSDYMYX", "ZGSZYMYX"):
                detected_type = "METAR"

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

        if detected_type not in RECOGNIZED_TYPES:
            result.errors.append(f"不支持的报文类型: {detected_type}")
            return result

        if detected_type in LABEL_ONLY_TYPES:
            # 只标注类型，不解析飞行计划
            return result

        if detected_type in SKIP_TYPES:
            # 标注类型但不记录（__main__ 层跳过 DB 保存）
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
            elif detected_type == "CNL":
                plan = self._parse_cnl(core_text, message_time)
            elif detected_type == "CHG":
                plan = self._parse_chg(core_text, message_time)
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
                "raw_text": str(payload.get("MessageText", payload.get("message_text", payload.get("raw_text", "")))),
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
        """提取括号后的完整类型词，遇到 - / 空格 / ) 截断"""
        if not text or not text.startswith("("):
            return ""
        end = 1
        while end < len(text) and text[end] not in ("-", " ", ")"):
            end += 1
        prefix = text[1:end].upper()
        return prefix if prefix in RECOGNIZED_TYPES else ""

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

        base_day = message_time.date()
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
            dof = dof_utc_day
        else:
            etd_utc = self._combine_day_hhmm(base_day, etd_hhmm)
            if etd_utc < message_time:
                etd_utc = self._combine_day_hhmm(base_day + timedelta(days=1), etd_hhmm)
            dof = etd_utc.date()

        # 提取规则与种类 — 编组8，位于航班号(callsign)与机型之间，
        # 如 IS/IN/IG/IM/IX/IB/VS/VN/VG/VX，长度为2
        flight_rule = ""
        if len(fields) > 2:
            f2 = fields[2].strip().upper()
            if f2 and len(f2) <= 2:
                flight_rule = f2

        plan = FlightPlan(
            callsign=callsign,
            adep=departure[:4],
            adest=arrival[:4],
            ssr=ssr,
            aircraft_type=fields[3].strip().upper(),
            route=route,
            dof=dof,
            etd=etd_utc,
            eta=etd_utc + timedelta(minutes=eet_minutes) if etd_utc else None,
            flight_rule=flight_rule,
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
        base_day = message_time.date()

        # 提取 DOF 字段
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
            dof = dof_utc_day
        else:
            dof = base_day

        # ATD 始终基于收报日期（base_day），不等于 DOF
        time_utc = self._combine_day_hhmm(base_day, hhmm)
        if time_utc > message_time:
            time_utc = self._combine_day_hhmm(base_day - timedelta(days=1), hhmm)

        plan = FlightPlan(
            callsign=callsign,
            adep=departure[:4],
            adest=fields[3].strip().upper()[:4],
            ssr=ssr,
            dof=dof,
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
        base_day = message_time.date()

        # 提取 DOF 字段
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
            dof = dof_utc_day
        else:
            dof = base_day

        # ETD 始终基于收报日期（base_day），不等于 DOF
        time_utc = self._combine_day_hhmm(base_day, hhmm)
        if time_utc < message_time:
            time_utc = self._combine_day_hhmm(base_day + timedelta(days=1), hhmm)

        plan = FlightPlan(
            callsign=callsign,
            adep=departure[:4],
            adest=fields[3].strip().upper()[:4],
            ssr=ssr,
            dof=dof,
            etd=time_utc,
        )
        return plan

    def _parse_chg(self, core_text: str, message_time: datetime) -> FlightPlan:
        fields = self._split_fields(core_text)
        if len(fields) < 5:
            raise AftnParseError(f"CHG 报文段数不足: {len(fields)}")
        callsign, ssr = self._parse_callsign_and_ssr(fields[1])
        if not callsign:
            raise AftnParseError("CHG 缺少呼号")

        departure = fields[2].strip().upper()
        adest = fields[3].strip().upper()[:4]

        # 提取 DOF（同 DLA 逻辑）
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

        if dof_utc_day is None:
            raise AftnParseError("CHG 缺少 DOF")

        # 提取编组 15（航路），去掉打头的速度/巡航高度（如 K0780S0950）
        route: str = ""
        for field in fields:
            if field.startswith("15/"):
                raw = field[3:].strip()
                # 去掉第一个空格前的速度/高度信息
                route = raw.split(" ", 1)[1].strip() if " " in raw else ""
                break

        plan = FlightPlan(
            callsign=callsign,
            adep=departure[:4],
            adest=adest,
            ssr=ssr or "",
            dof=dof_utc_day,
            route=route,
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
        base_day = message_time.date()

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
            dof = dof_utc_day
        else:
            dof = base_day

        # ATA 始终基于收报日期（base_day），不等于 DOF
        ata_utc = self._combine_day_hhmm(base_day, ata_hhmm)
        if ata_utc > message_time:
            ata_utc = self._combine_day_hhmm(base_day - timedelta(days=1), ata_hhmm)

        plan = FlightPlan(
            callsign=callsign,
            adep=fields[2].strip().upper()[:4] if len(fields) >= 4 else "",
            adest=arrival[:4],
            dof=dof,
            ata=ata_utc,
        )
        return plan

    def _parse_cnl(self, core_text: str, message_time: datetime) -> FlightPlan:
        """CNL 取消报：提取航班号、起降机场（无 SSR 字段），数据库层按 key 删除。"""
        fields = self._split_fields(core_text)
        if len(fields) < 4:
            raise AftnParseError(f"CNL 报文段数不足: {len(fields)}")
        callsign_raw = fields[1].strip().upper()
        if not callsign_raw:
            raise AftnParseError("CNL 缺少呼号")
        # 去掉 /A 后缀（如有）
        callsign = callsign_raw.split("/A")[0].strip()
        departure = fields[2].strip().upper()
        arrival = fields[3].strip().upper()
        return FlightPlan(
            callsign=callsign,
            ssr="",
            adep=departure[:4] if len(departure) >= 4 else departure,
            adest=arrival[:4] if len(arrival) >= 4 else arrival,
        )


    def _parse_est(self, core_text: str, message_time: datetime) -> FlightPlan:
        """EST 延误报：出港 EST 更新 ETD（进港暂不处理）。
        格式：(EST-callsign-adepHHMM-adest)
        - adepHHMM：起飞地+预计起飞时间
        """
        fields = self._split_fields(core_text)
        if len(fields) < 4:
            raise AftnParseError(f"EST 报文段数不足: {len(fields)}")
        callsign_raw = fields[1].strip().upper()
        if not callsign_raw:
            raise AftnParseError("EST 缺少呼号")
        callsign = callsign_raw.split("/A")[0].strip()
        # fields[2]=ZGOW0930，拆出机场+时间
        f2 = fields[2].strip().upper()
        adep = f2[:4]
        hhmm = f2[4:8] if len(f2) >= 8 else "0000"
        if not hhmm.isdigit():
            raise AftnParseError(f"EST 时间字段非法: {hhmm!r}")
        adest = fields[3].strip().upper()[:4]
        base_day = _beijing_date_from_utc(message_time)
        etd_utc = self._combine_day_hhmm(base_day, hhmm)
        return FlightPlan(
            callsign=callsign,
            ssr="",
            aircraft_type="",
            dof=base_day,
            adep=adep,
            etd=etd_utc,
            atd=None,
            adest=adest,
            eta=None,
            ata=None,
            route="",
            source_message_type="EST",
            last_message_time=message_time,
            raw_message_text=core_text,
        )
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
