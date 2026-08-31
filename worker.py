# worker.py
import os
import logging
import datetime

from .phase2_flip_alerts import phase2_strong_flip_alerts
from .phase1_logic import phase1_process_instrument
from . import config
from .config import INSTRUMENTS, InstrumentConfig, INTERVAL_SECONDS, TIMEZONE, \
    GOOGLE_SHEET_ID, SHEETS_WRITE_ENABLED, SIM_FIRECRAWL_REQUESTS
from .time_utils import (
    now_formatted,
    next_half_hour_slot,
    _now_tz
)

from .firecrawl_client import  process_instrument
from .sheets_client import create_gspread_client, append_row_for_instrument
from .plus500_client import send_plus500_prepare, send_plus500_close_page
from .models import InstrumentResult

from .db_client import (
    insert_instrument_data,
    insert_strong_signal
)

from collections import Counter

plus500_stats = Counter()


logger = logging.getLogger(__name__)
PREPARE_OFFSET_MIN = float(os.getenv("PREPARE_OFFSET_MIN", "1.3"))
PREPARE_ENABLED = os.getenv("PREPARE_ENABLED", "1")
PREPARE_DAYS_RAW = os.getenv("PREPARE_DAYS", "mon,tue,wed,thu,fri").strip()

_DAY_NAME_TO_NUM = {
    "mon": 0, "monday": 0,
    "tue": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

def _parse_prepare_days(raw: str) -> set[int]:
    out: set[int] = set()
    for part in (raw or "").split(","):
        key = part.strip().lower()
        if not key:
            continue

        if key in _DAY_NAME_TO_NUM:
            out.add(_DAY_NAME_TO_NUM[key])
            continue

        try:
            n = int(key)
        except ValueError:
            logger.warning("PREPARE_DAYS: unknown value '%s' — skip", part)
            continue

        if 0 <= n <= 6:
            out.add(n)
        else:
            logger.warning("PREPARE_DAYS: day number out of range '%s' — skip", part)

    return out

PREPARE_DAYS = _parse_prepare_days(PREPARE_DAYS_RAW)

def _should_prepare_by_day(dt: datetime.datetime) -> bool:
    """
    Prepare должен идти в том же временном окне, где может быть рабочий парсинг/сигнал:

    start: понедельник 00:00
    end:   суббота 00:00 включительно

    run_once не трогаем. Меняем только окно, в котором разрешён /prepare.
    """
    enabled = str(PREPARE_ENABLED).strip().lower() in ("1", "true", "yes", "y", "on")
    if not enabled:
        return False

    wd = dt.weekday()  # 0=Mon ... 5=Sat ... 6=Sun

    # Понедельник-пятница: все слоты 00:00 / 00:30 / ... / 23:30
    if 0 <= wd <= 4:
        return True

    # Суббота 00:00 — последний слот перехода пятница -> суббота
    if wd == 5 and dt.hour == 0 and dt.minute == 0:
        return True

    return False

STRONG_FLIP_ALERT_CODES = {
    c.strip().upper()
    for c in os.getenv("STRONG_FLIP_ALERT_CODES", "BITCOIN").split(",")
    if c.strip()
}

TradeMode = str  # "TRADE" | "CLOSE_ONLY" | "PAUSE"

_TF_TG_LABEL = {
    "30m": "30м",
    "1h": "1ч",
    "5h": "5ч",
    "1d": "1д",
    "1w": "1н",
    "1mo": "1мес",
}

# --- TG flip alerts for FX on multiple TFs ---
def _csv_set(env_name: str, default: str) -> set[str]:
    return {x.strip() for x in os.getenv(env_name, default).split(",") if x.strip()}

STRONG_FLIP_ALERT_TFS = tuple(
    x.strip()
    for x in os.getenv("STRONG_FLIP_ALERT_TFS", "30m,1h,5h").split(",")
    if x.strip()
)


def _is_fx_pair(instr: InstrumentConfig) -> bool:
    """
    Валютная пара = есть plus500_name со слэшем (EUR/GBP, GBP/USD, ...),
    и это не индекс/крипта.
    """
    code = (instr.code or "").upper()
    if code in ("S&P500", "BITCOIN", "SOLBTC"):
        return False
    p = (instr.plus500_name or "")
    return ("/" in p)  # простой и надёжный признак FX в твоей конфигурации

def _telegram_tf_label(tf_key: str) -> str:
    return _TF_TG_LABEL.get(str(tf_key), str(tf_key))

def _tg_short(instr: InstrumentConfig, tf_key: str, signal: str) -> str:
    # пример: "usdjpy 5h strong sell."
    code = (instr.code or instr.name or "").lower()
    return f"{code} {str(tf_key)} {str(signal).lower()}"

def _format_slot(dt: datetime.datetime) -> str:
    # нужно: 17.12.2025 13.30
    return dt.strftime("%d.%m.%Y %H.%M")

def validate_required_env() -> bool:
    ok = True

    if not config.FIRECRAWL_API_KEY:
        logger.error("FIRECRAWL_API_KEY не задан — невозможно получать HTML через Firecrawl.")
        ok = False

    if config.SHEETS_WRITE_ENABLED and not config.GOOGLE_SHEET_ID:
        logger.error("GOOGLE_SHEET_ID не задан — невозможно писать в таблицу (SHEETS_WRITE_ENABLED=1).")
        ok = False

    if not config.PLUS500_PREPARE_URL:
        logger.error("PLUS500_PREPARE_URL не задан — /prepare не будет вызываться.")
        ok = False

    if not config.PLUS500_OPEN_URL:
        logger.error("PLUS500_OPEN_URL не задан — /open не будет вызываться.")
        ok = False

    if not config.PLUS500_CLOSE_POSITION_URL:
        logger.error("PLUS500_CLOSE_POSITION_URL не задан — /close или /clouse не будет вызываться.")
        ok = False

    if not config.PLUS500_CLOSE_PAGE_URL:
        logger.error("PLUS500_CLOSE_PAGE_URL не задан — /close_page не будет вызываться.")
        ok = False

    return ok


def _mode_priority(mode: TradeMode) -> int:
    return {"TRADE": 2, "CLOSE_ONLY": 1, "PAUSE": 0}.get(mode, 0)


def _telegram_time_now() -> str:
    # Требуемый формат для Telegram: 2025-12-19 03:01:15
    return _now_tz(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

def _telegram_time_from_slot(executed_at: str) -> str:
    """Показываем время слота в нужном для Telegram формате.
    executed_at в Google остаётся как есть (обычно 'dd.mm.YYYY HH.MM').
    """
    for fmt in ("%d.%m.%Y %H.%M", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(executed_at, fmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return _telegram_time_now()


def _env_flag(name: str, default: str = "0") -> bool:
    v = os.getenv(name, default).strip().lower()
    return v in ("1", "true", "yes", "y", "on")


PLUS500_PREPARE_PER_SIGNAL = _env_flag("PLUS500_PREPARE_PER_SIGNAL", "1")
PARSER_MAX_WORKERS = int(os.getenv("PARSER_MAX_WORKERS", "8"))

import os
import time
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Optional, List, Dict, Tuple

def run_once(executed_at_override: Optional[str] = None) -> None:
    def _tf_action(res: InstrumentResult, tf_key: str) -> Optional[str]:
        try:
            return (res.timeframes.get(tf_key) or {}).get("action")
        except Exception:
            return None

    def _tf_compact(res: InstrumentResult, tfs: List[str]) -> str:
        parts = []
        for tf in tfs:
            a = _tf_action(res, tf)
            parts.append(f"{tf}={a or '-'}")
        return " | ".join(parts)

    def _wait_and_process(codes: List[str]) -> List[Tuple[str, InstrumentResult]]:
        out: List[Tuple[str, InstrumentResult]] = []
        if not codes:
            logger.info("WAIT+PROCESS: пусто — пропуск.")
            return out

        logger.info("WAIT+PROCESS: start | codes=%s", ", ".join(codes))

        for code in codes:
            fut = fetch_futures.get(code)
            if not fut:
                logger.error("WAIT+PROCESS: нет future для %s", code)
                continue

            # НЕ делаем запросов — только ждём результат prefetch
            try:
                res, dt_sec = fut.result()
            except Exception:
                logger.exception("WAIT+PROCESS: fetch crashed for %s", code)
                continue

            if not res:
                logger.error("WAIT+PROCESS: fetch returned None for %s (dt=%.2fs)", code, dt_sec)
                continue

            logger.info(
                "WAIT+PROCESS: GOT %s (dt=%.2fs) price=%s num=%s | tfs: %s",
                code,
                dt_sec,
                res.price_display,
                res.price_number,
                _tf_compact(res, ["30m", "1h", "5h", "1d", "1w", "1mo"]),
            )

            # Вся логика “что делать сегодня” внутри phase1_process_instrument
            # (в т.ч. CLOSE-only даже когда SIGNAL disabled, если ты уже внедрила это в phase1_logic)
            try:
                instr = instr_by_code.get(code)
                if not instr:
                    logger.error("WAIT+PROCESS: unknown instrument %s (нет в INSTRUMENTS)", code)
                else:
                    phase1_process_instrument(instr, res, now_dt=now_dt, executed_at=executed_at)
            except Exception:
                logger.exception("WAIT+PROCESS: phase1_process_instrument crashed for %s", code)

            out.append((code, res))

        logger.info("WAIT+PROCESS: done | results=%d", len(out))
        return out

    logger.info("Старт задачи 'pounds'")

    if not validate_required_env():
        logger.error("ENV невалиден — остановка run_once()")
        return

    now_dt = _now_tz(TIMEZONE)
    executed_at = executed_at_override or now_formatted(TIMEZONE)

    instr_by_code = {i.code: i for i in INSTRUMENTS}

    # Берём ВСЕ инструменты (без высчитывания лишних)
    fetch_queue: List[str] = [i.code for i in INSTRUMENTS if i and getattr(i, "code", None)]
    if not fetch_queue:
        logger.info("Нет инструментов для обработки — выходим.")
        return

    # 1) PREFETCH: запускаем ВСЕ парсинги сразу (process_instrument сам решит SIM/REAL)
    max_fetch_workers = int(os.getenv("FIRECRAWL_MAX_WORKERS", "6"))
    fetch_futures: Dict[str, Future[Tuple[Optional[InstrumentResult], float]]] = {}

    logger.info(
        "PREFETCH: start | total=%d | workers=%d | simulate=%s | queue=%s",
        len(fetch_queue),
        max_fetch_workers,
        bool(SIM_FIRECRAWL_REQUESTS),
        " -> ".join(fetch_queue),
    )

    fetch_executor = ThreadPoolExecutor(max_workers=max_fetch_workers)

    for code in fetch_queue:
        instr = instr_by_code.get(code)
        if not instr:
            logger.error("PREFETCH: unknown instrument %s (нет в INSTRUMENTS)", code)
            continue
        if code in fetch_futures:
            continue

        def _task(_instr=instr):
            t0 = time.monotonic()
            # ВАЖНО: один раз получаем результат (SIM/REAL внутри process_instrument)
            res = process_instrument(_instr, executed_at_override)
            dt_sec = time.monotonic() - t0
            return res, dt_sec

        fetch_futures[code] = fetch_executor.submit(_task)

    # 2) WAIT+PROCESS: один проход, никакой второй фазы/повторных обработок
    results_all = _wait_and_process(fetch_queue)

    phase2_strong_flip_alerts(results_all, executed_at=executed_at)

    # 3) Закрываем executor
    fetch_executor.shutdown(wait=False)
    logger.info("run_once: done | results_all=%d", len(results_all))

    _STRONG = {"STRONG BUY", "STRONG SELL"}  # если нет в worker.py

    # --- DB persist for ALL results (SIM and REAL) ---
    persisted_codes = set()

    for code, res in results_all:
        if not res:
            continue
        if code in persisted_codes:
            continue
        persisted_codes.add(code)

        # 1) instrument_data snapshot
        try:
            insert_instrument_data(code, executed_at, res.price_number, res.timeframes)
            logger.info("DB: instrument_data saved | %s | executed_at=%s", code, executed_at)
        except Exception:
            logger.exception("DB: insert_instrument_data failed | %s", code)

        # 2) instrument_signals (strong history)
        try:
            for tf_k, obj in (res.timeframes or {}).items():
                act = (obj or {}).get("action")
                if act in _STRONG:
                    insert_strong_signal(code, executed_at, res.price_number, act, timeframe=str(tf_k))
            logger.info("DB: instrument_signals saved | %s | executed_at=%s", code, executed_at)
        except Exception:
            logger.exception("DB: insert_strong_signal failed | %s", code)

    # ---------------------------------------------------------------------
    # 5) Plus500 close_page + Sheets (по флагу) — адаптировано под results_all
    # ---------------------------------------------------------------------
    try:
        logger.info("Plus500 -> /close_page (end of run_once)")
        ok_close = send_plus500_close_page()
        if ok_close:
            plus500_stats["closed_no_action"] += 1
            logger.info("Plus500: /close_page ok (действий нет).")
        else:
            plus500_stats["fail_close_page"] += 1
            logger.error("Plus500: /close_page FAILED (действий нет).")
    except Exception:
        logger.exception("Plus500: ошибка при /close_page (действий нет).")

    # ---- Google Sheets append (по флагу) ----
    if not SHEETS_WRITE_ENABLED:
        logger.info("Google Sheets запись отключена (SHEETS_WRITE_ENABLED=0) — пропускаем.")
        logger.info("Готово. Plus500 stats=%s", dict(plus500_stats))
        return

    gc = None
    try:
        gc = create_gspread_client()
        logger.info("Google Sheets клиент создан.")
    except Exception:
        logger.exception("Не удалось создать Google Sheets клиент")
        logger.info("Готово. Plus500 stats=%s", dict(plus500_stats))
        return

    # Пишем в таблицу по каждому полученному результату (без повторных запросов, только results_all)
    for code, res in results_all:
        if not res:
            continue
        try:
            # executed_at у нас общий на run_once
            ok = append_row_for_instrument(gc, GOOGLE_SHEET_ID, res, executed_at)
            if not ok:
                logger.error("Google Sheets: строка для %s НЕ добавлена (time=%s)", code, executed_at)
            else:
                logger.debug("Google Sheets: строка для %s добавлена (time=%s)", code, executed_at)
        except Exception:
            logger.exception("Google Sheets: ошибка записи для %s", code)

    logger.info("Готово. Plus500 stats=%s", dict(plus500_stats))


def run_forever() -> None:
    interval = INTERVAL_SECONDS
    if interval <= 0:
        logger.error("INTERVAL_SECONDS <= 0 — циклический режим выключен")
        return

    logger.info(
        "Запускаю бесконечный цикл по сетке 00/30 минут (таймзона %s). PREPARE_ENABLED=%s PREPARE_DAYS=%s PREPARE_OFFSET_MIN=%.2f",
        TIMEZONE,
        PREPARE_ENABLED,
        PREPARE_DAYS_RAW,
        PREPARE_OFFSET_MIN,
    )

    while True:
        slot_dt = next_half_hour_slot(TIMEZONE)
        now_dt = _now_tz(TIMEZONE)
        human_slot = _format_slot(slot_dt)

        should_prepare = _should_prepare_by_day(slot_dt)
        prepare_dt = slot_dt - datetime.timedelta(minutes=PREPARE_OFFSET_MIN)

        if should_prepare:
            if prepare_dt > now_dt:
                sleep_to_prepare = int(max(0, (prepare_dt - now_dt).total_seconds()))
                logger.info(
                    "Следующий слот: %s. Сегодня prepare РАЗРЕШЁН. Спим %d сек до /prepare.",
                    human_slot,
                    sleep_to_prepare,
                )
                if sleep_to_prepare:
                    time.sleep(sleep_to_prepare)
            else:
                logger.info(
                    "Следующий слот: %s. Сегодня prepare РАЗРЕШЁН. /prepare запускается сразу.",
                    human_slot,
                )

            try:
                logger.info(
                    "Plus500: pre-slot /prepare START sync (slot=%s, weekday=%s, offset=%.2f min)",
                    human_slot,
                    slot_dt.weekday(),
                    PREPARE_OFFSET_MIN,
                )
                t0 = time.monotonic()
                ok_prepare = send_plus500_prepare()
                dt_sec = time.monotonic() - t0

                if ok_prepare:
                    logger.info(
                        "Plus500: pre-slot /prepare DONE ok (slot=%s, duration=%.2fs)",
                        human_slot,
                        dt_sec,
                    )
                else:
                    logger.warning(
                        "Plus500: pre-slot /prepare DONE not-ok (slot=%s, duration=%.2fs)",
                        human_slot,
                        dt_sec,
                    )
            except Exception:
                logger.exception(
                    "Plus500: pre-slot /prepare FAILED with exception (slot=%s)",
                    human_slot,
                )
        else:
            logger.info(
                "Следующий слот: %s. Сегодня prepare ЗАПРЕЩЁН по PREPARE_DAYS=%s — пропускаем /prepare.",
                human_slot,
                PREPARE_DAYS_RAW,
            )

        now_dt = _now_tz(TIMEZONE)
        sleep_to_slot = int(max(0, (slot_dt - now_dt).total_seconds()))
        logger.info("Спим %d сек до запуска run_once() для слота %s.", sleep_to_slot, human_slot)
        if sleep_to_slot:
            time.sleep(sleep_to_slot)

        executed_at = human_slot
        logger.info("Запуск задачи в слоте executedAt=%s", executed_at)

        try:
            run_once(executed_at_override=executed_at)
        except Exception:
            logger.exception("Исключение внутри run_once(), продолжаю цикл")