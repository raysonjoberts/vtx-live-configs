#!/usr/bin/env python3
"""
File: usr/utils/check_unused_scripts.py

Purpose:
--------
Identify scripts in usr/scripts/default that are NOT referenced
by usr/config/run/orchestrator.yaml.

Logic:
------
1. Enumerate *.py files in usr/scripts/default
2. Parse orchestrator.yaml
3. Extract referenced script paths / filenames
4. Report scripts that exist on disk but are unused

Read-only utility. No side effects.
"""

from __future__ import annotations

import os
import sys
import yaml
import shlex
from typing import Set, Dict, Any

# ------------------------------------------------------------
# VTX Paths
# ------------------------------------------------------------
VTX_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPTS_DIR = os.path.join(VTX_ROOT, "usr", "scripts", "default")
ORCHESTRATOR_YAML = os.path.join(
    VTX_ROOT, "usr", "config", "run", "orchestrator.yaml"
)

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def list_scripts_on_disk() -> Set[str]:
    """Return set of script filenames in usr/scripts/default"""
    return {
        f for f in os.listdir(SCRIPTS_DIR)
        if f.endswith(".py") and os.path.isfile(os.path.join(SCRIPTS_DIR, f))
    }


def extract_scripts_from_command(cmd: str) -> Set[str]:
    """
    Extract possible script names from a shell command.
    Handles:
      - python foo.py
      - ./foo.py
      - /abs/path/foo.py
    """
    scripts = set()

    try:
        parts = shlex.split(cmd)
    except Exception:
        return scripts

    for part in parts:
        if part.endswith(".py"):
            scripts.add(os.path.basename(part))

    return scripts


def extract_referenced_scripts(orchestrator: Dict[str, Any]) -> Set[str]:
    """Extract all script filenames referenced in orchestrator.yaml"""
    found: Set[str] = set()

    def scan_job(job: Dict[str, Any]) -> None:
        for key in ("script", "command", "cmd", "exec"):
            if key in job and isinstance(job[key], str):
                found.update(extract_scripts_from_command(job[key]))

    for section in ("cron_jobs", "backbone_jobs", "jobs"):
        jobs = orchestrator.get(section, [])
        if isinstance(jobs, list):
            for job in jobs:
                if isinstance(job, dict):
                    scan_job(job)

    return found


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
def main() -> None:
    if not os.path.exists(ORCHESTRATOR_YAML):
        print(f"[ERROR] Missing orchestrator.yaml: {ORCHESTRATOR_YAML}")
        sys.exit(1)

    with open(ORCHESTRATOR_YAML, "r", encoding="utf-8") as f:
        orchestrator = yaml.safe_load(f) or {}

    scripts_on_disk = list_scripts_on_disk()
    scripts_referenced = extract_referenced_scripts(orchestrator)

    unused_scripts = sorted(scripts_on_disk - scripts_referenced)
    used_scripts = sorted(scripts_on_disk & scripts_referenced)

    print("\n=== VTX Script Usage Audit ===\n")

    print(f"Scripts on disk:     {len(scripts_on_disk)}")
    print(f"Scripts referenced:  {len(scripts_referenced)}")
    print(f"Scripts UNUSED:      {len(unused_scripts)}\n")

    if unused_scripts:
        print("Unused scripts:")
        for s in unused_scripts:
            print(f"  - {s}")
    else:
        print("No unused scripts found 🎉")

    print("\n---")
    print("Referenced scripts:")
    for s in used_scripts:
        print(f"  - {s}")

    print("")


if __name__ == "__main__":
    main()