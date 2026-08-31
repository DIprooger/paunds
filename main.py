# main.py
from .logging_setup import setup_logging
from .config import INTERVAL_SECONDS
from .worker import run_once, run_forever

logger = setup_logging()

if __name__ == "__main__":
    try:
        if INTERVAL_SECONDS > 0:
            logger.info("INTERVAL_SECONDS=%s > 0 — периодический режим", INTERVAL_SECONDS)
            run_forever()
        else:
            logger.info("INTERVAL_SECONDS <= 0 — единичный запуск")
            run_once()
    except Exception:
        import sys, logging
        logging.getLogger(__name__).exception("Фатальная ошибка в main")
        sys.exit(1)
