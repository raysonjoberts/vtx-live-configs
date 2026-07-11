#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
orchestrator_status_vtx.py
--------------------------
Purpose:
  Generate a human-readable markdown report describing the current
  "would-run" status of every orchestrator job without executing any jobs.

Inputs (read-only):
  - usr/config/run/orchestrator.yaml
  - var/logs/orchestrator.log
  - var/runtime/orchestrator_state.json (optional; for last-run timing)

Outputs:
  - var/platform/orchestrator_state.md
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import yaml  # PyYAML
except Exception as e:
    raise SystemExit("Missing dependency: pyyaml. Install it in the VTX venv.") from e

try:
    from croniter import croniter  # optional
    HAVE_CRON = True
except Exception:
    HAVE_CRON = False


# ---------------------------------------------------------------------
# VTX Root / Path handling (platform-agnostic)
# ---------------------------------------------------------------------
def resolve_vtx_root() -> Path:
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    here = Path(__file__).resolve()
    return here.parents[3]


VTX_ROOT = resolve_vtx_root()
DEFAULT_ORCH_PATH = VTX_ROOT / "usr" / "config" / "run" / "orchestrator.yaml"
DEFAULT_LOG_PATH = VTX_ROOT / "var" / "logs" / "orchestrator.log"
DEFAULT_STATE_PATH = VTX_ROOT / "var" / "runtime" / "orchestrator_state.json"
DEFAULT_REPORT_PATH = VTX_ROOT / "var" / "platform" / "orchestrator_state.md"


def vtx_path(path_str: str | Path, *, must_exist: bool = False) -> Path:
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


import re
_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_WIN_UNC_RE = re.compile(r"^(\\\\|//)[^\\/]+[\\/][^\\/]+")


def is_abs_any_platform(p: str) -> bool:
    p = (p or "").strip()
    if not p:
        return False
    if p.startswith("/"):
        return True
    if _WIN_DRIVE_RE.match(p):
        return True
    if _WIN_UNC_RE.match(p):
        return True
    return os.path.isabs(p)


def resolve_path(p: str) -> str:
    p = str(p or "").strip()
    if not p:
        return p
    p = (
        p.replace("{VTX_ROOT}", str(VTX_ROOT))
         .replace("${VTX_ROOT}", str(VTX_ROOT))
         .replace("$VTX_ROOT", str(VTX_ROOT))
    )
    if is_abs_any_platform(p):
        return os.path.normpath(p)
    return os.path.normpath(os.path.join(VTX_ROOT, p))


# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------
def get_logger(component: str) -> logging.Logger:
    lib_dir = VTX_ROOT / "usr" / "lib"
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))
    try:
        import vtx_logging  # type: ignore
        return vtx_logging.get_logger(component=component)
    except Exception:
        logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
        return logging.getLogger(component)


logger = get_logger(component="orchestrator_status_vtx")


# ---------------------------------------------------------------------
# Helpers from orchestrator.py (logic parity)
# ---------------------------------------------------------------------
def now_local() -> datetime:
    return datetime.now()


def parse_interval(s: str) -> timedelta:
    s = str(s).strip().lower()
    num = "".join(ch for ch in s if ch.isdigit())
    unit = "".join(ch for ch in s if ch.isalpha())
    if not num or unit not in {"s", "m", "h", "d"}:
        raise ValueError(f"Invalid interval '{s}'. Use e.g. 30s, 5m, 2h, 1d")
    n = int(num)
    return {"s": timedelta(seconds=n), "m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]


def safe_stat(path: str) -> Optional[os.stat_result]:
    try:
        return os.stat(path)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def mtime_seconds(st: os.stat_result) -> int:
    return int(float(st.st_mtime))


def max_mtime(paths: List[str]) -> Optional[float]:
    mt: Optional[float] = None
    for p in paths:
        st = safe_stat(p)
        if not st:
            continue
        m = float(mtime_seconds(st))
        mt = m if mt is None else max(mt, m)
    return mt


def min_mtime(paths: List[str]) -> Optional[float]:
    mt: Optional[float] = None
    for p in paths:
        st = safe_stat(p)
        if not st:
            continue
        m = float(mtime_seconds(st))
        mt = m if mt is None else min(mt, m)
    return mt


def max_mtime_in_watch_dirs(
    dirs: List[str],
    pattern: str = "",
    recursive: bool = False,
    file_limit: int = 50000,
) -> Tuple[Optional[float], int, bool]:
    import fnmatch
    max_mt: Optional[float] = None
    count = 0
    truncated = False
    pat = (pattern or "").strip()

    def consider_file(fp: str) -> None:
        nonlocal max_mt, count, truncated
        if count >= file_limit:
            truncated = True
            return
        st = safe_stat(fp)
        if not st:
            return
        count += 1
        mt = float(mtime_seconds(st))
        max_mt = mt if max_mt is None else max(max_mt, mt)

    for d in dirs or []:
        try:
            if not os.path.isdir(d):
                continue
            if not recursive:
                with os.scandir(d) as it:
                    for ent in it:
                        if truncated:
                            break
                        if not ent.is_file():
                            continue
                        if pat and not fnmatch.fnmatch(ent.name, pat):
                            continue
                        consider_file(ent.path)
            else:
                for root, _subdirs, files in os.walk(d):
                    if truncated:
                        break
                    for name in files:
                        if truncated:
                            break
                        if pat and not fnmatch.fnmatch(name, pat):
                            continue
                        consider_file(os.path.join(root, name))
        except Exception:
            continue

    return (max_mt, count, truncated)


def scan_mtime_in_watch_dirs(
    dirs: List[str],
    pattern: str = "",
    recursive: bool = False,
    file_limit: int = 50000,
) -> Tuple[Optional[float], Optional[float], int, bool]:
    import fnmatch
    max_mt: Optional[float] = None
    min_mt: Optional[float] = None
    count = 0
    truncated = False
    pat = (pattern or "").strip()

    def consider_file(fp: str) -> None:
        nonlocal max_mt, min_mt, count, truncated
        if count >= file_limit:
            truncated = True
            return
        st = safe_stat(fp)
        if not st:
            return
        count += 1
        mt = float(mtime_seconds(st))
        max_mt = mt if max_mt is None else max(max_mt, mt)
        min_mt = mt if min_mt is None else min(min_mt, mt)

    for d in dirs or []:
        try:
            if not os.path.isdir(d):
                continue
            if not recursive:
                with os.scandir(d) as it:
                    for ent in it:
                        if truncated:
                            break
                        if not ent.is_file():
                            continue
                        if pat and not fnmatch.fnmatch(ent.name, pat):
                            continue
                        consider_file(ent.path)
            else:
                for root, _subdirs, files in os.walk(d):
                    if truncated:
                        break
                    for name in files:
                        if truncated:
                            break
                        if pat and not fnmatch.fnmatch(name, pat):
                            continue
                        consider_file(os.path.join(root, name))
        except Exception:
            continue

    return (max_mt, min_mt, count, truncated)


def default_marker_file(job_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in job_id)
    return os.path.join(VTX_ROOT, "var", "state", "orchestrator_markers", f"{safe}.marker")


@dataclass
class CronJob:
    id: str
    enabled: bool
    schedule: Dict[str, Any]
    interval: Optional[timedelta] = None
    daily_time: Optional[Tuple[int, int]] = None
    cron: Optional[str] = None
    run_on_start: bool = False
    last_run: Optional[datetime] = None

    def compute_next_run(self, base: Optional[datetime] = None) -> datetime:
        base = base or now_local()
        if self.interval:
            return base + self.interval
        if self.daily_time:
            hh, mm = self.daily_time
            dt = datetime(base.year, base.month, base.day, hh, mm)
            if dt <= base:
                dt += timedelta(days=1)
            return dt
        if self.cron and HAVE_CRON:
            return croniter(self.cron, base).get_next(datetime)
        raise RuntimeError("No schedule configured")


@dataclass
class BackboneJob:
    id: str
    enabled: bool
    rule: str
    require_inputs_exist: bool
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    watch_dirs: List[str] = field(default_factory=list)
    watch_glob: str = ""
    watch_recursive: bool = False
    marker_file: str = ""
    marker_match: str = "any"


def backbone_is_stale(j: BackboneJob) -> Tuple[bool, str]:
    if j.rule == "any_file_in_dir":
        dirs = (j.watch_dirs or [])
        if not dirs:
            return (False, "no_watch_dirs")
        pat = (j.watch_glob or "").strip()
        for d in dirs:
            try:
                if not os.path.isdir(d):
                    continue
                if pat:
                    import glob
                    hits = glob.glob(os.path.join(d, pat))
                    if any(os.path.isfile(h) for h in hits):
                        return (True, "dir_has_matching_files")
                else:
                    for name in os.listdir(d):
                        full = os.path.join(d, name)
                        if os.path.isfile(full):
                            return (True, "dir_has_files")
            except Exception:
                return (False, "watch_dir_error")
        return (False, "dir_empty")

    if j.rule == "mtime_input_newer_than_last_success_marker":
        if j.outputs:
            missing_out = [p for p in j.outputs if safe_stat(p) is None]
            if missing_out:
                return (True, f"outputs_missing({len(missing_out)})")

        mf = j.marker_file or default_marker_file(j.id)
        st_m = safe_stat(mf)
        if st_m is None:
            return (True, "marker_missing")

        marker_mt = float(mtime_seconds(st_m))
        input_mtimes: List[float] = []
        missing_inputs = 0
        for p in (j.inputs or []):
            st = safe_stat(p)
            if st is None:
                missing_inputs += 1
                continue
            input_mtimes.append(float(mtime_seconds(st)))

        if j.require_inputs_exist and missing_inputs > 0:
            return (False, f"inputs_missing({missing_inputs})")

        dir_max = dir_min = None
        dir_count = 0
        dir_trunc = False
        if j.watch_dirs:
            dir_max, dir_min, dir_count, dir_trunc = scan_mtime_in_watch_dirs(
                j.watch_dirs,
                pattern=(j.watch_glob or ""),
                recursive=bool(getattr(j, "watch_recursive", False)),
            )

        watched_count = len(input_mtimes) + (dir_count or 0)
        if watched_count == 0:
            return (False, "no_watch_files")

        all_max = max(input_mtimes) if input_mtimes else None
        all_min = min(input_mtimes) if input_mtimes else None

        if dir_max is not None:
            all_max = dir_max if all_max is None else max(all_max, dir_max)
        if dir_min is not None:
            all_min = dir_min if all_min is None else min(all_min, dir_min)

        match = (j.marker_match or "any").strip().lower()
        if match not in ("any", "all"):
            match = "any"

        if match == "any":
            if all_max is not None and all_max > marker_mt:
                suffix = "_truncated" if dir_trunc else ""
                return (True, f"watch_newer_than_marker(files={watched_count}{suffix})")
            return (False, "up_to_date")

        if all_min is not None and all_min > marker_mt:
            suffix = "_truncated" if dir_trunc else ""
            return (True, f"all_watch_newer_than_marker(files={watched_count}{suffix})")
        return (False, "up_to_date")

    if j.rule == "mtime_any_file_in_watch_dirs_newer_than_any_output":
        dirs = (j.watch_dirs or [])
        if not dirs:
            return (False, "no_watch_dirs")

        missing_out = [p for p in j.outputs if safe_stat(p) is None]
        if missing_out:
            return (True, f"outputs_missing({len(missing_out)})")

        out_min = min_mtime(j.outputs) if j.outputs else None
        if out_min is None:
            return (False, "no_output_mtime")

        in_max, count, truncated = max_mtime_in_watch_dirs(
            dirs,
            pattern=(j.watch_glob or ""),
            recursive=bool(getattr(j, "watch_recursive", False)),
        )
        if in_max is None:
            return (False, "no_watch_files")

        if in_max > out_min:
            suffix = "_truncated" if truncated else ""
            return (True, f"watch_dirs_newer_than_output(files={count}{suffix})")
        return (False, "up_to_date")

    if j.require_inputs_exist:
        missing = [p for p in j.inputs if safe_stat(p) is None]
        if missing:
            return (False, f"inputs_missing({len(missing)})")

    missing_out = [p for p in j.outputs if safe_stat(p) is None]
    if missing_out:
        return (True, f"outputs_missing({len(missing_out)})")

    in_max = max_mtime(j.inputs) if j.inputs else None
    out_min = min_mtime(j.outputs) if j.outputs else None

    if j.rule == "exists_only":
        return (False, "exists_only")

    if j.rule in ("mtime_any_input_newer_than_any_output", "mtime_any_input_newer"):
        if in_max is None or out_min is None:
            return (False, "no_mtime_data")
        if in_max > out_min:
            return (True, "input_newer_than_output")
        return (False, "up_to_date")

    if in_max is None or out_min is None:
        return (False, "no_mtime_data")
    if in_max > out_min:
        return (True, "input_newer_than_output")
    return (False, "up_to_date")


# ---------------------------------------------------------------------
# Parsing + status evaluation
# ---------------------------------------------------------------------
def load_yaml(path: Path) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    doc = yaml.safe_load(raw) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"YAML root must be a dict: {path}")
    return doc


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def parse_cron_jobs(cfg: List[Dict[str, Any]], state: Dict[str, Any]) -> List[CronJob]:
    out: List[CronJob] = []
    state_cron = (state.get("cron_jobs") or {})
    for job in cfg:
        jid = str(job.get("id") or "").strip()
        if not jid:
            continue
        sched = dict(job.get("schedule") or {})
        cj = CronJob(id=jid, enabled=bool(job.get("enabled", True)), schedule=sched)
        if "interval" in sched:
            cj.interval = parse_interval(sched["interval"])
        if "daily" in sched:
            hhmm = str(sched["daily"]).strip()
            hh, mm = hhmm.split(":")
            cj.daily_time = (int(hh), int(mm))
        if "cron" in sched:
            cj.cron = str(sched["cron"]).strip()
        cj.run_on_start = bool(job.get("run_on_start", False))

        meta = state_cron.get(jid) or {}
        ts = meta.get("last_run")
        if ts:
            try:
                cj.last_run = datetime.fromisoformat(ts)
            except Exception:
                cj.last_run = None
        out.append(cj)
    return out


def parse_backbone_jobs(cfg: List[Dict[str, Any]]) -> List[BackboneJob]:
    out: List[BackboneJob] = []
    for job in cfg:
        jid = str(job.get("id") or "").strip()
        if not jid:
            continue
        inputs = [resolve_path(p) for p in (job.get("inputs", []) or [])]
        outputs = [resolve_path(p) for p in (job.get("outputs", []) or [])]
        watch_dirs = [resolve_path(p) for p in (job.get("watch_dirs", []) or [])]
        marker_file = str(job.get("marker_file", "") or "").strip()
        marker_file = resolve_path(marker_file) if marker_file else ""
        bj = BackboneJob(
            id=jid,
            enabled=bool(job.get("enabled", True)),
            rule=str(job.get("rule", "mtime_any_input_newer_than_any_output")),
            require_inputs_exist=bool(job.get("require_inputs_exist", True)),
            inputs=inputs,
            outputs=outputs,
            watch_dirs=watch_dirs,
            watch_glob=str(job.get("watch_glob", "") or "").strip(),
            watch_recursive=bool(job.get("watch_recursive", False)),
            marker_file=marker_file,
            marker_match=str(job.get("marker_match", "any") or "any").strip().lower(),
        )
        out.append(bj)
    return out


def eval_cron_status(j: CronJob) -> Tuple[str, str]:
    if not j.enabled:
        return ("🟡", "disabled")
    if j.cron and not HAVE_CRON:
        return ("🟡", "cron_disabled_missing_croniter")
    if not any([j.interval, j.daily_time, j.cron]):
        return ("🟡", "no_schedule")

    now = now_local()
    if j.last_run is None:
        if j.run_on_start:
            return ("🔴", "run_on_start_pending")
        return ("🟡", "no_last_run")

    try:
        next_run = j.compute_next_run(j.last_run)
    except Exception:
        return ("🟡", "schedule_error")

    if now >= next_run:
        return ("🔴", f"past_due(next_run={next_run.isoformat(timespec='seconds')})")
    return ("🟢", f"next_run={next_run.isoformat(timespec='seconds')}")


def eval_backbone_status(j: BackboneJob) -> Tuple[str, str]:
    if not j.enabled:
        return ("🟡", "disabled")
    stale, reason = backbone_is_stale(j)
    if stale:
        return ("🔴", reason)
    warn_prefixes = (
        "inputs_missing",
        "no_watch_dirs",
        "no_watch_files",
        "no_output_mtime",
        "no_mtime_data",
        "watch_dir_error",
    )
    if reason.startswith(warn_prefixes) or reason == "dir_empty":
        return ("🟡", reason)
    return ("🟢", reason)


def load_log_lines(path: Path) -> List[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []


def last_log_entries(job_id: str, lines: List[str], limit: int = 10) -> List[str]:
    hits: List[str] = []
    for line in reversed(lines):
        if job_id in line:
            hits.append(line)
            if len(hits) >= limit:
                break
    return list(reversed(hits))


def write_report(
    out_path: Path,
    cron_results: List[Tuple[str, CronJob, str]],
    bb_results: List[Tuple[str, BackboneJob, str]],
    log_lines: List[str],
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    now_str = datetime.now().astimezone().isoformat(timespec="seconds")
    lines: List[str] = []
    lines.append("# VTX Orchestrator Status")
    lines.append("")
    lines.append(f"Generated: {now_str}")
    lines.append("")
    all_results: List[Tuple[str, str, str, str]] = []
    for emoji, job, reason in cron_results:
        all_results.append((emoji, "cron", job.id, reason))
    for emoji, job, reason in bb_results:
        all_results.append((emoji, "backbone", job.id, reason))

    total = len(all_results)
    counts = {"🟢": 0, "🟡": 0, "🔴": 0}
    for emoji, _kind, _jid, _reason in all_results:
        counts[emoji] = counts.get(emoji, 0) + 1

    lines.append(f"Total jobs: {total}")
    lines.append(f"Status counts: 🔴 {counts.get('🔴', 0)} | 🟡 {counts.get('🟡', 0)} | 🟢 {counts.get('🟢', 0)}")
    lines.append(f"Cron jobs: {len(cron_results)}")
    lines.append(f"Backbone jobs: {len(bb_results)}")
    lines.append("")

    def render_job(kind: str, job_id: str, emoji: str, reason: str) -> None:
        lines.append(f"## {job_id}")
        lines.append("")
        lines.append(f"- Type: {kind}")
        lines.append(f"- Status: {emoji}")
        lines.append(f"- Reason: {reason}")
        if emoji != "🟢":
            entries = last_log_entries(job_id, log_lines, limit=10)
            if entries:
                lines.append("")
                lines.append("Recent log entries:")
                lines.append("```")
                lines.extend(entries)
                lines.append("```")
            else:
                lines.append("")
                lines.append("_No recent log entries found for this job._")
        lines.append("")

    order = {"🔴": 0, "🟡": 1, "🟢": 2}
    all_results.sort(key=lambda r: (order.get(r[0], 9), r[2].lower()))
    for emoji, kind, jid, reason in all_results:
        render_job(kind, jid, emoji, reason)

    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


# ---------------------------------------------------------------------
# CLI / Main
# ---------------------------------------------------------------------
@dataclass
class Options:
    orchestrator_path: Path
    log_path: Path
    state_path: Path
    output_path: Path


def parse_args(argv: Optional[List[str]] = None) -> Options:
    ap = argparse.ArgumentParser(description="Generate a VTX orchestrator status markdown report.")
    ap.add_argument("--orchestrator", default=str(DEFAULT_ORCH_PATH), help="Path to orchestrator.yaml")
    ap.add_argument("--log", default=str(DEFAULT_LOG_PATH), help="Path to orchestrator.log")
    ap.add_argument("--state", default=str(DEFAULT_STATE_PATH), help="Path to orchestrator_state.json")
    ap.add_argument("--output", default=str(DEFAULT_REPORT_PATH), help="Output markdown path")
    args = ap.parse_args(argv)
    return Options(
        orchestrator_path=vtx_path(args.orchestrator, must_exist=True),
        log_path=vtx_path(args.log),
        state_path=vtx_path(args.state),
        output_path=vtx_path(args.output),
    )


def main(argv: Optional[List[str]] = None) -> int:
    opt = parse_args(argv)
    logger.info("VTX_ROOT=%s", VTX_ROOT)
    logger.info("Orchestrator=%s", opt.orchestrator_path)

    cfg = load_yaml(opt.orchestrator_path)
    state = load_state(opt.state_path)
    log_lines = load_log_lines(opt.log_path)

    cron_cfg: List[Dict[str, Any]] = list(cfg.get("cron_jobs", []) or [])
    legacy_jobs = cfg.get("jobs", None)
    if legacy_jobs is not None:
        cron_cfg = list(legacy_jobs or [])
    bb_cfg: List[Dict[str, Any]] = list(cfg.get("backbone_jobs", []) or [])

    cron_jobs = parse_cron_jobs(cron_cfg, state)
    bb_jobs = parse_backbone_jobs(bb_cfg)

    cron_results: List[Tuple[str, CronJob, str]] = []
    for j in cron_jobs:
        emoji, reason = eval_cron_status(j)
        cron_results.append((emoji, j, reason))

    bb_results: List[Tuple[str, BackboneJob, str]] = []
    for j in bb_jobs:
        emoji, reason = eval_backbone_status(j)
        bb_results.append((emoji, j, reason))

    write_report(opt.output_path, cron_results, bb_results, log_lines)
    logger.info("Wrote report: %s", opt.output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
