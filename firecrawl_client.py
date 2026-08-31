# firecrawl_client.py
import json
import os
import time
import logging
import requests
from typing import Optional

from .telegram_client import notify_error
from .models import InstrumentResult
from .nums import parse_price
from .parsers import parse_timeframes, parse_technical_indicators, parse_pivot_points
from .time_utils import now_formatted
from .html_utils import get_raw_text_from_json, inner_text, detect_antibot_page
from .config import FIRECRAWL_API_KEY, FIRECRAWL_URL, InstrumentConfig, TIMEZONE, SIM_FIRECRAWL_REQUESTS, MAX_RETRIES, \
    RETRY_DELAY_SECONDS
import re
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

import os, json, re, time
from typing import Optional, Dict, List, Any

def _env_key_from_code(code: str) -> str:
    s = (code or "").upper()
    s = s.replace("&", "")                 # S&P500 -> SP500
    s = re.sub(r"[^A-Z0-9]+", "_", s)      # всё лишнее -> _
    return s.strip("_")

def _load_sim_payload_for(instr: InstrumentConfig) -> Optional[dict]:
    """
    ENV:
      SIM_RESULT_<CODE> = JSON
    пример:
      SIM_RESULT_SP500='{"price_display":"6914,7","price_number":6914.7,"timeframes":{"30m":{"action":"STRONG BUY"},"1h":{"action":"BUY"},"5h":{"action":"STRONG SELL"}}}'
    """
    key = f"SIM_RESULT_{_env_key_from_code(instr.code)}"
    raw = (os.getenv(key, "") or "").strip()
    if not raw:
        logger.error("SIM: %s пуст — нет данных симуляции для %s", key, instr.code)
        return None
    try:
        return json.loads(raw)
    except Exception:
        logger.exception("SIM: плохой JSON в %s (%s)", key, instr.code)
        return None

def process_instrument(instr: InstrumentConfig, executed_at_override: Optional[str]) -> Optional[InstrumentResult]:
    logger.info("Обработка инструмента %s (%s)", instr.name, instr.url)

    # =====================================================================
    # 1) SIMULATION BRANCH (fast path)
    # =====================================================================
    if SIM_FIRECRAWL_REQUESTS:
        payload = _load_sim_payload_for(instr)
        if not payload:
            last_reason = "SIM payload missing/invalid"
            err_text = f"SIM: Не удалось получить симулированные данные для {instr.name} ({instr.url}). Причина: {last_reason}"
            logger.error(err_text)
            notify_error(f"❌ {err_text}")
            return None

        price_display = payload.get("price_display")
        price_number = float(payload.get("price_number") or 0)
        timeframes = payload.get("timeframes") or {}
        tech = payload.get("technical_indicators") or {}
        pivots = payload.get("pivot_points") or {}

        if price_display is None or not timeframes:
            last_reason = f"SIM invalid payload: price_display={price_display}, len(timeframes)={len(timeframes)}"
            err_text = f"SIM: Некорректные симулированные данные для {instr.name} ({instr.url}). {last_reason}"
            logger.error(err_text)
            notify_error(f"❌ {err_text}")
            return None

        executed_at_local = executed_at_override or now_formatted(TIMEZONE)
        return InstrumentResult(
            instrument=instr,
            price_display=price_display,
            price_number=price_number,
            timeframes=timeframes,
            technical_indicators=tech,
            pivot_points=pivots,
            executed_at=executed_at_local,
        )

    # =====================================================================
    # 2) REAL BRANCH (original logic unchanged)
    # =====================================================================
    last_reason = "неизвестно"

    for attempt in range(1, MAX_RETRIES + 1):
        html = fetch_html_via_firecrawl(instr.url)
        if not html:
            last_reason = "не получили HTML от Firecrawl"
        else:
            price_display, price_number = parse_price(html, instr.url)
            timeframes = parse_timeframes(html)
            tech = parse_technical_indicators(html)
            pivots = parse_pivot_points(html)

            if price_display is not None and timeframes:
                executed_at_local = executed_at_override or now_formatted(TIMEZONE)
                return InstrumentResult(
                    instrument=instr,
                    price_display=price_display,
                    price_number=price_number,
                    timeframes=timeframes,
                    technical_indicators=tech,
                    pivot_points=pivots,
                    executed_at=executed_at_local,
                )

            if attempt == MAX_RETRIES:
                html_snippet = inner_text(html)[:400]
                logger.error(
                    "Парсер не смог получить цену/таймфреймы для %s. price_display=%r len(timeframes)=%d snippet=%r",
                    instr.name, price_display, len(timeframes), html_snippet
                )
            last_reason = f"price_display={price_display}, len(timeframes)={len(timeframes)}"

        if attempt < MAX_RETRIES:
            logger.warning(
                "Попытка %d/%d для %s неуспешна (%s). Повтор через %d секунд",
                attempt, MAX_RETRIES, instr.name, last_reason, RETRY_DELAY_SECONDS
            )
            time.sleep(RETRY_DELAY_SECONDS)

    err_text = f"Не удалось спарсить инструмент {instr.name} ({instr.url}) за {MAX_RETRIES} попытки. Последняя причина: {last_reason}"
    logger.error(err_text)
    notify_error(f"❌ {err_text}")
    return None

def fetch_html_via_firecrawl(url: str) -> Optional[str]:
    if not FIRECRAWL_API_KEY:
        logger.error("FIRECRAWL_API_KEY не задан")
        return None

    ts = int(time.time() * 1000)
    # добавляем anti-cache параметр, как {{Date.now()}}
    sep = "&" if "?" in url else "?"
    full_url = f"{url}{sep}_={ts}"

    payload = {
        "url": full_url,
        "formats": ["html"],
        "waitFor": 500,
        "proxy": "auto",
    }
    headers = {
        "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
        "Content-Type": "application/json",
    }

    logger.info("Запрашиваю Firecrawl для %s", full_url)

    try:
        resp = requests.post(
            FIRECRAWL_URL,
            headers=headers,
            json=payload,
            timeout=60,
        )
    except requests.exceptions.ReadTimeout as e:
        logger.error(
            "ReadTimeout при запросе к Firecrawl для %s: %s",
            full_url,
            e,
        )
        return None
    except Exception as e:
        logger.exception(
            "Исключение при запросе к Firecrawl для %s: %s",
            full_url,
            e,
        )
        return None

    status = resp.status_code
    text_snippet = (resp.text or "")[:500]

    # 403 Forbidden
    if status == 403:
        logger.error(
            "Firecrawl вернул 403 Forbidden для %s. Сниппет ответа: %r",
            full_url,
            text_snippet,
        )
        return None

    # Любая 5xx ошибка (500, 502, 503, 504 и т.п.)
    if 500 <= status <= 599:
        logger.error(
            "Firecrawl вернул %s (5xx) для %s. Сниппет ответа: %r",
            status,
            full_url,
            text_snippet,
        )
        return None

    # Другие неожиданные коды (4xx, 3xx и т.п.)
    if not resp.ok:
        logger.error(
            "Firecrawl вернул неожиданный статус %s для %s. Тело ответа (сниппет): %r",
            status,
            full_url,
            text_snippet,
        )
        return None

    # Если HTTP ок, парсим JSON
    try:
        data = resp.json()
    except Exception as e:
        logger.exception(
            "Не удалось распарсить JSON Firecrawl для %s. Сниппет ответа: %r. Ошибка: %s",
            full_url,
            text_snippet,
            e,
        )
        return None

    html = get_raw_text_from_json(data)
    if not html:
        logger.error(
            "Не удалось извлечь HTML из ответа Firecrawl для %s. Сниппет JSON: %r",
            full_url,
            str(data)[:500],
        )
        return None

    # Проверка на страницу антибот-защиты / доступа
    antibot_reason = detect_antibot_page(html)
    if antibot_reason:
        logger.error(
            "Получена страница антибот-защиты для %s (%s). Текстовый сниппет: %r",
            full_url,
            antibot_reason,
            inner_text(html)[:400],
        )
        return None

    return html