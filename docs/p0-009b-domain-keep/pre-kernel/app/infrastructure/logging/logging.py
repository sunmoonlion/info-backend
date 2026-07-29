import logging
import sys

from core.config import get_settings


def setup_logging():
    settings = get_settings()
    root_logger = logging.getLogger()
    root_logger.handlers.clear()

    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root_logger.setLevel(log_level)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(log_level)
    root_logger.addHandler(console_handler)

    root_logger.info("日志系统初始化完成")
