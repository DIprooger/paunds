from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

try:
    from .api_auth import require_api_key
    from .db_client import (
        ensure_schema,
        export_strategy_json_from_db,
        get_instrument_trade_settings,
        get_plus500_instrument_metadata,
        get_strategy_instrument_detail,
        get_tf_priority,
        list_instrument_trade_settings,
        list_plus500_instrument_metadata,
        list_strategy_instruments,
        replace_strategy_week_rules,
        set_instrument_trade_amount,
        set_strategy_instrument_signals_enabled,
        soft_delete_strategy_instrument,
        strategy_timeframe_to_api,
    )
except ImportError:
    from api_auth import require_api_key
    from db_client import (
        ensure_schema,
        export_strategy_json_from_db,
        get_instrument_trade_settings,
        get_plus500_instrument_metadata,
        get_strategy_instrument_detail,
        get_tf_priority,
        list_instrument_trade_settings,
        list_plus500_instrument_metadata,
        list_strategy_instruments,
        replace_strategy_week_rules,
        set_instrument_trade_amount,
        set_strategy_instrument_signals_enabled,
        soft_delete_strategy_instrument,
        strategy_timeframe_to_api,
    )


router = APIRouter(
    prefix="/strategies",
    tags=["strategies"],
    dependencies=[Depends(require_api_key)],
)


class StrategyRuleIn(BaseModel):
    day_of_week: int = Field(..., ge=0, le=6)

    start: str = "00:00"
    end: str = "24:00"

    timeframe: str
    signal: str

    skip: int = Field(default=0, ge=0)

    trade_amount: float | None = Field(default=None, gt=0)


class WeekReplaceRequest(BaseModel):
    strategy_key: str
    rules: list[StrategyRuleIn]


class InstrumentCreateRequest(BaseModel):
    strategy_key: str
    signals_enabled: bool = True
    rules: list[StrategyRuleIn]


class SignalsEnabledRequest(BaseModel):
    strategy_key: str
    enabled: bool


class TradeAmountRequest(BaseModel):
    strategy_key: str
    trade_amount: float


def _merge_meta_and_trade(meta: dict[str, Any] | None, trade: dict[str, Any] | None) -> dict[str, Any] | None:
    if not meta and not trade:
        return None

    out = dict(meta or {})
    out.update(trade or {})
    return out


def _rule_to_api(rule: dict[str, Any]) -> dict[str, Any]:
    out = dict(rule)

    if "timeframe_db" in out:
        out["timeframe"] = out.get("timeframe") or strategy_timeframe_to_api(out["timeframe_db"])

    if out.get("timeframe") in ("1d", "1w", "1mo"):
        out["timeframe"] = strategy_timeframe_to_api(out["timeframe"])

    out.setdefault("start", out.pop("start_time", "00:00"))
    out.setdefault("end", out.pop("end_time", "24:00"))

    # Совместимость с БД
    if "skip_count" in out:
        out["skip"] = out.pop("skip_count")

    return out


def _enrich_detail(strategy_key: str) -> dict[str, Any]:
    detail = get_strategy_instrument_detail(strategy_key)

    if not detail:
        raise HTTPException(status_code=404, detail=f"Instrument not found: {strategy_key}")

    meta = get_plus500_instrument_metadata(detail.get("strategy_key") or strategy_key)
    trade = get_instrument_trade_settings(detail.get("strategy_key") or strategy_key)

    week = detail.get("week") or {}
    for day_key, rules in week.items():
        week[day_key] = [_rule_to_api(r) for r in (rules or [])]

    detail["week"] = week
    detail["metadata"] = _merge_meta_and_trade(meta, trade)

    return detail


@router.get("/options")
def get_options() -> dict[str, Any]:
    return {
        "ok": True,
        "days": [
            {"value": 0, "label": "ПН"},
            {"value": 1, "label": "ВТ"},
            {"value": 2, "label": "СР"},
            {"value": 3, "label": "ЧТ"},
            {"value": 4, "label": "ПТ"},
            {"value": 5, "label": "СБ"},
            {"value": 6, "label": "ВС"},
        ],
        "timeframes": [
            {"value": "30m", "label": "30m"},
            {"value": "1h", "label": "1h"},
            {"value": "5h", "label": "5h"},
            {"value": "day", "label": "day"},
            {"value": "week", "label": "week"},
            {"value": "month", "label": "month"},
        ],
        "timeframes_db": get_tf_priority(),
        "signals": [
            "BUY_ON_STRONG_BUY",
            "BUY_ON_STRONG_SELL",
            "SELL_ON_STRONG_BUY",
            "SELL_ON_STRONG_SELL",
        ],
        "default_start": "00:00",
        "default_end": "24:00",
    }


@router.get("/instruments")
def get_instruments() -> dict[str, Any]:
    base_items = list_strategy_instruments()
    metadata_items = list_plus500_instrument_metadata()
    trade_items = list_instrument_trade_settings()

    metadata_by_code = {x.get("code"): x for x in metadata_items}
    trade_by_code = {x.get("code"): x for x in trade_items}

    out = []

    for item in base_items:
        merged = dict(item)
        meta = metadata_by_code.get(item.get("code"))
        trade = trade_by_code.get(item.get("code"))
        merged["metadata"] = _merge_meta_and_trade(meta, trade)
        out.append(merged)

    return {"ok": True, "items": out}


@router.get("/instrument")
def get_instrument(strategy_key: str = Query(...)) -> dict[str, Any]:
    return {"ok": True, "item": _enrich_detail(strategy_key)}


@router.put("/instrument/week")
def put_instrument_week(payload: WeekReplaceRequest) -> dict[str, Any]:
    try:

        detail = replace_strategy_week_rules(
            payload.strategy_key,
            [x.model_dump() for x in payload.rules],
            actor="api",
            require_one_rule=True,
        )

        return {"ok": True, "item": _enrich_detail(detail.get("strategy_key") or payload.strategy_key)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/instrument")
def create_or_update_instrument(payload: InstrumentCreateRequest) -> dict[str, Any]:
    try:
        set_strategy_instrument_signals_enabled(
            payload.strategy_key,
            bool(payload.signals_enabled),
            actor="api",
        )

        detail = replace_strategy_week_rules(
            payload.strategy_key,
            [x.model_dump() for x in payload.rules],
            actor="api",
            require_one_rule=True,
        )

        return {"ok": True, "item": _enrich_detail(detail.get("strategy_key") or payload.strategy_key)}

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/instrument/signals-enabled")
def patch_signals_enabled(payload: SignalsEnabledRequest) -> dict[str, Any]:
    try:
        detail = set_strategy_instrument_signals_enabled(
            payload.strategy_key,
            bool(payload.enabled),
            actor="api",
        )
        return {"ok": True, "item": _enrich_detail(detail.get("strategy_key") or payload.strategy_key)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/instrument/trade-amount")
def patch_trade_amount(payload: TradeAmountRequest) -> dict[str, Any]:
    try:
        set_instrument_trade_amount(
            payload.strategy_key,
            payload.trade_amount,
            actor="api",
        )
        return {"ok": True, "item": _enrich_detail(payload.strategy_key)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/instrument")
def delete_instrument(strategy_key: str = Query(...)) -> dict[str, Any]:
    try:
        item = soft_delete_strategy_instrument(strategy_key, actor="api")
        return {"ok": True, "item": item}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/export")
def export_strategy() -> dict[str, Any]:
    return {"ok": True, "data": export_strategy_json_from_db()}
