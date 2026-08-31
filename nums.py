"""
nums.py — вспомогательный модуль для парсинга и форматирования цен.

Отвечает за:
- определение типа формата цены по URL инструмента;
- извлечение сырой цены из HTML (data-test="instrument-price-last");
- преобразование сырой строки в:
    * display-строку (как нужно положить в Google Sheets);
    * числовое значение (float) для расчётов.

Поддерживаются специальные кейсы:

1. FX (GBPUSD / GBPEUR / AUDUSD):
   - HTML: '1.3442'
   - display: '1,3442'
   - number: 1.3442

2. Bitcoin RU:
   - HTML: '120.343,8'
   - display: '120343,8'
   - number: 120343.8

3. US SPX 500 Futures:
   - HTML: как есть (например '5,402.2')
   - display: строка без изменений
   - number: корректный float (5402.2)
"""

from __future__ import annotations

import re
from typing import Optional, Tuple


# ---------- Определение формата цены по URL ----------

def get_price_format(url: str) -> str:
    """
    Возвращает тип формата цены для инструмента по его URL:

    - 'fx_comma'     : форекс-пары (GBPUSD, GBPEUR, AUDUSD, USDJPY, USDCAD, EURUSD)
    - 'ru_crypto'    : Bitcoin RU
    - 'raw_preserve' : S&P 500 futures (строку не трогаем)
    - 'auto'         : универсальный fallback
    """
    if not url:
        return "auto"

    url = url.lower()

    # 1) FX: GBPUSD / EURGBP / AUDUSD / USDJPY / USDCAD / EURUSD
    if "currencies/gbp-usd-technical" in url or "currencies/gbp-usd" in url:
        return "fx_comma"

    # EURGBP — правильный URL на investing: /eur-gbp
    if "currencies/eur-gbp" in url or "currencies/gbp-eur" in url:
        return "fx_comma"

    if "currencies/aud-usd" in url:
        return "fx_comma"
    if "currencies/usd-jpy" in url:
        return "fx_comma"
    if "currencies/usd-cad" in url:
        return "fx_comma"
    if "currencies/eur-usd" in url:
        return "fx_comma"
    if "currencies/eur-chf" in url:
        return "fx_comma"
    if "currencies/aud-nzd" in url:
        return "fx_comma"
    if "currencies/chf-jpy" in url:
        return "fx_comma"

    # 2) Bitcoin RU
    if "ru.investing.com/crypto/bitcoin" in url:
        return "ru_crypto"

    # 2b) Crypto technical pages with many decimals (e.g. SOL/BTC):
    # Investing often shows '0.00141520' where '.' is the decimal separator and there are 6-8 decimals.
    # In such cases we MUST treat '.' as decimal (not thousands) and keep 8 decimals for display.
    if "investing.com/crypto/" in url and "technical" in url:
        return "crypto_dot8"

    # 3) US SPX 500 Futures
    if "indices/us-spx-500-futures" in url:
        return "raw_preserve"

    # запасной вариант
    return "auto"

# ---------- Универсальный парсер числа ----------

def smart_price_to_number_and_display(raw: str) -> Tuple[Optional[float], Optional[str]]:
    """
    Универсальный парсер строки в число + display-строку.

    Примеры:
    - '1.3442'     -> (1.3442, '1,3442')
    - '120.343,8'  -> (120343.8, '120343,8')
    - '5,402.2'    -> (5402.2, '5402,2')
    - '5 402,2'    -> (5402.2, '5402,2')
    """
    if not raw:
        return None, None

    # убираем все виды пробелов (обычные + неразрывные)
    t = str(raw).replace("\u00A0", " ").replace("\u202F", " ")
    t = re.sub(r"\s+", "", t)

    has_comma = "," in t
    has_dot = "." in t

    if has_comma and has_dot:
        # и запятая, и точка — определяем, что правее, и считаем это десятичным разделителем
        last_comma = t.rfind(",")
        last_dot = t.rfind(".")
        if last_comma > last_dot:
            dec_sep = ","
            thou_sep = "."
        else:
            dec_sep = "."
            thou_sep = ","
        num_str = t.replace(thou_sep, "").replace(dec_sep, ".")
    elif has_comma:
        # только запятая — смотрим, похоже ли это на десятичную часть
        parts = t.split(",")
        dec = parts[1] if len(parts) > 1 else ""
        if 0 < len(dec) <= 2:
            num_str = t.replace(",", ".")
        else:
            # вероятно, это тысячные разделители
            num_str = t.replace(",", "")
    elif has_dot:
        parts = t.split(".")
        dec = parts[1] if len(parts) > 1 else ""
        if 0 < len(dec) <= 2:
            # likely decimal separator
            num_str = t
        elif t.count(".") == 1 and len(parts[0]) <= 2:
            # small numbers like 0.00141520: '.' is decimal even with many digits after it
            num_str = t
        else:
            # likely thousands separators
            num_str = t.replace(".", "")
    else:
        num_str = t

    try:
        number = float(num_str)
    except Exception:
        return None, None

    # display по умолчанию — с запятой как десятичным
    display = num_str.replace(".", ",")
    return number, display


# ---------- Парсинг цены из HTML с учётом типа инструмента ----------

PRICE_RE = re.compile(
    r'data-test="instrument-price-last"[^>]*>\s*([0-9][0-9\.,\s\u00A0\u202F]*)\s*</div>',
    re.I,
)


def parse_price(html: str, instrument_url: str) -> Tuple[Optional[str], Optional[float]]:
    """
    Парсим цену из HTML и форматируем в зависимости от типа инструмента.

    Возвращает:
    - display: строка для записи в таблицу;
    - number: float для вычислений.
    """
    if not html:
        return None, None

    m = PRICE_RE.search(html)
    if not m:
        return None, None

    raw = m.group(1)
    if raw is None:
        return None, None

    # нормализуем пробелы, но НЕ меняем запятые/точки,
    # чтобы специальные кейсы могли работать исходным форматом.
    raw_clean_spaces = raw.replace("\u00A0", " ").replace("\u202F", " ").strip()
    fmt = get_price_format(instrument_url)

    # 1) FX: GBPUSD / GBPEUR / AUDUSD / ...
    if fmt == "fx_comma":
        # убираем пробелы и неразрывные пробелы
        t = (
            raw_clean_spaces
            .replace(" ", "")
            .replace("\u00A0", "")
            .replace("\u202F", "")
        )

        # Случай: есть и запятая, и точка -> один из них тысячный, другой десятичный
        if "," in t and "." in t:
            last_comma = t.rfind(",")
            last_dot = t.rfind(".")
            # тот, что правее, считаем десятичным
            if last_comma > last_dot:
                dec_sep = ","
                thou_sep = "."
            else:
                dec_sep = "."
                thou_sep = ","
            num_str = t.replace(thou_sep, "").replace(dec_sep, ".")
        elif "," in t:
            # только запятая — считаем её десятичной
            num_str = t.replace(",", ".")
        else:
            # только точка — считаем её десятичной (в т.ч. 4 знака после точки для FX)
            num_str = t

        try:
            number = float(num_str)
        except Exception:
            return None, None

        # display всегда с запятой как десятичным
        display = num_str.replace(".", ",")
        return display, number

    # 2) Bitcoin RU: "120.343,8" → display: "120343,8", number: 120343.8
    if fmt == "ru_crypto":
        t = raw_clean_spaces.replace(" ", "")
        # убираем точки (тысячные)
        t_no_dots = t.replace(".", "")
        try:
            number = float(t_no_dots.replace(",", "."))
        except Exception:
            return None, None
        display = t_no_dots
        return display, number


    # 2b) Crypto technical pages with many decimals (e.g. SOL/BTC):
    # '0.00141520' -> display '0,00141520', number 0.0014152
    if fmt == "crypto_dot8":
        t = (
            raw_clean_spaces
            .replace(" ", "")
            .replace("\u00A0", "")
            .replace("\u202F", "")
        )

        # If both separators appear, use the right-most as decimal (same logic as elsewhere).
        if "," in t and "." in t:
            last_comma = t.rfind(",")
            last_dot = t.rfind(".")
            if last_comma > last_dot:
                dec_sep = ","
                thou_sep = "."
            else:
                dec_sep = "."
                thou_sep = ","
            num_str = t.replace(thou_sep, "").replace(dec_sep, ".")
        elif "," in t:
            # treat comma as decimal
            num_str = t.replace(",", ".")
        else:
            # treat dot as decimal even if many digits after it
            num_str = t

        try:
            number = float(num_str)
        except Exception:
            return None, None

        # Force 8 decimals for display (keeps leading zeros)
        display = f"{number:.8f}".replace(".", ",")
        return display, number

    # 3) SPX: хотим display в RU-формате, как и для других —
    # без тысячных запятых и с запятой как десятичным
    if fmt == "raw_preserve":
        number, display = smart_price_to_number_and_display(raw_clean_spaces)
        return display, number

    # 4) Fallback: универсальный режим
    number, display = smart_price_to_number_and_display(raw_clean_spaces)
    return display, number
