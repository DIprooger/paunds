import json
import logging
import os

from .config import INSTRUMENTS
logger = logging.getLogger(__name__)

def _load_signal_strategy() -> dict:
    raw = os.getenv("SIGNAL_STRATEGY_JSON", "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        logger.exception("Bad SIGNAL_STRATEGY_JSON")
        return {}

_SIGNAL_STRATEGY_CACHE = None

def _get_signal_strategy() -> dict:
    global _SIGNAL_STRATEGY_CACHE
    # если хочешь горячую перезагрузку env на лету — убери кеш
    if _SIGNAL_STRATEGY_CACHE is None:
        _SIGNAL_STRATEGY_CACHE = _load_signal_strategy()
    return _SIGNAL_STRATEGY_CACHE


def _hhmm_to_min(s: str) -> int:
    s = (s or "").strip()
    if s == "24:00":
        return 1440
    hh, mm = s.split(":")
    return int(hh) * 60 + int(mm)

def _in_window(dt, start: str, end: str) -> bool:
    try:
        a = _hhmm_to_min(start)
        b = _hhmm_to_min(end)
        now = dt.hour * 60 + dt.minute
        # ожидаем "обычные" окна 00:00..24:00; через полночь не поддерживаем
        return a <= now < b
    except Exception:
        return False

def _instrument_mode_for_slot(instr_code: str, slot_dt) -> str:
    """
    Возвращает 'SIGNAL' или 'PAUSE' для инструмента на текущий слот.
    instr_code: например 'S&P500'
    """
    cfg = _get_signal_strategy()
    instruments = (cfg.get("instruments") or {})
    instr_cfg = instruments.get(instr_code) or instruments.get("DEFAULT") or {}
    week = (instr_cfg.get("week") or {})
    day_cfg = week.get(str(int(slot_dt.weekday()))) or {}

    mode = day_cfg.get("mode") or ["PAUSE"]
    mode0 = (mode[0] if mode else "PAUSE")
    mode0 = str(mode0).upper()

    if mode0 != "SIGNAL":
        return "PAUSE"

    # если start/end нет — считаем что SIGNAL весь день
    start = day_cfg.get("start", "00:00")
    end = day_cfg.get("end", "24:00")

    return "SIGNAL" if _in_window(slot_dt, start, end) else "PAUSE"


def _should_prepare_for_slot_new(slot_dt) -> bool:
    """
    Решаем: надо ли делать pre-slot /prepare.
    Логика: если ХОТЯ БЫ ОДИН инструмент в SIGNAL на этот слот -> prepare нужен.
    """
    for instr in INSTRUMENTS:
        if _instrument_mode_for_slot(instr.code, slot_dt) == "SIGNAL":
            return True
    return False