import re
from typing import Any, Optional
import logging

logger = logging.getLogger(__name__)

def get_raw_text_from_json(obj: Any) -> Optional[str]:
    """
    Порт getRawTextFromJson из n8n:
    ищем самый длинный текст, бустим ключи html|text|content|raw|body.
    """
    candidates = []

    def walk(key, val):
        if isinstance(val, str):
            boost = 2 if re.search(r"(html|text|content|raw|body)", str(key), flags=re.I) else 1
            candidates.append((boost * len(val), val))
        elif isinstance(val, list):
            for v in val:
                walk("", v)
        elif isinstance(val, dict):
            for k, v in val.items():
                walk(k, v)

    walk("root", obj)
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def inner_text(html: str) -> str:
    s = str(html)
    s = re.sub(r"<script[\s\S]*?</script>", " ", s, flags=re.I)
    s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&nbsp;?", " ", s, flags=re.I)
    s = re.sub(r"&amp;", "&", s, flags=re.I)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def slice_after(haystack: str, anchor_re: re.Pattern, until_re: re.Pattern) -> Optional[str]:
    a = anchor_re.search(haystack)
    if not a:
        return None
    start = a.end()
    tail = haystack[start:]
    b = until_re.search(tail)
    return tail[: b.end()] if b else tail

def detect_antibot_page(html: str) -> Optional[str]:
    """
    Пытаемся понять, что это страница антибот-защиты / ошибки доступа,
    а не нормальная страница инструмента.
    Возвращаем текстовое описание причины или None, если всё ок.
    """
    text = inner_text(html).lower()

    patterns = [
        ("подтвердите, что вы не робот", "ru: подтвердите, что вы не робот"),
        ("подтвердите что вы не робот", "ru: подтвердите, что вы не робот"),
        ("are you a robot", "en: are you a robot"),
        ("are you human", "en: are you human"),
        ("access denied", "access denied"),
        ("доступ запрещен", "ru: доступ запрещен"),
        ("temporarily blocked", "temporarily blocked"),
        ("ddos protection", "ddos protection"),
        ("checking your browser", "cloudflare: checking your browser"),
        ("please wait while we check your browser", "cloudflare check"),
        ("captcha", "captcha on page"),
    ]

    for needle, reason in patterns:
        if needle in text:
            return reason

    return None