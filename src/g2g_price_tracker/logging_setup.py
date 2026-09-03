import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(path: str | Path) -> logging.Logger:
    """Configure one small rotating application log and return its logger."""
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("g2g_price_tracker")
    logger.setLevel(logging.INFO)
    if not any(
        isinstance(handler, RotatingFileHandler)
        and Path(handler.baseFilename) == log_path.resolve()
        for handler in logger.handlers
    ):
        handler = RotatingFileHandler(
            log_path,
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(threadName)s | %(message)s")
        )
        logger.addHandler(handler)
    return logger
