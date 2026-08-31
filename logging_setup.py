# logging_setup.py
import logging
import os
from . import config  # если сделаем пакет; если нет – просто import config

def setup_logging() -> logging.Logger:
    log_file = config.LOG_FILE
    log_level_value = getattr(logging, config.LOG_LEVEL, logging.INFO)

    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level_value)
    root_logger.handlers.clear()

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(log_format))
    root_logger.addHandler(console_handler)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(log_format))
    root_logger.addHandler(file_handler)

    return logging.getLogger("pounds-worker")
