# trade_time.py
import json
import os
import logging
from dataclasses import dataclass
from datetime import datetime, time
from typing import Literal, Optional

from .time_utils import _now_tz

logger = logging.getLogger(__name__)

TradeMode = Literal["TRADE", "CLOSE_ONLY", "PAUSE"]
SignalLogic = Literal["DIRECT", "INVERSE"]
SignalTimeframe = Literal["30m", "1h", "5h", "1d", "1w", "1mo"]

@dataclass(frozen=True)
class ScheduleRule:
    days: set[int]
    mode: TradeMode
    start: time
    end: time
    signals_enabled: Optional[bool] = None
    logic: Optional[SignalLogic] = None
    timeframe_key: Optional[SignalTimeframe] = None

@dataclass(frozen=True)
class TradeContext:
    mode: TradeMode
    signals_enabled: bool
    logic: SignalLogic
    timeframe_key: SignalTimeframe

def _normalize_timeframe(v: object) -> Optional[SignalTimeframe]:
    if v is None:
        return None
    s = str(v).strip().lower()

    aliases = {
        "30": "30m", "30m": "30m", "30min": "30m", "m30": "30m",
        "1": "1h", "1h": "1h", "60": "1h", "h1": "1h",
        "5": "5h", "5h": "5h", "h5": "5h",
        "24": "1d", "1d": "1d", "d1": "1d", "daily": "1d",
        "168": "1w", "1w": "1w", "w1": "1w", "weekly": "1w",
        "720": "1mo", "1mo": "1mo", "mo1": "1mo", "monthly": "1mo",
    }
    out = aliases.get(s)
    return out  # type: ignore[return-value]

def _parse_hhmm(s: str) -> time:
    # допускаем "24:00" как "конец дня"
    s = str(s).strip()
    if s == "24:00":
        return time(23, 59, 59)
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


def _time_in_range(t: time, start: time, end: time) -> bool:
    """Поддержка окон:
      - обычных: start <= t <= end
      - ночных (через полночь): start > end
    """
    if start <= end:
        return start <= t <= end
    # окно через полночь
    return t >= start or t <= end


def _normalize_logic(v: object) -> Optional[SignalLogic]:
    if v is None:
        return None
    s = str(v).strip().upper()
    if s in ("DIRECT", "COUNTER", "COUNTER_SIGNAL", "COUNTER-SIGNAL"):
        return "DIRECT"
    if s in ("INVERSE", "SIGNAL", "REVERSE", "INV"):
        return "INVERSE"
    # неизвестное — считаем ошибкой, но не падаем
    return None


def _normalize_signals(v: object) -> Optional[bool]:
    """Поддерживает:
    - true/false
    - "ON"/"OFF"
    - "TRUE"/"FALSE"
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return None


def _rules_from_env() -> dict[str, list[ScheduleRule]]:
    """ENV:
      TRADING_SCHEDULE_JSON='{
        "DEFAULT": [
          {"days":[0,1,2,3,4], "mode":"TRADE", "start":"00:00", "end":"24:00"},
          {"days":[5,6], "mode":"PAUSE", "start":"00:00", "end":"24:00"}
        ],
        "GBPUSD":[
          {"days":[0,1,4], "mode":"TRADE", "start":"00:00", "end":"24:00", "signals": true, "logic":"COUNTER"},
          {"days":[2,3], "mode":"CLOSE_ONLY", "start":"00:00", "end":"24:00", "signals": true, "logic":"COUNTER"}
        ]
      }'

    Доп. поля правила (опционально):
      - signals: true/false (или "ON"/"OFF")
      - logic:   DIRECT/COUNTER/COUNTER_SIGNAL  или  INVERSE/SIGNAL/REVERSE
    """
    raw = os.getenv("TRADING_SCHEDULE_JSON", "").strip()
    if not raw:
        return {}

    try:
        data = json.loads(raw)
    except Exception:
        logger.exception("TRADING_SCHEDULE_JSON: не удалось распарсить JSON, игнорирую")
        return {}

    out: dict[str, list[ScheduleRule]] = {}

    for key, rules in (data or {}).items():
        parsed: list[ScheduleRule] = []
        for r in (rules or []):
            try:
                days = set(int(x) for x in r["days"])
                mode = str(r["mode"]).upper()
                if mode not in ("TRADE", "CLOSE_ONLY", "PAUSE"):
                    raise ValueError(f"bad mode={mode}")

                start = _parse_hhmm(r.get("start", "00:00"))
                end = _parse_hhmm(r.get("end", "24:00"))

                signals_raw = r.get("signals", None)
                if signals_raw is None:
                    signals_raw = r.get("signal", None)
                signals = _normalize_signals(signals_raw)

                logic = _normalize_logic(r.get("logic", None))

                tf_raw = r.get("timeframe", None)
                if tf_raw is None:
                    tf_raw = r.get("tf", None)
                if tf_raw is None:
                    tf_raw = r.get("time", None)  # NEW
                tf_key = _normalize_timeframe(tf_raw)

                parsed.append(
                    ScheduleRule(
                        days=days,
                        mode=mode,  # type: ignore[arg-type]
                        start=start,
                        end=end,
                        signals_enabled=signals,
                        logic=logic,
                        timeframe_key=tf_key,
                    )
                )
            except Exception:
                logger.exception("Плохое правило в TRADING_SCHEDULE_JSON для key=%r: %r", key, r)

        out[str(key)] = parsed
    return out


def _default_rules() -> dict[str, list[ScheduleRule]]:
    # Дефолт под текущее поведение:
    # - всё (DEFAULT) работает в будни (TRADE), в выходные PAUSE
    # - BITCOIN и SOLBTC работают 7/7
    return {
        "DEFAULT": [
            ScheduleRule(days={0, 1, 2, 3, 4}, mode="TRADE", start=time(0, 0), end=time(23, 59, 59)),
            ScheduleRule(days={5, 6}, mode="PAUSE", start=time(0, 0), end=time(23, 59, 59)),
        ],
        "BITCOIN": [
            ScheduleRule(days={0, 1, 2, 3, 4, 5, 6}, mode="TRADE", start=time(0, 0), end=time(23, 59, 59)),
        ],
        "SOLBTC": [
            ScheduleRule(days={0, 1, 2, 3, 4, 5, 6}, mode="TRADE", start=time(0, 0), end=time(23, 59, 59)),
        ],
    }


def get_trade_context(instr_name: str, sheet_name: str, tz_name: str, dt: Optional[datetime] = None) -> TradeContext:
    """Возвращает расширенный контекст расписания для инструмента.

    Правило выбора ключа расписания:
      1) instr_name (например "GBPUSD")
      2) sheet_name (обычно совпадает с code)
      3) DEFAULT

    Defaults для совместимости:
      - signals_enabled = True
      - logic = DIRECT (COUNTER)
    """
    dt = dt or _now_tz(tz_name)
    wd = dt.weekday()
    tm = dt.timetz().replace(tzinfo=None)

    rules_env = _rules_from_env()
    rules_base = _default_rules()

    def _pick_rules(key: str) -> Optional[list[ScheduleRule]]:
        if key in rules_env and rules_env[key]:
            return rules_env[key]
        if key in rules_base and rules_base[key]:
            return rules_base[key]
        return None

    rules = (
        _pick_rules(instr_name)
        or _pick_rules(sheet_name)
        or _pick_rules("DEFAULT")
        or []
    )

    default_tf = os.getenv("DEFAULT_SIGNAL_TIMEFRAME", "30m").strip().lower()
    default_tf_key = _normalize_timeframe(default_tf) or "30m"  # fallback

    for r in rules:
        if wd in r.days and _time_in_range(tm, r.start, r.end):
            return TradeContext(
                mode=r.mode,
                signals_enabled=True if r.signals_enabled is None else bool(r.signals_enabled),
                logic="DIRECT" if r.logic is None else r.logic,
                timeframe_key=(r.timeframe_key or default_tf_key),  # NEW
            )

    return TradeContext(mode="PAUSE", signals_enabled=False, logic="DIRECT", timeframe_key=default_tf_key)


def get_trade_mode(instr_name: str, sheet_name: str, tz_name: str, dt: Optional[datetime] = None) -> TradeMode:
    """Backwards compatible: возвращает только mode."""
    return get_trade_context(instr_name=instr_name, sheet_name=sheet_name, tz_name=tz_name, dt=dt).mode