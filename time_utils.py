# time_utils.py
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


def _now_tz(tz_name: str) -> datetime:
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(tz_name))
        except Exception:
            logger.exception("Некорректная таймзона %s, использую UTC", tz_name)
            return datetime.utcnow()
    return datetime.utcnow()


def now_formatted(tz_name: str) -> str:
    dt = _now_tz(tz_name)
    return dt.strftime("%d.%m.%Y %H.%M")


def next_half_hour_slot(tz_name: str) -> datetime:
    now = _now_tz(tz_name)
    base = now.replace(second=0, microsecond=0)
    step = 30
    block = (base.minute // step) + 1
    slot = base.replace(minute=0) + timedelta(minutes=block * step)
    return slot


def next_work_slot(tz_name: str) -> datetime:
    from .time_utils import next_half_hour_slot  # можно и без локального импорта

    slot = next_half_hour_slot(tz_name)
    if slot.weekday() >= 5:
        days_to_monday = (7 - slot.weekday()) % 7 or 1
        slot = slot.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days_to_monday)
        logger.info("Перепрыгиваем на понедельник %s", slot)
    return slot

import os
import json
import tempfile
from pathlib import Path
from statistics import mean
from typing import Optional, List


def _env_flag(name: str, default: str = "0") -> bool:
    v = os.getenv(name, default).strip().lower()
    return v in ("1", "true", "yes", "y", "on")


def _prepare_stats_path() -> Path:
    # ВАЖНО: путь должен быть в volume, чтобы переживать рестарты контейнера
    p = os.getenv("PREPARE_STATS_PATH", "/app/data/prepare_stats.json").strip()
    return Path(p)


def _read_prepare_durations(path: Path) -> List[float]:
    try:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        arr = data.get("durations_sec", [])
        out = []
        for x in arr:
            try:
                fx = float(x)
                if fx > 0:
                    out.append(fx)
            except Exception:
                continue
        return out
    except Exception:
        logger.exception("prepare_stats: failed to read %s", str(path))
        return []


def _write_prepare_durations(path: Path, durations: List[float]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"durations_sec": durations}
        tmp_fd, tmp_path = tempfile.mkstemp(prefix="prepare_stats_", suffix=".json", dir=str(path.parent))
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp_path, path)  # atomic replace
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
    except Exception:
        logger.exception("prepare_stats: failed to write %s", str(path))


def record_prepare_duration(seconds: float) -> None:
    window = int(os.getenv("DYN_PREPARE_WINDOW", "5"))
    if window <= 0:
        window = 5
    path = _prepare_stats_path()
    arr = _read_prepare_durations(path)
    arr.append(float(seconds))
    arr = arr[-window:]
    _write_prepare_durations(path, arr)


def get_dynamic_prepare_offset_min(default_offset_min: float) -> float:
    """
    Возвращает offset (в минутах) для /prepare:
    - среднее последних N вызовов /prepare (в секундах) + буфер
    - переведённое в минуты
    - ограниченное min/max
    """
    if not _env_flag("DYN_PREPARE_ENABLED", "1"):
        return float(default_offset_min)

    window = int(os.getenv("DYN_PREPARE_WINDOW", "5"))
    if window <= 0:
        window = 5

    min_off = float(os.getenv("DYN_PREPARE_MIN", "0.3"))   # минимум 18 сек
    max_off = float(os.getenv("DYN_PREPARE_MAX", "5.0"))   # максимум 5 минут
    buffer_sec = float(os.getenv("DYN_PREPARE_BUFFER_SEC", "5"))

    path = _prepare_stats_path()
    arr = _read_prepare_durations(path)
    if not arr:
        return float(default_offset_min)

    from statistics import median
    avg_sec = median(arr[-window:])

    off_min = (avg_sec + buffer_sec) / 60.0

    if off_min < min_off:
        off_min = min_off
    if off_min > max_off:
        off_min = max_off

    return float(off_min)
