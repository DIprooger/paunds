# parsers.py
import re
from typing import Dict, Any, List, Optional
from .html_utils import inner_text, slice_after
import logging

logger = logging.getLogger(__name__)

SIGNALS_EN = ["STRONG SELL", "STRONG BUY", "SELL", "BUY", "NEUTRAL"]
RU_TO_EN = {
    "АКТИВНО ПРОДАВАТЬ": "STRONG SELL",
    "АКТИВНО ПОКУПАТЬ": "STRONG BUY",
    "СИЛЬНО ПРОДАВАТЬ": "STRONG SELL",
    "СИЛЬНО ПОКУПАТЬ": "STRONG BUY",
    "ПРОДАВАТЬ": "SELL",
    "ПОКУПАТЬ": "BUY",
    "НЕЙТРАЛЬНО": "NEUTRAL",
    "НЕЙТРАЛЬНЫЙ": "NEUTRAL",
}

def normalize_action(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    u = " ".join(str(s).upper().split())
    if u in SIGNALS_EN:
        return u
    if u in RU_TO_EN:
        return RU_TO_EN[u]
    for ru, en in RU_TO_EN.items():
        if ru in u:
            return en
    return None


def parse_timeframes(html: str) -> Dict[str, Dict[str, str]]:
    """
    keys: 1m,5m,15m,30m,1h,5h,1d,1w,1mo
    (в лист пишем только 30m,1h,5h,1d,1w,1mo)
    """
    keys = ["1m", "5m", "15m", "30m", "1h", "5h", "1d", "1w", "1mo"]
    result: Dict[str, Dict[str, str]] = {}

    for k in keys:
        pattern = re.compile(
            fr'(<(?:button|a)[^>]+data-test="{re.escape(k)}"[\s\S]*?</(?:button|a)>)',
            flags=re.I,
        )
        m = pattern.search(html)
        if not m:
            continue
        btn = m.group(1)
        spans = [
            inner_text(s)
            for s in re.findall(r"<span[^>]*>([\s\S]*?)</span>", btn, flags=re.I)
        ]
        action = None
        for s in spans:
            n = normalize_action(s)
            if n:
                action = n
                break

        m_label = re.search(
            r'data-test="[^"]+"[^>]*>([\s\S]*?)</(?:button|a)>',
            btn,
            flags=re.I,
        )
        label_raw = m_label.group(1) if m_label else ""
        label = (inner_text(label_raw).split() or [k])[0]

        if action:
            result[k] = {"label": label, "action": action}

    return result


# -------------------- TECHNICAL INDICATORS & PIVOT POINTS --------------------

def parse_technical_indicators(html: str) -> Dict[str, Any]:
    block = slice_after(
        html,
        re.compile(r'data-test="technical-indicators-title"', re.I),
        re.compile(r"</table>", re.I),
    )
    if not block:
        return {"summary": None, "rows": []}

    summary_raw = None
    for pattern in [
        r"Technical Indicators[\s\S]*?Summary:\s*</span>\s*<span[^>]*>\s*([^<]+)\s*</span>",
        r"Индикаторы[\s\S]*?Сводка:\s*</span>\s*<span[^>]*>\s*([^<]+)\s*</span>",
        r'<div[^>]*class="[^"]*bg-positive-light[^"]*"[^>]*>\s*([^<]+)\s*</div>',
    ]:
        m = re.search(pattern, block, flags=re.I)
        if m:
            summary_raw = m.group(1)
            break

    table = re.search(r"<table[\s\S]*?</table>", block, flags=re.I)
    if not table:
        return {"summary": summary_raw, "rows": []}
    table_html = table.group(0)

    body = re.search(r"<tbody[^>]*>([\s\S]*?)</tbody>", table_html, flags=re.I)
    if not body:
        return {"summary": summary_raw, "rows": []}
    body_html = body.group(1)

    rows: List[Dict[str, Any]] = []
    for m_tr in re.finditer(r"<tr[^>]*>([\s\S]*?)</tr>", body_html, flags=re.I):
        tr = m_tr.group(1)
        name = inner_text(
            re.search(r'<span[^>]*font-semibold[^>]*>([\s\S]*?)</span>', tr, flags=re.I)
            or re.search(r"<th[^>]*>([\s\S]*?)</th>", tr, flags=re.I)
            or re.match(r"^([\s\S]+)$", tr)
        .group(1)
        )
        tds = [
            inner_text(td)
            for td in re.findall(r"<td[^>]*>([\s\S]*?)</td>", tr, flags=re.I)
        ]
        if not name:
            continue
        value = tds[1] if len(tds) > 1 else (tds[0] if tds else None)
        raw_action = tds[2] if len(tds) > 2 else (tds[1] if len(tds) > 1 else None)
        action = normalize_action(raw_action) or raw_action
        rows.append({"name": name, "value": value, "action": action})

    summary = normalize_action(summary_raw) or summary_raw if summary_raw else None
    return {"summary": summary, "rows": rows}


def parse_pivot_points(html: str) -> Dict[str, Any]:
    block = slice_after(
        html,
        re.compile(r'data-test="pivot-points-title"', re.I),
        re.compile(r"</table>", re.I),
    )
    if not block:
        return {"classic": None}

    table = re.search(r"<table[\s\S]*?</table>", block, flags=re.I)
    if not table:
        return {"classic": None}
    table_html = table.group(0)

    body = re.search(r"<tbody[^>]*>([\s\S]*?)</tbody>", table_html, flags=re.I)
    if not body:
        return {"classic": None}
    body_html = body.group(1)

    m_tr = (
        re.search(r"<tr[^>]*>[\s\S]*?Classic[\s\S]*?</tr>", body_html, flags=re.I)
        or re.search(r"<tr[^>]*>[\s\S]*?Классический[\s\S]*?</tr>", body_html, flags=re.I)
    )
    if not m_tr:
        return {"classic": None}
    tr = m_tr.group(0)

    tds = [
        inner_text(td)
        for td in re.findall(r"<td[^>]*>([\s\S]*?)</td>", tr, flags=re.I)
    ]
    vals = tds[1:]
    names = ["S3", "S2", "S1", "Pivot", "R1", "R2", "R3"]
    classic = {name: (vals[i] if i < len(vals) and vals[i] else None) for i, name in enumerate(names)}
    return {"classic": classic}
