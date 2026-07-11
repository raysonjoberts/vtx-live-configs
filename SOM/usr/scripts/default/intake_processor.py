import os
import sys
import csv
import time
import shutil
import fnmatch
import configparser
from pathlib import Path
from datetime import datetime
from importlib import import_module
from typing import Dict, List, Optional, Tuple

import pandas as pd

# ---------------------------------------------
# Paths & bootstrap
# ---------------------------------------------
BTDM_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
CONFIG_DIR = os.path.join(BTDM_ROOT, "usr", "config", "default")
CUSTOMER_DIR = os.path.join(BTDM_ROOT, "var", "customerdata")
OUTPUT_DIR = os.path.join(BTDM_ROOT, "var", "tables")
#LOG_DIR = os.path.join(BTDM_ROOT, "var", "logs")

TRANSFORMER_PATH = os.path.join(BTDM_ROOT, "usr", "lib", "transformers")
if TRANSFORMER_PATH not in sys.path:
    sys.path.insert(0, TRANSFORMER_PATH)

# Optional modules (present in your tree)
try:
    from reformat_rvtools import reformat_rvtools  # type: ignore
except Exception:
    reformat_rvtools = None  # type: ignore

try:
    from license_check import validate_license  # type: ignore
except Exception:
    def validate_license():  # type: ignore
        return True

# ------------------------------------------------------------
# Logging (BTDM/VTX style)
# ------------------------------------------------------------
try:
    sys.path.append(os.path.join(BTDM_ROOT, "usr", "lib"))
    import btdm_logging  # type: ignore

    logger = btdm_logging.get_logger(component="intake_processor")
except Exception:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    logger = logging.getLogger("intake_processor")

# ---------------------------------------------
# Config loading & parsing
# ---------------------------------------------

def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.optionxform = str  # preserve case
    cfg.read(os.path.join(CONFIG_DIR, "monitor.conf"))
    return cfg


def _norm_path_for_match(p: str) -> str:
    # Normalize slashes and case for cross-platform wildcard matching
    return p.replace("/", os.sep).replace("\\", os.sep)


def enumerate_monitor_sections(cfg: configparser.ConfigParser) -> List[Tuple[str, configparser.SectionProxy]]:
    """Return list of (pattern_str, section_proxy) for [monitor: <pattern>]."""
    items: List[Tuple[str, configparser.SectionProxy]] = []
    for sec in cfg.sections():
        if not sec.lower().startswith("monitor:"):
            continue
        pattern = sec.split(":", 1)[1].strip()
        items.append((_norm_path_for_match(pattern), cfg[sec]))
    return items


def find_matching_section(
    file_path: str,
    sections: List[Tuple[str, configparser.SectionProxy]],
) -> Optional[Tuple[str, configparser.SectionProxy]]:
    """Return the most specific matching section (longest pattern) for file_path, if any."""
    normalized = _norm_path_for_match(file_path)
    matches: List[Tuple[str, configparser.SectionProxy]] = []
    for pattern, sec in sections:
        # Allow simple wildcard matching
        if fnmatch.fnmatch(normalized, pattern):
            matches.append((pattern, sec))
    if not matches:
        return None
    # choose the longest pattern (most specific)
    matches.sort(key=lambda t: len(t[0]), reverse=True)
    return matches[0]


# ---------------------------------------------
# Transform helpers (unchanged behavior)
# ---------------------------------------------

def _apply_custom_reformats(df: pd.DataFrame, sec: configparser.SectionProxy) -> pd.DataFrame:
    apply_str = sec.get("apply_transforms", "").strip()
    if not apply_str:
        return df
    transform_list = [s.strip() for s in apply_str.split(",") if s.strip()]
    for script_name in transform_list:
        module_name = Path(script_name).stem
        # RVTools handled separately
        if module_name == "reformat_rvtools":
            logger.warning(f"Skipping transform '{module_name}' here (handled by is_rvtools=true path).")
            continue
        try:
            mod = import_module(module_name)
            if hasattr(mod, module_name):
                df = getattr(mod, module_name)(df, sec)
                logger.info(f"Applied custom reformat: {module_name}")
            else:
                logger.warning(f"Module '{module_name}' missing callable '{module_name}'")
        except Exception as e:
            logger.error(f"Failed to apply transform {module_name}: {e}")
    return df


def _rename_fields(df: pd.DataFrame, sec: configparser.SectionProxy) -> pd.DataFrame:
    rename_str = sec.get("rename_fields", "").strip()
    if not rename_str:
        return df
    rename_map: Dict[str, str] = {}
    for pair in rename_str.split(","):
        if ":" in pair:
            old, new = pair.split(":", 1)
            rename_map[old.strip()] = new.strip()
    if rename_map:
        df = df.rename(columns=rename_map)
        logger.info(f"Renamed fields: {rename_map}")
    return df


def _cast_all_to_str(df: pd.DataFrame) -> pd.DataFrame:
    return df.applymap(lambda x: str(x).strip() if pd.notnull(x) else "")


# ---------------------------------------------
# Excel helpers
# ---------------------------------------------

def _sanitize_sheet_name(name: str) -> str:
    # lower, spaces->underscore, strip non-filename friendly
    base = name.strip().lower().replace(" ", "_")
    keep = [c if (c.isalnum() or c in ("_", "-")) else "_" for c in base]
    return "".join(keep).strip("._") or "sheet"


def _iter_excel_frames(path: str, wanted_tabs: Optional[List[str]]) -> List[Tuple[str, pd.DataFrame]]:
    """Return list of (sheet_name, df). If wanted_tabs is provided, filter to those (case-sensitive match)."""
    xls = pd.read_excel(path, sheet_name=None)
    if wanted_tabs:
        tabs = [t for t in wanted_tabs if t in xls]
        missing = set(wanted_tabs) - set(tabs)
        if missing:
            logger.warning(f"Tabs not found in {Path(path).name}: {sorted(missing)}")
        return [(t, xls[t]) for t in tabs]
    return list(xls.items())


# ---------------------------------------------
# Core processing
# ---------------------------------------------

def _write_csv(df: pd.DataFrame, out_path: str) -> None:
    df.to_csv(out_path, index=False, quoting=csv.QUOTE_NONNUMERIC)
    logger.info(f"Wrote {out_path}")


def _delete_source(path: str) -> None:
    try:
        os.remove(path)
        logger.info(f"Removed {path}")
    except Exception as e:
        logger.warning(f"Could not delete source file '{path}': {e}")


def _rvtools_path(sec: Optional[configparser.SectionProxy]) -> bool:
    return bool(sec and sec.get("is_rvtools", "false").lower() == "true")


def _output_base_for(file_path: str, sec: Optional[configparser.SectionProxy]) -> str:
    """Decide the base output filename (without sheet suffix, with .csv). Always lowercased.
    Priority:
      - monitor.conf: output_name (if present)
      - else: source filename stem
    """
    if sec:
        oname = sec.get("output_name", "").strip()
        if oname:
            return oname.lower() if oname.lower().endswith(".csv") else f"{oname.lower()}.csv"
    return f"{Path(file_path).stem.lower()}.csv"


def process_one_file(file_path: str, match: Optional[Tuple[str, configparser.SectionProxy]]) -> bool:
    """Process a single source file. Returns True iff at least one output is written, and cleans up source on success."""
    try:
        # Handle RVTools fast-path
        if _rvtools_path(match[1] if match else None):
            if reformat_rvtools is None:
                logger.error("is_rvtools=true but reformat_rvtools module not available")
                return False
            try:
                prefix = Path(file_path).stem.lower()
                logger.info("Detected RVTools source — applying reformat_rvtools.py")
                reformat_rvtools(file_path, OUTPUT_DIR, prefix=prefix)
                _delete_source(file_path)
                return True
            except Exception as e:
                logger.error(f"RVTools reformat failed: {e}")
                return False

        sec = match[1] if match else None
        tabs_raw = sec.get("tabs", "").strip() if sec else ""
        wanted_tabs = [t.strip() for t in tabs_raw.split(",") if t.strip()] if tabs_raw else None

        wrote_any = False
        suffix = ".csv"

        if file_path.lower().endswith((".xls", ".xlsx")):
            # Excel -> possibly multiple sheets
            frames = _iter_excel_frames(file_path, wanted_tabs)
            base_name = _output_base_for(file_path, sec)
            base_stem = base_name[:-4] if base_name.lower().endswith(".csv") else base_name

            for sheet_name, df in frames:
                df = _cast_all_to_str(_rename_fields(_apply_custom_reformats(df, sec) if sec else df, sec) if sec else df)
                # If multiple sheets, append _<sheet>
                add_sheet = len(frames) > 1
                sheet_suffix = f"_{_sanitize_sheet_name(sheet_name)}" if add_sheet else ""
                out_name = f"{base_stem}{sheet_suffix}{suffix}".lower()
                out_path = os.path.join(OUTPUT_DIR, out_name)
                _write_csv(df, out_path)
                wrote_any = True

        elif file_path.lower().endswith(".csv"):
            # CSV -> single output
            try:
                try:
                    df = pd.read_csv(file_path, encoding="utf-8")
                except UnicodeDecodeError:
                    df = pd.read_csv(file_path, encoding="ISO-8859-1")
            except Exception as e:
                logger.error(f"Failed to read CSV '{file_path}': {e}")
                return False

            if sec:
                df = _cast_all_to_str(_rename_fields(_apply_custom_reformats(df, sec), sec))
            else:
                df = _cast_all_to_str(df)

            out_name = _output_base_for(file_path, sec)
            out_path = os.path.join(OUTPUT_DIR, out_name)
            _write_csv(df, out_path)
            wrote_any = True
        else:
            logger.warning(f"Unsupported file type, skipping: {file_path}")
            return False

        if wrote_any:
            _delete_source(file_path)
        return wrote_any

    except Exception as e:
        logger.error(f"Failed to process {file_path}: {e}")
        return False


# ---------------------------------------------
# Post-processing chain
# ---------------------------------------------

def _python_exec_cmd() -> str:
    if os.name == "nt" and shutil.which("pythonw"):
        return "pythonw"
    return sys.executable


def run_postprocessors(process_list: List[str], delay_seconds: int = 5) -> None:
    creation_flags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creation_flags = subprocess.CREATE_NO_WINDOW  # type: ignore[name-defined]

    py = _python_exec_cmd()

    for script_name in process_list:
        label = Path(script_name).stem
        abs_path = (
            script_name
            if os.path.isabs(script_name)
            else os.path.join(BTDM_ROOT, "usr", "scripts", "default", script_name)
        )
        logger.info(f"Launching {label}...")
        try:
            import subprocess  # local import to avoid top-level on some platforms
            result = subprocess.run([py, abs_path], capture_output=False)
            logger.info(f"{label} finished with return code {result.returncode}.")
        except Exception as e:
            logger.error(f"{label} failed: {e}")
        time.sleep(delay_seconds)


# ---------------------------------------------
# Main
# ---------------------------------------------

def main() -> None:
#    try:
#        validate_license()
#    except RuntimeError as e:  # pragma: no cover
#        logger.error(f"License error: {e}")
#        sys.exit(1)

    cfg = load_config()
    monitor_sections = enumerate_monitor_sections(cfg)

    processed_any = False

    # NEW behavior: process every file in CUSTOMER_DIR by default
    for entry in sorted(Path(CUSTOMER_DIR).glob("*")):
        if entry.is_dir():
            continue
        # Find matching monitor section by wildcard (if any)
        match = find_matching_section(str(entry), monitor_sections)
        # Decide processing (default applies even without a match)
        ok = process_one_file(str(entry), match)
        processed_any = processed_any or ok

    if processed_any:
        post_chain = [
            #"program_attribute_reporting.py",
            #"program_task_processor.py",
            #"datasource_field_profiler.py",
            #"data_source_analysis.py",
            #"program_attribute_reporting.py",
            "dataset_structure_profiler",
        ]
        run_postprocessors(post_chain, delay_seconds=5)
    else:
        logger.info("No files were processed. Skipping post-processing.")


if __name__ == "__main__":
    main()
