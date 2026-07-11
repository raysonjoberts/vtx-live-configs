#!/usr/bin/env python3
"""
BTDM File Move/Copy Utility (single file or glob)
- Choose exactly one of:
    --source <file>           # a single file
    --source-glob <pattern>   # e.g. var/tables/views/consolidated*.csv
- Destination:
    --dest <dir-or-file>      # if glob matches >1, dest must be a directory
- Other options:
    --mode move|copy          # default: move
    --append-date yes|no      # default: no
    --date-format %Y%m%d      # default: %Y%m%d
    --skip-missing yes|no     # default: yes (no error if no matches)
"""

from __future__ import annotations
import os, sys, argparse, shutil, glob
from datetime import datetime
from pathlib import Path

# BTDM globals
BTDM_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LOGS_DIR = os.path.join(BTDM_ROOT, "var", "logs")

# Logging
try:
    sys.path.append(os.path.join(BTDM_ROOT, "usr", "lib"))
    import btdm_logging  # type: ignore
    logger = btdm_logging.get_logger(component="file_move")
except Exception:
    import logging
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    logger = logging.getLogger("file_move")

def resolve_path(p: str) -> Path:
    # Allow BTDM-relative (recommended) or absolute paths
    return Path(p if os.path.isabs(p) else os.path.join(BTDM_ROOT, p)).resolve()

def resolve_pattern(p: str) -> str:
    # Return absolute pattern string; do not Path() the whole pattern (it can drop '*')
    return p if os.path.isabs(p) else os.path.join(BTDM_ROOT, p)

def add_date_suffix(path: Path, date_fmt: str) -> Path:
    stem, suffix = path.stem, path.suffix
    tag = datetime.utcnow().strftime(date_fmt)
    return path.with_name(f"{stem}_{tag}{suffix}")

def ensure_dir_for_target(target: Path) -> None:
    # If target is intended as a directory, create it; if it's a file path, create its parent
    base = target if target.suffix == "" else target.parent
    base.mkdir(parents=True, exist_ok=True)

def collect_sources(args) -> list[Path]:
    if args.source and args.source_glob:
        raise SystemExit("--source and --source-glob are mutually exclusive")

    if args.source:
        src = resolve_path(args.source)
        if src.is_file():
            return [src]
        else:
            logger.error(f"--source is not a file: {src}")
            raise SystemExit(2)

    # Glob mode
    pattern_abs = resolve_pattern(args.source_glob)
    # recursive=True makes ** work; single * works either way
    matches = [Path(p) for p in glob.glob(pattern_abs, recursive=True) if os.path.isfile(p)]
    if not matches:
        msg = f"No files matched pattern: {args.source_glob}"
        if args.skip_missing == "yes":
            logger.info(msg)
            return []
        logger.error(msg)
        raise SystemExit(3)
    return sorted(matches)

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source")                    # single file
    ap.add_argument("--source-glob")              # glob pattern
    ap.add_argument("--dest", required=True)      # dir or file
    ap.add_argument("--mode", choices=["move","copy"], default="move")
    ap.add_argument("--append-date", choices=["yes","no"], default="no")
    ap.add_argument("--date-format", default="%Y%m%d")
    ap.add_argument("--skip-missing", choices=["yes","no"], default="yes")
    args = ap.parse_args()

    sources = collect_sources(args)
    if not sources:
        # nothing to do (not an error if skip-missing=yes)
        return 0

    dest_input = resolve_path(args.dest)
    dest_is_dir = (dest_input.suffix == "") or dest_input.is_dir()

    # If multiple sources, dest must be a directory
    if len(sources) > 1 and not dest_is_dir:
        logger.error("When using --source-glob (multiple sources), --dest must be a directory.")
        return 4

    # Prepare destination directory (for dir or single file)
    ensure_dir_for_target(dest_input)

    ok = 0
    for src in sources:
        try:
            if dest_is_dir:
                target = dest_input / src.name
            else:
                target = dest_input

            if args.append_date == "yes":
                if target.suffix == "":
                    # If someone passed a dest directory without trailing filename
                    target = target / src.name
                target = add_date_suffix(target, args.date_format)

            ensure_dir_for_target(target)

            if args.mode == "move":
                logger.info(f"Moving: {src} -> {target}")
                shutil.move(str(src), str(target))
            else:
                logger.info(f"Copying: {src} -> {target}")
                shutil.copy2(str(src), str(target))
            ok += 1
        except Exception as e:
            logger.exception(f"Failed to {args.mode} {src} -> {target}: {e}")

    logger.info(f"Completed: {ok}/{len(sources)} files processed.")
    return 0 if ok == len(sources) else 5

if __name__ == "__main__":
    sys.exit(main())
