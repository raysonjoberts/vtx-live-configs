#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
check_missing_imports.py

Scan scripts under:
  - VTX_ROOT/usr/scripts/default
  - VTX_ROOT/usr/utils
  - VTX_ROOT/usr/utils/OnDemand

Identify scripts that may fail in the CURRENT venv due to missing packages.

How it works:
- AST-parse each .py file and collect top-level import modules.
- Ignore:
    * stdlib modules
    * relative imports (from .x import y)
    * local VTX modules under VTX_ROOT/usr
- For remaining modules, attempt importlib.import_module(module).
- Report missing modules per file and overall.

Usage:
  python usr/utils/check_missing_imports.py
  python usr/utils/check_missing_imports.py --json var/logs/missing_imports_report.json
  python usr/utils/check_missing_imports.py --fail-on-missing
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple


# -----------------------------
# Paths / discovery
# -----------------------------

def find_vtx_root(start: Path) -> Path:
    env_root = os.environ.get("VTX_ROOT")
    if env_root:
        p = Path(env_root).expanduser().resolve()
        if p.exists():
            return p

    cur = start.resolve()
    for parent in [cur] + list(cur.parents):
        if (parent / "usr").is_dir():
            return parent

    return start.resolve()


def iter_py_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*.py"):
        parts = {p.lower() for p in path.parts}
        if any(x in parts for x in (".venv", "venv", "__pycache__", ".git", ".pytest_cache", "node_modules")):
            continue
        yield path


def safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1", errors="replace")


def top_level_module(name: str) -> str:
    return name.split(".")[0].strip()


# -----------------------------
# Filtering
# -----------------------------

def get_stdlib_names() -> Set[str]:
    names: Set[str] = set(getattr(sys, "builtin_module_names", ()))
    stdlib = getattr(sys, "stdlib_module_names", None)
    if stdlib:
        names |= set(stdlib)
        return names

    # Minimal fallback (best-effort)
    fallback = {
        "os", "sys", "re", "json", "csv", "math", "time", "datetime", "pathlib", "typing",
        "logging", "subprocess", "shutil", "glob", "itertools", "functools", "collections",
        "dataclasses", "argparse", "statistics", "hashlib", "hmac", "base64", "ssl", "socket",
        "http", "urllib", "email", "xml", "html", "traceback", "inspect", "threading",
        "multiprocessing", "queue", "sqlite3", "unittest", "doctest", "pdb", "pickle",
        "platform", "tempfile", "getpass", "textwrap", "types", "enum", "uuid",
        "fnmatch", "copy", "copyreg", "contextlib", "importlib", "pkgutil",
    }
    names |= fallback
    return names


def build_local_module_set(vtx_root: Path) -> Set[str]:
    """
    Collect top-level module names that exist locally in the repo
    so we don't treat them as missing pip deps.
    """
    local: Set[str] = set()

    usr_dir = vtx_root / "usr"
    if usr_dir.exists():
        for p in usr_dir.rglob("*.py"):
            if p.name == "__init__.py":
                local.add(p.parent.name)
            else:
                local.add(p.stem)

    # top-level *.py (if any)
    for p in vtx_root.glob("*.py"):
        local.add(p.stem)

    return local


# Some modules commonly importable only on specific OSes or environments.
DEFAULT_OPTIONAL_PLATFORM_MODULES = {
    "win32com", "pythoncom", "pywintypes",  # pywin32 (Windows)
}

# Some imports are often "optional" / plugins, and you may not want them flagged.
DEFAULT_ALLOWLIST = set()


# -----------------------------
# Import parsing
# -----------------------------

def parse_imports_from_file(py_path: Path) -> Set[str]:
    src = safe_read_text(py_path)

    try:
        tree = ast.parse(src, filename=str(py_path))
    except SyntaxError:
        # fallback: scrape obvious import lines
        mods: Set[str] = set()
        for line in src.splitlines():
            line = line.strip()
            if line.startswith("import "):
                rest = line[len("import "):]
                for chunk in rest.split(","):
                    mod = chunk.strip().split(" as ")[0].strip()
                    if mod:
                        mods.add(top_level_module(mod))
            elif line.startswith("from "):
                m = re.match(r"from\s+([A-Za-z0-9_\.]+)\s+import\s+", line)
                if m:
                    mods.add(top_level_module(m.group(1)))
        return mods

    mods: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(top_level_module(alias.name))
        elif isinstance(node, ast.ImportFrom):
            # ignore relative imports: from .x import y
            if getattr(node, "level", 0):
                continue
            if node.module:
                mods.add(top_level_module(node.module))

    return mods


# -----------------------------
# Reporting
# -----------------------------

@dataclass
class ScriptImportCheck:
    file: str
    missing_modules: List[str]
    checked_modules: List[str]


@dataclass
class MissingImportsReport:
    vtx_root: str
    python_executable: str
    python_version: str
    scanned_paths: List[str]
    files_scanned: int
    scripts_with_missing: int
    missing_module_counts: Dict[str, int]
    results: List[ScriptImportCheck]


def main() -> int:
    ap = argparse.ArgumentParser(description="Check VTX scripts for missing imports in the current venv.")
    ap.add_argument("--vtx-root", default="", help="Path to VTX root (defaults to VTX_ROOT env var or auto-detect).")
    ap.add_argument("--json", default="", help="Write full report to JSON file (relative to VTX root if not absolute).")
    ap.add_argument("--fail-on-missing", action="store_true", help="Exit non-zero if any missing imports are found.")
    ap.add_argument("--allow", nargs="*", default=[], help="Modules to allow/ignore (treated as OK).")
    ap.add_argument("--ignore-path", nargs="*", default=[], help="Path fragments to skip (e.g. tests experimental).")
    args = ap.parse_args()

    vtx_root = Path(args.vtx_root).expanduser().resolve() if args.vtx_root else find_vtx_root(Path(__file__).parent)

    targets = [
        vtx_root / "usr" / "scripts" / "default",
        vtx_root / "usr" / "utils",
        vtx_root / "usr" / "utils" / "OnDemand",
    ]
    scan_roots = [p for p in targets if p.exists()]
    if not scan_roots:
        print(f"ERROR: None of the expected scan paths exist under VTX root: {vtx_root}")
        for t in targets:
            print(f"  - {t}")
        return 2

    stdlib = get_stdlib_names()
    local = build_local_module_set(vtx_root)
    allow = set(DEFAULT_ALLOWLIST) | set(args.allow)

    results: List[ScriptImportCheck] = []
    missing_counts: Dict[str, int] = {}
    files_scanned = 0

    ignore_fragments = [s.lower() for s in args.ignore_path]

    for root in scan_roots:
        for py_file in iter_py_files(root):
            rel = str(py_file.relative_to(vtx_root))
            rel_lower = rel.lower()
            if any(frag in rel_lower for frag in ignore_fragments):
                continue

            files_scanned += 1
            mods = parse_imports_from_file(py_file)

            # Candidates = not stdlib, not local, not allowlist
            candidates = sorted(m for m in mods if m and m not in stdlib and m not in local and m not in allow)

            missing: List[str] = []
            for m in candidates:
                # Optional platform modules: only flag if on the expected platform.
                if m in DEFAULT_OPTIONAL_PLATFORM_MODULES and os.name != "nt":
                    # If not Windows, skip flagging pywin32 imports
                    continue

                try:
                    importlib.import_module(m)
                except Exception:
                    missing.append(m)

            if missing:
                for m in missing:
                    missing_counts[m] = missing_counts.get(m, 0) + 1

            results.append(
                ScriptImportCheck(
                    file=rel,
                    missing_modules=sorted(set(missing)),
                    checked_modules=candidates,
                )
            )

    # Keep only scripts that actually have missing modules for console output
    bad = [r for r in results if r.missing_modules]

    # Console summary
    if not bad:
        print("OK: No missing third-party imports detected for scanned scripts in the current venv.")
    else:
        print(f"FOUND: {len(bad)} script(s) likely to fail due to missing imports in the current venv.\n")

        # Sort missing modules by frequency
        for mod, cnt in sorted(missing_counts.items(), key=lambda kv: (-kv[1], kv[0].lower())):
            print(f"Missing module '{mod}' referenced by {cnt} script(s)")

        print("\nTop offending scripts:")
        for r in bad[:40]:
            print(f"  - {r.file}")
            print(f"      missing: {', '.join(r.missing_modules)}")
        if len(bad) > 40:
            print(f"  ... ({len(bad) - 40} more)")

    report = MissingImportsReport(
        vtx_root=str(vtx_root),
        python_executable=sys.executable,
        python_version=sys.version.replace("\n", " "),
        scanned_paths=[str(p) for p in scan_roots],
        files_scanned=files_scanned,
        scripts_with_missing=len(bad),
        missing_module_counts=dict(sorted(missing_counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))),
        results=results,
    )

    if args.json:
        out = Path(args.json)
        if not out.is_absolute():
            out = vtx_root / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(asdict(report), indent=2, sort_keys=True), encoding="utf-8")
        print(f"\nWrote JSON report: {out}")

    if args.fail_on_missing and bad:
        return 3

    return 0


if __name__ == "__main__":
    raise SystemExit(main())