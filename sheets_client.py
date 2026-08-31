# sheets_client.py
import logging
import time
from typing import Optional, Dict, Any

import gspread
from google.oauth2.service_account import Credentials

from . import config
from .telegram_client import notify_error
from .config import SHEET_NAME_BOT
from .config import MAX_RETRIES, RETRY_DELAY_SECONDS
from .models import InstrumentResult

logger = logging.getLogger(__name__)

def create_gspread_client() -> gspread.Client:
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(
        config.GOOGLE_SERVICE_ACCOUNT_FILE,
        scopes=scopes,
    )
    return gspread.authorize(creds)

def append_row_for_instrument(
    gc: gspread.Client,
    sheet_id: str,
    instrument_result: InstrumentResult,
    executed_at: str,
) -> bool:
    """
    Добавляет строку в лист с ретраями.
    Возвращает True при успехе, False при полном провале (после MAX_RETRIES).
    При провале шлёт уведомление об ошибке.
    """
    instr = instrument_result.instrument

    tf = instrument_result.timeframes

    def tf_action(key: str) -> str:
        return (tf.get(key) or {}).get("action", "")

    # структура ровно как в n8n:
    # TIME, PRICE, 30 MIN, HOURLY, 5 HOURS, DAILY, WEEKLY, MONTHLY
    row = [
        executed_at,
        instrument_result.price_display or "",
        tf_action("30m"),
        tf_action("1h"),
        tf_action("5h"),
        tf_action("1d"),
        tf_action("1w"),
        tf_action("1mo"),
    ]

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            sh = gc.open_by_key(sheet_id)
            ws = sh.worksheet(instr.sheet_name)
        except Exception as e:
            last_error = e
            logger.exception(
                "Не удалось открыть таблицу/лист для %s (%s) на попытке %d/%d",
                instr.name,
                instr.sheet_name,
                attempt,
                MAX_RETRIES,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
            continue

        try:
            ws.append_row(row, value_input_option="USER_ENTERED")
            logger.info(
                "Добавили строку в лист %s (%s) на попытке %d/%d: %s",
                instr.sheet_name,
                instr.name,
                attempt,
                MAX_RETRIES,
                row,
            )
            return True
        except Exception as e:
            last_error = e
            logger.exception(
                "Не удалось добавить строку для %s на попытке %d/%d",
                instr.name,
                attempt,
                MAX_RETRIES,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)

    # все попытки провалились
    err_text = (
        f"Не удалось добавить строку в Google Sheets для {instr.name} "
        f"(лист '{instr.sheet_name}') за {MAX_RETRIES} попытки. Ошибка: {last_error}"
    )
    logger.error(err_text)
    notify_error(f"❌ {err_text}")
    return False


def fetch_bot_message(
    gc: gspread.Client,
    sheet_id: str,
    executed_at: str,
    sheet_name_bot: str = SHEET_NAME_BOT,
) -> Optional[Dict[str, str]]:
    """
    Ищет в листе 'Бот' строку с заданным executed_at (колонка 'Время').
    Возвращает dict {"time": <строка>, "message": <строка>} или None.
    """
    try:
        sh = gc.open_by_key(sheet_id)
        ws = sh.worksheet(sheet_name_bot)
    except Exception as e:
        logger.exception("Не удалось открыть лист с ботом (%s): %s", sheet_name_bot, e)
        return None

    try:
        records = ws.get_all_records()
    except Exception as e:
        logger.exception("Не удалось прочитать лист '%s': %s", sheet_name_bot, e)
        return None

    for row in reversed(records):
        if str(row.get("Время")) == executed_at:
            msg = (
                row.get("СООБЩЕНИЕ БОТА")
                or row.get("Сообщение бота")
                or row.get("message")
            )
            if msg:
                logger.info(
                    "Нашли строку бота во времени %s в листе '%s'",
                    executed_at,
                    sheet_name_bot,
                )
                return {
                    "time": str(row.get("Время")),
                    "message": str(msg),
                }

    logger.info("Сообщение для времени %s в листе '%s' не найдено", executed_at, sheet_name_bot)
    return None


def is_bot_message_error(msg: str) -> bool:
    """
    Проверяем, что в сообщении бота ошибка формулы (типа #REF!).
    Если такая ошибка есть — сообщение НЕ рассылаем, а шлём алерт в error-чат.
    """
    if not msg:
        return False

    upper = msg.upper()

    error_markers = [
        "#REF!",
        "REFERENCE DOES NOT EXIST",
        "#VALUE!",
        "#NAME?",
        "#N/A",
        "DIV/0!",
    ]
    return any(marker in upper for marker in error_markers)
