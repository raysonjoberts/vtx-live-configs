#!/usr/bin/env python3
"""
VTX Config Compiler
-------------------
Merges configs from:
- Default path: <VTX_ROOT>/usr/config/default
- Auto    path: <VTX_ROOT>/usr/config/auto
- Auto V2 path: <VTX_ROOT>/usr/config/auto_v2
- Custom  path: <VTX_ROOT>/usr/config/custom
into:
- Run     path: <VTX_ROOT>/usr/config/run

Precedence / merge order:
  DEFAULT -> AUTO -> AUTO_V2 -> CUSTOM   (later overrides earlier)

Merge rules (by filename base name):
- .yaml/.yml: deep-merge dicts; lists concatenate; later wins
- .ini/.conf: INI-merge; later overrides keys/sections
- others: concatenate in order with headers

Notes:
- Does not fail if any of default/auto/auto_v2/custom dirs are missing.
- Writes are atomic (temp file + replace) and only when output changes.

Usage:
  python config_compiler.py
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import io
import logging
import os
import sys
import tempfile
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

# ------------------------------------------------------------
# Globals / Paths (VTX style)
# ------------------------------------------------------------
VTX_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

DEFAULT_DIR = os.environ.get("VTX_CFG_DEFAULT", os.path.join(VTX_ROOT, "usr", "config", "default"))
AUTO_DIR    = os.environ.get("VTX_CFG_AUTO",    os.path.join(VTX_ROOT, "usr", "config", "auto"))
AUTO_V2_DIR = os.environ.get("VTX_CFG_AUTO_V2", os.path.join(VTX_ROOT, "usr", "config", "auto_v2"))
CUSTOM_DIR  = os.environ.get("VTX_CFG_CUSTOM",  os.path.join(VTX_ROOT, "usr", "config", "custom"))
RUN_DIR     = os.environ.get("VTX_CFG_RUN",     os.path.join(VTX_ROOT, "usr", "config", "run"))

SUPPORTED_YAML = {".yaml", ".yml"}
SUPPORTED_INI  = {".ini", ".conf"}  # treat .conf as INI first; fallback to concat on parse error
ORIGIN_KEY_YAML = "_vtx_origin"
ORIGIN_KEY_INI  = "_vtx_origin"

# Hardcoded exclusions: ignore these files when discovered in auto_v2.
# Keep both table_aggregator extensions for compatibility with historical references.
IGNORED_AUTO_V2_REL_PATHS = {
    "usr/config/auto_v2/server_inventory_matrix_vtx.yaml",
    "usr/config/auto_v2/table_aggregator_vtx.py",
}

# ------------------------------------------------------------
# Logging (BTDM / VTX style)
# ------------------------------------------------------------
try:
    sys.path.append(os.path.join(VTX_ROOT, "usr", "lib"))
    import btdm_logging  # type: ignore

    logger = btdm_logging.get_logger(component="config_compiler")
except Exception:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    logger = logging.getLogger("config_compiler")


def vlog(msg: str, verbose: bool) -> None:
    """Verbose detail logging."""
    if verbose:
        logger.info(msg)
    else:
        logger.debug(msg)


def sha256_bytes(b: bytes) -> str:
    h = hashlib.sha256()
    h.update(b)
    return h.hexdigest()


def read_file_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def atomic_write(path: str, data: bytes) -> None:
    """Write using a temp file then replace, to avoid partial writes."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_config_", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        os.chmod(path, 0o644)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def deep_merge(base: Any, override: Any) -> Any:
    """
    Deep-merge two YAML structures.
    - If both are dicts: keys merged, override wins.
    - If both are lists: concatenate (override appended).
    - Else: override.
    """
    if isinstance(base, dict) and isinstance(override, dict):
        result = dict(base)
        for k, v in override.items():
            if k in result:
                result[k] = deep_merge(result[k], v)
            else:
                result[k] = v
        return result
    elif isinstance(base, list) and isinstance(override, list):
        return base + override
    else:
        return override


def load_yaml(path: str) -> Any:
    if yaml is None:
        raise RuntimeError("PyYAML is required for YAML merge operations.")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def dump_yaml(data: Any) -> bytes:
    if yaml is None:
        raise RuntimeError("PyYAML is required for YAML merge operations.")
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    return text.encode("utf-8")


def parse_ini(path: str) -> Tuple[configparser.ConfigParser, bool]:
    cfg = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg.read_file(f)
        return cfg, True
    except Exception:
        return cfg, False

def merge_ini_many_with_section_origin(paths_in_order: List[Tuple[str, str]]) -> bytes:
    """
    Merge INI configs in order: later overrides earlier, but emit a per-section
    origin comment indicating the *highest-precedence file that defines that section*.

    This is stanza-level provenance (section-level), not per-key.
    """
    parsed: List[Tuple[str, configparser.ConfigParser, bool, str]] = []
    for label, p in paths_in_order:
        cfg, ok = parse_ini(p)
        parsed.append((label, cfg, ok, p))

    if not all(ok for _, _, ok, _ in parsed):
        # fallback to raw concat (your existing behavior)
        header = "# [WARN] One or more INI files failed to parse. Concatenating raw.\n"
        parts = [header]
        for label, p in paths_in_order:
            parts.append(f"# --- BEGIN {label}: {p} ---\n")
            with open(p, "r", encoding="utf-8") as f:
                parts.append(f.read())
            parts.append(f"# --- END {label}: {p} ---\n")
        return "".join(parts).encode("utf-8")

    # Build merged config (same as before)
    out = configparser.ConfigParser(interpolation=None)
    for _, cfg, _, _ in parsed:
        for section in cfg.sections():
            if not out.has_section(section):
                out.add_section(section)
            for k, v in cfg.items(section):
                out.set(section, k, v)

    # Determine section origin: last file (highest precedence) containing the section
    section_origin: Dict[str, str] = {}
    for label, cfg, _, p in parsed:
        o = origin_string(label, p)
        for section in cfg.sections():
            section_origin[section] = o  # later overwrites earlier

    # Custom write with origin comment per section
    buf = io.StringIO()

    # NOTE: ConfigParser doesn't preserve input ordering reliably across sources.
    # We'll output sections in sorted order for stability.
    for section in sorted(out.sections()):
        buf.write(f"; {ORIGIN_KEY_INI} = {section_origin.get(section, 'UNKNOWN')}\n")
        buf.write(f"[{section}]\n")
        for k, v in out.items(section):
            # out.items(section) includes all keys in that section
            buf.write(f"{k} = {v}\n")
        buf.write("\n")

    return buf.getvalue().encode("utf-8")

def merge_ini_many(paths_in_order: List[Tuple[str, str]]) -> bytes:
    """
    Merge INI configs in order: later overrides earlier.
    paths_in_order: list of (label, path) in precedence order.
    """
    parsed: List[Tuple[str, configparser.ConfigParser, bool]] = []
    for label, p in paths_in_order:
        cfg, ok = parse_ini(p)
        parsed.append((label, cfg, ok))

    if not all(ok for _, _, ok in parsed):
        header = "# [WARN] One or more INI files failed to parse. Concatenating raw.\n"
        parts = [header]
        for label, p in paths_in_order:
            parts.append(f"# --- BEGIN {label}: {p} ---\n")
            with open(p, "r", encoding="utf-8") as f:
                parts.append(f.read())
            parts.append(f"# --- END {label}: {p} ---\n")
        return "".join(parts).encode("utf-8")

    out = configparser.ConfigParser(interpolation=None)

    # Apply in order; later overrides earlier
    for _, cfg, _ in parsed:
        for section in cfg.sections():
            if not out.has_section(section):
                out.add_section(section)
            for k, v in cfg.items(section):
                out.set(section, k, v)

    buf = io.StringIO()
    out.write(buf)
    return buf.getvalue().encode("utf-8")


def concat_files_with_header_many(paths_in_order: List[Tuple[str, str]]) -> bytes:
    parts: List[str] = []
    for label, p in paths_in_order:
        parts.append(f"# --- BEGIN {label}: {p} ---\n")
        with open(p, "r", encoding="utf-8") as f:
            parts.append(f.read())
        parts.append(f"\n# --- END {label}: {p} ---\n")
    return "".join(parts).encode("utf-8")

def find_nested_candidates(dir_path: str) -> List[str]:
    """
    Return relative paths for config files found in subdirectories.
    These are not compiled by default to avoid flattening/renaming.
    """
    if not os.path.isdir(dir_path):
        return []
    out: List[str] = []
    for root, _, files in os.walk(dir_path):
        if os.path.abspath(root) == os.path.abspath(dir_path):
            continue
        for f in files:
            if "." not in f:
                continue
            full = os.path.join(root, f)
            out.append(rel_norm(full))
    return out

def warn_duplicate_job_ids(stanzas: Any, label: str) -> None:
    if not isinstance(stanzas, list):
        return
    seen = set()
    dups = []
    for item in stanzas:
        if not isinstance(item, dict):
            continue
        jid = str(item.get("id") or "").strip()
        if not jid:
            continue
        if jid in seen:
            dups.append(jid)
        else:
            seen.add(jid)
    if dups:
        sample = ", ".join(dups[:5])
        more = "" if len(dups) <= 5 else f" (+{len(dups)-5} more)"
        logger.warning("[ORCH-VALIDATE] Duplicate %s ids detected: %s%s", label, sample, more)


def list_candidates(dir_path: str, basename: str) -> List[str]:
    """
    Returns full paths for files in dir_path matching "<basename>.*"
    Does not fail if dir missing.
    """
    if not os.path.isdir(dir_path):
        return []
    out: List[str] = []
    for f in os.listdir(dir_path):
        if f.startswith(basename + "."):
            out.append(os.path.join(dir_path, f))
    out.sort()
    return out


def rel_norm(path: str) -> str:
    """Normalize path to stable VTX-root relative POSIX form."""
    try:
        rel = os.path.relpath(path, VTX_ROOT)
    except Exception:
        rel = path
    return rel.replace("\\", "/")


def filter_auto_v2_candidates(paths: List[str], verbose: bool) -> Tuple[List[str], List[str]]:
    """Exclude hardcoded auto_v2 files from compilation and return (kept, skipped)."""
    kept: List[str] = []
    skipped: List[str] = []
    for p in paths:
        rel = rel_norm(p)
        if rel in IGNORED_AUTO_V2_REL_PATHS:
            vlog(f"[SKIP-AUTO_V2-IGNORE] {rel}", verbose)
            skipped.append(p)
            continue
        kept.append(p)
    return kept, skipped


def choose_preferred_path(
    custom_paths: List[str],
    auto_v2_paths: List[str],
    auto_paths: List[str],
    default_paths: List[str],
) -> Optional[str]:
    """
    Choose output filename/extension based on highest-precedence existing file:
      custom > auto_v2 > auto > default
    """
    return (
        custom_paths[0]
        if custom_paths
        else (
            auto_v2_paths[0]
            if auto_v2_paths
            else (auto_paths[0] if auto_paths else (default_paths[0] if default_paths else None))
        )
    )

def origin_string(label: str, path: str) -> str:
    """
    Stable origin string to embed in compiled configs.
    Example: 'CUSTOM:usr/config/custom/orchestrator.yaml'
    """
    # Keep it stable and readable. Use relpath if possible.
    try:
        rel = os.path.relpath(path, VTX_ROOT)
        if rel.startswith(".."):
            rel = path
    except Exception:
        rel = path
    rel = rel.replace("\\", "/")
    return f"{label}:{rel}"


def annotate_yaml_stanzas(obj: Any, origin: str) -> Any:
    """
    Annotate "stanzas" in a YAML object with ORIGIN_KEY_YAML.

    Standing rule (reduce noise):
      - Tag only the *top-most* dict for each stanza we add.
      - Do NOT recurse into that stanza to tag nested dicts.

    Practical behavior:
      - If root is a dict: tag direct child dict values, and tag dict elements in any direct child lists.
      - If root is a list: tag dict elements.
      - Everything deeper than that is left untouched.

    This yields one `_vtx_origin` per stanza, not one per indentation layer.
    """

    def tag_dict(d: Dict[str, Any]) -> Dict[str, Any]:
        dd = dict(d)
        dd.setdefault(ORIGIN_KEY_YAML, origin)
        return dd

    def tag_list(lst: List[Any]) -> List[Any]:
        out: List[Any] = []
        for item in lst:
            if isinstance(item, dict):
                out.append(tag_dict(item))
            else:
                out.append(item)
        return out

    if isinstance(obj, dict):
        new = dict(obj)
        for k, v in list(new.items()):
            if isinstance(v, dict):
                new[k] = tag_dict(v)
            elif isinstance(v, list):
                new[k] = tag_list(v)
        return new

    if isinstance(obj, list):
        return tag_list(obj)

    return obj

def compile_file(basename: str, verbose: bool) -> bool:
    """
    Compile/merge a single config file by base name (without extension).
    Returns True if the output file was changed (written), False otherwise.
    """
    default_candidates = list_candidates(DEFAULT_DIR, basename)
    auto_candidates    = list_candidates(AUTO_DIR, basename)
    auto_v2_candidates = list_candidates(AUTO_V2_DIR, basename)
    custom_candidates  = list_candidates(CUSTOM_DIR, basename)
    auto_v2_candidates, skipped_auto_v2_candidates = filter_auto_v2_candidates(auto_v2_candidates, verbose=verbose)

    if skipped_auto_v2_candidates:
        skipped_rel = ", ".join(rel_norm(p) for p in skipped_auto_v2_candidates)
        logger.info(f"[AUTO_V2-FILTER] {basename}: skipped {skipped_rel}")

    chosen_path = choose_preferred_path(custom_candidates, auto_v2_candidates, auto_candidates, default_candidates)
    if not chosen_path:
        if skipped_auto_v2_candidates:
            logger.warning(
                f"[SKIP] No compilable tiers remain for {basename} after auto_v2 filtering."
            )
        vlog(f"[SKIP] No default/auto/custom for {basename}", verbose)
        return False

    _, ext = os.path.splitext(chosen_path)
    ext = ext.lower()

    # Build output path (same filename as chosen_path)
    out_filename = os.path.basename(chosen_path)
    out_path = os.path.join(RUN_DIR, out_filename)

    # Build merge inputs in precedence order
    inputs: List[Tuple[str, str]] = []
    if default_candidates:
        inputs.append(("DEFAULT", default_candidates[0]))
    if auto_candidates:
        inputs.append(("AUTO", auto_candidates[0]))
    if auto_v2_candidates:
        inputs.append(("AUTO_V2", auto_v2_candidates[0]))
    if custom_candidates:
        inputs.append(("CUSTOM", custom_candidates[0]))

    # Merge based on extension
    if ext in SUPPORTED_YAML:
        merged: Any = {}
        for label, p in inputs:
            data = load_yaml(p)
            data = annotate_yaml_stanzas(data, origin_string(label, p))
            merged = deep_merge(merged, data)
            vlog(f"[YAML-APPLY] {basename} <= {label}: {p}", verbose)
        if basename == "orchestrator":
            if isinstance(merged, dict):
                added = False
                if "backbone_jobs" not in merged:
                    merged["backbone_jobs"] = []
                    added = True
                if "cron_jobs" not in merged:
                    merged["cron_jobs"] = []
                    added = True
                if added:
                    logger.warning(
                        "[ORCH-VALIDATE] orchestrator.yaml missing backbone_jobs/cron_jobs; defaulted to empty lists."
                    )
                warn_duplicate_job_ids(merged.get("backbone_jobs"), "backbone_jobs")
                warn_duplicate_job_ids(merged.get("cron_jobs"), "cron_jobs")
            else:
                logger.warning("[ORCH-VALIDATE] orchestrator.yaml root is not a dict; cannot validate.")
        out_bytes = dump_yaml(merged)
        logger.info(f"[MERGE-YAML+ORIGIN] {basename} ({len(inputs)} tiers)")
    elif ext in SUPPORTED_INI:
        out_bytes = merge_ini_many_with_section_origin(inputs)
        logger.info(f"[MERGE-INI+ORIGIN] {basename} ({len(inputs)} tiers)")
    else:
        out_bytes = concat_files_with_header_many(inputs)
        logger.info(f"[CONCAT] {basename} ({len(inputs)} tiers)")

    # Only write if contents changed
    if os.path.exists(out_path):
        existing_bytes = read_file_bytes(out_path)
        if sha256_bytes(existing_bytes) == sha256_bytes(out_bytes):
            vlog(f"[UNCHANGED] {basename}", verbose)
            return False

    atomic_write(out_path, out_bytes)
    logger.info(f"[WRITE] {basename} -> {out_path}")
    return True


def compile_all(verbose: bool) -> int:
    """
    Compile all configs by discovering basenames from default/auto/custom directories.
    Returns count of files updated/written.
    """
    nested: List[str] = []
    for d in (DEFAULT_DIR, AUTO_DIR, AUTO_V2_DIR, CUSTOM_DIR):
        nested.extend(find_nested_candidates(d))
    if nested:
        sample = ", ".join(sorted(nested)[:5])
        more = "" if len(nested) <= 5 else f" (+{len(nested)-5} more)"
        logger.warning(
            "[SKIP-NESTED] Found config files in subdirectories (not compiled by default): %s%s",
            sample,
            more,
        )

    basenames = set()

    for d in (DEFAULT_DIR, AUTO_DIR, AUTO_V2_DIR, CUSTOM_DIR):
        if os.path.isdir(d):
            for f in os.listdir(d):
                if "." in f:
                    basenames.add(f.split(".", 1)[0])

    changed_count = 0
    for base in sorted(basenames):
        if compile_file(base, verbose=verbose):
            changed_count += 1
    return changed_count


def watch_loop(interval: float, verbose: bool) -> None:
    """Naive watch loop: every interval seconds, re-run compile_all."""
    logger.info(f"[WATCH] Watching for changes every {interval} seconds...")
    while True:
        changed = compile_all(verbose=verbose)
        if changed:
            logger.info(f"[WATCH] {changed} file(s) updated.")
        time.sleep(interval)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="VTX Config Compiler")
    # Kept for backward-compatibility; behavior now always includes custom-only/auto-only.
    p.add_argument(
        "--include-custom-only",
        action="store_true",
        help="(Legacy) Previously required to include custom-only files. Now custom-only files are always included.",
    )
    p.add_argument(
        "--watch",
        action="store_true",
        help="Watch mode: periodically recompile configs.",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=30.0,
        help="Watch interval in seconds (default: 30).",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose logging (more detail).",
    )
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    # Only ensure RUN exists. Source dirs may legitimately be absent.
    os.makedirs(RUN_DIR, exist_ok=True)

    if args.include_custom_only:
        logger.info("[NOTE] --include-custom-only is legacy; custom-only is always included now.")

    changed = compile_all(verbose=args.verbose)
    logger.info(f"Initial compile complete. {changed} file(s) written.")

    if args.watch:
        try:
            watch_loop(args.interval, verbose=args.verbose)
        except KeyboardInterrupt:
            logger.info("Exiting watch loop.")


if __name__ == "__main__":
    main()
