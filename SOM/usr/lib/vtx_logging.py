# vtx_logging.py
import os, sys, socket, logging, argparse, gzip, shutil
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler

VTX_ROOT = os.environ.get("VTX_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
LOG_DIR = os.path.join(VTX_ROOT, "var", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

class CsvFormatter(logging.Formatter):
    # timestamp,level,component,script,host,pid,message
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds")
        level = record.levelname
        component = getattr(record, "component", record.name)
        script = getattr(record, "script", os.path.basename(sys.argv[0] or "unknown"))
        host = socket.gethostname()
        pid = os.getpid()
        msg = super().format(record)  # includes record.getMessage()
        # Quote message, escape embedded quotes by doubling them
        safe_msg = '"' + str(msg).replace('"', '""') + '"'
        return f'{ts},{level},{component},{script},{host},{pid},{safe_msg}'

def _gzip_rotator(source, dest):
    with open(source, "rb") as f_in, gzip.open(dest + ".gz", "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(source)

def _dated_namer(default_name):
    # default_name ends with .../component.log.YYYY-MM-DD
    return default_name  # rotator adds .gz

def get_logger(component: str,
               level: int = logging.INFO,
               script_name: str | None = None) -> logging.Logger:
    """
    Use one logger per component. Safe to call repeatedly.
    """
    logger = logging.getLogger(component)
    if logger.handlers:
        return logger

    logger.setLevel(level)
    log_path = os.path.join(LOG_DIR, f"{component}.log")

    handler = TimedRotatingFileHandler(
        log_path, when="midnight", backupCount=30, encoding="utf-8", utc=False
    )
    # compress rotated files
    handler.namer = _dated_namer
    handler.rotator = _gzip_rotator

    fmt = CsvFormatter("%(message)s")  # message only; formatter builds CSV row
    handler.setFormatter(fmt)
    logger.addHandler(handler)

    # Avoid double logging to root
    logger.propagate = False

    # Inject defaults via LoggerAdapter for component/script
    extra = {"component": component, "script": script_name or os.path.basename(sys.argv[0] or "unknown")}
    return logging.LoggerAdapter(logger, extra)  # type: ignore[return-value]

# --- CLI so Bash can log with the same pipeline ---
def _cli():
    p = argparse.ArgumentParser(description="VTX logging CLI")
    p.add_argument("--component", required=True)
    p.add_argument("--level", default="INFO", choices=["DEBUG","INFO","WARNING","ERROR","CRITICAL"])
    p.add_argument("--script", default=None, help="override script name")
    p.add_argument("message", nargs="+")
    args = p.parse_args()

    level_map = {
        "DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
        "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL
    }
    logger = get_logger(args.component, level_map[args.level], script_name=args.script)
    msg = " ".join(args.message)
    getattr(logger, args.level.lower())(msg)

if __name__ == "__main__":
    _cli()