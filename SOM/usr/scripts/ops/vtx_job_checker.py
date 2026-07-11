#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
vtx_job_checker.py
------------------
Purpose:
  Audit orchestrator.yaml coverage for scripts and config jobs.

Inputs:
  - VTX/usr/config/run/orchestrator.yaml
  - VTX/usr/config/run/*.yaml (excluding orchestrator.yaml)
  - Scripts under VTX/usr/scripts (excluding any Archive folders)

Outputs:
  - VTX/var/platform/vtx_job_checker.md
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import yaml  # PyYAML
except Exception as e:
    raise SystemExit("Missing dependency: pyyaml. Install it in the VTX venv.") from e


# ---------------------------------------------------------------------
# Globals / Paths (VTX style)
# ---------------------------------------------------------------------
def resolve_vtx_root() -> Path:
    """
    Determine VTX_ROOT in a cross-platform way.
    Priority:
      1) env VTX_ROOT
      2) env BTDM_ROOT (legacy)
      3) infer relative to this file: <VTX_ROOT>/usr/scripts/ops/<script>.py
    """
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    here = Path(__file__).resolve()
    inferred = here.parents[3]  # ops/<script>.py -> scripts -> usr -> VTX_ROOT
    return inferred


VTX_ROOT = resolve_vtx_root()

DEFAULT_ORCH_PATH = VTX_ROOT / "usr" / "config" / "run" / "orchestrator.yaml"
DEFAULT_RUN_DIR = VTX_ROOT / "usr" / "config" / "run"
DEFAULT_OUTPUT = VTX_ROOT / "var" / "platform" / "vtx_job_checker.md"


def vtx_path(path_str: str | Path, *, must_exist: bool = False) -> Path:
    """
    Resolve a path that may be:
      - VTX-relative: "var/tables/x.csv"
      - absolute: "/opt/vtx/..." or "C:\\BTDM_7.1\\..."
      - contains env vars: "$VTX_ROOT/var/..." or "%VTX_ROOT%\\var\\..."
      - contains a literal prefix "VTX_ROOT/" or "BTDM_ROOT/" (legacy)
    """
    if isinstance(path_str, Path):
        p = path_str
    else:
        s = str(path_str).strip()
        s = os.path.expandvars(s)
        s = os.path.expanduser(s)
        s = s.replace("VTX_ROOT" + os.sep, str(VTX_ROOT) + os.sep)
        s = s.replace("BTDM_ROOT" + os.sep, str(VTX_ROOT) + os.sep)
        s = s.replace("VTX_ROOT/", str(VTX_ROOT) + "/")
        s = s.replace("BTDM_ROOT/", str(VTX_ROOT) + "/")
        p = Path(s)

    if not p.is_absolute():
        p = (VTX_ROOT / p).resolve()

    if must_exist and not p.exists():
        raise FileNotFoundError(f"Path does not exist: {p}")
    return p


# ---------------------------------------------------------------------
# Logging (VTX style)
# ---------------------------------------------------------------------
def get_logger(component: str) -> logging.Logger:
    """
    Prefer vtx_logging if present; fall back to stdlib.
    """
    lib_dir = VTX_ROOT / "usr" / "lib"
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))

    try:
        import vtx_logging  # type: ignore

        return vtx_logging.get_logger(component=component)
    except Exception:
        logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
        return logging.getLogger(component)


logger = get_logger(component="vtx_job_checker")


# ---------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------
def load_yaml(path: Path) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    doc = yaml.safe_load(raw) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"Config root must be a mapping/dict: {path}")

    if "config" in doc and isinstance(doc.get("config"), dict):
        cfg = doc["config"]
    else:
        cfg = doc

    for k in ("reports", "jobs", "stretches", "inputs", "outputs"):
        if k in cfg and cfg[k] is None:
            cfg[k] = []

    return cfg


def coerce_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def extract_jobs(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        jobs = cfg.get("payload", {}).get("jobs", None)
        if jobs is not None:
            return [j for j in coerce_list(jobs) if isinstance(j, dict)]
    except Exception:
        pass

    try:
        reps = cfg.get("payload", {}).get("reports", None)
        if reps is not None:
            return [r for r in coerce_list(reps) if isinstance(r, dict)]
    except Exception:
        pass

    reps = cfg.get("reports", None)
    if reps is not None:
        return [r for r in coerce_list(reps) if isinstance(r, dict)]

    jobs = cfg.get("jobs", None)
    if jobs is not None:
        return [j for j in coerce_list(jobs) if isinstance(j, dict)]

    return []


def job_id(job: Dict[str, Any]) -> str:
    return str(job.get("id") or job.get("name") or "").strip()


# ---------------------------------------------------------------------
# Audit logic
# ---------------------------------------------------------------------
def collect_orchestrator(orchestrator_path: Path) -> Tuple[Set[str], Set[str]]:
    cfg = load_yaml(orchestrator_path)
    jobs = []
    for key in ("backbone_jobs", "cron_jobs"):
        jobs.extend([j for j in coerce_list(cfg.get(key)) if isinstance(j, dict)])

    job_ids = {str(j.get("id") or "").strip() for j in jobs if j.get("id")}

    scripts = set()
    for j in jobs:
        script = j.get("script")
        if not script:
            continue
        p = vtx_path(script)
        try:
            rel = p.relative_to(VTX_ROOT)
            scripts.add(str(rel).replace(os.sep, "/"))
        except Exception:
            scripts.add(str(p).replace(os.sep, "/"))

    return job_ids, scripts


def collect_script_paths(root: Path) -> List[Path]:
    scripts: List[Path] = []
    for p in root.rglob("*.py"):
        if any(part.lower() == "archive" for part in p.parts):
            continue
        if "__pycache__" in p.parts:
            continue
        scripts.append(p)
    return scripts


def collect_run_jobs(run_dir: Path, orchestrator_path: Path) -> List[Tuple[str, str, str]]:
    jobs: List[Tuple[str, str, str]] = []
    for p in sorted(run_dir.glob("*.yaml")):
        if p.name == orchestrator_path.name:
            continue
        try:
            cfg = load_yaml(p)
        except Exception as exc:
            logger.warning("run_config_failed,path=%s,error=%s", p, exc)
            continue
        for idx, job in enumerate(extract_jobs(cfg)):
            jid = job_id(job)
            jobs.append((jid, p.name, str(idx)))
    return jobs


def rag_sort_key(item: Tuple[str, str, str, str]) -> Tuple[int, str]:
    status = item[0]
    order = {"red": 0, "yellow": 1, "green": 2}
    return (order.get(status, 3), item[1].lower())


def build_markdown(
    script_rows: List[Tuple[str, str, str]],
    job_rows: List[Tuple[str, str, str, str]],
) -> str:
    lines: List[str] = []
    lines.append("# VTX Job Checker")
    lines.append("")
    lines.append("**Scripts**")
    for status, label, note in script_rows:
        emoji = {"red": "🟥", "yellow": "🟨", "green": "🟩"}.get(status, "⬜")
        suffix = f" — {note}" if note else ""
        lines.append(f"- {emoji} `{label}`{suffix}")
    lines.append("")
    lines.append("**Jobs**")
    for status, label, note, source in job_rows:
        emoji = {"red": "🟥", "yellow": "🟨", "green": "🟩"}.get(status, "⬜")
        extra = f" — {note}" if note else ""
        lines.append(f"- {emoji} `{label}` (from `{source}`){extra}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------
# CLI / Options
# ---------------------------------------------------------------------
@dataclass
class Options:
    orchestrator_path: Path
    run_dir: Path
    output_path: Path


def parse_args(argv: Optional[List[str]] = None) -> Options:
    p = argparse.ArgumentParser(description="Audit orchestrator coverage for scripts and jobs")
    p.add_argument(
        "--orchestrator",
        default=str(DEFAULT_ORCH_PATH),
        help=f"Path to orchestrator.yaml (default: {DEFAULT_ORCH_PATH})",
    )
    p.add_argument(
        "--run-dir",
        default=str(DEFAULT_RUN_DIR),
        help=f"Path to config run directory (default: {DEFAULT_RUN_DIR})",
    )
    p.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help=f"Path to output markdown (default: {DEFAULT_OUTPUT})",
    )
    args = p.parse_args(argv)

    orch_path = vtx_path(args.orchestrator, must_exist=True)
    run_dir = vtx_path(args.run_dir, must_exist=True)
    output_path = vtx_path(args.output, must_exist=False)
    return Options(orchestrator_path=orch_path, run_dir=run_dir, output_path=output_path)


# ---------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    opt = parse_args(argv)
    logger.info("VTX_ROOT=%s", VTX_ROOT)
    logger.info("Orchestrator=%s", opt.orchestrator_path)
    logger.info("RunDir=%s", opt.run_dir)

    orch_job_ids, orch_scripts = collect_orchestrator(opt.orchestrator_path)

    script_paths = collect_script_paths(VTX_ROOT / "usr" / "scripts")
    script_rows: List[Tuple[str, str, str]] = []
    for p in script_paths:
        rel = p.relative_to(VTX_ROOT)
        rel_str = str(rel).replace(os.sep, "/")
        if rel_str in orch_scripts:
            script_rows.append(("green", rel_str, "referenced in orchestrator"))
        else:
            script_rows.append(("red", rel_str, "not referenced in orchestrator"))

    missing_scripts = sorted(orch_scripts - {str(p.relative_to(VTX_ROOT)).replace(os.sep, "/") for p in script_paths})
    for rel in missing_scripts:
        script_rows.append(("yellow", rel, "referenced in orchestrator but missing on disk"))

    script_rows.sort(key=lambda x: rag_sort_key((x[0], x[1], "", "")))

    run_jobs = collect_run_jobs(opt.run_dir, opt.orchestrator_path)
    job_rows: List[Tuple[str, str, str, str]] = []
    for jid, source, idx in run_jobs:
        if not jid:
            job_rows.append(("yellow", f"(missing id)#{idx}", "missing job id/name", source))
        elif jid in orch_job_ids:
            job_rows.append(("green", jid, "referenced in orchestrator", source))
        else:
            job_rows.append(("red", jid, "not referenced in orchestrator", source))

    job_rows.sort(key=rag_sort_key)

    output = build_markdown(script_rows, job_rows)
    opt.output_path.parent.mkdir(parents=True, exist_ok=True)
    opt.output_path.write_text(output, encoding="utf-8")
    logger.info("vtx_job_checker_written,path=%s", opt.output_path)
    print(f"[vtx_job_checker] Wrote {opt.output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
