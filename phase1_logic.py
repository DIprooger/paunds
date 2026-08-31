# phase1_logic.py
import os
import json
import time
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .plus500_client import plus500_close_if_enabled, plus500_open_if_enabled
from .telegram_client import send_telegram_messages, send_telegram_to_error_chat
from .models import InstrumentResult
from .config import InstrumentConfig
from .db_client import (
    insert_instrument_data,
    insert_strong_signal,
    get_last_strong_signal_before,
    get_active_position_for_tf,
    set_position_state_tf,
    close_position_by_id,
    handoff_position_to_tf,
    has_position_today, swap_handoff_positions,  # <-- NEW
    get_instrument_trade_settings,
    get_allowed_strategy_rules_by_tf,
    get_strategy_cycle_state,
    set_strategy_waiting_transition,
    advance_strategy_cycle,
    reset_strategy_cycle,
)

logger = logging.getLogger(__name__)

def _trade_settings_for_open(code: str) -> dict:
    """
    Возвращает настройки количества покупки для открытия позиции.

    Источник: instruments.trade_amount в parser-service БД.
    Если значение не задано — вернём пустой dict, тогда Plus500 worker
    продолжит использовать свой fallback getAmountByPair().
    """
    try:
        data = get_instrument_trade_settings(code)
    except Exception:
        logger.exception("PHASE1 %s: failed to read trade settings", code)
        return {}

    if not data:
        return {}

    return {
        "trade_amount": data.get("trade_amount_raw") or data.get("trade_amount"),
        "trade_amount_currency": data.get("trade_amount_currency") or "",
        "trade_amount_unit": data.get("trade_amount_unit") or "",
    }



_STRONG = {"STRONG BUY", "STRONG SELL"}
_TF_PRIORITY = ["30m", "1h", "5h"]


# ----------------------------
# Helpers
# ----------------------------
PHASE1_NOTIFY = os.getenv("PHASE1_NOTIFY", "0").strip() == "1"
PHASE1_NOTIFY_MODE = os.getenv("PHASE1_NOTIFY_MODE", "BATCH").strip().upper()
# BATCH = одно сообщение на инструмент за запуск
# STREAM = каждое событие отдельным сообщением

def _rule_human(rule: str) -> str:
    r = (rule or "").upper()
    if r == "BUY_ON_STRONG_SELL":
        return "Buy-on-StrongSell"
    if r == "SELL_ON_STRONG_BUY":
        return "Sell-on-StrongBuy"
    return r or "-"

def _state_human(state: str) -> str:
    s = (state or "").upper()
    if s == "LONG":
        return "LONG"
    if s == "SHORT":
        return "SHORT"
    return s or "-"

def _tf_human(tf: str) -> str:
    tf = str(tf or "")
    # выравнивание в одну колонку
    return f"{tf:<3}"

def _tg_send(text: str) -> None:
    try:
        send_telegram_to_error_chat(text)
    except Exception:
        logger.exception("PHASE1: Telegram send failed")

def _ev_prefix(code: str, executed_at: str, price: float) -> str:
    return f"📌 PHASE1 {code} | {executed_at} | price={price}"

def _fmt_kv(**kwargs) -> str:
    # короткий formatter key=value
    parts = []
    for k, v in kwargs.items():
        if v is None or v == "":
            continue
        parts.append(f"{k}={v}")
    return " ".join(parts)

def _norm_tf(tf: str) -> str:
    return str(tf or "").strip().lower()


def _tf_rank(tf: str) -> int:
    tf = _norm_tf(tf)
    try:
        return _TF_PRIORITY.index(tf)
    except ValueError:
        return 999


def _fmt_pos(pos: Optional[Dict[str, Any]]) -> str:
    if not pos:
        return "-"
    return (
        f"id={pos.get('id')} state={pos.get('state')} "
        f"tf={pos.get('position_tf')} owner={pos.get('owner_tf')} "
        f"rule={pos.get('entry_rule')} num={pos.get('position_num')}"
    )


def _close_on_for_rule(rule: Optional[str]) -> Optional[str]:
    r = (rule or "").upper().strip()
    if not r:
        return None
    # BUY_ON_STRONG_SELL closes on STRONG BUY
    if r.endswith("_ON_STRONG_SELL"):
        return "STRONG BUY"
    # SELL_ON_STRONG_BUY closes on STRONG SELL
    if r.endswith("_ON_STRONG_BUY"):
        return "STRONG SELL"
    return None


def _state_from_action(action: str) -> str:
    return "LONG" if str(action).upper() == "BUY" else "SHORT"


from typing import Dict, Optional, Tuple, Any

Desired = Dict[str, Any]  # {'action': 'BUY'|'SELL', 'rule': '...', ...}

def _action_from_state(state: str) -> Optional[str]:
    s = (state or "").upper()
    if s == "LONG":
        return "BUY"
    if s == "SHORT":
        return "SELL"
    return None

def pick_handoff_target(
    slot_tf: str,
    pos_state: str,
    desired_all: Dict[str, Desired],
    active_by_position_tf: Dict[str, Optional[dict]],
    active_by_owner_tf: Dict[str, Optional[dict]],
    consumed: Dict[str, bool],
    slot_order: Tuple[str, ...] = ("30m", "1h", "5h"),
) -> Optional[Tuple[str, Desired]]:
    """
    Возвращает (target_tf, target_desired) если:
      - мы закрываем позицию в slot_tf
      - есть другой tf, который в этом же шаге хочет ТО ЖЕ действие BUY/SELL
      - у target_tf нет активной позиции (ни как slot, ни как owner)
      - target_tf ещё не consumed
    """
    current_action = _action_from_state(pos_state)
    if not current_action:
        return None

    # Детерминированный выбор: по slot_order
    for tf in slot_order:
        if tf == slot_tf:
            continue
        if consumed.get(tf):
            continue

        des = desired_all.get(tf)
        if not des:
            continue

        if (des.get("action") or "").upper() != current_action:
            continue

        # не создаём дубль: если уже есть активная позиция на этом tf
        if active_by_position_tf.get(tf) or active_by_owner_tf.get(tf):
            continue

        return tf, des

    return None


def _entry_from_transition(types: List[str], prev: str, curr: str) -> Optional[Tuple[str, str]]:
    """
    Первый вход в день: только по flip.
    Поддерживаем BUY-правила тоже (как у тебя в логике дня):
      STRONG SELL -> STRONG BUY: BUY_ON_STRONG_BUY => BUY
      STRONG BUY  -> STRONG SELL: BUY_ON_STRONG_SELL => BUY

    (SELL-правила оставляем для совместимости)
    """
    prev = (prev or "").upper().strip()
    curr = (curr or "").upper().strip()
    tset = {str(x).upper().strip() for x in (types or [])}

    # flip: BUY -> SELL
    if prev == "STRONG BUY" and curr == "STRONG SELL":
        if "BUY_ON_STRONG_SELL" in tset:
            return "BUY", "BUY_ON_STRONG_SELL"
        if "SELL_ON_STRONG_SELL" in tset:
            return "SELL", "SELL_ON_STRONG_SELL"
        return None

    # flip: SELL -> BUY
    if prev == "STRONG SELL" and curr == "STRONG BUY":
        if "BUY_ON_STRONG_BUY" in tset:
            return "BUY", "BUY_ON_STRONG_BUY"
        if "SELL_ON_STRONG_BUY" in tset:
            return "SELL", "SELL_ON_STRONG_BUY"
        return None

    return None


from typing import List, Optional, Tuple

def _entry_from_curr(types: List[str], curr: str) -> Optional[Tuple[str, str]]:
    """
    После первого входа в день: работаем от текущего STRONG (без flip).

      STRONG BUY  -> BUY_ON_STRONG_BUY   => BUY
      STRONG SELL -> BUY_ON_STRONG_SELL  => BUY

      STRONG BUY  -> SELL_ON_STRONG_BUY  => SELL
      STRONG SELL -> SELL_ON_STRONG_SELL => SELL
    """
    c = (curr or "").upper().strip()
    tset = {str(x).upper().strip() for x in (types or [])}

    # BUY rules
    if c == "STRONG BUY" and "BUY_ON_STRONG_BUY" in tset:
        return "BUY", "BUY_ON_STRONG_BUY"

    if c == "STRONG SELL" and "BUY_ON_STRONG_SELL" in tset:
        return "BUY", "BUY_ON_STRONG_SELL"

    # SELL rules
    if c == "STRONG BUY" and "SELL_ON_STRONG_BUY" in tset:
        return "SELL", "SELL_ON_STRONG_BUY"

    if c == "STRONG SELL" and "SELL_ON_STRONG_SELL" in tset:
        return "SELL", "SELL_ON_STRONG_SELL"

    return None


def _load_signal_strategy() -> Dict[str, Any]:
    raw = os.getenv("SIGNAL_STRATEGY_JSON", "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        logger.exception("SIGNAL_STRATEGY_JSON: invalid JSON")
        return {}


_SIGNAL_STRATEGY = _load_signal_strategy()

_STRATEGY_SOURCE = os.getenv("STRATEGY_SOURCE", "env").strip().lower()

def _strategy_tf_to_phase1(tf: str) -> str:
    s = str(tf or "").strip().lower()
    return {
        "day": "1d",
        "daily": "1d",
        "1day": "1d",
        "week": "1w",
        "weekly": "1w",
        "1week": "1w",
        "month": "1mo",
        "monthly": "1mo",
        "1month": "1mo",
    }.get(s, s)


def _normalize_allowed_rules(raw: Dict[str, List[dict]]) -> Dict[str, List[dict]]:
    """
    Приводит правила стратегии к внутреннему формату phase1.

    На входе:
    {
        "30m": [
            {
                "signal": "...",
                "skip": 0,
                "trade_amount": 100
            }
        ]
    }

    На выходе:
    {
        "M30": [
            {
                "signal": "...",
                "skip": 0,
                "trade_amount": 100
            }
        ]
    }
    """

    out: Dict[str, List[dict]] = {}

    for tf, rules in (raw or {}).items():
        ntf = _strategy_tf_to_phase1(tf)

        normalized_rules = []

        for rule in rules or []:
            if not isinstance(rule, dict):
                continue

            signal = str(rule.get("signal") or "").strip().upper()

            if not signal:
                continue

            normalized_rules.append({
                "signal": signal,
                "skip": int(rule.get("skip", 0)),
                "trade_amount": rule.get("trade_amount"),
            })

        if normalized_rules:
            out[ntf] = normalized_rules

    return out

def _select_rule_for_cycle(
    signal_rules,
    cycle,
    prev_signal,
    curr_signal,
):
    """
    Анализирует состояние цикла одной стратегии.

    Возвращает:

        ("OPEN", rule)
        ("WAIT", None)
        ("ADVANCE", None)
        ("RESET", None)
        ("IGNORE", None)
    """

    if not signal_rules:
        return "IGNORE", None

    #
    # На всякий случай всегда сортируем правила по skip.
    #
    signal_rules = sorted(
        signal_rules,
        key=lambda r: int(r.get("skip", 0)),
    )

    #
    # Все правила должны относиться к одной стратегии.
    #
    signals = {
        str(rule["signal"]).upper()
        for rule in signal_rules
    }

    if len(signals) != 1:
        raise RuntimeError(
            f"Mixed strategy rules: {sorted(signals)}"
        )

    signal_rule = signals.pop()

    current_skip = int(cycle.get("current_skip", 0))
    waiting = bool(cycle.get("waiting_transition", False))
    curr_signal = (curr_signal or "").upper()

    #
    # Определяем STRONG открытия и STRONG завершения цикла.
    #
    if signal_rule.endswith("STRONG_BUY"):
        open_signal = "STRONG BUY"
        close_signal = "STRONG SELL"
    else:
        open_signal = "STRONG SELL"
        close_signal = "STRONG BUY"

    #
    # Ищем правило для текущего skip.
    #
    selected_rule = None

    for rule in signal_rules:
        if int(rule.get("skip", 0)) == current_skip:
            selected_rule = rule
            break

    #
    # Противоположный STRONG всегда завершает цикл.
    #
    if curr_signal == close_signal:
        return "RESET", None

    #
    # После открытия сделки ждём промежуточный сигнал.
    #
    if waiting:

        if curr_signal in ("BUY", "SELL", "NEUTRAL"):
            return "ADVANCE", None

        #
        # Любой STRONG во время ожидания игнорируется.
        #
        return "IGNORE", None

    #
    # Цикл может начаться только с флипа, смена одного сильного сигнала на противоположный
    #
    if current_skip == 0 and not waiting:

        prev_signal = (prev_signal or "").upper()

        if prev_signal != close_signal:
            return "IGNORE", None

        if curr_signal != open_signal:
            return "IGNORE", None

    #
    # Есть правило для текущего skip — открываем сделку.
    #
    if selected_rule is not None:
        return "OPEN", selected_rule

    #
    # Пользователь не описал этот skip.
    # Сделку не открываем, но цикл продолжается.
    #
    return "WAIT", None

def _weekday_idx(now_dt) -> int:
    return int(now_dt.weekday())


def _instrument_cfg_for(code: str) -> Dict[str, Any]:
    instruments = (_SIGNAL_STRATEGY or {}).get("instruments") or {}
    return instruments.get(code) or instruments.get("DEFAULT") or {}


def _day_cfg_for(code: str, now_dt) -> Dict[str, Any]:
    instr = _instrument_cfg_for(code)
    week = (instr.get("week") or {}) if isinstance(instr, dict) else {}
    cfg = week.get(str(_weekday_idx(now_dt))) or {}
    return cfg if isinstance(cfg, dict) else {}


def _day_is_signal_enabled(day_cfg: Dict[str, Any]) -> bool:
    modes = day_cfg.get("mode")
    if not modes:
        return False
    if isinstance(modes, str):
        modes = [modes]
    return any(str(m).upper() == "SIGNAL" for m in modes)


def _allowed_types_by_tf(code: str, now_dt) -> Dict[str, List[str]]:
    """
    Источник правил:
      STRATEGY_SOURCE=db  -> strategy_rules из PostgreSQL, которыми управляет plus500-web
      STRATEGY_SOURCE=env -> старый SIGNAL_STRATEGY_JSON fallback
    """
    if _STRATEGY_SOURCE == "db":
        try:
            allowed = get_allowed_strategy_rules_by_tf(code, now_dt)
            allowed = _normalize_allowed_rules(allowed)
            logger.info("PHASE1 %s: strategy_source=db allowed=%s", code, allowed)
            return allowed
        except Exception:
            logger.exception("PHASE1 %s: failed to load DB strategy rules", code)
            return {}

    day_cfg = _day_cfg_for(code, now_dt)
    if not _day_is_signal_enabled(day_cfg):
        logger.info("PHASE1 %s: strategy_source=env allowed={}", code)
        return {}

    out: Dict[str, List[str]] = {}
    for k, v in day_cfg.items():
        if k in ("start", "end", "mode"):
            continue
        if isinstance(v, list) and v:
            out[_strategy_tf_to_phase1(str(k))] = [str(x).strip().upper() for x in v if str(x).strip()]

    logger.info("PHASE1 %s: strategy_source=env allowed=%s", code, out)
    return out

def phase1_manage_open_positions_close_only(
    instr: InstrumentConfig,
    res: InstrumentResult,
    *,
    now_dt,
    executed_at: str,
) -> Tuple[List[str], Dict[str, Optional[dict]]]:
    """
    CLOSE-only слой: выполняется КАЖДЫЙ день, независимо от SIGNAL schedule.
    Делает только:
      - snapshot активных позиций по TF
      - закрытие позиций по правилу entry_rule и текущему strong на owner_tf
    Не делает:
      - desired
      - swap/handoff
      - open
    Возвращает:
      close_lines: список строк для Telegram/логов
      active_now: snapshot активных позиций (по position_tf)
    """
    if not instr:
        return [], {}

    code = instr.code
    close_lines: List[str] = []

    # snapshot активных позиций: берём все TF из приоритета (а не allowed)
    active_now: Dict[str, Optional[dict]] = {}
    for tf in _TF_PRIORITY:
        t = _norm_tf(tf)
        try:
            active_now[t] = get_active_position_for_tf(code, t)
            logger.info("PHASE1 %s: active_by_position_tf[%s]=%s", code, t, _fmt_pos(active_now[t]))
        except Exception:
            logger.exception("PHASE1 %s: get_active_position_for_tf failed for tf=%s", code, t)
            active_now[t] = None

    # план CLOSE: по текущему strong на owner_tf
    close_candidates: Dict[str, dict] = {}

    for slot_tf in _TF_PRIORITY:
        slot_tf = _norm_tf(slot_tf)
        pos = active_now.get(slot_tf)
        if not pos:
            continue

        owner_tf = _norm_tf(pos.get("owner_tf") or slot_tf)
        entry_rule = str(pos.get("entry_rule") or "").upper().strip()
        close_on = _close_on_for_rule(entry_rule)
        owner_curr = ((res.timeframes.get(owner_tf) or {}).get("action") or None)

        if not close_on:
            continue
        if owner_curr not in _STRONG:
            continue
        if owner_curr != close_on:
            continue

        close_candidates[slot_tf] = {
            "pos": pos,
            "owner_tf": owner_tf,
            "entry_rule": entry_rule,
            "close_on": close_on,
            "owner_curr": owner_curr,
        }

    # EXECUTE CLOSES
    for slot_tf in _TF_PRIORITY:
        slot_tf = _norm_tf(slot_tf)
        info = close_candidates.get(slot_tf)
        if not info:
            continue

        pos = info["pos"]
        try:
            ok_close = close_position_by_id(
                int(pos["id"]),
                comment=f"PHASE1 CLOSE | slot_tf={slot_tf} rule={info['entry_rule']} on {info['owner_curr']}",
            )
        except Exception:
            logger.exception("PHASE1 %s: close_position_by_id failed pos_id=%s", code, pos.get("id"))
            ok_close = False

        # Plus500 close — только если реально закрыли
        if ok_close:
            try:
                plus500_close_if_enabled(
                    code,
                    registered_at=executed_at,
                    opened_at=pos.get("opened_at"),
                    position_num=pos.get("position_num"),
                    reason=(
                        f"PHASE1 CLOSE slot_tf={slot_tf} rule={info.get('entry_rule')} "
                        f"close_on={info.get('close_on')} curr={info.get('owner_curr')}"
                    ),
                )
            except Exception:
                logger.exception("PHASE1 %s: plus500_close_if_enabled failed pos_id=%s", code, pos.get("id"))

        close_lines.append(
            f"• {_tf_human(slot_tf)} #{pos.get('id')}  {_state_human(pos.get('state'))}  "
            f"закрыли по сигналу: {info.get('owner_curr')}  "
            f"(правило: {_rule_human(info.get('entry_rule'))})  "
            f"{'✅' if ok_close else '❌'}"
        )
        logger.info("PHASE1 %s: CLOSE_DONE pos_id=%s ok=%s", code, pos.get("id"), ok_close)

    return close_lines, active_now

# ----------------------------
# Core logic
# ----------------------------
def phase1_process_instrument(
    instr: InstrumentConfig,
    res: InstrumentResult,
    *,
    now_dt,
    executed_at: str,
) -> None:
    if not instr:
        logger.error("PHASE1: instr is None (skip)")
        return

    code = instr.code
    handoff_lines: list[str] = []
    open_lines: list[str] = []
    swap_lines: list[str] = []  # если используешь pair swap

    # Day bounds (naive, because DB uses TIMESTAMP without TZ)
    day_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
    day_end = (day_start + timedelta(days=1))


    # ------------------------------------------------------------
    # 0) persist data + strong signals
    # ------------------------------------------------------------
    try:
        insert_instrument_data(code, executed_at, res.price_number, res.timeframes)
    except Exception:
        logger.exception("PHASE1 %s: insert_instrument_data failed", code)

    try:
        for tf_k, obj in (res.timeframes or {}).items():
            act = (obj or {}).get("action")
            if act in _STRONG:
                insert_strong_signal(code, executed_at, res.price_number, act, timeframe=str(tf_k))
    except Exception:
        logger.exception("PHASE1 %s: insert_strong_signal failed", code)

    # # ------------------------------------------------------------
    # # 0.5) CLOSE-only слой: всегда (даже когда SIGNAL disabled)
    # # ------------------------------------------------------------
    # close_lines, _active_snapshot = phase1_manage_open_positions_close_only(
    #     instr, res, now_dt=now_dt, executed_at=executed_at
    # )

    # ------------------------------------------------------------
    # 1) allowed strategies
    # ------------------------------------------------------------
    allowed = _allowed_types_by_tf(code, now_dt)
    logger.info("PHASE1 %s: allowed=%s", code, allowed)

    if not allowed:
        close_lines, _active_snapshot = phase1_manage_open_positions_close_only(
            instr, res, now_dt=now_dt, executed_at=executed_at
        )
        logger.info("PHASE1 %s: SIGNAL disabled today (skip)", code)
        return

    # если allowed есть — close-only НЕ делаем
    close_lines = []

    # ------------------------------------------------------------
    # 2) desired action per TF-slot:
    #    - if no positions today for this slot => FIRST ENTRY requires flip
    #    - else => use current STRONG directly (no flip)
    # ------------------------------------------------------------
    # desired: Dict[str, List[Desired]] = {}
    #
    # for tf_raw, types in allowed.items():
    #     slot_tf = _norm_tf(tf_raw)
    #     curr = ((res.timeframes.get(tf_raw) or {}).get("action") or None)
    #
    #     logger.info("PHASE1 %s: TF=%s strategies=%s curr=%s", code, slot_tf, types, curr)
    #
    #     if curr not in _STRONG:
    #         logger.info("PHASE1 %s: TF=%s -> SKIP (curr not STRONG)", code, slot_tf)
    #         continue
    #
    #     started = has_position_today(code, slot_tf, day_start, day_end)
    #     logger.info("PHASE1 %s: TF=%s started_today=%s", code, slot_tf, started)
    #
    #     if not started:
    #         prev = get_last_strong_signal_before(code, timeframe=slot_tf, before_executed_at=executed_at)
    #         logger.info("PHASE1 %s: TF=%s first-entry prev=%s curr=%s", code, slot_tf, prev, curr)
    #
    #         if prev not in _STRONG or prev == curr:
    #             logger.info("PHASE1 %s: TF=%s first-entry -> SKIP (no flip)", code, slot_tf)
    #             continue
    #
    #         ent = _entry_from_transition(types, prev, curr)
    #         if not ent:
    #             logger.info("PHASE1 %s: TF=%s first-entry -> SKIP (no matching rule for flip)", code, slot_tf)
    #             continue
    #
    #         action, rule = ent
    #         desired.setdefault(slot_tf, []).append({
    #             "action": action,
    #             "rule": rule,
    #             "mode": "FIRST_ENTRY_FLIP",
    #             "prev": prev,
    #             "curr": curr,
    #
    #             # пока временно
    #             "skip": 0,
    #             "trade_amount": None,
    #         })
    #         logger.info("PHASE1 %s: TF=%s desired=%s", code, slot_tf, desired[slot_tf])
    #         continue
    #
    #     ent2 = _entry_from_curr(types, curr)
    #     if not ent2:
    #         logger.info("PHASE1 %s: TF=%s in-day -> SKIP (no rule for curr strong)", code, slot_tf)
    #         continue
    #
    #     action, rule = ent2
    #     desired.setdefault(slot_tf, []).append({
    #         "action": action,
    #         "rule": rule,
    #         "mode": "IN_DAY_CURR",
    #         "curr": curr,
    #
    #         "skip": 0,
    #         "trade_amount": None,
    #     })
    #     logger.info("PHASE1 %s: TF=%s desired=%s", code, slot_tf, desired[slot_tf])
    #
    # logger.info("PHASE1 %s: desired_all=%s", code, desired)

    from collections import defaultdict

    desired: Dict[str, List[Desired]] = {}

    for tf_raw, rules in allowed.items():

        slot_tf = _norm_tf(tf_raw)

        curr = (
                (res.timeframes.get(tf_raw) or {})
                .get("action")
                or ""
        ).upper()

        #
        # Разбиваем правила по типу стратегии.
        #
        rules_by_signal: dict[str, list] = defaultdict(list)

        for rule in rules:
            rules_by_signal[rule["signal"]].append(rule)

        #
        # Каждая стратегия имеет собственный цикл.
        #
        for signal_rule, signal_rules in rules_by_signal.items():

            cycle = get_strategy_cycle_state(
                code,
                signal_rule,
            )

            logger.info(
                "TF CHECK: tf_raw=%s slot_tf=%s timeframes_keys=%s curr=%s",
                tf_raw,
                slot_tf,
                list(res.timeframes.keys()),
                curr,
            )

            prev = get_last_strong_signal_before(
                code,
                timeframe=slot_tf,
                before_executed_at=executed_at,
            )

            state, selected_rule = _select_rule_for_cycle(
                signal_rules,
                cycle,
                prev,
                curr,
            )

            logger.info(
                "PHASE1 %s tf=%s signal=%s state=%s cycle=%s",
                code,
                slot_tf,
                signal_rule,
                state,
                cycle,
            )

            if state == "RESET":
                reset_strategy_cycle(
                    code,
                    signal_rule,
                )

                continue

            if state == "ADVANCE":
                advance_strategy_cycle(
                    code,
                    signal_rule,
                )

                continue

            if state == "WAIT":
                set_strategy_waiting_transition(
                    code,
                    signal_rule,
                )

                continue

            if state != "OPEN":
                continue

            desired.setdefault(slot_tf, []).append({

                "action": (
                    "BUY"
                    if selected_rule["signal"].startswith("BUY_")
                    else "SELL"
                ),

                "rule": selected_rule["signal"],

                "skip": selected_rule["skip"],

                "trade_amount": selected_rule.get("trade_amount"),

                "mode": "SKIP",

                "curr": curr,
            })

            set_strategy_waiting_transition(
                code,
                signal_rule,
            )

    logger.info(
        "PHASE1 %s: desired=%s",
        code,
        desired,
    )

    # ------------------------------------------------------------
    # 2.4) snapshot active positions BEFORE any actions
    # ------------------------------------------------------------
    active_by_position_tf: dict[str, Optional[dict]] = {}
    active_by_owner_tf: dict[str, Optional[dict]] = {}

    for tf in _TF_PRIORITY:
        t = _norm_tf(tf)
        pos = get_active_position_for_tf(code, t)
        active_by_position_tf[t] = pos
        logger.info("PHASE1 %s: active_by_position_tf[%s]=%s", code, t, _fmt_pos(pos))

        # owner_tf может отличаться — учитываем для анти-дубликатов
        if pos:
            o = _norm_tf(pos.get("owner_tf") or t)
            if o and o not in active_by_owner_tf:
                active_by_owner_tf[o] = pos
        else:
            if t not in active_by_owner_tf:
                active_by_owner_tf[t] = None

    # ------------------------------------------------------------
    # 2.5) PAIR SWAP (A<->B): атомарный swap двух позиций
    # ------------------------------------------------------------
    swapped_tfs: set[str] = set()
    swap_consumed: set[str] = set()

    def _pos_action(pos: Optional[dict]) -> Optional[str]:
        return _action_from_state(pos.get("state")) if pos else None

    tfs_order = [_norm_tf(x) for x in sorted(list(allowed.keys()), key=lambda x: _tf_rank(_norm_tf(x)))]

    for i, a in enumerate(tfs_order):
        if a in swapped_tfs:
            continue

        pos_a = active_by_position_tf.get(a)
        rules_a = desired.get(a) or []

        if not pos_a or not rules_a:
            continue

        des_a = rules_a[0]

        act_a = (_pos_action(pos_a) or "").upper()
        want_a = (des_a.get("action") or "").upper()
        if not act_a or not want_a:
            continue

        # A must want opposite to its current position action
        if want_a == act_a:
            continue

        for b in tfs_order[i + 1:]:
            if b in swapped_tfs:
                continue

            pos_b = active_by_position_tf.get(b)
            rules_b = desired.get(b) or []

            if not pos_b or not rules_b:
                continue

            des_b = rules_b[0]

            act_b = (_pos_action(pos_b) or "").upper()
            want_b = (des_b.get("action") or "").upper()
            if not act_b or not want_b:
                continue

            if want_b == act_b:
                continue

            # swap condition:
            # posA action X, desiredB = X
            # posB action Y, desiredA = Y
            # X != Y
            if act_a != act_b and want_a == act_b and want_b == act_a:
                comment_a = (
                    f"PHASE1 PAIR_SWAP | pos_id={pos_a.get('id')} from_tf={a} -> to_tf={b} "
                    f"old_rule={pos_a.get('entry_rule')} new_rule={des_b.get('rule')} act={act_a}"
                )
                comment_b = (
                    f"PHASE1 PAIR_SWAP | pos_id={pos_b.get('id')} from_tf={b} -> to_tf={a} "
                    f"old_rule={pos_b.get('entry_rule')} new_rule={des_a.get('rule')} act={act_b}"
                )

                ok = swap_handoff_positions(
                    int(pos_a["id"]), b, str(des_b.get("rule")), comment_a,
                    int(pos_b["id"]), a, str(des_a.get("rule")), comment_b,
                )

                if ok:
                    swap_lines.append(
                        f"• обмен: #{pos_a.get('id')} {_state_human(pos_a.get('state'))} {a} → {b} "
                        f"(правило: {_rule_human(des_b.get('rule'))})  ⇄  "
                        f"#{pos_b.get('id')} {_state_human(pos_b.get('state'))} {b} → {a} "
                        f"(правило: {_rule_human(des_a.get('rule'))})"
                    )
                else:
                    swap_lines.append(
                        f"• обмен НЕ удался: a={a} posA=#{pos_a.get('id')} ⇄ b={b} posB=#{pos_b.get('id')}"
                    )

                logger.info(
                    "PHASE1 %s: PAIR_SWAP_DONE ok=%s | %s(%s)->%s rule=%s | %s(%s)->%s rule=%s",
                    code, ok,
                    pos_a.get("id"), act_a, b, des_b.get("rule"),
                    pos_b.get("id"), act_b, a, des_a.get("rule"),
                )

                if ok:
                    swapped_tfs.update({a, b})
                    swap_consumed.update({a, b})

                    # refresh positions for swapped TFs
                    active_by_position_tf[a] = get_active_position_for_tf(code, a)
                    active_by_position_tf[b] = get_active_position_for_tf(code, b)

                break  # stop searching pairs for this 'a'

    # ------------------------------------------------------------
    # 3) PLAN CLOSE/HANDOFF (2-pass)
    # ------------------------------------------------------------
    consumed: set[str] = set(swap_consumed)
    reserved_targets: set[str] = set()
    forced_close: set[str] = set()

    def _best_handoff_target(action: str, slot_tf: str, will_be_empty: callable) -> Optional[Tuple[str, Dict[str, str]]]:
        a = str(action).upper()

        cands = []
        for tf, rules in desired.items():
            if not rules:
                continue
            meta = rules[0]
            tf = _norm_tf(tf)
            if tf == slot_tf:
                continue
            if tf in consumed:
                continue
            if str(meta.get("action")).upper() != a:
                continue
            if not will_be_empty(tf):
                continue
            cands.append((_tf_rank(tf), tf, meta))

        if not cands:
            return None

        cands.sort(key=lambda x: x[0])
        return cands[0][1], cands[0][2]

    # --- collect close candidates (ALWAYS over all TF priority) ---
    close_candidates_allowed: dict[str, dict] = {}
    close_candidates_disallowed: dict[str, dict] = {}

    allowed_tfs = {_norm_tf(k) for k in (allowed or {}).keys()}

    for slot_tf in _TF_PRIORITY:
        slot_tf = _norm_tf(slot_tf)
        if slot_tf in swapped_tfs:
            continue

        pos = active_by_position_tf.get(slot_tf)
        if not pos:
            continue

        owner_tf = _norm_tf(pos.get("owner_tf") or slot_tf)
        entry_rule = str(pos.get("entry_rule") or "").upper().strip()
        close_on = _close_on_for_rule(entry_rule)
        owner_curr = ((res.timeframes.get(owner_tf) or {}).get("action") or None)

        if not close_on or owner_curr not in _STRONG:
            continue
        if owner_curr != close_on:
            continue

        current_action = _action_from_state(pos.get("state"))
        info = {
            "pos": pos,
            "owner_tf": owner_tf,
            "entry_rule": entry_rule,
            "close_on": close_on,
            "owner_curr": owner_curr,
            "current_action": current_action,
        }

        if slot_tf in allowed_tfs:
            close_candidates_allowed[slot_tf] = info
        else:
            close_candidates_disallowed[slot_tf] = info

    self_switch_plan: dict[str, dict] = {}

    for slot_tf, info in close_candidates_allowed.items():
        slot_tf = _norm_tf(slot_tf)
        rules = desired.get(slot_tf) or []

        if not rules:
            continue

        des = rules[0]

        # нужно "то же действие", но другое правило
        if (des.get("action") or "").upper() != (info.get("current_action") or "").upper():
            continue

        old_rule = (info.get("entry_rule") or "").upper()
        new_rule = (des.get("rule") or "").upper()

        if not new_rule or new_rule == old_rule:
            continue

        # это и есть "передача правила внутри TF"
        self_switch_plan[slot_tf] = des

        # слот считаем consumed, чтобы потом не сделать OPEN дубль
        consumed.add(slot_tf)

    close_slots = set(close_candidates_allowed.keys()) | set(close_candidates_disallowed.keys())

    def _will_be_empty(tf: str) -> bool:
        tf = _norm_tf(tf)

        if tf in reserved_targets:
            return False
        if tf in swapped_tfs:
            return False  # swap уже занял слот

        if active_by_position_tf.get(tf) is None and active_by_owner_tf.get(tf) is None:
            return True

        # ключевая логика: если слот занят, но будет закрыт в этом же проходе
        if tf in close_slots:
            return True

        if tf in forced_close:
            return True

        return False

    # --- plan handoffs ---
    handoff_plan: dict[str, tuple[str, dict]] = {}
    for tf_raw in sorted(list(allowed.keys()), key=lambda x: _tf_rank(_norm_tf(x))):
        slot_tf = _norm_tf(tf_raw)
        if slot_tf in swapped_tfs:
            continue
        if slot_tf not in close_candidates_allowed:
            continue
        if slot_tf in forced_close:
            continue

        info = close_candidates_allowed[slot_tf]
        current_action = info["current_action"]

        handoff = _best_handoff_target(current_action, slot_tf, _will_be_empty)
        if not handoff:
            continue

        target_tf, meta = handoff
        target_tf = _norm_tf(target_tf)

        reserved_targets.add(target_tf)
        consumed.add(target_tf)

        # target не должен стать source
        forced_close.add(target_tf)

        handoff_plan[slot_tf] = (target_tf, meta)

    # ------------------------------------------------------------
    # 3A0) CLOSE-only for DISALLOWED TF slots
    #   (positions exist, but today this TF has no rules)
    # ------------------------------------------------------------
    for slot_tf in _TF_PRIORITY:
        slot_tf = _norm_tf(slot_tf)
        info = close_candidates_disallowed.get(slot_tf)
        if not info:
            continue

        pos = info["pos"]

        ok_close = close_position_by_id(
            int(pos["id"]),
            comment=(
                f"PHASE1 CLOSE(DISALLOWED) | slot_tf={slot_tf} "
                f"rule={info['entry_rule']} on {info['owner_curr']}"
            ),
        )

        if ok_close:
            plus500_close_if_enabled(
                code,
                registered_at=executed_at,
                opened_at=pos.get("opened_at"),
                position_num=pos.get("position_num"),
                reason=(
                    f"PHASE1 CLOSE(DISALLOWED) slot_tf={slot_tf} "
                    f"rule={info.get('entry_rule')} close_on={info.get('close_on')} curr={info.get('owner_curr')}"
                ),
            )

        close_lines.append(
            f"• {_tf_human(slot_tf)} #{pos.get('id')}  {_state_human(pos.get('state'))}  "
            f"закрыли (TF без правил сегодня): {info.get('owner_curr')}  "
            f"(правило: {_rule_human(info.get('entry_rule'))})  "
            f"{'✅' if ok_close else '❌'}"
        )
        logger.info("PHASE1 %s: CLOSE_DISALLOWED_DONE pos_id=%s ok=%s", code, pos.get("id"), ok_close)

        # обновим snapshot, чтобы дальше OPEN/HANDOFF не думали что слот занят
        active_by_position_tf[slot_tf] = None

    # ------------------------------------------------------------
    # 3A) EXECUTE CLOSES first (except handoff sources)
    # ------------------------------------------------------------
    for tf_raw in sorted(list(allowed.keys()), key=lambda x: _tf_rank(_norm_tf(x))):
        slot_tf = _norm_tf(tf_raw)
        if slot_tf in self_switch_plan:
            continue
        if slot_tf in swapped_tfs:
            continue
        if slot_tf not in close_candidates_allowed:
            continue
        if slot_tf in handoff_plan:
            continue  # source of handoff stays open

        info = close_candidates_allowed[slot_tf]
        pos = info["pos"]

        ok_close = close_position_by_id(
            int(pos["id"]),
            comment=f"PHASE1 CLOSE | slot_tf={slot_tf} rule={info['entry_rule']} on {info['owner_curr']}",
        )
        # Plus500: CLOSE (только на реальный CLOSE, не на swap/handoff)
        if ok_close:
            plus500_close_if_enabled(
                code,
                registered_at=executed_at,
                opened_at=pos.get("opened_at"),
                position_num=pos.get("position_num"),
                reason=f"PHASE1 CLOSE slot_tf={slot_tf} rule={info.get('entry_rule')} close_on={info.get('close_on')} curr={info.get('owner_curr')}",
            )

        close_lines.append(
            f"• {_tf_human(slot_tf)} #{pos.get('id')}  {_state_human(pos.get('state'))}  "
            f"закрыли по сигналу: {info.get('owner_curr')}  "
            f"(правило: {_rule_human(info.get('entry_rule'))})  "
            f"{'✅' if ok_close else '❌'}"
        )
        logger.info("PHASE1 %s: CLOSE_DONE pos_id=%s ok=%s", code, pos.get("id"), ok_close)

    # ------------------------------------------------------------
    # 3B0) SELF SWITCH (rule change inside same TF)
    # ------------------------------------------------------------
    for slot_tf, des in self_switch_plan.items():
        info = close_candidates_allowed[slot_tf]
        pos = info["pos"]
        new_rule = des["rule"]

        comment = (
            f"PHASE1 RULE_SWITCH | pos_id={pos.get('id')} slot_tf={slot_tf} "
            f"keep={info['current_action']} old_rule={info['entry_rule']} new_rule={new_rule} "
            f"owner_curr={info['owner_curr']}"
        )

        # используем существующую функцию: "handoff" в тот же tf
        ok = handoff_position_to_tf(int(pos["id"]), slot_tf, new_rule, comment)

        handoff_lines.append(
            f"• #{pos.get('id')} {_state_human(pos.get('state'))} сменили правило в {slot_tf}: "
            f"{_rule_human(info.get('entry_rule'))} → {_rule_human(new_rule)} "
            f"({'✅' if ok else '❌'})"
        )

    # ------------------------------------------------------------
    # 3B) EXECUTE HANDOFFS
    # ------------------------------------------------------------
    for tf_raw in sorted(list(allowed.keys()), key=lambda x: _tf_rank(_norm_tf(x))):
        slot_tf = _norm_tf(tf_raw)
        if slot_tf in swapped_tfs:
            continue
        if slot_tf not in handoff_plan:
            continue

        target_tf, meta = handoff_plan[slot_tf]
        info = close_candidates_allowed[slot_tf]
        pos = info["pos"]

        comment = (
            f"PHASE1 HANDOFF | pos_id={pos.get('id')} from_slot={slot_tf} keep={info['current_action']} "
            f"from_owner={info['owner_tf']} to_slot={target_tf} old_rule={info['entry_rule']} "
            f"new_rule={meta.get('rule')} owner_curr={info['owner_curr']}"
        )

        ok = handoff_position_to_tf(int(pos["id"]), target_tf, meta["rule"], comment)
        keep = info.get("current_action")  # BUY/SELL
        handoff_lines.append(
            f"• #{pos.get('id')}  {_state_human(pos.get('state'))}  перенесли: {slot_tf} → {target_tf}  "
            f"(сохранили {keep}, правило: {_rule_human(meta.get('rule'))})  "
            f"{'✅' if ok else '❌'}"
        )
        logger.info(
            "PHASE1 %s: HANDOFF_DONE pos_id=%s ok=%s to_tf=%s new_rule=%s",
            code, pos.get("id"), ok, target_tf, meta.get("rule"),
        )

    # ------------------------------------------------------------
    # refresh active positions after CLOSE/HANDOFF (for OPEN phase)
    # ------------------------------------------------------------
    active_now = {}
    for tf_raw in sorted(list(allowed.keys()), key=lambda x: _tf_rank(_norm_tf(x))):
        t = _norm_tf(tf_raw)
        active_now[t] = get_active_position_for_tf(code, t)

    # ------------------------------------------------------------
    # 4) OPEN for empty slots (skip consumed)
    # ------------------------------------------------------------
    for tf_raw in sorted(list(allowed.keys()), key=lambda x: _tf_rank(_norm_tf(x))):
        slot_tf = _norm_tf(tf_raw)
        rules = desired.get(slot_tf) or []

        if not rules:
            continue

        d = rules[0]

        logger.info("PHASE1 %s: OPEN_CHECK slot_tf=%s desired=%s consumed=%s", code, slot_tf, d, slot_tf in consumed)

        if not d or slot_tf in consumed:
            logger.info("PHASE1 %s: OPEN_SKIP slot_tf=%s (no desired or consumed)", code, slot_tf)
            continue

        if get_active_position_for_tf(code, slot_tf):
            logger.info("PHASE1 %s: OPEN_SKIP slot_tf=%s (already active)", code, slot_tf)
            continue

        pos_num = int(time.time_ns() // 1_000_000)
        new_id = set_position_state_tf(
            code,
            _state_from_action(d["action"]),
            slot_tf,
            comment=f"PHASE1 OPEN | tf={slot_tf} rule={d['rule']} mode={d.get('mode')} prev={d.get('prev')} curr={d.get('curr')}",
            position_num=pos_num,
            opened_at=executed_at,
            entry_rule=d["rule"],
            owner_tf=slot_tf,
        )

        # Plus500: OPEN (только на реальный OPEN, не на swap/handoff)
        trade_settings = _trade_settings_for_open(code)

        plus500_open_if_enabled(
            code,
            d["action"],  # BUY/SELL
            reason=f"PHASE1 OPEN tf={slot_tf} mode={d.get('mode')} rule={d.get('rule')} curr={d.get('curr')}",
            trade_amount=d.get("trade_amount")
        )
        open_lines.append(
            f"• {_tf_human(slot_tf)} #{new_id}  {_state_human(_state_from_action(d['action']))}  "
            f"открыли по сигналу: {d.get('curr')}  (правило: {_rule_human(d.get('rule'))})"
        )
        logger.info(
            "PHASE1 %s: OPEN_DONE slot_tf=%s new_id=%s state=%s rule=%s owner=%s",
            code, slot_tf, new_id, _state_from_action(d["action"]), d["rule"], slot_tf
        )
    if PHASE1_NOTIFY and PHASE1_NOTIFY_MODE == "BATCH":
        lines = [
            f"📌 {code} • {executed_at[:5]} {executed_at[6:] if len(executed_at) > 5 else executed_at} • {res.price_number}",
            "",
        ]

        if close_lines:
            lines += [f"❌ Закрыли ({len(close_lines)})"] + close_lines + [""]

        if swap_lines:
            lines += [f"🔄 Обмен ({len(swap_lines)})"] + swap_lines + [""]

        if handoff_lines:
            lines += [f"🔁 Передали ({len(handoff_lines)})"] + handoff_lines + [""]

        if open_lines:
            lines += [f"✅ Открыли ({len(open_lines)})"] + open_lines

        msg = "\n".join(lines).rstrip()
        _tg_send(msg)
