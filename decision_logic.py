from typing import Optional, Literal

TradeMode = str  # "TRADE" | "CLOSE_ONLY" | "PAUSE"
SignalLogic = Literal["DIRECT", "INVERSE"]

def decide_reverse_order(prev_signal: Optional[str], new_signal: Optional[str]) -> Optional[str]:
    if not prev_signal or not new_signal:
        return None

    prev = prev_signal.upper()
    curr = new_signal.upper()

    if prev == "STRONG SELL" and curr == "STRONG BUY":
        return "SELL"
    if prev == "STRONG BUY" and curr == "STRONG SELL":
        return "BUY"
    return None


def decide_direct_order(prev_signal: Optional[str], new_signal: Optional[str]) -> Optional[str]:
    """Прямая (текущая) логика.

    - STRONG BUY  -> STRONG SELL => BUY
    - STRONG SELL -> STRONG BUY  => SELL
    """
    return decide_reverse_order(prev_signal, new_signal)


def decide_inverse_order(prev_signal: Optional[str], new_signal: Optional[str]) -> Optional[str]:
    """Обратная логика (инверсия относительно прямой).

    - STRONG BUY  -> STRONG SELL => SELL
    - STRONG SELL -> STRONG BUY  => BUY
    """
    a = decide_reverse_order(prev_signal, new_signal)
    if a == "BUY":
        return "SELL"
    if a == "SELL":
        return "BUY"
    return None


def _signal_to_position(signal: Optional[str]) -> str:
    if not signal:
        return "NONE"
    s = signal.upper()
    if s == "STRONG BUY":
        return "LONG"
    if s == "STRONG SELL":
        return "SHORT"
    return "NONE"


def decide_plus500_action(
    prev_signal: Optional[str],
    new_signal: Optional[str],
    position_state: str,
    trade_mode: TradeMode,
    signal_logic: SignalLogic = "DIRECT",
) -> tuple[Optional[str], str]:
    strong = {"STRONG BUY", "STRONG SELL"}

    if not new_signal:
        return None, position_state

    new_signal = new_signal.upper()

    if new_signal not in strong:
        return None, position_state

    if trade_mode == "PAUSE":
        return None, position_state

    if trade_mode == "CLOSE_ONLY":
        if position_state == "LONG" and new_signal == "STRONG SELL":
            return "SELL_HALF", "NONE"
        if position_state == "SHORT" and new_signal == "STRONG BUY":
            return "BUY_HALF", "NONE"
        return None, position_state

    # TRADE
    if prev_signal not in strong:
        return None, position_state

    logic = str(signal_logic or "DIRECT").upper()
    if logic not in ("DIRECT", "INVERSE"):
        logic = "DIRECT"

    if logic == "INVERSE":
        action_full = decide_inverse_order(prev_signal, new_signal)
    else:
        action_full = decide_direct_order(prev_signal, new_signal)
    if not action_full:
        return None, position_state

    return action_full, _signal_to_position(new_signal)

# Для обратной совместимости (если где-то ещё импортируется старое имя)
decide_gbpusd_plus500_action = decide_plus500_action
from dataclasses import dataclass

@dataclass(frozen=True)
class StrategyDef:
    strategy_type: str
    side: Literal["BUY","SELL"]
    entry_prev: Literal["STRONG BUY","STRONG SELL"]
    entry_curr: Literal["STRONG BUY","STRONG SELL"]

SP500_STRATEGIES: dict[str, StrategyDef] = {
    "BUY_ON_STRONG_BUY": StrategyDef("BUY_ON_STRONG_BUY", "BUY", "STRONG SELL", "STRONG BUY"),
    "SELL_ON_STRONG_SELL": StrategyDef("SELL_ON_STRONG_SELL", "SELL", "STRONG BUY", "STRONG SELL"),
    "BUY_ON_STRONG_SELL": StrategyDef("BUY_ON_STRONG_SELL", "BUY", "STRONG BUY", "STRONG SELL"),
    "SELL_ON_STRONG_BUY": StrategyDef("SELL_ON_STRONG_BUY", "SELL", "STRONG SELL", "STRONG BUY"),
}

def sp500_entry_matches(strategy_type: str, prev_signal: Optional[str], curr_signal: Optional[str]) -> bool:
    d = SP500_STRATEGIES.get(str(strategy_type).upper())
    if not d or not prev_signal or not curr_signal:
        return False
    return prev_signal.upper() == d.entry_prev and curr_signal.upper() == d.entry_curr

def sp500_exit_matches(side: str, curr_signal: Optional[str]) -> bool:
    if not curr_signal:
        return False
    s = str(side).upper()
    c = curr_signal.upper()
    if s == "BUY":
        return c == "STRONG SELL"
    if s == "SELL":
        return c == "STRONG BUY"
    return False

def sp500_action_for_open(side: str) -> str:
    return "BUY" if str(side).upper() == "BUY" else "SELL"

def sp500_action_for_close(side: str) -> str:
    return "SELL" if str(side).upper() == "BUY" else "BUY"