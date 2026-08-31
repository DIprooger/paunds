import os
import logging
from typing import Iterable, Tuple, List, Optional

import requests

from .db_client import get_last_strong_signal_before

logger = logging.getLogger(__name__)

_STRONG = {"STRONG BUY", "STRONG SELL"}


def _parse_csv(s: str) -> List[str]:
    if not s:
        return []
    raw = (
        str(s)
        .replace(";", ",")
        .replace("\n", ",")
        .replace("\t", ",")
        .replace(" ", ",")
        .split(",")
    )
    out: List[str] = []
    for x in raw:
        x = x.strip()
        if x:
            out.append(x)
    return out


def _norm_tf(tf: str) -> str:
    return str(tf or "").strip().lower()


def _telegram_send_one_line(text: str) -> None:
    """
    Sends to all TELEGRAM_CHAT_IDS. Message is ONE LINE.
    Required env:
      TELEGRAM_BOT_TOKEN
      TELEGRAM_CHAT_IDS (comma/space separated)
    Optional:
      TELEGRAM_API_BASE (default https://api.telegram.org)
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = _parse_csv(os.getenv("TELEGRAM_CHAT_IDS", ""))

    if not token or not chat_ids:
        logger.info("PHASE2: telegram disabled (no TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_IDS)")
        return

    base = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
    url = f"{base}/bot{token}/sendMessage"

    # enforce one line
    text = " ".join(str(text).splitlines()).strip()

    for cid in chat_ids:
        try:
            r = requests.post(
                url,
                timeout=15,
                json={
                    "chat_id": cid,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )
            if r.status_code != 200:
                logger.error(
                    "PHASE2: telegram send failed chat_id=%s status=%s body=%s",
                    cid, r.status_code, r.text[:500]
                )
            else:
                logger.info("PHASE2: telegram sent chat_id=%s", cid)
        except Exception:
            logger.exception("PHASE2: telegram send crashed chat_id=%s", cid)


def _get_tf_action(res, tf: str) -> Optional[str]:
    try:
        obj = (res.timeframes or {}).get(tf) or {}
        act = (obj or {}).get("action")
        return str(act).strip() if act else None
    except Exception:
        return None


def phase2_strong_flip_alerts(
    results: Iterable[Tuple[str, object]],
    *,
    executed_at: str,
) -> int:
    """
    PHASE-2: Strong flip alerts ONLY (Telegram), using DB history (NO json state).

    Rule:
      - Look at current tf action (curr) from results
      - If curr is STRONG BUY/SELL:
          prev := last STRONG action in DB strictly before executed_at for same (instrument, tf)
          send Telegram only when prev exists and prev != curr and {prev,curr} == {STRONG BUY, STRONG SELL}

    Message format (one line, required):
      "{code_lower} {tf} {curr_lower}"
      Example: "chfjpy 1h strong buy"

    Env:
      STRONG_FLIP_ALERT_TFS=30m,1h,5h
      TELEGRAM_BOT_TOKEN=...
      TELEGRAM_CHAT_IDS=...
    """
    tfs = [_norm_tf(x) for x in _parse_csv(os.getenv("STRONG_FLIP_ALERT_TFS", "30m,1h,5h"))]
    tfs = [x for x in tfs if x]

    if not tfs:
        logger.info("PHASE2: no tfs configured (STRONG_FLIP_ALERT_TFS empty) -> skip")
        return 0

    alerts_sent = 0

    # debug counters to understand "why no signals"
    total_checks = 0
    strong_now = 0
    no_prev = 0
    same_strong = 0
    flip_cnt = 0
    db_err = 0

    for code, res in results:
        if not res:
            continue

        code_s = str(code).strip()
        code_l = code_s.lower()

        for tf in tfs:
            total_checks += 1
            curr = _get_tf_action(res, tf)

            # Only STRONG states are relevant for flip logic
            if curr not in _STRONG:
                continue

            strong_now += 1

            # prev STRONG from DB (same logic as PHASE1 "first-entry")
            try:
                prev = get_last_strong_signal_before(code_s, timeframe=tf, before_executed_at=executed_at)
            except Exception:
                db_err += 1
                logger.exception("PHASE2: DB error get_last_strong_signal_before code=%s tf=%s", code_s, tf)
                continue

            if prev not in _STRONG:
                no_prev += 1
                continue

            if prev == curr:
                same_strong += 1
                continue

            if {prev, curr} == {"STRONG BUY", "STRONG SELL"}:
                flip_cnt += 1
                text = f"{code_l} {tf} {str(curr).lower()}"
                _telegram_send_one_line(text)
                alerts_sent += 1

    logger.info(
        "PHASE2: done | sent=%d | tfs=%s | stats: checks=%d strong_now=%d no_prev=%d same_strong=%d flips=%d db_err=%d | at=%s",
        alerts_sent,
        ",".join(tfs),
        total_checks,
        strong_now,
        no_prev,
        same_strong,
        flip_cnt,
        db_err,
        executed_at,
    )
    return alerts_sent