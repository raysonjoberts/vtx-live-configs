# File: usr/scripts/default/scheduler.py
# Purpose: Hybrid Scheduler + Orchestrator for VTX/BTDM
#
# - cron_jobs: strict schedule-driven jobs (cron/daily/interval)
# - backbone_jobs: frequent in-process checks (mtime/exists) that queue real work only when stale
#
# Notes:
# - YAML loaded once and hot-reloaded on change.
# - Backbone checks do NOT consume worker slots; only actual job runs do.
# - Logging for backbone checks is transition-based (quiet by default).
#
# YAML compatibility:
# - If YAML contains "jobs:" (legacy), those are treated as cron_jobs.

from __future__ import annotations

import os
import sys
import time
import json
import shlex
import queue
import signal
import threading
import glob
import subprocess
import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from enum import Enum

# ============================
# Global paths (respect BTDM)
# ============================
VTX_ROOT = os.environ.get("VTX_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
YAML_DEFAULT = os.path.join(VTX_ROOT, "usr", "config", "run", "orchestrator.yaml")
RUNTIME_DIR = os.path.join(VTX_ROOT, "var", "runtime")
STATE_FILE = os.path.join(RUNTIME_DIR, "orchestrator_state.json")
MARKERS_DIR = os.path.join(VTX_ROOT, "var", "state", "orchestrator_markers")
LOGS_DIR = os.path.join(VTX_ROOT, "var", "logs")

# ======================
# Logging (BTDM logging)
# ======================
try:
    sys.path.append(os.path.join(VTX_ROOT, "usr", "lib"))
    import btdm_logging  # type: ignore
    logger = btdm_logging.get_logger(component="orchestrator")
except Exception:
    import logging
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    logger = logging.getLogger("orchestrator")

# =============
# Dependencies
# =============
try:
    import yaml  # PyYAML
except ImportError:
    logger.error("PyYAML is required. Install in venv: pip install pyyaml")
    raise

try:
    from croniter import croniter  # optional
    HAVE_CRON = True
except Exception:
    HAVE_CRON = False
    logger.info("croniter not found; cron expressions disabled for cron_jobs. Install with: pip install croniter")


# ======================
# Helpers / primitives
# ======================

class Role(str, Enum):
    client = "client"
    server = "server"
    both = "both"


def now_local() -> datetime:
    return datetime.now()


def parse_interval(s: str) -> timedelta:
    """Parse '30s', '5m', '2h', '1d'."""
    s = str(s).strip().lower()
    num = "".join(ch for ch in s if ch.isdigit())
    unit = "".join(ch for ch in s if ch.isalpha())
    if not num or unit not in {"s", "m", "h", "d"}:
        raise ValueError(f"Invalid interval '{s}'. Use e.g. 30s, 5m, 2h, 1d")
    n = int(num)
    return {"s": timedelta(seconds=n), "m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]


def find_venv_python() -> str:
    candidates = [
        # Windows first
        os.path.join(VTX_ROOT, "venv", "Scripts", "python.exe"),
        os.path.join(VTX_ROOT, "venv", "Scripts", "python"),
        # POSIX fallbacks
        os.path.join(VTX_ROOT, "venv", "bin", "python"),
        os.path.join(VTX_ROOT, "venv", "bin", "python3"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return sys.executable


import re

_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_WIN_UNC_RE = re.compile(r"^(\\\\|//)[^\\/]+[\\/][^\\/]+")

def is_abs_any_platform(p: str) -> bool:
    """True if path is absolute on POSIX or Windows (drive/UNC), regardless of current OS."""
    p = (p or "").strip()
    if not p:
        return False
    # POSIX absolute (/opt/vtx/...)
    if p.startswith("/"):
        return True
    # Windows drive absolute (C:\... or C:/...)
    if _WIN_DRIVE_RE.match(p):
        return True
    # UNC path (\\server\share\... or //server/share/...)
    if _WIN_UNC_RE.match(p):
        return True
    return os.path.isabs(p)  # fallback


def resolve_path(p: str) -> str:
    """
    Resolve a YAML path:
    - Absolute (POSIX /..., Windows C:\..., UNC \\server\share\...) => keep as-is.
    - Relative (var/..., usr/...) => join to VTX_ROOT.
    Also expands tokens like ${VTX_ROOT} / $VTX_ROOT / {VTX_ROOT}.
    """
    p = str(p or "").strip()
    if not p:
        return p

    # expand simple tokens (safe, no shell eval)
    p = (
        p.replace("{VTX_ROOT}", VTX_ROOT)
         .replace("${VTX_ROOT}", VTX_ROOT)
         .replace("$VTX_ROOT", VTX_ROOT)
    )

    if is_abs_any_platform(p):
        return os.path.normpath(p)

    return os.path.normpath(os.path.join(VTX_ROOT, p))


def expand_args_paths(args: str) -> str:
    """
    Expand path-like tokens inside args.
    Rules:
    - Replaces {VTX_ROOT}, ${VTX_ROOT}, $VTX_ROOT anywhere.
    - For common flags that take paths, if the value is relative, rewrite it to VTX_ROOT-relative absolute.
      (Works with quoted or unquoted values.)
    """
    if not args:
        return args

    # First expand explicit root tokens
    s = (
        args.replace("{VTX_ROOT}", VTX_ROOT)
            .replace("${VTX_ROOT}", VTX_ROOT)
            .replace("$VTX_ROOT", VTX_ROOT)
    )

    # Tokenize like the shell (handles quotes). Then rewrite known path flags.
    try:
        toks = shlex.split(s)
    except Exception:
        # If tokenization fails, at least return token-expanded string
        return s

    # Flags where the next token is a path
    path_flags = {
        "--source", "--src", "--dest", "--dst",
        "--input", "--in", "--output", "--out",
        "--config", "--yaml", "--yml",
        "--path", "--file",
        "--dir", "--directory",
        "--tables-dir", "--report-dir",
    }

    out: List[str] = []
    i = 0
    while i < len(toks):
        tok = toks[i]
        out.append(tok)

        if tok in path_flags and i + 1 < len(toks):
            val = toks[i + 1]
            if val and not is_abs_any_platform(val):
                val = resolve_path(val)
            out.append(val)
            i += 2
            continue

        i += 1

    return " ".join(shlex.quote(t) for t in out)


def safe_stat(path: str) -> Optional[os.stat_result]:
    try:
        return os.stat(path)
    except FileNotFoundError:
        return None
    except Exception:
        return None

def mtime_seconds(st: os.stat_result) -> int:
    """Normalize mtime to whole seconds (drop sub-second precision)."""
    return int(float(st.st_mtime))

def file_sig(path: str) -> Optional[Tuple[float, int]]:
    """Signature: (mtime_seconds, size). None if missing/unreadable."""
    st = safe_stat(path)
    if not st:
        return None
    return (float(mtime_seconds(st)), int(st.st_size))


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

def ensure_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass

def is_processable_watch_file(path: str) -> bool:
    """Return True for real payload files that should trigger watch-dir jobs."""
    name = os.path.basename(path)
    if not name or name.startswith("."):
        return False
    if name.endswith((".tmp", ".part", ".crdownload")):
        return False
    return os.path.isfile(path)


def default_marker_file(job_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in job_id)
    return os.path.join(MARKERS_DIR, f"{safe}.marker")


def touch_marker_file(path: str, payload: Optional[Dict[str, Any]] = None) -> None:
    """
    Update marker mtime to "now" and optionally write a small JSON payload.
    Marker mtime is used as the 'last successful run' timestamp.
    """
    ensure_dir(os.path.dirname(path))
    try:
        if payload is None:
            # Touch-only
            with open(path, "a", encoding="utf-8"):
                pass
        else:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
        os.utime(path, None)
    except Exception:
        pass


def max_mtime_in_watch_dirs(
    dirs: List[str],
    pattern: str = "",
    recursive: bool = False,
    file_limit: int = 50000,
) -> Tuple[Optional[float], int, bool]:
    """
    Return (max_mtime, files_considered, truncated).
    Uses fast scandir; optional glob-style pattern match on filename.
    """
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
    """
    Return (max_mtime, min_mtime, files_considered, truncated).
    Uses fast scandir/os.walk; optional glob-style pattern match on filename.
    """
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

def infer_job_role(job_cfg: Dict[str, Any]) -> str:
    role = str(job_cfg.get("role", "")).strip().lower()
    if role in ("client", "server", "both"):
        return role
    script = str(job_cfg.get("script", "")).replace("\\", "/").lower()
    if script.endswith("/file_move.py") or script.endswith("file_move.py"):
        return "client"
    return "server"


def filter_cfg_by_role(items: List[Dict[str, Any]], selected: str) -> List[Dict[str, Any]]:
    if selected == Role.both.value:
        return items
    keep: List[Dict[str, Any]] = []
    for cfg in items:
        r = infer_job_role(cfg)
        if r == selected or r == Role.both.value:
            keep.append(cfg)
    return keep


# ======================
# Data models
# ======================

@dataclass
class CronJob:
    id: str
    script: str
    args: str = ""
    enabled: bool = True
    cwd: str = ""
    env: Dict[str, str] = field(default_factory=dict)

    schedule: Dict[str, Any] = field(default_factory=dict)
    interval: Optional[timedelta] = None
    daily_time: Optional[Tuple[int, int]] = None
    cron: Optional[str] = None

    timeout_seconds: Optional[int] = None
    retries: int = 0
    backoff_seconds: int = 0
    jitter_seconds: int = 0
    run_on_start: bool = False
    depends_on: List[str] = field(default_factory=list)

    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None
    running: bool = False

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
    script: str
    args: str = ""
    enabled: bool = True
    cwd: str = ""
    env: Dict[str, str] = field(default_factory=dict)

    # Orchestrator check behavior
    # - If check_every_seconds <= 0: ASAP sweep mode (checked whenever the monitor sweep reaches it, within budget)
    # - If check_every_seconds > 0: explicit per-job throttle (legacy behavior, but now opt-in)
    check_every_seconds: int = 0
    settle_seconds: int = 0
    debounce_seconds: int = 0
    rule: str = "mtime_any_input_newer_than_any_output"
    require_inputs_exist: bool = True

    inputs: List[str] = field(default_factory=list)
    config_inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)

    watch_dirs: List[str] = field(default_factory=list)
    watch_glob: str = ""
    watch_recursive: bool = False

    # Marker file for rules that compare to "last successful run"
    marker_file: str = ""

    # Marker comparison mode for marker-based rules:
    # - "any" (default): run if ANY watched file is newer than marker
    # - "all": run only if ALL watched files are newer than marker
    marker_match: str = "any"

    # Downstream marker comparison (for downstream_success_marker rule)
    downstream_jobs: List[str] = field(default_factory=list)
    downstream_mode: str = "any"  # any|all
    require_downstream_markers: bool = False

    timeout_seconds: Optional[int] = None
    retries: int = 0
    backoff_seconds: int = 0
    jitter_seconds: int = 0
    run_on_start: bool = False
    missing_outputs_retry_seconds: int = 30
    depends_on: List[str] = field(default_factory=list)

    # runtime state
    next_check: float = 0.0
    last_seen_fingerprint: Optional[str] = None
    last_change_epoch: Optional[float] = None
    stale_since_epoch: Optional[float] = None
    last_run_epoch: Optional[float] = None
    last_run_fingerprint: Optional[str] = None
    consecutive_failures: int = 0
    last_failure_epoch: Optional[float] = None
    next_allowed_run_epoch: float = 0.0
    running: bool = False
    dirty: bool = False


@dataclass
class PipelineStage:
    id: str
    script: str
    args: str = ""
    cwd: str = ""
    env: Dict[str, str] = field(default_factory=dict)
    timeout_seconds: Optional[int] = None
    retries: int = 0
    backoff_seconds: int = 0
    jitter_seconds: int = 0


@dataclass
class PipelineJob:
    id: str
    enabled: bool = True
    role: str = Role.server.value
    cooldown_seconds: int = 60
    trigger_markers: List[str] = field(default_factory=list)
    stages: List[PipelineStage] = field(default_factory=list)
    running: bool = False
    last_failure_epoch: Optional[float] = None
    next_allowed_run_epoch: float = 0.0
    failed_stage_index: int = 0
    cooldown_notice_epoch: float = 0.0
    already_running_notice: bool = False


def _expand_input_paths(paths: List[str]) -> List[str]:
    expanded: List[str] = []
    seen: set[str] = set()
    for raw in (paths or []):
        p = str(raw or "").strip()
        if not p:
            continue
        matches: List[str]
        if any(ch in p for ch in ("*", "?", "[")):
            try:
                matches = sorted(glob.glob(p))
            except Exception:
                matches = []
            if not matches:
                matches = [p]
        else:
            matches = [p]
        for m in matches:
            if m in seen:
                continue
            seen.add(m)
            expanded.append(m)
    return expanded


def fingerprint_inputs(inputs: List[str]) -> str:
    parts: List[str] = []
    for p in _expand_input_paths(inputs):
        sig = file_sig(p)
        if sig is None:
            parts.append(f"{p}:MISSING")
        else:
            mt, sz = sig
            parts.append(f"{p}:{mt:.3f}:{sz}")
    return "|".join(parts)


# ======================
# Orchestrator
# ======================

class Orchestrator:
    def __init__(self, cfg_path: str, selected_role: str = Role.both.value):
        self.cfg_path = cfg_path
        self.selected_role = selected_role
        self.cfg_mtime: float = 0.0

        self.venv_python = find_venv_python()

        # Config
        self.max_workers: int = 4

        # Monitoring loop tuning
        self.backbone_tick_hz: float = 20.0
        self.backbone_target_ms_per_sec: float = 25.0
        self.backbone_stat_benchmark_samples: int = 12
        self.backbone_stat_benchmark_iters: int = 200
        self.backbone_failure_cooldown_seconds: int = 120

        # Derived
        self._avg_stat_ms: float = 0.10
        self._bb_budget_ms_per_tick: float = 1.0

        self.cron_jobs: Dict[str, CronJob] = {}
        self.backbone_jobs: Dict[str, BackboneJob] = {}
        self.pipeline_jobs: Dict[str, PipelineJob] = {}

        # RR sweep
        self._bb_rr: List[str] = []
        self._bb_rr_idx: int = 0

        # Runtime
        self.queue: "queue.Queue[Tuple[str, str]]" = queue.Queue()
        self.stop_event = threading.Event()
        self.state_lock = threading.Lock()
        self.running_jobs: set[str] = set()

    # ---------- Config loading ----------
    def load_config(self) -> None:
        path = Path(self.cfg_path)
        if not path.exists():
            raise FileNotFoundError(f"Config not found: {self.cfg_path}")

        self.cfg_mtime = path.stat().st_mtime
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        sched_cfg = cfg.get("scheduler", {}) or {}
        self.max_workers = int(sched_cfg.get("max_workers", 4))

        # compat: backbone_check_hz aliases backbone_tick_hz
        if "backbone_tick_hz" in sched_cfg:
            self.backbone_tick_hz = float(sched_cfg.get("backbone_tick_hz", 20.0))
        else:
            self.backbone_tick_hz = float(sched_cfg.get("backbone_check_hz", 20.0))
        if self.backbone_tick_hz <= 0:
            self.backbone_tick_hz = 20.0

        self.backbone_target_ms_per_sec = float(sched_cfg.get("backbone_target_ms_per_sec", 25.0))
        if self.backbone_target_ms_per_sec <= 0:
            self.backbone_target_ms_per_sec = 25.0

        self.backbone_stat_benchmark_samples = int(sched_cfg.get("backbone_stat_benchmark_samples", 12))
        if self.backbone_stat_benchmark_samples <= 0:
            self.backbone_stat_benchmark_samples = 12

        self.backbone_stat_benchmark_iters = int(sched_cfg.get("backbone_stat_benchmark_iters", 200))
        if self.backbone_stat_benchmark_iters <= 0:
            self.backbone_stat_benchmark_iters = 200
        self.backbone_failure_cooldown_seconds = int(
            sched_cfg.get("backbone_failure_cooldown_seconds", 120)
        )
        if self.backbone_failure_cooldown_seconds <= 0:
            self.backbone_failure_cooldown_seconds = 120

        legacy_jobs = cfg.get("jobs", None)
        cron_cfg: List[Dict[str, Any]] = list(legacy_jobs or []) if legacy_jobs is not None else list(cfg.get("cron_jobs", []) or [])
        backbone_cfg: List[Dict[str, Any]] = list(cfg.get("backbone_jobs", []) or [])
        pipeline_cfg: List[Dict[str, Any]] = list(cfg.get("pipeline_jobs", []) or [])

        cron_cfg = filter_cfg_by_role(cron_cfg, self.selected_role)
        backbone_cfg = filter_cfg_by_role(backbone_cfg, self.selected_role)
        pipeline_cfg = filter_cfg_by_role(pipeline_cfg, self.selected_role)

        self.cron_jobs = self._parse_cron_jobs(cron_cfg)
        self.backbone_jobs = self._parse_backbone_jobs(backbone_cfg)
        self.pipeline_jobs = self._parse_pipeline_jobs(pipeline_cfg)

        self._bb_rr = sorted(self.backbone_jobs.keys())
        self._bb_rr_idx = 0

        logger.info(
            f"Loaded config: cron_jobs={len(self.cron_jobs)} backbone_jobs={len(self.backbone_jobs)} pipeline_jobs={len(self.pipeline_jobs)} "
            f"max_workers={self.max_workers} role={self.selected_role}"
        )

        self._bb_budget_ms_per_tick = self.backbone_target_ms_per_sec / max(1.0, self.backbone_tick_hz)

    def maybe_reload(self) -> None:
        try:
            mtime = Path(self.cfg_path).stat().st_mtime
            if mtime != self.cfg_mtime:
                logger.info("Config change detected; reloading...")
                self.load_config()
                self._init_schedules()
                self._benchmark_backbone_stat_latency()
        except Exception as e:
            logger.warning(f"Reload check failed: {e}")

    def _parse_cron_jobs(self, items: List[Dict[str, Any]]) -> Dict[str, CronJob]:
        out: Dict[str, CronJob] = {}
        for cfg in items:
            jid = str(cfg.get("id", "")).strip()
            if not jid:
                raise ValueError("cron job missing 'id'")
            script = resolve_path(cfg.get("script", ""))
            if not script:
                raise ValueError(f"cron job '{jid}' missing 'script'")

            j = CronJob(
                id=jid,
                script=script,
                args=expand_args_paths(str(cfg.get("args", "") or "")),
                enabled=bool(cfg.get("enabled", True)),
                cwd=resolve_path(cfg.get("cwd", "")) if cfg.get("cwd") else "",
                env=dict(cfg.get("env", {}) or {}),
                schedule=dict(cfg.get("schedule", {}) or {}),
                timeout_seconds=cfg.get("timeout_seconds"),
                retries=int(cfg.get("retries", 0)),
                backoff_seconds=int(cfg.get("backoff_seconds", 0)),
                jitter_seconds=int(cfg.get("jitter_seconds", 0)),
                run_on_start=bool(cfg.get("run_on_start", False)),
                depends_on=list(cfg.get("depends_on", []) or []),
            )

            if "interval" in j.schedule:
                j.interval = parse_interval(j.schedule["interval"])
            if "daily" in j.schedule:
                hhmm = str(j.schedule["daily"]).strip()
                hh, mm = hhmm.split(":")
                j.daily_time = (int(hh), int(mm))
            if "cron" in j.schedule:
                j.cron = str(j.schedule["cron"]).strip()
                if j.cron and not HAVE_CRON:
                    raise ValueError(f"cron job '{jid}': cron provided but croniter not installed")

            if not any([j.interval, j.daily_time, j.cron]):
                raise ValueError(f"cron job '{jid}': provide schedule.interval, schedule.daily, or schedule.cron")

            out[j.id] = j
        return out

    def _parse_backbone_jobs(self, items: List[Dict[str, Any]]) -> Dict[str, BackboneJob]:
        out: Dict[str, BackboneJob] = {}
        for cfg in items:
            jid = str(cfg.get("id", "")).strip()
            if not jid:
                raise ValueError("backbone job missing 'id'")
            script = resolve_path(cfg.get("script", ""))
            if not script:
                raise ValueError(f"backbone job '{jid}' missing 'script'")

            inputs = [resolve_path(p) for p in (cfg.get("inputs", []) or [])]
            config_inputs = [resolve_path(p) for p in (cfg.get("config_inputs", []) or [])]
            outputs = [resolve_path(p) for p in (cfg.get("outputs", []) or [])]
            watch_dirs = [resolve_path(p) for p in (cfg.get("watch_dirs", []) or [])]
            watch_glob = str(cfg.get("watch_glob", "") or "").strip()
            watch_recursive = bool(cfg.get("watch_recursive", False))
            marker_file = str(cfg.get("marker_file", "") or "").strip()
            marker_file = resolve_path(marker_file) if marker_file else ""

            # NEW: marker_match parsing (for marker-based rules)
            marker_match = str(cfg.get("marker_match", "any") or "any").strip().lower()
            if marker_match not in ("any", "all"):
                raise ValueError(
                    f"backbone job '{jid}': marker_match must be 'any' or 'all' (got '{marker_match}')"
                )

            downstream_jobs = [str(x).strip() for x in (cfg.get("downstream_jobs", []) or []) if str(x).strip()]
            downstream_mode = str(cfg.get("mode", "any") or "any").strip().lower()
            if downstream_mode not in ("any", "all"):
                logger.warning(
                    "backbone job '%s': downstream mode must be 'any' or 'all' (got '%s'); defaulting to 'any'",
                    jid,
                    downstream_mode,
                )
                downstream_mode = "any"
            require_downstream_markers = bool(cfg.get("require_downstream_markers", False))

            # IMPORTANT:
            # - If missing => default 0 (ASAP sweep)
            # - If <= 0 => ASAP sweep
            # - If > 0 => explicit per-job throttle
            check_every = int(cfg.get("check_every_seconds", 0))

            j = BackboneJob(
                id=jid,
                script=script,
                args=expand_args_paths(str(cfg.get("args", "") or "")),
                enabled=bool(cfg.get("enabled", True)),
                cwd=resolve_path(cfg.get("cwd", "")) if cfg.get("cwd") else "",
                env=dict(cfg.get("env", {}) or {}),
                check_every_seconds=check_every,
                settle_seconds=int(cfg.get("settle_seconds", 0)),
                debounce_seconds=int(cfg.get("debounce_seconds", 0)),
                rule=str(cfg.get("rule", "mtime_any_input_newer_than_any_output")),
                require_inputs_exist=bool(cfg.get("require_inputs_exist", True)),
                inputs=inputs,
                config_inputs=config_inputs,
                outputs=outputs,
                watch_dirs=watch_dirs,
                watch_glob=watch_glob,
                timeout_seconds=cfg.get("timeout_seconds"),
                retries=int(cfg.get("retries", 0)),
                backoff_seconds=int(cfg.get("backoff_seconds", 0)),
                jitter_seconds=int(cfg.get("jitter_seconds", 0)),
                run_on_start=bool(cfg.get("run_on_start", False)),
                missing_outputs_retry_seconds=max(0, int(cfg.get("missing_outputs_retry_seconds", 30))),
                depends_on=list(cfg.get("depends_on", []) or []),
                watch_recursive=watch_recursive,
                marker_file=marker_file,
                marker_match=marker_match,
                downstream_jobs=downstream_jobs,
                downstream_mode=downstream_mode,
                require_downstream_markers=require_downstream_markers,
            )

            out[j.id] = j
        return out

    def _parse_pipeline_jobs(self, items: List[Dict[str, Any]]) -> Dict[str, PipelineJob]:
        out: Dict[str, PipelineJob] = {}
        for cfg in items:
            jid = str(cfg.get("id", "")).strip()
            if not jid:
                raise ValueError("pipeline job missing 'id'")

            stages_cfg = list(cfg.get("stages", []) or [])
            if not stages_cfg:
                raise ValueError(f"pipeline job '{jid}' has no stages")

            stages: List[PipelineStage] = []
            for idx, stage_cfg in enumerate(stages_cfg):
                sid = str(stage_cfg.get("id", "")).strip() or f"{jid}_stage_{idx+1}"
                script = resolve_path(stage_cfg.get("script", ""))
                if not script:
                    raise ValueError(f"pipeline job '{jid}' stage '{sid}' missing 'script'")
                stages.append(
                    PipelineStage(
                        id=sid,
                        script=script,
                        args=expand_args_paths(str(stage_cfg.get("args", "") or "")),
                        cwd=resolve_path(stage_cfg.get("cwd", "")) if stage_cfg.get("cwd") else "",
                        env=dict(stage_cfg.get("env", {}) or {}),
                        timeout_seconds=stage_cfg.get("timeout_seconds"),
                        retries=int(stage_cfg.get("retries", 0)),
                        backoff_seconds=int(stage_cfg.get("backoff_seconds", 0)),
                        jitter_seconds=int(stage_cfg.get("jitter_seconds", 0)),
                    )
                )

            trigger_markers = [resolve_path(p) for p in (cfg.get("trigger_markers", []) or []) if str(p).strip()]
            out[jid] = PipelineJob(
                id=jid,
                enabled=bool(cfg.get("enabled", True)),
                role=str(cfg.get("role", Role.server.value) or Role.server.value).strip().lower(),
                cooldown_seconds=max(1, int(cfg.get("cooldown_seconds", 300) or 300)),
                trigger_markers=trigger_markers,
                stages=stages,
            )
        return out

    # ---------- State ----------
    def load_state(self) -> None:
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}

            cron_data = (data.get("cron_jobs") or {})
            for jid, meta in cron_data.items():
                if jid in self.cron_jobs:
                    ts = meta.get("last_run")
                    if ts:
                        self.cron_jobs[jid].last_run = datetime.fromisoformat(ts)

            bb_data = (data.get("backbone_jobs") or {})
            for jid, meta in bb_data.items():
                if jid in self.backbone_jobs:
                    j = self.backbone_jobs[jid]
                    j.last_run_fingerprint = meta.get("last_run_fingerprint")
                    j.last_run_epoch = meta.get("last_run_epoch")
                    j.last_seen_fingerprint = meta.get("last_seen_fingerprint")
                    j.last_change_epoch = meta.get("last_change_epoch")
                    j.stale_since_epoch = meta.get("stale_since_epoch")
                    j.consecutive_failures = int(meta.get("consecutive_failures") or 0)
                    j.last_failure_epoch = meta.get("last_failure_epoch")
                    j.next_allowed_run_epoch = float(meta.get("next_allowed_run_epoch") or 0.0)

            pipeline_data = (data.get("pipeline_jobs") or {})
            for jid, meta in pipeline_data.items():
                if jid in self.pipeline_jobs:
                    p = self.pipeline_jobs[jid]
                    p.last_failure_epoch = meta.get("last_failure_epoch")
                    p.next_allowed_run_epoch = float(meta.get("next_allowed_run_epoch") or 0.0)
                    p.failed_stage_index = int(meta.get("failed_stage_index") or 0)
        except Exception as e:
            logger.warning(f"Failed to load state: {e}")

    def save_state(self) -> None:
        try:
            os.makedirs(RUNTIME_DIR, exist_ok=True)
            payload: Dict[str, Any] = {"cron_jobs": {}, "backbone_jobs": {}}

            for jid, j in self.cron_jobs.items():
                payload["cron_jobs"][jid] = {
                    "last_run": j.last_run.isoformat() if j.last_run else None,
                    "next_run": j.next_run.isoformat() if j.next_run else None,
                    "running": j.running,
                }

            for jid, j in self.backbone_jobs.items():
                payload["backbone_jobs"][jid] = {
                    "last_run_fingerprint": j.last_run_fingerprint,
                    "last_run_epoch": j.last_run_epoch,
                    "last_seen_fingerprint": j.last_seen_fingerprint,
                    "last_change_epoch": j.last_change_epoch,
                    "stale_since_epoch": j.stale_since_epoch,
                    "consecutive_failures": j.consecutive_failures,
                    "last_failure_epoch": j.last_failure_epoch,
                    "next_allowed_run_epoch": j.next_allowed_run_epoch,
                }

            payload["pipeline_jobs"] = {}
            for jid, p in self.pipeline_jobs.items():
                payload["pipeline_jobs"][jid] = {
                    "last_failure_epoch": p.last_failure_epoch,
                    "next_allowed_run_epoch": p.next_allowed_run_epoch,
                    "failed_stage_index": p.failed_stage_index,
                }

            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            logger.warning(f"Failed to save state: {e}")

    # ---------- Scheduling init ----------
    def _init_schedules(self) -> None:
        now = now_local()
        for j in self.cron_jobs.values():
            if not j.enabled:
                continue
            j.next_run = now if j.run_on_start else j.compute_next_run(now)

        epoch = time.time()
        for j in self.backbone_jobs.values():
            j.next_check = epoch  # eligible immediately
            if j.enabled and j.run_on_start:
                key = f"backbone:{j.id}"
                if key not in self.running_jobs:
                    j.running = True
                    self.running_jobs.add(key)
                    self.queue.put(("backbone", j.id))

    # ---------- Startup benchmark / tuning ----------
    def _benchmark_backbone_stat_latency(self) -> None:
        try:
            candidates: List[str] = []
            seen: set[str] = set()
            for j in self.backbone_jobs.values():
                for p in (j.inputs or []) + (j.outputs or []):
                    if not p or p in seen:
                        continue
                    st = safe_stat(p)
                    if st is None:
                        continue
                    seen.add(p)
                    candidates.append(p)
                    if len(candidates) >= self.backbone_stat_benchmark_samples:
                        break
                if len(candidates) >= self.backbone_stat_benchmark_samples:
                    break

            if not candidates:
                logger.info("Backbone stat benchmark: no existing sample files found; using fallback stat cost.")
                self._avg_stat_ms = 0.10
            else:
                iters = max(20, int(self.backbone_stat_benchmark_iters))
                total_calls = 0
                t0 = time.perf_counter()
                for i in range(iters):
                    p = candidates[i % len(candidates)]
                    try:
                        os.stat(p)
                    except Exception:
                        pass
                    total_calls += 1
                dt = (time.perf_counter() - t0)
                if total_calls <= 0 or dt <= 0:
                    self._avg_stat_ms = 0.10
                else:
                    self._avg_stat_ms = (dt / total_calls) * 1000.0

            approx_stats_per_sec = (self.backbone_target_ms_per_sec / max(0.001, self._avg_stat_ms))
            logger.info(
                "Backbone tuning: tick_hz=%.1f target_ms_per_sec=%.1f => budget_ms_per_tick=%.3f; "
                "bench_avg_stat_ms=%.3f (~%.0f stats/sec within target)",
                self.backbone_tick_hz,
                self.backbone_target_ms_per_sec,
                self._bb_budget_ms_per_tick,
                self._avg_stat_ms,
                approx_stats_per_sec,
            )
        except Exception as e:
            logger.warning(f"Backbone stat benchmark failed: {e}")
            self._avg_stat_ms = 0.10

    # ---------- Execution ----------
    def _build_cmd(self, script: str, args: str) -> List[str]:
        cmd = [self.venv_python, script]
        if args:
            cmd += shlex.split(args)
        return cmd

    def _pipeline_stage_marker(self, pipeline_id: str, stage_id: str) -> str:
        return default_marker_file(f"{pipeline_id}__{stage_id}")

    def _execute_script(
        self,
        label: str,
        script: str,
        args: str,
        cwd: str,
        env_overrides: Dict[str, str],
        timeout_seconds: Optional[int],
        retries: int,
        backoff_seconds: int,
        jitter_seconds: int,
    ) -> Tuple[bool, Dict[str, Any]]:
        env = os.environ.copy()
        env["VTX_ROOT"] = VTX_ROOT
        env.update({k: str(v) for k, v in (env_overrides or {}).items()})
        run_cwd = cwd if cwd else VTX_ROOT
        cmd = self._build_cmd(script, args)

        if jitter_seconds:
            delay = int.from_bytes(os.urandom(1), "big") % (jitter_seconds + 1)
            if delay:
                logger.info(f"[{label}] jitter sleep {delay}s")
                time.sleep(delay)

        attempt = 0
        last_result: Dict[str, Any] = {}
        while True:
            attempt += 1
            logger.info(f"[{label}] starting (attempt {attempt}) -> {cmd}")
            rc = 0
            out = ""
            err = ""
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=run_cwd,
                    env=env,
                    timeout=timeout_seconds,
                    capture_output=True,
                    text=True,
                )
                rc = proc.returncode
                out = proc.stdout or ""
                err = proc.stderr or ""
            except subprocess.TimeoutExpired:
                rc = -999
                err = f"Timed out after {timeout_seconds}s"
            except Exception as e:
                rc = -998
                err = f"Launch failed: {e}"

            if out.strip():
                logger.info(f"[{label}] stdout:\n{out.rstrip()}")
            if err.strip():
                logger.warning(f"[{label}] stderr:\n{err.rstrip()}")

            ok = (rc == 0)
            last_result = {"ok": ok, "rc": rc, "stdout": out, "stderr": err, "cmd": cmd}
            if ok:
                logger.info(f"[{label}] finished successfully")
                return True, last_result

            logger.error(f"[{label}] failed (rc={rc})")
            if attempt <= retries:
                backoff = max(0, backoff_seconds)
                if backoff:
                    logger.info(f"[{label}] backoff {backoff}s before retry")
                    time.sleep(backoff)
                continue
            return False, last_result

    def _launch_job(self, kind: str, job_id: str) -> bool:
        if kind == "cron":
            j = self.cron_jobs[job_id]
            label = f"cron:{j.id}"
        else:
            j = self.backbone_jobs[job_id]
            label = f"backbone:{j.id}"
        start_dt = now_local()
        start_epoch = time.time()
        ok, _result = self._execute_script(
            label=label,
            script=j.script,
            args=j.args,
            cwd=getattr(j, "cwd", ""),
            env_overrides=getattr(j, "env", {}) or {},
            timeout_seconds=getattr(j, "timeout_seconds", None),
            retries=int(getattr(j, "retries", 0)),
            backoff_seconds=int(getattr(j, "backoff_seconds", 0)),
            jitter_seconds=int(getattr(j, "jitter_seconds", 0)),
        )
        if kind == "cron":
            j.last_run = start_dt
        else:
            j.last_run_epoch = start_epoch
        return ok

    def _should_trigger_pipeline(self, p: PipelineJob) -> Tuple[bool, str]:
        if not p.trigger_markers:
            return (False, "no_trigger_markers")
        newest_trigger = max_mtime(p.trigger_markers)
        if newest_trigger is None:
            return (False, "trigger_markers_missing")
        pipeline_marker = default_marker_file(p.id)
        st = safe_stat(pipeline_marker)
        if st is None:
            return (True, "pipeline_marker_missing")
        pipeline_mt = float(mtime_seconds(st))
        if newest_trigger > pipeline_mt:
            return (True, "trigger_newer_than_pipeline_marker")
        return (False, "up_to_date")

    def _launch_pipeline(self, pipeline_id: str) -> bool:
        p = self.pipeline_jobs[pipeline_id]
        logger.info("[pipeline:%s] started", p.id)
        stage_index = max(0, int(p.failed_stage_index or 0))

        while stage_index < len(p.stages):
            stage = p.stages[stage_index]
            stage_label = f"pipeline:{p.id}:stage:{stage.id}"
            logger.info("[pipeline:%s] stage_started stage=%s script=%s", p.id, stage.id, stage.script)
            ok, result = self._execute_script(
                label=stage_label,
                script=stage.script,
                args=stage.args,
                cwd=stage.cwd,
                env_overrides=stage.env,
                timeout_seconds=stage.timeout_seconds,
                retries=stage.retries,
                backoff_seconds=stage.backoff_seconds,
                jitter_seconds=stage.jitter_seconds,
            )
            if ok:
                touch_marker_file(
                    self._pipeline_stage_marker(p.id, stage.id),
                    payload={
                        "pipeline_id": p.id,
                        "stage_id": stage.id,
                        "last_success_epoch": time.time(),
                        "last_success_iso": datetime.now().astimezone().isoformat(timespec="seconds"),
                    },
                )
                logger.info("[pipeline:%s] stage_success stage=%s", p.id, stage.id)
                p.failed_stage_index = stage_index + 1
                p.last_failure_epoch = None
                p.next_allowed_run_epoch = 0.0
                p.cooldown_notice_epoch = 0.0
                stage_index += 1
                continue

            p.failed_stage_index = stage_index
            p.last_failure_epoch = time.time()
            p.next_allowed_run_epoch = p.last_failure_epoch + float(max(300, p.cooldown_seconds))
            p.cooldown_notice_epoch = 0.0
            logger.error(
                "[pipeline:%s] stage_failed stage=%s script=%s rc=%s stdout=%r stderr=%r",
                p.id,
                stage.id,
                stage.script,
                result.get("rc"),
                (result.get("stdout") or "")[:4000],
                (result.get("stderr") or "")[:4000],
            )
            logger.error("[pipeline:%s] stopped_due_to_failure failed_stage=%s retry_after=%s", p.id, stage.id, datetime.fromtimestamp(p.next_allowed_run_epoch).astimezone().isoformat(timespec="seconds"))
            return False

        touch_marker_file(
            default_marker_file(p.id),
            payload={
                "pipeline_id": p.id,
                "last_success_epoch": time.time(),
                "last_success_iso": datetime.now().astimezone().isoformat(timespec="seconds"),
            },
        )
        p.failed_stage_index = 0
        p.last_failure_epoch = None
        p.next_allowed_run_epoch = 0.0
        p.cooldown_notice_epoch = 0.0
        logger.info("[pipeline:%s] completed_successfully", p.id)
        return True

    def worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                kind, jid = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue

            ok = False
            try:
                if kind == "pipeline":
                    ok = self._launch_pipeline(jid)
                else:
                    ok = self._launch_job(kind, jid)
            finally:
                with self.state_lock:
                    if kind == "cron":
                        j = self.cron_jobs.get(jid)
                        if j:
                            j.running = False
                            j.next_run = j.compute_next_run()
                    else:
                        j2 = self.backbone_jobs.get(jid)
                        if j2:
                            j2.running = False
                            j2.last_run_fingerprint = fingerprint_inputs((j2.inputs or []) + (j2.config_inputs or []))
                            j2.dirty = False
                            # Only stamp success if declared outputs exist. This prevents
                            # false-success markers for jobs that exited 0 but produced nothing.
                            if ok:
                                if j2.consecutive_failures > 0 or j2.next_allowed_run_epoch > 0:
                                    logger.info(
                                        "[backbone:%s] cooldown reset after successful run (previous_failures=%d)",
                                        j2.id,
                                        j2.consecutive_failures,
                                    )
                                j2.consecutive_failures = 0
                                j2.last_failure_epoch = None
                                j2.next_allowed_run_epoch = 0.0
                                missing_out = [p for p in (j2.outputs or []) if safe_stat(p) is None]
                                if missing_out:
                                    logger.warning(
                                        f"[backbone:{j2.id}] success rc=0 but outputs missing; suppressing success marker ({len(missing_out)})"
                                    )
                                else:
                                    mf = j2.marker_file or default_marker_file(j2.id)
                                    touch_marker_file(
                                        mf,
                                        payload={
                                            "job_id": j2.id,
                                            "last_success_epoch": time.time(),
                                            "last_success_iso": datetime.now().astimezone().isoformat(timespec="seconds"),
                                        },
                                    )
                            else:
                                j2.consecutive_failures = int(j2.consecutive_failures or 0) + 1
                                j2.last_failure_epoch = time.time()
                                cooldown = max(1, int(self.backbone_failure_cooldown_seconds))
                                j2.next_allowed_run_epoch = j2.last_failure_epoch + float(cooldown)
                                logger.error(
                                    "[backbone:%s] entering failure cooldown for %ss (failures=%d, next_allowed=%s)",
                                    j2.id,
                                    cooldown,
                                    j2.consecutive_failures,
                                    datetime.fromtimestamp(j2.next_allowed_run_epoch).astimezone().isoformat(timespec="seconds"),
                                )
                            if j2.debounce_seconds:
                                j2.next_check = max(j2.next_check, time.time() + j2.debounce_seconds)

                    if kind == "pipeline":
                        p = self.pipeline_jobs.get(jid)
                        if p:
                            p.running = False
                            p.already_running_notice = False

                    self.running_jobs.discard(f"{kind}:{jid}")
                    self.save_state()

                self.queue.task_done()

    # ---------- Backbone freshness logic ----------
    def _backbone_is_stale(self, j: BackboneJob) -> Tuple[bool, str]:
        # Global fallback: if declared outputs are missing, treat as stale regardless of rule.
        # Preserve exists_only semantics by not forcing runs for that mode.
        if j.outputs and j.rule != "exists_only":
            missing_outputs = [p for p in (j.outputs or []) if safe_stat(p) is None]
            if missing_outputs:
                retry_delay = max(0, int(getattr(j, "missing_outputs_retry_seconds", 30) or 0))
                if j.last_run_epoch is not None and retry_delay > 0:
                    elapsed = now_local().timestamp() - float(j.last_run_epoch)
                    if elapsed < retry_delay:
                        wait_s = int(max(1, retry_delay - elapsed))
                        return (False, f"outputs_missing_wait({len(missing_outputs)},retry_in={wait_s}s)")
                return (True, f"outputs_missing({len(missing_outputs)})")

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
                        hits = glob.glob(os.path.join(d, pat))
                        if any(os.path.isfile(h) for h in hits):
                            return (True, "dir_has_matching_files")
                    else:
                        for name in os.listdir(d):
                            full = os.path.join(d, name)
                            if is_processable_watch_file(full):
                                return (True, "dir_has_files")
                except Exception:
                    return (False, "watch_dir_error")

            return (False, "dir_empty")
        
        if j.rule in ("mtime_input_newer_than_last_success_marker", "mtime_any_input_newer_than_latest_success_marker"):
            # Compare watched file mtimes to marker mtime written on last successful run.
            #
            # Watched set = inputs[] + files in watch_dirs (filtered by watch_glob, optional recursive).
            # marker_match:
            #   - any (default): stale if ANY watched file is newer than marker
            #   - all: stale if ALL watched files are newer than marker

            mf = j.marker_file or default_marker_file(j.id)
            st_m = safe_stat(mf)
            if st_m is None:
                return (True, "marker_missing")

            marker_mt = float(mtime_seconds(st_m))

            # ---- gather mtimes from explicit inputs + config_inputs ----
            input_mtimes: List[float] = []
            config_mtimes: List[float] = []
            missing_inputs = 0
            missing_config_inputs = 0
            for p in _expand_input_paths(j.inputs or []):
                st = safe_stat(p)
                if st is None:
                    missing_inputs += 1
                    continue
                input_mtimes.append(float(mtime_seconds(st)))
            for p in _expand_input_paths(j.config_inputs or []):
                st = safe_stat(p)
                if st is None:
                    missing_config_inputs += 1
                    continue
                config_mtimes.append(float(mtime_seconds(st)))

            if j.require_inputs_exist and (missing_inputs > 0 or missing_config_inputs > 0):
                return (False, f"inputs_missing({missing_inputs}),config_inputs_missing({missing_config_inputs})")

            # ---- gather mtimes from watch_dirs ----
            dir_max = dir_min = None
            dir_count = 0
            dir_trunc = False
            if j.watch_dirs:
                dir_max, dir_min, dir_count, dir_trunc = scan_mtime_in_watch_dirs(
                    j.watch_dirs,
                    pattern=(j.watch_glob or ""),
                    recursive=bool(getattr(j, "watch_recursive", False)),
                )

            # ---- combine into one watched set ----
            watched_count = len(input_mtimes) + len(config_mtimes) + (dir_count or 0)

            if watched_count == 0:
                # Nothing to compare to marker.
                return (False, "no_watch_files")

            all_max = max(input_mtimes) if input_mtimes else None
            all_min = min(input_mtimes) if input_mtimes else None
            cfg_max = max(config_mtimes) if config_mtimes else None
            cfg_min = min(config_mtimes) if config_mtimes else None

            if cfg_max is not None:
                all_max = cfg_max if all_max is None else max(all_max, cfg_max)
            if cfg_min is not None:
                all_min = cfg_min if all_min is None else min(all_min, cfg_min)

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
                    if cfg_max is not None and cfg_max > marker_mt:
                        return (True, f"config_newer_than_marker(files={watched_count}{suffix})")
                    return (True, f"watch_newer_than_marker(files={watched_count}{suffix})")
                return (False, "up_to_date")

            # match == "all"
            if all_min is not None and all_min > marker_mt:
                suffix = "_truncated" if dir_trunc else ""
                return (True, f"all_watch_newer_than_marker(files={watched_count}{suffix})")
            return (False, "up_to_date")

        if j.rule == "downstream_success_marker":
            downstream = list(j.downstream_jobs or [])
            if not downstream:
                logger.warning("[backbone:%s] downstream_success_marker missing downstream_jobs", j.id)
                return (False, "downstream_jobs_missing")

            mf_self = j.marker_file or default_marker_file(j.id)
            st_self = safe_stat(mf_self)
            if st_self is None:
                logger.info(
                    "[backbone:%s] downstream_success_marker self=missing mode=%s require=%s downstream=%d -> trigger",
                    j.id,
                    j.downstream_mode,
                    j.require_downstream_markers,
                    len(downstream),
                )
                return (True, "marker_missing")

            self_mt = float(mtime_seconds(st_self))
            mode = (j.downstream_mode or "any").strip().lower()
            if mode not in ("any", "all"):
                mode = "any"

            preds: List[bool] = []
            details: List[str] = []
            for dj in downstream:
                dm = default_marker_file(dj)
                st_d = safe_stat(dm)
                if st_d is None:
                    details.append(f"{dj}:missing")
                    preds.append(False)
                    continue
                dmt = float(mtime_seconds(st_d))
                details.append(f"{dj}:{int(dmt)}")
                preds.append(dmt > self_mt)

            trigger = any(preds) if mode == "any" else (all(preds) and len(preds) > 0)
            if trigger:
                logger.info(
                    "[backbone:%s] downstream_success_marker self=%d mode=%s require=%s downstream=[%s] -> trigger",
                    j.id,
                    int(self_mt),
                    mode,
                    j.require_downstream_markers,
                    ", ".join(details),
                )
            if trigger:
                return (True, f"downstream_newer_than_marker(mode={mode})")
            return (False, "downstream_not_newer")

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

    def _maybe_queue_backbone(self, j: BackboneJob) -> None:
        now = time.time()
        if now < float(getattr(j, "next_allowed_run_epoch", 0.0) or 0.0):
            # Skip trigger/fingerprint checks entirely while in cooldown.
            # This prevents high-frequency stale/cooldown log churn.
            j.next_check = max(j.next_check, float(j.next_allowed_run_epoch))
            return

        fp = fingerprint_inputs((j.inputs or []) + (j.config_inputs or []))

        if j.last_seen_fingerprint is None:
            j.last_seen_fingerprint = fp
            j.last_change_epoch = now
        elif fp != j.last_seen_fingerprint:
            j.last_seen_fingerprint = fp
            j.last_change_epoch = now

        stale, reason = self._backbone_is_stale(j)
        if not stale:
            # Clear settle timer whenever the job is no longer stale
            j.stale_since_epoch = None
            if reason.startswith("inputs_missing"):
                if j.dirty:
                    logger.warning(f"[backbone:{j.id}] blocked: {reason}")
                j.dirty = False
            return

        # Start (or maintain) a settle timer based on how long the job has been stale
        if j.stale_since_epoch is None:
            j.stale_since_epoch = now

        if j.settle_seconds:
            if (now - j.stale_since_epoch) < j.settle_seconds:
                j.dirty = True
                return

        j.dirty = True

        key = f"backbone:{j.id}"
        if key in self.running_jobs or j.running:
            return

        if j.depends_on:
            for dep in j.depends_on:
                if any(rk.endswith(f":{dep}") for rk in self.running_jobs):
                    return

        if len(self.running_jobs) >= self.max_workers:
            return

        logger.info(f"[backbone:{j.id}] STALE -> queue (reason={reason})")
        with self.state_lock:
            j.running = True
            self.running_jobs.add(key)
            self.save_state()
        self.queue.put(("backbone", j.id))

    def _maybe_queue_pipeline(self, p: PipelineJob) -> None:
        now = time.time()
        key = f"pipeline:{p.id}"
        if key in self.running_jobs or p.running:
            if not p.already_running_notice:
                logger.info("[pipeline:%s] skipped_already_running", p.id)
                p.already_running_notice = True
            return
        p.already_running_notice = False
        if now < float(p.next_allowed_run_epoch or 0.0):
            if float(p.cooldown_notice_epoch or 0.0) != float(p.next_allowed_run_epoch or 0.0):
                logger.info(
                    "[pipeline:%s] skipped_cooldown next_allowed=%s",
                    p.id,
                    datetime.fromtimestamp(p.next_allowed_run_epoch).astimezone().isoformat(timespec="seconds"),
                )
                p.cooldown_notice_epoch = float(p.next_allowed_run_epoch or 0.0)
            return
        p.cooldown_notice_epoch = 0.0
        should_run, reason = self._should_trigger_pipeline(p)
        if not should_run:
            return
        if len(self.running_jobs) >= self.max_workers:
            return
        logger.info("[pipeline:%s] triggered reason=%s", p.id, reason)
        with self.state_lock:
            p.running = True
            self.running_jobs.add(key)
            self.save_state()
        logger.info("[pipeline:%s] queued", p.id)
        self.queue.put(("pipeline", p.id))

    # ---------- Main loops ----------
    def run(self) -> int:
        logger.info(f"Scheduler/Orchestrator starting. VTX_ROOT={VTX_ROOT}")
        os.makedirs(LOGS_DIR, exist_ok=True)
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        os.makedirs(MARKERS_DIR, exist_ok=True)

        self.load_config()
        self.load_state()
        self._init_schedules()

        self._benchmark_backbone_stat_latency()

        workers: List[threading.Thread] = []
        for i in range(self.max_workers):
            t = threading.Thread(target=self.worker_loop, name=f"worker-{i}", daemon=True)
            t.start()
            workers.append(t)

        def handle_sig(signum, frame):
            logger.info(f"Received signal {signum}; shutting down...")
            self.stop_event.set()

        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(s, handle_sig)
            except Exception:
                pass

        tick_sleep = 1.0 / max(1.0, self.backbone_tick_hz)

        try:
            while not self.stop_event.is_set():
                self.maybe_reload()

                # 1) cron scheduling
                now = now_local()
                for j in self.cron_jobs.values():
                    if not j.enabled or j.running:
                        continue
                    if j.depends_on and any(rk.endswith(f":{dep}") for dep in j.depends_on for rk in self.running_jobs):
                        continue
                    if j.next_run and now >= j.next_run:
                        key = f"cron:{j.id}"
                        if key in self.running_jobs:
                            continue
                        if len(self.running_jobs) >= self.max_workers:
                            break
                        with self.state_lock:
                            j.running = True
                            self.running_jobs.add(key)
                            self.save_state()
                        logger.info(f"[cron:{j.id}] due -> queue")
                        self.queue.put(("cron", j.id))

                # 2) backbone sweep (continuous RR within time budget)
                epoch = time.time()
                start_tick = time.perf_counter()
                budget_s = max(0.0001, self._bb_budget_ms_per_tick / 1000.0)

                if self._bb_rr:
                    max_scans = len(self._bb_rr)  # at most one full sweep per tick
                    scanned = 0

                    while scanned < max_scans:
                        if (time.perf_counter() - start_tick) >= budget_s:
                            break

                        jid = self._bb_rr[self._bb_rr_idx]
                        self._bb_rr_idx = (self._bb_rr_idx + 1) % len(self._bb_rr)
                        scanned += 1

                        j = self.backbone_jobs.get(jid)
                        if not j or not j.enabled:
                            continue

                        if j.next_check > epoch:
                            continue

                        # If debounce pushed next_check, respect it.
                        # Otherwise:
                        # - check_every_seconds > 0 => per-job throttle
                        # - check_every_seconds <= 0 => ASAP sweep (eligible again next sweep)
                        if j.check_every_seconds and j.check_every_seconds > 0:
                            j.next_check = epoch + max(1, j.check_every_seconds)
                        else:
                            j.next_check = epoch  # ASAP sweep mode

                        self._maybe_queue_backbone(j)

                # 3) pipeline trigger evaluation
                for p in self.pipeline_jobs.values():
                    if not p.enabled:
                        continue
                    self._maybe_queue_pipeline(p)

                time.sleep(tick_sleep)
        finally:
            logger.info("Waiting for queued jobs to finish...")
            self.queue.join()
            logger.info("Scheduler/Orchestrator stopped.")
        return 0

    # ---------- Admin / CLI helpers ----------
    def list_jobs(self) -> None:
        self.load_config()
        if self.cron_jobs:
            print("CRON JOBS:")
            for j in self.cron_jobs.values():
                status = "ENABLED" if j.enabled else "DISABLED"
                sched = j.schedule
                print(f"  {j.id}\t{status}\t{j.script}\t{j.args}\t{sched}")
        if self.backbone_jobs:
            print("\nBACKBONE JOBS:")
            for j in self.backbone_jobs.values():
                status = "ENABLED" if j.enabled else "DISABLED"
                ce = j.check_every_seconds
                ce_s = f"{ce}s" if ce and ce > 0 else "ASAP"
                print(f"  {j.id}\t{status}\t{j.script}\t{j.args}")
                print(f"    inputs={len(j.inputs)} outputs={len(j.outputs)} rule={j.rule} check_every={ce_s}")
        if self.pipeline_jobs:
            print("\nPIPELINE JOBS:")
            for p in self.pipeline_jobs.values():
                status = "ENABLED" if p.enabled else "DISABLED"
                print(f"  {p.id}\t{status}\tstages={len(p.stages)} cooldown={p.cooldown_seconds}s")
                for stage in p.stages:
                    print(f"    - {stage.id}\t{stage.script}\t{stage.args}")

    def run_once(self, ids: List[str], include_disabled: bool = False) -> int:
        self.load_config()
        failures = 0

        all_ids = set(ids)
        targets: List[Tuple[str, str]] = []

        for jid, j in self.cron_jobs.items():
            if (not all_ids or jid in all_ids) and (include_disabled or j.enabled):
                targets.append(("cron", jid))

        for jid, j in self.backbone_jobs.items():
            if (not all_ids or jid in all_ids) and (include_disabled or j.enabled):
                targets.append(("backbone", jid))

        for jid, p in self.pipeline_jobs.items():
            if (not all_ids or jid in all_ids) and (include_disabled or p.enabled):
                targets.append(("pipeline", jid))

        if ids and not targets:
            logger.error(f"No matching jobs for: {', '.join(ids)}")
            return 2
        if not targets:
            logger.error("No jobs selected")
            return 2

        logger.info(f"Running {len(targets)} job(s) immediately")
        for kind, jid in targets:
            try:
                ok = self._launch_job(kind, jid)
                if not ok:
                    failures += 1
            except Exception as e:
                failures += 1
                logger.error(f"[{kind}:{jid}] run-once failed: {e}")

        return 1 if failures else 0


# -------------------------------
# CLI
# -------------------------------
def build_cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="BTDM Scheduler/Orchestrator")
    sub = p.add_subparsers(dest="cmd")

    p.add_argument("--yaml", default=YAML_DEFAULT, help="Path to orchestrator.yaml (or legacy scheduler.yaml)")
    p.add_argument("--role", choices=[r.value for r in Role], default=Role.both.value)

    sub.add_parser("run", help="Start scheduler/orchestrator loop (default)")
    sub.add_parser("list", help="List parsed jobs")

    r = sub.add_parser("run-once", help="Run one or more jobs immediately and exit")
    r.add_argument("--id", action="append", dest="ids", help="Job id to run (repeatable)")
    r.add_argument("--all", action="store_true", dest="run_all", help="Run all jobs once")
    r.add_argument("--include-disabled", action="store_true", help="Include disabled jobs")

    return p


def main(argv: Optional[List[str]] = None) -> int:
    cli = build_cli()
    args = cli.parse_args(argv)

    orch = Orchestrator(cfg_path=args.yaml, selected_role=args.role)

    if args.cmd in (None, "run"):
        return orch.run()

    if args.cmd == "list":
        orch.list_jobs()
        return 0

    if args.cmd == "run-once":
        ids = list(args.ids or [])
        if args.run_all:
            ids = []
        return orch.run_once(ids=ids, include_disabled=args.include_disabled)

    cli.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
