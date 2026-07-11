#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
usr_run_config_usage_report.py
------------------------------
Scan active scripts under VTX/usr/scripts (excluding any Archive folders)
and compare against config files in VTX/usr/config/run.

Outputs a Markdown report showing:
  - config/run files referenced by scripts
  - config/run files not referenced by any active script
  - scripts that still reference config/default (legacy)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple


def resolve_vtx_root() -> Path:
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


VTX_ROOT = resolve_vtx_root()
DEFAULT_SCRIPTS_ROOT = VTX_ROOT / "usr" / "scripts"
DEFAULT_CONFIG_RUN = VTX_ROOT / "usr" / "config" / "run"
DEFAULT_OUTPUT = VTX_ROOT / "var" / "platform" / "usr_run_config_usage_report.md"


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report config/run usage by active scripts.")
    parser.add_argument("--scripts-root", default=str(DEFAULT_SCRIPTS_ROOT))
    parser.add_argument("--config-run", default=str(DEFAULT_CONFIG_RUN))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    return parser.parse_args(argv)


def is_in_archive(path: Path) -> bool:
    return any(part.lower() == "archive" for part in path.parts)


def collect_active_scripts(root: Path) -> List[Path]:
    scripts: List[Path] = []
    for path in sorted(root.rglob("*.py")):
        if not path.is_file():
            continue
        if is_in_archive(path.relative_to(root)):
            continue
        scripts.append(path)
    return scripts


def collect_config_run_files(root: Path) -> List[Path]:
    return [p for p in sorted(root.iterdir()) if p.is_file() and not is_in_archive(p.relative_to(root))]


def normalize_rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def build_usage(
    scripts_root: Path, config_run_root: Path, scripts: Iterable[Path], config_files: Iterable[Path]
) -> Tuple[Dict[str, List[str]], List[str], List[str]]:
    config_rel_paths = [normalize_rel(config_run_root, p) for p in config_files]
    config_names = [Path(rel).name for rel in config_rel_paths]

    used_by: Dict[str, List[str]] = {rel: [] for rel in config_rel_paths}
    scripts_using_default: List[str] = []
    scripts_with_run_path_no_match: List[str] = []

    default_re = re.compile(r"config[\\/]+default", re.IGNORECASE)
    run_re = re.compile(r"config[\\/]+run", re.IGNORECASE)

    for script in scripts:
        rel_script = normalize_rel(scripts_root, script)
        try:
            content = script.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        if default_re.search(content):
            scripts_using_default.append(rel_script)

        matched_any = False
        for rel_path, name in zip(config_rel_paths, config_names):
            if rel_path in content or name in content:
                used_by[rel_path].append(rel_script)
                matched_any = True

        if run_re.search(content) and not matched_any:
            scripts_with_run_path_no_match.append(rel_script)

    for rel in used_by:
        used_by[rel] = sorted(
            [s for s in used_by[rel] if not is_in_archive(Path(s))]
        )

    scripts_using_default = sorted([s for s in scripts_using_default if not is_in_archive(Path(s))])
    scripts_with_run_path_no_match = sorted(
        [s for s in scripts_with_run_path_no_match if not is_in_archive(Path(s))]
    )

    return used_by, scripts_using_default, scripts_with_run_path_no_match


def bullet_list(items: Iterable[str]) -> str:
    items_list = list(items)
    if not items_list:
        return "- None\n"
    return "".join(f"- `{item}`\n" for item in items_list)


def used_list(used_by: Dict[str, List[str]]) -> str:
    lines: List[str] = []
    for rel_path in sorted(used_by.keys()):
        scripts = used_by[rel_path]
        if not scripts:
            continue
        joined = ", ".join(f"`{s}`" for s in scripts)
        lines.append(f"- `{rel_path}` (used by: {joined})")
    if not lines:
        return "- None\n"
    return "\n".join(lines) + "\n"


def render_report(
    scripts_root: Path,
    config_run_root: Path,
    scripts_count: int,
    used_by: Dict[str, List[str]],
    scripts_using_default: List[str],
    scripts_with_run_path_no_match: List[str],
) -> str:
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    used = [rel for rel, scripts in used_by.items() if scripts]
    unused = [rel for rel, scripts in used_by.items() if not scripts]

    summary = [
        "# VTX config/run usage report",
        "",
        f"- Generated: `{generated}`",
        f"- Scripts root: `{scripts_root}`",
        f"- Config/run root: `{config_run_root}`",
        f"- Active scripts scanned: `{scripts_count}`",
        f"- Config/run files: `{len(used_by)}`",
        f"- Config/run files referenced: `{len(used)}`",
        f"- Config/run files not referenced: `{len(unused)}`",
        f"- Scripts referencing config/default: `{len(scripts_using_default)}`",
        "",
        "## Config/run files in use",
        "",
        used_list(used_by),
        "## Config/run files not referenced by active scripts",
        "",
        bullet_list(sorted(unused)),
        "## Scripts referencing config/default (legacy)",
        "",
        bullet_list(scripts_using_default),
        "## Scripts referencing config/run path but no known config/run file",
        "",
        bullet_list(scripts_with_run_path_no_match),
    ]
    return "\n".join(summary)


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv)
    scripts_root = Path(args.scripts_root).expanduser().resolve()
    config_run_root = Path(args.config_run).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()

    if not scripts_root.exists():
        print(f"[usr_run_config_usage_report] ERROR: scripts root does not exist: {scripts_root}", file=sys.stderr)
        return 1
    if not config_run_root.exists():
        print(f"[usr_run_config_usage_report] ERROR: config/run path does not exist: {config_run_root}", file=sys.stderr)
        return 2

    scripts = collect_active_scripts(scripts_root)
    config_files = collect_config_run_files(config_run_root)
    used_by, scripts_using_default, scripts_with_run_path_no_match = build_usage(
        scripts_root, config_run_root, scripts, config_files
    )

    report = render_report(
        scripts_root,
        config_run_root,
        len(scripts),
        used_by,
        scripts_using_default,
        scripts_with_run_path_no_match,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")

    used = sum(1 for _, scripts in used_by.items() if scripts)
    unused = sum(1 for _, scripts in used_by.items() if not scripts)

    print(f"[usr_run_config_usage_report] Scripts root: {scripts_root}")
    print(f"[usr_run_config_usage_report] Config/run:   {config_run_root}")
    print(f"[usr_run_config_usage_report] Config used:  {used}")
    print(f"[usr_run_config_usage_report] Config unused:{unused}")
    print(f"[usr_run_config_usage_report] Report:       {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
