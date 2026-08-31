import logging
import time
from typing import Iterable, List, Optional, Tuple

import requests

from .config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_IDS,
    TELEGRAM_ERROR_CHAT_ID,
    MAX_RETRIES,
    RETRY_DELAY_SECONDS,
)

logger = logging.getLogger(__name__)

TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_TIMEOUT_SEC = 15

# Если захочешь разметку — включай "MarkdownV2" и используй escape ниже.
DEFAULT_PARSE_MODE: Optional[str] = None  # "MarkdownV2" или None


def _normalize_text(text: str) -> str:
    """Нормализуем переносы и типы."""
    if text is None:
        return ""
    s = str(text)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return s


def _split_telegram_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> List[str]:
    """
    Разбиваем длинный текст на несколько сообщений <= limit.
    Стратегия:
    - сначала по '\n\n'
    - затем по '\n'
    - затем по пробелам
    - затем жёстко режем
    """
    text = _normalize_text(text).strip()
    if not text:
        return ["(empty)"]

    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    remaining = text

    def take_by_sep(sep: str, s: str) -> Tuple[Optional[str], str]:
        """Возьмём максимально большой префикс <= limit по разделителю sep."""
        if len(s) <= limit:
            return s, ""
        parts = s.split(sep)
        buf: List[str] = []
        cur_len = 0
        for i, p in enumerate(parts):
            piece = p if i == 0 else sep + p
            if cur_len + len(piece) > limit:
                break
            buf.append(piece)
            cur_len += len(piece)
        if not buf:
            return None, s
        head = "".join(buf).strip()
        tail = s[len("".join(buf)) :].lstrip()
        return head, tail

    # 1) по двойным переносам
    while remaining and len(remaining) > limit:
        head, tail = take_by_sep("\n\n", remaining)
        if head:
            chunks.append(head)
            remaining = tail
            continue

        # 2) по одиночным переносам
        head, tail = take_by_sep("\n", remaining)
        if head:
            chunks.append(head)
            remaining = tail
            continue

        # 3) по пробелам
        head, tail = take_by_sep(" ", remaining)
        if head:
            chunks.append(head)
            remaining = tail
            continue

        # 4) жёсткая нарезка
        chunks.append(remaining[:limit])
        remaining = remaining[limit:].lstrip()

    if remaining:
        chunks.append(remaining)

    # Добавим метку, если дробили
    if len(chunks) > 1:
        chunks[0] = chunks[0] + "\n\n(1/{})".format(len(chunks))
        for i in range(1, len(chunks)):
            chunks[i] = chunks[i] + "\n\n({}/{})".format(i + 1, len(chunks))

    return chunks


def _escape_markdown_v2(text: str) -> str:
    """
    Экранирование для MarkdownV2. Используй только если включаешь parse_mode="MarkdownV2".
    """
    # Telegram MarkdownV2 reserved: _ * [ ] ( ) ~ ` > # + - = | { } . !
    specials = r"_*[]()~`>#+-=|{}.!\\"
    out = []
    for ch in text:
        if ch in specials:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _send_telegram_single(
    chat_id: str,
    text: str,
    *,
    parse_mode: Optional[str] = DEFAULT_PARSE_MODE,
) -> bool:
    """
    Отправляет одно сообщение в один чат.
    Возвращает True/False. Исключения наружу не выпускает.
    Обрабатывает 429 (retry_after) как "мягкий" фейл (False), чтобы внешний ретрай поспал.
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN не задан, Telegram-уведомления отключены")
        return False

    base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": str(chat_id),
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        resp = requests.post(base, json=payload, timeout=TELEGRAM_TIMEOUT_SEC)
    except Exception as e:
        logger.exception("Исключение при отправке Telegram в чат %s: %s", chat_id, e)
        return False

    if resp.ok:
        logger.info("Отправили Telegram в чат %s", chat_id)
        return True

    # Ошибка: попробуем распарсить json, чтобы понять 429 retry_after
    retry_after = None
    try:
        j = resp.json()
        params = (j.get("parameters") or {})
        retry_after = params.get("retry_after")
    except Exception:
        pass

    if resp.status_code == 429 and retry_after:
        logger.warning(
            "Telegram 429 rate limit for chat %s: retry_after=%s sec",
            chat_id, retry_after
        )
        # Внешний ретрай пусть поспит retry_after
        return False

    logger.error(
        "Ошибка отправки Telegram в чат %s: %s %s",
        chat_id, resp.status_code, resp.text
    )
    return False


def _send_with_retries_to_chat(
    chat_id: str,
    text: str,
    *,
    max_retries: int = MAX_RETRIES,
    retry_delay_seconds: int = RETRY_DELAY_SECONDS,
    parse_mode: Optional[str] = DEFAULT_PARSE_MODE,
) -> bool:
    """
    Ретраим отправку конкретному чату.
    Учитываем 429: если видим retry_after в ответе, спим его вместо retry_delay_seconds.
    """
    chunks = _split_telegram_text(text)

    # Важно: если сообщение дробится — считаем "успех" только если все чанки ушли.
    for idx, chunk in enumerate(chunks, start=1):
        sent = False
        for attempt in range(1, max_retries + 1):
            logger.info(
                "Telegram -> chat=%s | part=%d/%d | attempt %d/%d",
                chat_id, idx, len(chunks), attempt, max_retries
            )

            ok = _send_telegram_single(chat_id, chunk, parse_mode=parse_mode)
            if ok:
                sent = True
                break

            # Пауза между попытками
            if attempt < max_retries:
                time.sleep(retry_delay_seconds)

        if not sent:
            return False

    return True


def send_telegram_messages(text: str) -> bool:
    """
    Отправка обычного бот-сообщения во все TELEGRAM_CHAT_IDS.
    Возвращает True, если хоть в один чат сообщение ушло успешно.
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN не задан, Telegram-уведомления отключены")
        return False
    if not TELEGRAM_CHAT_IDS:
        logger.warning("TELEGRAM_CHAT_IDS не заданы, Telegram-уведомления отключены")
        return False

    ok_any = False
    for chat_id in TELEGRAM_CHAT_IDS:
        if _send_with_retries_to_chat(str(chat_id), text):
            ok_any = True
    return ok_any


def notify_error(text: str) -> None:
    """
    Уведомление об ошибке в отдельный Telegram-чат.
    Делает несколько попыток. Если не ушло — логируем.
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.error("Невозможно отправить уведомление об ошибке, TELEGRAM_BOT_TOKEN не задан: %s", text)
        return
    if not TELEGRAM_ERROR_CHAT_ID:
        logger.error("TELEGRAM_ERROR_CHAT_ID не задан, ошибка: %s", text)
        return

    # Ошибка обычно длиннее — лучше с читаемым заголовком и временем.
    msg = _normalize_text(text).strip()
    msg = f"❗️ERROR\n{msg}"

    ok = _send_with_retries_to_chat(str(TELEGRAM_ERROR_CHAT_ID), msg)
    if not ok:
        logger.critical("Сообщение об ошибке НЕ доставлено в Telegram: %s", msg)


def notify_plus500_signal(text: str) -> None:
    """
    Уведомление в обычные Telegram-чаты о том, что появился сигнал для plus500.
    Улучшение: ретраи по каждому чату отдельно + разбиение длинных сообщений.
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN не задан — не можем отправить уведомление о сигнале.")
        return
    if not TELEGRAM_CHAT_IDS:
        logger.error("TELEGRAM_CHAT_IDS не заданы — некуда отправлять уведомление о сигнале.")
        return

    msg = _normalize_text(text).strip()
    if not msg:
        logger.warning("notify_plus500_signal: пустой текст — пропуск")
        return

    delivered: List[str] = []
    failed: List[str] = []

    for chat_id in TELEGRAM_CHAT_IDS:
        ok = _send_with_retries_to_chat(str(chat_id), msg)
        if ok:
            delivered.append(str(chat_id))
        else:
            failed.append(str(chat_id))

    if failed:
        logger.warning(
            "notify_plus500_signal: частичная доставка | ok=%s | failed=%s",
            ",".join(delivered) if delivered else "-",
            ",".join(failed)
        )
    else:
        logger.info("notify_plus500_signal: доставлено во все чаты (%d).", len(delivered))

def send_telegram_to_error_chat(text: str) -> bool:
    """
    Отправка сообщения ТОЛЬКО в TELEGRAM_ERROR_CHAT_ID (без префикса ❗️ERROR).
    Возвращает True/False.
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN не задан, Telegram-уведомления отключены")
        return False
    if not TELEGRAM_ERROR_CHAT_ID:
        logger.warning("TELEGRAM_ERROR_CHAT_ID не задан, Telegram-уведомления отключены")
        return False

    msg = _normalize_text(text).strip()
    if not msg:
        logger.warning("send_telegram_to_error_chat: пустой текст — пропуск")
        return False

    return _send_with_retries_to_chat(str(TELEGRAM_ERROR_CHAT_ID), msg)