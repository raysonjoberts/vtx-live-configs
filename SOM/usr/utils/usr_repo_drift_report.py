#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
usr_repo_drift_report.py
------------------------
Compare install usr content against a repo usr tree and write a human-readable
Markdown drift report.

Default comparison targets:
  install usr: <VTX_ROOT>/usr
  repo usr:    <VTX_ROOT>/../repos/usr

Override paths with:
  --install-usr /path/to/VTX/usr
  --repo-usr    /path/to/repos/usr
  --output      /path/to/report.md
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


def resolve_vtx_root() -> Path:
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


VTX_ROOT = resolve_vtx_root()
DEFAULT_INSTALL_USR = VTX_ROOT / "usr"
DEFAULT_REPO_USR = VTX_ROOT / "repos" / "vtx-dev" / "usr"
DEFAULT_OUTPUT = VTX_ROOT / "var" / "platform" / "usr_repo_drift_report.md"


@dataclass(frozen=True)
class FileInfo:
    rel_path: str
    size: int
    mtime_epoch: float

    @property
    def mtime_text(self) -> str:
        return datetime.fromtimestamp(self.mtime_epoch).strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class DiffInfo:
    rel_path: str
    install_size: int
    repo_size: int
    install_mtime: str
    repo_mtime: str
    size_diff: bool
    mtime_diff: bool


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare VTX/usr against repo usr and write a Markdown drift report.")
    parser.add_argument("--install-usr", default=str(DEFAULT_INSTALL_USR))
    parser.add_argument("--repo-usr", default=str(DEFAULT_REPO_USR))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    return parser.parse_args(argv)


def is_in_archive(rel_path: str) -> bool:
    parts = [part.lower() for part in Path(rel_path).parts]
    return "archive" in parts


def is_ignored_install(rel_path: str) -> bool:
    return rel_path.startswith(("config/auto/", "config/auto_v2/", "config/run/", "config/custom/"))


def is_ignored_any(rel_path: str) -> bool:
    return is_in_archive(rel_path)


def collect_files(root: Path, ignore_func=None) -> Dict[str, FileInfo]:
    files: Dict[str, FileInfo] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if ignore_func and ignore_func(rel):
            continue
        stat = path.stat()
        files[rel] = FileInfo(rel_path=rel, size=stat.st_size, mtime_epoch=stat.st_mtime)
    return files


def build_diffs(install_files: Dict[str, FileInfo], repo_files: Dict[str, FileInfo]) -> Tuple[List[str], List[str], List[DiffInfo]]:
    install_only = sorted(set(install_files) - set(repo_files))
    repo_only = sorted(set(repo_files) - set(install_files))

    changed: List[DiffInfo] = []
    for rel in sorted(set(install_files) & set(repo_files)):
        install_info = install_files[rel]
        repo_info = repo_files[rel]
        size_diff = install_info.size != repo_info.size
        mtime_diff = int(install_info.mtime_epoch) != int(repo_info.mtime_epoch)
        if not (size_diff or mtime_diff):
            continue
        changed.append(
            DiffInfo(
                rel_path=rel,
                install_size=install_info.size,
                repo_size=repo_info.size,
                install_mtime=install_info.mtime_text,
                repo_mtime=repo_info.mtime_text,
                size_diff=size_diff,
                mtime_diff=mtime_diff,
            )
        )
    return install_only, repo_only, changed


def bullet_list(paths: Iterable[str]) -> str:
    items = list(paths)
    if not items:
        return "- None\n"
    return "".join(f"- `{item}`\n" for item in items)


def changed_table(rows: List[DiffInfo]) -> str:
    if not rows:
        return "_None_\n"
    lines = [
        "| Relative Path | Install Size | Repo Size | Install MTime | Repo MTime | Size Diff | MTime Diff |",
        "|---|---:|---:|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| `{row.rel_path}` | {row.install_size} | {row.repo_size} | {row.install_mtime} | {row.repo_mtime} | "
            f"{'yes' if row.size_diff else 'no'} | {'yes' if row.mtime_diff else 'no'} |"
        )
    return "\n".join(lines) + "\n"


def group_by_folder(paths: Iterable[str]) -> Dict[str, List[str]]:
    grouped: Dict[str, List[str]] = {}
    for path in paths:
        parts = path.split("/", 1)
        folder = parts[0] if len(parts) > 1 else "(root)"
        grouped.setdefault(folder, []).append(path)
    for items in grouped.values():
        items.sort()
    return dict(sorted(grouped.items(), key=lambda x: x[0]))


def grouped_list(paths: Iterable[str]) -> str:
    items = list(paths)
    if not items:
        return "- None\n"
    grouped = group_by_folder(items)
    lines: List[str] = []
    for folder, files in grouped.items():
        lines.append(f"### {folder}")
        lines.append("")
        lines.append(bullet_list(files).rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_report(install_usr: Path, repo_usr: Path, install_only: List[str], repo_only: List[str], changed: List[DiffInfo]) -> str:
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary = [
        "# VTX usr vs repo drift report",
        "",
        f"- Generated: `{generated}`",
        f"- Install usr: `{install_usr}`",
        f"- Repo usr: `{repo_usr}`",
        f"- Install-only files: `{len(install_only)}`",
        f"- Repo-only files: `{len(repo_only)}`",
        f"- Files present in both but different by size and/or mtime: `{len(changed)}`",
        "",
        "## Install-only files",
        "",
        grouped_list(install_only),
        "## Repo-only files",
        "",
        grouped_list(repo_only),
        "## Files present in both but different",
        "",
        changed_table(changed),
    ]
    return "\n".join(summary)


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv)
    install_usr = Path(args.install_usr).expanduser().resolve()
    repo_usr = Path(args.repo_usr).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()

    if not install_usr.exists():
        print(f"[usr_repo_drift_report] ERROR: install usr path does not exist: {install_usr}", file=sys.stderr)
        return 1
    if not repo_usr.exists():
        print(f"[usr_repo_drift_report] ERROR: repo usr path does not exist: {repo_usr}", file=sys.stderr)
        return 2

    install_files = collect_files(install_usr, ignore_func=lambda p: is_ignored_any(p) or is_ignored_install(p))
    repo_files = collect_files(repo_usr, ignore_func=is_ignored_any)
    install_only, repo_only, changed = build_diffs(install_files, repo_files)
    report = render_report(install_usr, repo_usr, install_only, repo_only, changed)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")

    print(f"[usr_repo_drift_report] Install usr: {install_usr}")
    print(f"[usr_repo_drift_report] Repo usr:    {repo_usr}")
    print(f"[usr_repo_drift_report] Install-only: {len(install_only)}")
    print(f"[usr_repo_drift_report] Repo-only:    {len(repo_only)}")
    print(f"[usr_repo_drift_report] Changed:      {len(changed)}")
    print(f"[usr_repo_drift_report] Report:       {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
