# --- BEGIN DEFAULT: C:\BTDM_7.1\usr\config\default\workflow_autogen.py ---
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
workflow_autogen.py
-------------------------
Purpose:
  Generate legacy-format orchestrator + stretch configs (auto_v2) by scanning source YAMLs
  and applying a first-match-wins policy YAML.

Inputs:
  - Policy YAML (VTX universal template; kind=autogen_policy recommended)
  - Source YAMLs in:
      - usr/config/default
      - usr/config/custom
    excluding:
      - orchestrator.yaml
      - stretch.yaml

Outputs:
  - usr/config/auto_v2/orchestrator.yaml   (legacy orchestrator format)
  - usr/config/auto_v2/stretch.yaml        (legacy stretch format)

Key Behaviors:
  - VTX_ROOT aware (VTX_ROOT / BTDM_ROOT)
  - Uses vtx_logging/btdm_logging if available; falls back to stdlib logging
  - First match wins: policies are evaluated in order; the first policy that matches a job is used
  - Defaults are merged with policy overrides (deep merge)
  - Deterministic IDs:
      Producer:      "Autogen - {vtx_id} - {job_id}"
      Stretch sync:  "Autogen - stretch_sync - {stretch_name}"
      Backup:        "Autogen - backup - {vtx_id} - {job_id}"

Notes:
  - This script intentionally emits legacy orchestrator/stretch formats (not the new universal template).
  - config_compiler can later merge auto_v2 + default + custom into run.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml  # PyYAML
except Exception as e:
    raise SystemExit(f"Missing dependency: PyYAML is required. Error: {e}")


# -----------------------------
# Logging (VTX style fallback)
# -----------------------------
def _get_logger(name: str = "workflow_autogen") -> logging.Logger:
    # Try VTX logging modules if present (name varies across installs)
    for mod_name in ("vtx_logging", "btdm_logging"):
        try:
            mod = __import__(mod_name)  # type: ignore
            if hasattr(mod, "get_logger"):
                return mod.get_logger(name)  # type: ignore
        except Exception:
            pass

    # stdlib fallback
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        handler.setFormatter(fmt)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


LOG = _get_logger()


# -----------------------------
# VTX_ROOT resolution
# -----------------------------
def resolve_vtx_root(cli_root: Optional[str] = None) -> Path:
    """
    Resolve VTX_ROOT:
      1) CLI --vtx-root if provided
      2) env VTX_ROOT
      3) env BTDM_ROOT
      4) ascend from script location, looking for 'usr' + 'var' siblings (best-effort)
    """
    if cli_root:
        return Path(cli_root).expanduser().resolve()

    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    # Best-effort: find likely root by walking up from this file
    here = Path(__file__).resolve()
    for parent in [here.parent] + list(here.parents):
        usr = parent / "usr"
        var = parent / "var"
        if usr.exists() and var.exists():
            return parent

    # Final fallback: current working directory
    return Path.cwd().resolve()


def vtx_path(root: Path, p: str) -> Path:
    """
    Convert VTX-relative path (e.g. "var/reporting/x.html") to absolute,
    or return absolute paths unchanged.
    """
    pp = Path(p).expanduser()
    if pp.is_absolute():
        return pp
    return (root / pp).resolve()


# -----------------------------
# YAML helpers
# -----------------------------
def yaml_load(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a dict: {path}")
    return data


def yaml_dump(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(
            data,
            f,
            sort_keys=False,
            default_flow_style=False,
            width=120,
            allow_unicode=True,
        )

def yaml_dump_orchestrator_with_commented_stretch(
    path: Path,
    data_without_stretch: Dict[str, Any],
    commented_stretch_jobs: List[Dict[str, Any]],
) -> None:
    """Write orchestrator.yaml where stretch_sync jobs are appended as fully commented blocks."""
    path.parent.mkdir(parents=True, exist_ok=True)

    # 1) Write active (non-stretch) YAML first
    base_text = yaml.safe_dump(
        data_without_stretch,
        sort_keys=False,
        default_flow_style=False,
        width=120,
        allow_unicode=True,
    )

    # 2) Prepare commented blocks for stretch jobs
    commented_parts: List[str] = []
    if commented_stretch_jobs:
        commented_parts.append("\n# --- autogen stretch_sync jobs (commented out for now) ---\n")
        for j in commented_stretch_jobs:
            # Dump as a single-item list so it resembles a backbone_jobs entry
            block = yaml.safe_dump(
                [j],
                sort_keys=False,
                default_flow_style=False,
                width=120,
                allow_unicode=True,
            ).rstrip("\n")
            for line in block.splitlines():
                commented_parts.append(f"# {line}")
            commented_parts.append("#")

    final_text = base_text.rstrip("\n") + "\n" + "\n".join(commented_parts).rstrip("\n") + "\n"

    with path.open("w", encoding="utf-8") as f:
        f.write(final_text)

def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """
    Recursively merge override into base (override wins).
    """
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)  # type: ignore
        else:
            out[k] = v
    return out


# -----------------------------
# Policy matching
# -----------------------------
def _listify(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]


def condition_matches(cond: Dict[str, Any], *, vtx_id: str, tags: List[str], job_id: Optional[str]) -> bool:
    """
    Supported condition keys:
      - source_vtx_id: <str>
      - source_job_id: <str>
      - source_tags_any: [tag1, tag2, ...]  (true if ANY provided tag in source tags)
    """
    if "source_vtx_id" in cond:
        if str(cond["source_vtx_id"]) != vtx_id:
            return False

    if "source_job_id" in cond:
        if job_id is None or str(cond["source_job_id"]) != job_id:
            return False

    if "source_tags_any" in cond:
        want = [str(t) for t in _listify(cond["source_tags_any"])]
        if not any(t in tags for t in want):
            return False

    return True


def policy_matches(policy: Dict[str, Any], *, vtx_id: str, tags: List[str], job_id: Optional[str]) -> bool:
    """
    Policy match format:
      match:
        all: [ {cond}, {cond} ... ]
        any: [ {cond}, {cond} ... ]
    Semantics:
      - If 'all' exists: every condition dict must match
      - If 'any' exists: at least one condition dict must match
      - If both exist: both constraints must be satisfied
      - If neither exists: does not match
    """
    match = policy.get("match") or {}
    if not isinstance(match, dict):
        return False

    has_all = "all" in match
    has_any = "any" in match
    if not has_all and not has_any:
        return False

    if has_all:
        all_conds = _listify(match.get("all"))
        if not all(isinstance(c, dict) and condition_matches(c, vtx_id=vtx_id, tags=tags, job_id=job_id) for c in all_conds):
            return False

    if has_any:
        any_conds = _listify(match.get("any"))
        if not any(isinstance(c, dict) and condition_matches(c, vtx_id=vtx_id, tags=tags, job_id=job_id) for c in any_conds):
            return False

    return True


def pick_policy(policies: List[Dict[str, Any]], *, vtx_id: str, tags: List[str], job_id: Optional[str]) -> Optional[Dict[str, Any]]:
    for pol in policies:
        if isinstance(pol, dict) and policy_matches(pol, vtx_id=vtx_id, tags=tags, job_id=job_id):
            return pol
    return None


# -----------------------------
# Source YAML parsing
# -----------------------------
def extract_source_meta(src: Dict[str, Any]) -> Tuple[str, List[str], List[Dict[str, Any]], List[str]]:
    """
    Returns:
      vtx_id, tags, jobs, consumers
    """
    vtx = src.get("_vtx") or {}
    vtx_id = str(vtx.get("id") or "").strip()
    tags = [str(t) for t in _listify(vtx.get("tags"))]
    consumers = [str(c) for c in _listify(vtx.get("consumers"))]

    payload = (((src.get("config") or {}).get("payload")) or {})
    jobs = _listify(payload.get("jobs"))

    jobs = [j for j in jobs if isinstance(j, dict)]
    return vtx_id, tags, jobs, consumers


def compute_output_path(job: Dict[str, Any]) -> Tuple[str, str, str]:
    """
    From job.output.dir + job.output.file:
      returns (dir, file, combined_rel_path)
    """
    out = job.get("output") or {}
    out_dir = str(out.get("dir") or "").strip()
    out_file = str(out.get("file") or "").strip()
    if not out_dir or not out_file:
        raise ValueError(f"Job missing output.dir or output.file: job.id={job.get('id')}")
    combined = str(Path(out_dir) / out_file).replace("\\", "/")
    return out_dir, out_file, combined


def normalize_snapshot_glob(pattern: str) -> str:
    """
    Convert snapshot patterns like:
      consolidated_application_view_%Y%m%d.csv
    into a watch glob like:
      consolidated_application_view_*.csv
    """
    p = str(pattern)
    # common date tokens -> wildcard
    for token in ("%Y%m%d", "%Y-%m-%d", "%Y_%m_%d"):
        p = p.replace(token, "*")
    return p


# -----------------------------
# Artifact generation
# -----------------------------
def ensure_list(d: Dict[str, Any], key: str) -> List[Any]:
    val = d.get(key)
    if val is None:
        d[key] = []
        return d[key]  # type: ignore
    if isinstance(val, list):
        return val
    d[key] = [val]
    return d[key]  # type: ignore


def _dedupe_paths(paths: List[Any]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for p in paths:
        s = str(p).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _extract_inputs_from_sources(sources: Any) -> List[str]:
    if sources is None:
        return []
    if isinstance(sources, list):
        return _dedupe_paths(sources)
    if isinstance(sources, str):
        return _dedupe_paths([sources])
    if not isinstance(sources, dict):
        return []

    out: List[Any] = []
    # Common path keys in sources dicts
    for key in (
        "input_table",
        "input_csv",
        "input",
        "path",
        "file",
        "program_attribute_mapping_yaml",
        "mapping_yaml",
        "mapping_path",
    ):
        if sources.get(key):
            out.append(sources.get(key))

    paths = sources.get("paths")
    if isinstance(paths, list):
        out.extend(paths)
    elif paths:
        out.append(paths)

    # Snapshot-style sources: use snapshots_dir if present
    if sources.get("snapshots_dir"):
        out.append(sources.get("snapshots_dir"))

    return _dedupe_paths(out)


def _extract_outputs_from_job(job: Dict[str, Any]) -> List[str]:
    raw_outputs = job.get("outputs")
    if raw_outputs:
        if isinstance(raw_outputs, list):
            return _dedupe_paths(raw_outputs)
        return _dedupe_paths([raw_outputs])

    output = job.get("output")
    if not output:
        return []
    if isinstance(output, list):
        return _dedupe_paths(output)
    if isinstance(output, str):
        return _dedupe_paths([output])
    if isinstance(output, dict):
        out_dir = str(output.get("dir") or "").strip()
        out_file = str(output.get("file") or "").strip()
        if out_dir and out_file:
            combined = str(Path(out_dir) / out_file).replace("\\", "/")
            return _dedupe_paths([combined])
        if output.get("path"):
            return _dedupe_paths([output.get("path")])
    return []


def build_producer_job(
    *,
    vtx_id: str,
    job: Dict[str, Any],
    consumer_script: str,
    merged: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Build a legacy-format orchestrator backbone job that runs the producer script.
    """
    job_id = str(job.get("id") or "").strip()
    if not job_id:
        raise ValueError("Job missing id")

    orch = ((merged.get("orchestrator") or {}).get("producer")) or {}
    mappings = merged.get("mappings") or {}

    producer_args_tmpl = str(mappings.get("producer_args_template") or '--job "{job_id}"')
    args = producer_args_tmpl.format(job_id=job_id, vtx_id=vtx_id)

    rule = str(orch.get("rule") or "mtime_input_newer_than_last_success_marker")
    marker_match = orch.get("marker_match")
    settle_seconds = orch.get("settle_seconds", 2)
    debounce_seconds = orch.get("debounce_seconds", 5)

    timeout_seconds = orch.get("timeout_seconds", 120)
    retries = orch.get("retries", 1)
    backoff_seconds = orch.get("backoff_seconds", 10)
    jitter_seconds = orch.get("jitter_seconds", 0)

    enabled = bool(job.get("enabled", True))
    role = str(orch.get("role") or "server")

    # Prefer snapshot-style watch if provided
    sources = job.get("sources") or {}
    watch_dirs = None
    watch_glob = None

    if isinstance(sources, dict) and sources.get("snapshots_dir") and sources.get("snapshots_pattern"):
        watch_dirs = [str(sources.get("snapshots_dir")).replace("\\", "/")]
        watch_glob = normalize_snapshot_glob(str(sources.get("snapshots_pattern")))
    else:
        # fallback: if job.inputs exists, use it (legacy orchestrator supports inputs in some VTX installs)
        inputs = _listify(job.get("inputs"))
        if inputs:
            # keep as VTX-relative strings if they are relative
            pass

    out_job: Dict[str, Any] = {
        "id": f"Autogen - {vtx_id} - {job_id}",
        "enabled": enabled,
        "role": role,
        "script": consumer_script,
        "args": args,
        "settle_seconds": settle_seconds,
        "debounce_seconds": debounce_seconds,
        "rule": rule,
        "timeout_seconds": timeout_seconds,
        "retries": retries,
        "backoff_seconds": backoff_seconds,
        "jitter_seconds": jitter_seconds,
    }

    if marker_match is not None:
        out_job["marker_match"] = marker_match

    # Add watch fields if present (matches your existing orchestrator schema)
    if watch_dirs is not None:
        out_job["watch_dirs"] = watch_dirs
        out_job["watch_glob"] = watch_glob
        # Many installs assume watch_recursive absent => false; keep explicit if provided
        if orch.get("watch_recursive") is not None:
            out_job["watch_recursive"] = bool(orch.get("watch_recursive"))

    if rule == "mtime_input_newer_than_last_success_marker":
        inputs = _extract_inputs_from_sources(job.get("sources"))
        if not inputs:
            inputs = _listify(job.get("inputs"))
        if not inputs:
            primary = job.get("primary_source")
            if primary:
                inputs = [primary]
        inputs = _dedupe_paths(inputs)
        if not inputs:
            LOG.warning(
                "Job uses rule mtime_input_newer_than_last_success_marker but has no sources/inputs/primary_source: %s/%s",
                vtx_id,
                job_id,
            )
        out_job["inputs"] = inputs

    outputs = _extract_outputs_from_job(job)
    if outputs:
        out_job["outputs"] = outputs

    return out_job

def build_producer_cron_job(
    *,
    vtx_id: str,
    job: Dict[str, Any],
    consumer_script: str,
    merged: Dict[str, Any],
) -> Dict[str, Any]:
    job_id = str(job.get("id") or "").strip()
    if not job_id:
        raise ValueError("Job missing id")

    orch = ((merged.get("orchestrator") or {}).get("producer")) or {}
    mappings = merged.get("mappings") or {}

    producer_args_tmpl = str(mappings.get("producer_args_template") or '--job "{job_id}"')
    args = producer_args_tmpl.format(job_id=job_id, vtx_id=vtx_id)

    schedule_cron = str(orch.get("schedule_cron") or "")
    if not schedule_cron:
        raise ValueError(f"producer.job_type=cron but no orchestrator.producer.schedule_cron provided for {vtx_id}/{job_id}")

    timeout_seconds = orch.get("timeout_seconds", 120)
    retries = orch.get("retries", 1)
    backoff_seconds = orch.get("backoff_seconds", 10)
    jitter_seconds = orch.get("jitter_seconds", 0)

    enabled = bool(job.get("enabled", True))
    role = str(orch.get("role") or "server")

    out_job = {
        "id": f"Autogen - {vtx_id} - {job_id}",
        "enabled": enabled,
        "role": role,
        "script": consumer_script,
        "args": args,
        "schedule": {"cron": schedule_cron},
        "timeout_seconds": timeout_seconds,
        "retries": retries,
        "backoff_seconds": backoff_seconds,
        "jitter_seconds": jitter_seconds,
    }

    outputs = _extract_outputs_from_job(job)
    if outputs:
        out_job["outputs"] = outputs

    return out_job

def build_stretch_entry(
    *,
    vtx_id: str,
    job: Dict[str, Any],
    merged: Dict[str, Any],
) -> Dict[str, Any]:
    stretch_cfg = merged.get("stretch") or {}
    mappings = merged.get("mappings") or {}

    job_id = str(job.get("id") or "").strip()
    _, out_file, out_path = compute_output_path(job)

    stretch_name_tmpl = str(mappings.get("stretch_name_template") or "{vtx_id}__{job_id}")
    name = stretch_name_tmpl.format(vtx_id=vtx_id, job_id=job_id)

    external_base_dir = str(stretch_cfg.get("external_base_dir") or "").rstrip("/").rstrip("\\")
    external_tmpl = str(mappings.get("stretch_external_file_template") or "{external_base_dir}/{output_file}")

    external_file = external_tmpl.format(
        external_base_dir=external_base_dir,
        output_file=out_file,
        output_path=out_path,
        vtx_id=vtx_id,
        job_id=job_id,
    ).replace("\\", "/")

    entry: Dict[str, Any] = {
        "name": name,
        "sync_type": str(stretch_cfg.get("sync_type") or "internal_to_external"),
        "csv_xlsx_convert": bool(stretch_cfg.get("csv_xlsx_convert", False)),
        "internal_file": out_path,
        "external_file": external_file,
        "sheet_name": str(stretch_cfg.get("sheet_name") or "Sheet1"),
        "key_columns": _listify(stretch_cfg.get("key_columns")) or [],
        "xlsx_truncate_extra_rows": bool(stretch_cfg.get("xlsx_truncate_extra_rows", True)),
        "xlsx_truncate_extra_cols": bool(stretch_cfg.get("xlsx_truncate_extra_cols", False)),
    }
    return entry


def build_stretch_trigger_job(
    *,
    stretch_entry: Dict[str, Any],
    merged: Dict[str, Any],
) -> Dict[str, Any]:
    orch = ((merged.get("orchestrator") or {}).get("stretch_trigger")) or {}

    name = str(stretch_entry.get("name"))
    sync_type = str(stretch_entry.get("sync_type"))
    internal_file = str(stretch_entry.get("internal_file"))
    external_file = str(stretch_entry.get("external_file"))

    script = str(orch.get("script") or "usr/scripts/default/stretch_sync.py")
    args_tmpl = str(orch.get("args_template") or '--name "{stretch_name}"')
    args = args_tmpl.format(stretch_name=name)

    rule = str(orch.get("rule") or "mtime_input_newer_than_last_success_marker")
    marker_match = orch.get("marker_match")
    settle_seconds = orch.get("settle_seconds", 2)
    debounce_seconds = orch.get("debounce_seconds", 5)

    timeout_seconds = orch.get("timeout_seconds", 300)
    retries = orch.get("retries", 1)
    backoff_seconds = orch.get("backoff_seconds", 10)
    jitter_seconds = orch.get("jitter_seconds", 0)

    role = str(orch.get("role") or "client")

    # Inputs vary by sync_type
    inputs: List[str] = []
    if sync_type == "two_way_sync":
        inputs = [internal_file, external_file]
    elif sync_type == "internal_to_external":
        inputs = [internal_file]
    elif sync_type == "external_to_internal":
        inputs = [external_file]
    else:
        inputs = [internal_file]

    out_job: Dict[str, Any] = {
        "id": f"Autogen - stretch_sync - {name}",
        "enabled": True,
        "role": role,
        "script": script,
        "args": args,
        "settle_seconds": settle_seconds,
        "debounce_seconds": debounce_seconds,
        "rule": rule,
        "timeout_seconds": timeout_seconds,
        "retries": retries,
        "backoff_seconds": backoff_seconds,
        "jitter_seconds": jitter_seconds,
        "inputs": inputs,
    }

    if marker_match is not None:
        out_job["marker_match"] = marker_match

    return out_job


def build_backup_job(
    *,
    vtx_id: str,
    job: Dict[str, Any],
    merged: Dict[str, Any],
) -> Dict[str, Any]:
    orch = ((merged.get("orchestrator") or {}).get("backup")) or {}
    bcfg = merged.get("backup") or {}

    job_id = str(job.get("id") or "").strip()
    _, _, out_path = compute_output_path(job)

    file_move_script = str(bcfg.get("file_move_script") or "usr/utils/file_move.py")
    dest_dir = str(bcfg.get("dest_dir") or "var/dailysnapshot")
    append_date = str(bcfg.get("append_date") or "yes")
    date_format = str(bcfg.get("date_format") or "%Y%m%d")
    skip_missing = str(bcfg.get("skip_missing") or "yes")

    schedule_cron = str(orch.get("schedule_cron") or "30 18 * * *")
    role = str(orch.get("role") or "server")

    # Keep args compatible with your existing file_move usage style
    args = (
        f'--source "{out_path}" '
        f'--dest "{dest_dir}" '
        f'--mode copy '
        f'--append-date {append_date} '
        f'--date-format {date_format} '
        f'--skip-missing {skip_missing}'
    )

    out_job: Dict[str, Any] = {
        "id": f"Autogen - backup - {vtx_id} - {job_id}",
        "enabled": True,
        "role": role,
        "script": file_move_script,
        "args": args,
        "schedule": {"cron": schedule_cron},
        "timeout_seconds": orch.get("timeout_seconds", 180),
        "retries": orch.get("retries", 1),
        "backoff_seconds": orch.get("backoff_seconds", 10),
        "jitter_seconds": orch.get("jitter_seconds", 0),
    }
    return out_job


def dedupe_by_id(jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for j in jobs:
        jid = str(j.get("id") or "")
        if not jid:
            continue
        if jid in seen:
            continue
        seen.add(jid)
        out.append(j)
    return out

def is_stretch_trigger_job(job: Dict[str, Any]) -> bool:
    """Return True if this orchestrator job is an autogen stretch_sync trigger job."""
    jid = str(job.get("id") or "")
    return jid.startswith("Autogen - stretch_sync -")

# -----------------------------
# File scanning
# -----------------------------
def iter_yaml_files(root: Path, *, excludes: List[str]) -> List[Path]:
    """
    Non-recursive scan of a directory for *.yaml/*.yml files.
    """
    out: List[Path] = []
    if not root.exists():
        return out

    for p in root.iterdir():
        if not p.is_file():
            continue
        if p.name in excludes:
            continue
        if p.suffix.lower() in (".yaml", ".yml"):
            out.append(p)
    return sorted(out)


def should_ignore_source(src: Dict[str, Any]) -> bool:
    """
    Ignore files that aren't in the universal schema-ish shape.
    """
    vtx = src.get("_vtx")
    cfg = src.get("config")
    if not isinstance(vtx, dict) or not isinstance(cfg, dict):
        return True
    payload = (cfg.get("payload") or {})
    if not isinstance(payload, dict):
        return True
    # Must at least have jobs to be useful for this autogen
    jobs = payload.get("jobs")
    if jobs is None:
        return True
    return False


# -----------------------------
# Main
# -----------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Autogen orchestrator/stretch configs from policy + source YAMLs.")
    ap.add_argument("--vtx-root", default=None, help="VTX root directory (overrides env VTX_ROOT/BTDM_ROOT).")
    ap.add_argument(
        "--policy",
        default="usr/config/default/workflow_autogen.yaml",
        help="Policy YAML path (VTX-relative or absolute).",
    )
    ap.add_argument(
        "--scan-dirs",
        default="usr/config/default,usr/config/custom",
        help="Comma-separated directories to scan (VTX-relative or absolute).",
    )
    ap.add_argument(
        "--exclude",
        default="orchestrator.yaml,stretch.yaml",
        help="Comma-separated filenames to exclude from scan.",
    )
    ap.add_argument(
        "--out-orchestrator",
        default="usr/config/auto_v2/orchestrator.yaml",
        help="Output orchestrator.yaml path (VTX-relative or absolute).",
    )
    ap.add_argument(
        "--out-stretch",
        default="usr/config/auto_v2/stretch.yaml",
        help="Output stretch.yaml path (VTX-relative or absolute).",
    )
    ap.add_argument("--dry-run", action="store_true", help="Do not write outputs; log what would be produced.")
    args = ap.parse_args()

    vtx_root = resolve_vtx_root(args.vtx_root)
    LOG.info("VTX_ROOT=%s", str(vtx_root))

    policy_path = vtx_path(vtx_root, args.policy)
    scan_dirs = [vtx_path(vtx_root, d.strip()) for d in str(args.scan_dirs).split(",") if d.strip()]
    excludes = [x.strip() for x in str(args.exclude).split(",") if x.strip()]
    out_orch_path = vtx_path(vtx_root, args.out_orchestrator)
    out_stretch_path = vtx_path(vtx_root, args.out_stretch)

    if not policy_path.exists():
        raise SystemExit(f"Policy file not found: {policy_path}")

    policy = yaml_load(policy_path)
    payload = ((policy.get("config") or {}).get("payload")) or {}

    defaults = payload.get("defaults") or {}
    mappings = payload.get("mappings") or {}
    policies = _listify(payload.get("policies")) or []

    if not isinstance(defaults, dict) or not isinstance(mappings, dict):
        raise SystemExit("Policy YAML missing config.payload.defaults or config.payload.mappings as dicts.")

    # base merged defaults: include mappings in merged for easy access
    base_merged = deep_merge(defaults, {"mappings": mappings})

    backbone_jobs: List[Dict[str, Any]] = []
    cron_jobs: List[Dict[str, Any]] = []
    stretches: List[Dict[str, Any]] = []

    for scan_dir in scan_dirs:
        files = iter_yaml_files(scan_dir, excludes=excludes)
        LOG.info("Scanning %s (%d yaml files)", str(scan_dir), len(files))

        for fp in files:
            try:
                src = yaml_load(fp)
            except Exception as e:
                LOG.warning("Skipping unreadable YAML: %s (%s)", str(fp), e)
                continue

            if should_ignore_source(src):
                continue

            try:
                vtx_id, tags, jobs, consumers = extract_source_meta(src)
            except Exception as e:
                LOG.warning("Skipping invalid source YAML: %s (%s)", str(fp), e)
                continue

            if not vtx_id:
                continue
            if not jobs:
                continue
            consumer_script = consumers[0] if consumers else ""
            if not consumer_script:
                LOG.warning("Source YAML has no _vtx.consumers[0]; cannot build producer: %s", str(fp))
                continue

            for job in jobs:
                job_id = str(job.get("id") or "").strip()
                if not job_id:
                    continue

                # Find first matching policy for this job
                pol = pick_policy(policies, vtx_id=vtx_id, tags=tags, job_id=job_id)
                if pol is None:
                    # Not matched => ignored (per your requirement)
                    continue

                apply_overrides = pol.get("apply") or {}
                if not isinstance(apply_overrides, dict):
                    apply_overrides = {}

                merged = deep_merge(base_merged, apply_overrides)

                # Producer job
                try:
                    orch_prod = ((merged.get("orchestrator") or {}).get("producer")) or {}
                    job_type = str(orch_prod.get("job_type") or "backbone").strip().lower()
                    
                    if job_type == "cron":
                        prod = build_producer_cron_job(vtx_id=vtx_id, job=job, consumer_script=consumer_script, merged=merged)
                        cron_jobs.append(prod)
                    else:
                        prod = build_producer_job(vtx_id=vtx_id, job=job, consumer_script=consumer_script, merged=merged)
                        backbone_jobs.append(prod)
                except Exception as e:
                    LOG.warning("Failed building producer job for %s/%s (%s)", vtx_id, job_id, e)
                    continue

                # Stretch (optional)
                stretch_cfg = merged.get("stretch") or {}
                stretch_enabled = bool(stretch_cfg.get("enabled", False))

                if stretch_enabled:
                    try:
                        s_entry = build_stretch_entry(vtx_id=vtx_id, job=job, merged=merged)
                        stretches.append(s_entry)

                        # Stretch trigger job (client)
                        orch_stretch = ((merged.get("orchestrator") or {}).get("stretch_trigger")) or {}
                        trigger_enabled = bool(orch_stretch.get("enabled", True))
                        if trigger_enabled:
                            backbone_jobs.append(build_stretch_trigger_job(stretch_entry=s_entry, merged=merged))
                    except Exception as e:
                        LOG.warning("Failed building stretch for %s/%s (%s)", vtx_id, job_id, e)

                # Backup (optional)
                bcfg = merged.get("backup") or {}
                backup_enabled = bool(bcfg.get("enabled", False))
                if backup_enabled:
                    try:
                        cron_jobs.append(build_backup_job(vtx_id=vtx_id, job=job, merged=merged))
                    except Exception as e:
                        LOG.warning("Failed building backup for %s/%s (%s)", vtx_id, job_id, e)

    # De-dupe for deterministic output
    backbone_jobs = dedupe_by_id(backbone_jobs)
    cron_jobs = dedupe_by_id(cron_jobs)

    # Comment out stretch_sync jobs in orchestrator.yaml for now (still generate stretch.yaml entries)
    active_backbone_jobs: List[Dict[str, Any]] = []
    commented_stretch_jobs: List[Dict[str, Any]] = []
    for j in backbone_jobs:
        if is_stretch_trigger_job(j):
            commented_stretch_jobs.append(j)
        else:
            active_backbone_jobs.append(j)

    # Emit empty lists ([]) instead of null so downstream compilers/merges
    # don't accidentally treat "null" as an explicit override.
    out_orch = {
        "backbone_jobs": active_backbone_jobs if active_backbone_jobs is not None else [],
        "cron_jobs": cron_jobs if cron_jobs is not None else [],
    }
    out_stretch = {
        "stretches": stretches if stretches is not None else [],
    }

    if args.dry_run:
        LOG.info("DRY RUN: would write orchestrator to %s", str(out_orch_path))
        LOG.info("DRY RUN: would write stretch to %s", str(out_stretch_path))
        LOG.info("Active backbone jobs (producer only): %d", len(active_backbone_jobs))
        LOG.info("Commented-out stretch_sync backbone jobs: %d", len(commented_stretch_jobs))
        LOG.info("Backup cron jobs: %d", len(cron_jobs))
        LOG.info("Stretch entries: %d", len(stretches))
        return 0

    yaml_dump_orchestrator_with_commented_stretch(out_orch_path, out_orch, commented_stretch_jobs)
    yaml_dump(out_stretch_path, out_stretch)

    LOG.info("Wrote orchestrator: %s", str(out_orch_path))
    LOG.info("Wrote stretch:      %s", str(out_stretch_path))
    LOG.info("Backbone jobs: %d | Cron jobs: %d | Stretches: %d", len(backbone_jobs), len(cron_jobs), len(stretches))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# --- END DEFAULT: C:\BTDM_7.1\usr\config\default\workflow_autogen.py ---
