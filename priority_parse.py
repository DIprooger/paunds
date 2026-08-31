import os
import json
import datetime
from typing import Dict, List, Set, Tuple, Any


def _load_json_env(name: str, default: str) -> Any:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return json.loads(default)
    return json.loads(raw)


def _hhmm_to_min(s: str) -> int:
    s = (s or "").strip()
    if s == "24:00":
        return 1440
    hh, mm = s.split(":")
    return int(hh) * 60 + int(mm)


def _in_window(dt: datetime.datetime, start: str, end: str) -> bool:
    # Ожидаем "обычные" окна start<=end (00:00..24:00), без "через полночь"
    a = _hhmm_to_min(start)
    b = _hhmm_to_min(end)
    now = dt.hour * 60 + dt.minute
    return a <= now < b


def _trading_mode_for(instr_code: str, dt: datetime.datetime, trading_cfg: dict) -> str:
    """
    Возвращает 'TRADE' или 'PAUSE' для инструмента по TRADING_SCHEDULE_JSON на текущий момент.
    Правило: берём ПЕРВОЕ совпавшее по days + time window.
    Если инструмента нет — используем DEFAULT.
    """
    day = int(dt.weekday())  # 0=Mon..6=Sun
    rules = trading_cfg.get(instr_code) or trading_cfg.get("DEFAULT") or []
    for r in rules:
        if day not in (r.get("days") or []):
            continue
        start = r.get("start", "00:00")
        end = r.get("end", "24:00")
        if _in_window(dt, start, end):
            return str(r.get("mode", "PAUSE")).upper()
    return "PAUSE"


def _signal_mode_for(instr_code: str, dt: datetime.datetime, strategy_cfg: dict) -> str:
    """
    Возвращает 'SIGNAL' или 'PAUSE' по SIGNAL_STRATEGY_JSON на текущий момент.
    Берём instruments[instr_code] иначе instruments['DEFAULT'].
    Учитываем start/end если есть (если нет — считаем весь день).
    """
    day_key = str(int(dt.weekday()))
    instruments = (strategy_cfg.get("instruments") or {})
    instr_cfg = instruments.get(instr_code) or instruments.get("DEFAULT") or {}
    week = instr_cfg.get("week") or {}
    dcfg = week.get(day_key) or {}

    mode = dcfg.get("mode") or ["PAUSE"]
    mode0 = str(mode[0]).upper() if mode else "PAUSE"
    if mode0 != "SIGNAL":
        return "PAUSE"

    # если start/end не заданы — SIGNAL весь день
    start = dcfg.get("start")
    end = dcfg.get("end")
    if not start or not end:
        return "SIGNAL"

    return "SIGNAL" if _in_window(dt, start, end) else "PAUSE"


def build_priority_lists(now_dt: datetime.datetime, instruments: List[Any]) -> Tuple[List[str], List[str], List[str]]:
    """
    instruments: список объектов INSTRUMENTS, у каждого ожидаем:
      - instr.code: str
      - instr.signals_enabled: bool (может отсутствовать -> False)
    """
    trading_cfg = _load_json_env("TRADING_SCHEDULE_JSON", '{"DEFAULT":[]}')
    strategy_cfg = _load_json_env(
        "SIGNAL_STRATEGY_JSON",
        '{"tf_priority":["30m","1h","5h"],"instruments":{"DEFAULT":{"week":{"0":{"mode":["PAUSE"]},"1":{"mode":["PAUSE"]},"2":{"mode":["PAUSE"]},"3":{"mode":["PAUSE"]},"4":{"mode":["PAUSE"]},"5":{"mode":["PAUSE"]},"6":{"mode":["PAUSE"]}}}}}'
    )

    # --- 1) trade: только те, кто сегодня TRADE по TRADING_SCHEDULE ---
    trade: List[str] = []
    trade_set: Set[str] = set()

    # --- 2) сигнал: signals_enabled=True и сегодня SIGNAL по SIGNAL_STRATEGY ---
    signal: List[str] = []
    signal_set: Set[str] = set()

    # --- 3) maybe_close_only: signals_enabled=True, но НЕ в сигнал ---
    maybe_close_only: List[str] = []

    # Сначала считаем trade и предварительные статусы
    for instr in instruments:
        code = getattr(instr, "code", None)
        if not code:
            continue

        tmode = _trading_mode_for(code, now_dt, trading_cfg)  # TRADE/PAUSE
        s_enabled = bool(getattr(instr, "signals_enabled", False))

        if tmode == "TRADE":
            trade.append(code)
            trade_set.add(code)

    # Затем считаем signal и maybe_close_only
    # (signal строим по твоему требованию: из signals_enabled=True + SIGNAL_STRATEGY day=SIGNAL)
    for instr in instruments:
        code = getattr(instr, "code", None)
        if not code:
            continue

        s_enabled = bool(getattr(instr, "signals_enabled", False))
        if not s_enabled:
            continue

        smode = _signal_mode_for(code, now_dt, strategy_cfg)  # SIGNAL/PAUSE

        if smode == "SIGNAL":
            signal.append(code)
            signal_set.add(code)


    # maybe_close_only: signals_enabled=True минус signal
    for instr in instruments:
        code = getattr(instr, "code", None)
        if not code:
            continue

        s_enabled = bool(getattr(instr, "signals_enabled", False))
        if not s_enabled:
            continue

        if code not in signal_set:
            maybe_close_only.append(code)

    return trade, signal, maybe_close_only
