import logging
import os
from typing import Optional, Tuple, Literal, Union

import requests
from requests.exceptions import RequestException, Timeout

from .config import (
    PLUS500_PREPARE_URL,
    PLUS500_CLOSE_URL,
    PLUS500_CLOSE_PAGE_URL,
    PLUS500_SIGNAL_SECRET, PLUS500_CLOSE_POSITION_URL, PLUS500_OPEN_URL,
)


Action = Literal["BUY", "SELL"]
logger = logging.getLogger(__name__)

# Базовый таймаут для быстрых эндпоинтов (/signal, /close_page).
# Один timeout в requests = и connect timeout, и read timeout (если передать float).
PLUS500_HTTP_TIMEOUT = float(os.getenv("PLUS500_HTTP_TIMEOUT", "30"))

# Для /prepare используем раздельные таймауты:
# - connect маленький: быстро понять, что сервер недоступен
# - read большой: /prepare может долго выполняться (прогрев браузера/логин/загрузка)
PLUS500_PREPARE_CONNECT_TIMEOUT = float(os.getenv("PLUS500_PREPARE_CONNECT_TIMEOUT", "5"))
PLUS500_PREPARE_READ_TIMEOUT = float(os.getenv("PLUS500_PREPARE_READ_TIMEOUT", "240"))

# requests принимает timeout либо float, либо tuple(connect, read)
PLUS500_PREPARE_TIMEOUT: Tuple[float, float] = (
    PLUS500_PREPARE_CONNECT_TIMEOUT,
    PLUS500_PREPARE_READ_TIMEOUT,
)

import os
import logging
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Вкл/выкл интеграцию вообще
PLUS500_ACTIONS_ENABLED = os.getenv("PLUS500_ACTIONS_ENABLED", "0").strip() == "1"
# DRY_RUN=1 -> только логируем, не вызываем HTTP
PLUS500_DRY_RUN = os.getenv("PLUS500_DRY_RUN", "1").strip() == "1"


# def _dt_to_str(x: Any) -> str:
#     """opened_at может быть datetime или строкой. Приводим к строке."""
#     if x is None:
#         return ""
#     if isinstance(x, datetime):
#         return x.isoformat(sep=" ", timespec="seconds")
#     return str(x)

from datetime import datetime, timezone

def _dt_to_str(x: Any) -> str:
    """opened_at может быть datetime или строкой. Приводим к ISO UTC."""
    if x is None:
        return ""

    if isinstance(x, datetime):
        dt = x
        if dt.tzinfo is None:
            # если у вас naїve datetime, считаем его UTC
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)

        return dt.isoformat(timespec="seconds").replace("+00:00", "Z")

    return str(x).strip()

def plus500_open_if_enabled(
    pair: str,
    action: str,
    *,
    reason: str,
    trade_amount: Optional[Union[int, float, str]] = None,
    trade_amount_currency: str = "",
    trade_amount_unit: str = "",
) -> bool:
    """
    Вызываем Plus500 OPEN только при реальном OPEN в нашей логике.
    """
    pair = str(pair)
    action = str(action).upper()

    if not PLUS500_ACTIONS_ENABLED:
        logger.info(
            "PLUS500(SKIP disabled) OPEN pair=%s action=%s amount=%s currency=%s unit=%s reason=%s",
            pair, action, trade_amount, trade_amount_currency, trade_amount_unit, reason
        )
        return False

    if PLUS500_DRY_RUN:
        logger.info(
            "PLUS500(DRY_RUN) OPEN pair=%s action=%s amount=%s currency=%s unit=%s reason=%s",
            pair, action, trade_amount, trade_amount_currency, trade_amount_unit, reason
        )
        return True

    ok = send_plus500_open(
        pair=pair,
        action=action,
        trade_amount=trade_amount,
        trade_amount_currency=trade_amount_currency,
        trade_amount_unit=trade_amount_unit,
    )
    logger.info(
        "PLUS500(REAL) OPEN pair=%s action=%s amount=%s currency=%s unit=%s ok=%s reason=%s",
        pair, action, trade_amount, trade_amount_currency, trade_amount_unit, ok, reason
    )
    return ok


def plus500_close_if_enabled(
    pair: str,
    *,
    registered_at: str,
    opened_at: Any,
    position_num: Any,
    reason: str,
) -> bool:
    """
    Вызываем Plus500 CLOSE только при реальном CLOSE в нашей логике.
    """
    pair = str(pair)
    reg = str(registered_at)
    opn = _dt_to_str(opened_at)
    posnum = position_num

    if not PLUS500_ACTIONS_ENABLED:
        logger.info(
            "PLUS500(SKIP disabled) CLOSE pair=%s position_num=%s registered_at=%s opened_at=%s reason=%s",
            pair, posnum, reg, opn, reason
        )
        return False

    if PLUS500_DRY_RUN:
        logger.info(
            "PLUS500(DRY_RUN) CLOSE pair=%s position_num=%s registered_at=%s opened_at=%s reason=%s",
            pair, posnum, reg, opn, reason
        )
        return True

    ok = send_plus500_close(
        pair=pair,
        registered_at=reg,
        opened_at=opn,
        position_num=posnum,
    )  # :contentReference[oaicite:1]{index=1}

    logger.info(
        "PLUS500(REAL) CLOSE pair=%s position_num=%s registered_at=%s opened_at=%s ok=%s reason=%s",
        pair, posnum, reg, opn, ok, reason
    )
    return ok

# def _post(
#     url: str,
#     payload: dict,
#     *,
#     timeout,
#     soft_timeout: bool = False,
#     soft_fail: bool = False,
# ) -> bool:
#     """
#     Унифицированный POST-клиент.
#
#     Возвращает:
#       True  — если сервер ответил 2xx
#       False — если HTTP != 2xx или произошла ошибка/таймаут
#
#     Параметры "soft_*":
#       soft_fail=True    -> ошибки логируются как warning (не как error/exception)
#       soft_timeout=True -> Timeout логируется как warning (ожидаемый/допустимый)
#     """
#     try:
#         r = requests.post(url, json=payload, timeout=timeout)
#
#         # Любой не-2xx считаем провалом.
#         if not r.ok:
#             if soft_fail:
#                 logger.warning("plus500 call not-ok url=%s status=%s", url, r.status_code)
#             else:
#                 # Для жёстких вызовов пишем body, чтобы видеть причину (например, 401/500).
#                 logger.error(
#                     "plus500 call failed url=%s status=%s body=%s",
#                     url,
#                     r.status_code,
#                     r.text,
#                 )
#             return False
#
#         return True
#
#     except Timeout:
#         # Для /prepare таймаут может быть нормой (долгая операция) -> soft_timeout=True
#         if soft_timeout or soft_fail:
#             logger.warning("plus500 timeout (soft) url=%s timeout=%s", url, timeout)
#             return False
#
#         # Для критичных вызовов хотим traceback в логах
#         logger.exception("plus500 timeout url=%s timeout=%s", url, timeout)
#         return False
#
#     except RequestException as e:
#         # Сетевые ошибки requests: ConnectionError, DNS fail, MaxRetryError и т.д.
#         if soft_fail:
#             logger.warning("plus500 request error (soft) url=%s err=%s", url, str(e))
#             return False
#
#         logger.exception("plus500 request error url=%s", url)
#         return False
#
#     except Exception as e:
#         # Любая другая неожиданная ошибка
#         if soft_fail:
#             logger.warning("plus500 unexpected error (soft) url=%s err=%s", url, str(e))
#             return False
#
#         logger.exception("plus500 unexpected error url=%s", url)
#         return False

def _post(
    url: str,
    payload: dict,
    *,
    timeout,
    soft_timeout: bool = False,
    soft_fail: bool = False,
) -> bool:
    """
    Унифицированный POST-клиент.

    True  -> только если HTTP 2xx И body.ok == true
    False -> во всех остальных случаях
    """
    try:
        r = requests.post(url, json=payload, timeout=timeout)

        if not r.ok:
            if soft_fail:
                logger.warning("plus500 call not-ok url=%s status=%s body=%s", url, r.status_code, r.text)
            else:
                logger.error("plus500 call failed url=%s status=%s body=%s", url, r.status_code, r.text)
            return False

        try:
            body = r.json()
        except ValueError:
            # Если сервис вернул не-JSON, это плохой контракт
            if soft_fail:
                logger.warning("plus500 bad json url=%s body=%s", url, r.text)
            else:
                logger.error("plus500 bad json url=%s body=%s", url, r.text)
            return False

        ok = bool(body.get("ok"))
        if not ok:
            worker_result = body.get("worker_result")
            error = body.get("error")
            if soft_fail:
                logger.warning(
                    "plus500 logical fail url=%s ok=%s error=%s worker_result=%s",
                    url, ok, error, worker_result
                )
            else:
                logger.error(
                    "plus500 logical fail url=%s ok=%s error=%s worker_result=%s",
                    url, ok, error, worker_result
                )
            return False

        return True

    except Timeout:
        if soft_timeout or soft_fail:
            logger.warning("plus500 timeout (soft) url=%s timeout=%s", url, timeout)
            return False

        logger.exception("plus500 timeout url=%s timeout=%s", url, timeout)
        return False

    except RequestException:
        if soft_fail:
            logger.warning("plus500 request error (soft) url=%s", url, exc_info=True)
            return False

        logger.exception("plus500 request error url=%s", url)
        return False

    except Exception as e:
        if soft_fail:
            logger.warning("plus500 unexpected error (soft) url=%s err=%s", url, str(e))
            return False

        logger.exception("plus500 unexpected error url=%s", url)
        return False

def send_plus500_prepare(
    instrument: Optional[str] = None,
    prepare_side: Optional[str] = None,
) -> bool:
    """
    Вызывает /prepare на Plus500-боте.

    instrument / prepare_side — опциональные подсказки боту:
      - instrument: какой инструмент открыть/подготовить
      - prepare_side: какая сторона (BUY/SELL) нужна для подготовки UI

    Важно:
      /prepare может выполняться долго -> большой read-timeout.
      Ошибки и таймауты обрабатываются "мягко" (warning),
      чтобы не шуметь error-логами в циклическом режиме.
    """
    payload = {"secret": PLUS500_SIGNAL_SECRET}
    if instrument is not None:
        payload["instrument"] = instrument
    if prepare_side is not None:
        payload["prepare_side"] = prepare_side

    # Секрет в лог не пишем.
    logger.info("Plus500 -> /prepare payload=%s", {k: v for k, v in payload.items() if k != "secret"})

    return _post(
        PLUS500_PREPARE_URL,
        payload,
        timeout=PLUS500_PREPARE_TIMEOUT,
        soft_timeout=True,
        soft_fail=True,
    )


# =========================
# NEW: open / close
# =========================
def send_plus500_open(
    pair: str,
    action: Action,
    *,
    trade_amount: Optional[Union[int, float, str]] = None,
    trade_amount_currency: str = "",
    trade_amount_unit: str = "",
) -> bool:
    """
    Открытие позиции.
    Передаём:
      - pair: торговая пара/инструмент (например "GBPUSD")
      - action: "BUY" или "SELL"
    """
    payload = {
        "secret": PLUS500_SIGNAL_SECRET,
        "pair": pair,
        "action": action,
    }

    if trade_amount is not None and str(trade_amount).strip():
        payload["trade_amount"] = str(trade_amount).strip()
        payload["amount"] = str(trade_amount).strip()

    if trade_amount_currency:
        payload["trade_amount_currency"] = str(trade_amount_currency).strip()

    if trade_amount_unit:
        payload["trade_amount_unit"] = str(trade_amount_unit).strip()

    # секрет не логируем
    logger.info(
        "Plus500 -> /open pair=%s action=%s amount=%s currency=%s unit=%s",
        pair,
        action,
        payload.get("trade_amount", ""),
        payload.get("trade_amount_currency", ""),
        payload.get("trade_amount_unit", ""),
    )

    # Открытие обычно важно: если не дошло — хотим error/exception в логах.
    return _post(PLUS500_OPEN_URL, payload, timeout=PLUS500_HTTP_TIMEOUT)


def send_plus500_close(
    pair: str,
    registered_at: str,
    opened_at: str,
    position_num: Union[int, str],
) -> bool:
    """
    Закрытие позиции.
    Передаём:
      - pair: торговая пара/инструмент
      - registered_at: время регистрации поля (ISO string)
      - opened_at: время открытия позиции (ISO string)
      - position_num: номер позиции (int)
    """
    payload = {
        "secret": PLUS500_SIGNAL_SECRET,
        "pair": pair,
        "registered_at": registered_at,
        "opened_at": opened_at,
        "position_num": int(position_num),
    }

    logger.info(
        "Plus500 -> /close pair=%s position_num=%s registered_at=%s opened_at=%s",
        pair,
        payload["position_num"],
        registered_at,
        opened_at,
    )

    # Закрытие тоже обычно критично (иначе позиция останется висеть).
    # Если у тебя закрытие "best effort", можно поставить soft_fail=True.
    return _post(PLUS500_CLOSE_POSITION_URL, payload, timeout=PLUS500_HTTP_TIMEOUT)


def send_plus500_close_page() -> bool:
    """
    Вызывает /close_page (best-effort).

    Закрытие страницы часто не критично:
      - бот мог уже упасть
      - страницы могло не быть
    Поэтому ошибки логируются мягко (soft_fail=True).
    """
    payload = {"secret": PLUS500_SIGNAL_SECRET}
    logger.info("Plus500 -> /close_page")
    return _post(PLUS500_CLOSE_PAGE_URL, payload, timeout=PLUS500_HTTP_TIMEOUT, soft_fail=True)

