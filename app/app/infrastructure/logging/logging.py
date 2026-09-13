import logging
import sys

from core.config import get_settings


def configure_library_logging(**_: object) -> None:
    """Keep SQL/HTTP wire logs off, including when the application uses DEBUG.

    Also used after Celery installs its own handlers. Do not replace those
    handlers or change application/task log levels here. This is noise control,
    not a general-purpose secret redactor.
    """
    namespaces = ("sqlalchemy.engine", "sqlalchemy.pool", "httpx", "httpcore")
    for name in namespaces:
        logging.getLogger(name).setLevel(logging.WARNING)
    # Existing child loggers may have explicit levels from prior initialization.
    for name, logger in list(logging.root.manager.loggerDict.items()):
        if isinstance(logger, logging.Logger) and any(
            name.startswith(f"{namespace}.") for namespace in namespaces
        ):
            logger.setLevel(logging.WARNING)


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
    configure_library_logging()

    root_logger.info("日志系统初始化完成")
