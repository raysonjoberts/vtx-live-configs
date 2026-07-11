import os
import logging
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime

# Resolve BTDM_ROOT dynamically
BTDM_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LOG_DIR = os.path.join(BTDM_ROOT, "var", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

def get_logger(name="btdm_script", level=logging.INFO):
    """
    Creates or retrieves a named logger that writes to a rotating log file.
    :param name: Name of the logger (also used in log filename)
    :param level: Logging level (e.g., logging.INFO, logging.DEBUG)
    :return: Configured logger object
    """
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger  # Logger already configured

    log_file = os.path.join(LOG_DIR, f"{name}.log")

    handler = TimedRotatingFileHandler(log_file, when="midnight", interval=1, backupCount=14)
    handler.suffix = "%Y%m%d"

    formatter = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s", "%Y-%m-%d %H:%M:%S")
    handler.setFormatter(formatter)

    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False  # Prevent duplicate logs

    return logger
