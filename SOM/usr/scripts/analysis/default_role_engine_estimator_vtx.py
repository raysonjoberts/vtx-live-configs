#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
default_role_engine_estimator_vtx.py
------------------------------------
Estimate default role identities within comparison groups.

For each configured job:
  - load one CSV or Parquet source
  - evaluate required and optional key fields
  - build a comparison group from populated key values on the current row
  - calculate role identity metrics per anchor row
  - write one CSV output per job
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

try:
    import pandas as pd  # type: ignore
except Exception as e:
    raise SystemExit("Missing dependency: pandas. Install it in the VTX venv.") from e

try:
    import yaml  # type: ignore
except Exception as e:
    raise SystemExit("Missing dependency: pyyaml. Install it in the VTX venv.") from e


def resolve_vtx_root() -> Path:
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    here = Path(__file__).resolve()
    return here.parents[3]


VTX_ROOT = resolve_vtx_root()
DEFAULT_CONFIG_PATH = VTX_ROOT / "usr" / "config" / "run" / "default_role_engine_estimator_vtx.yaml"


def vtx_path(path_str: str | Path, *, must_exist: bool = False) -> Path:
    if isinstance(path_str, Path):
        path = path_str
    else:
        raw = str(path_str).strip()
        raw = os.path.expandvars(raw)
        raw = os.path.expanduser(raw)
        root = str(VTX_ROOT)
        raw = raw.replace("VTX_ROOT" + os.sep, root + os.sep)
        raw = raw.replace("BTDM_ROOT" + os.sep, root + os.sep)
        raw = raw.replace("VTX_ROOT/", root + "/")
        raw = raw.replace("BTDM_ROOT/", root + "/")
        path = Path(raw)

    if not path.is_absolute():
        path = (VTX_ROOT / path).resolve()
    else:
        path = path.resolve()

    if must_exist and not path.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")
    return path


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


logger = get_logger(component="default_role_engine_estimator_vtx")


@dataclass
class KeyFieldSpec:
    name: str
    field: str
    required: bool


@dataclass
class RoleFieldSpec:
    name: str
    field: str


@dataclass
class JobSpec:
    job_id: str
    enabled: bool
    source_path: Path
    output_path: Path
    anchor_field: str
    key_fields: List[KeyFieldSpec]
    role_fields: List[RoleFieldSpec]


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    text = str(value).strip()
    if text.lower() in {"", "nan", "none", "null"}:
        return ""
    return text


def normalize_identity(value: Any) -> str:
    return re.sub(r"\s+", " ", normalize_text(value))


def split_multi_value_cell(value: Any) -> List[str]:
    text = normalize_text(value)
    if not text:
        return []
    out: List[str] = []
    for part in text.split(";"):
        cleaned = normalize_identity(part)
        if cleaned:
            out.append(cleaned)
    return out


def sanitize_label(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", str(value).strip())
    safe = re.sub(r"_+", "_", safe).strip("_")
    return safe or "field"


def canonical_identity_key(value: str) -> str:
    return normalize_identity(value).casefold()


def load_yaml(path: Path) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    doc = yaml.safe_load(raw) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"Config root must be a mapping/dict: {path}")
    return doc


def parse_key_fields(raw_items: Any, *, label: str) -> List[KeyFieldSpec]:
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError(f"{label} must be a non-empty list")

    out: List[KeyFieldSpec] = []
    for idx, item in enumerate(raw_items, start=1):
        if isinstance(item, str):
            field = item.strip()
            name = field
            required = True
        elif isinstance(item, dict):
            field = str(item.get("field") or item.get("source") or item.get("column") or "").strip()
            name = str(item.get("name") or item.get("label") or field).strip()
            required = bool(item.get("required", False))
        else:
            raise ValueError(f"{label}[{idx}] must be a string or object")

        if not field:
            raise ValueError(f"{label}[{idx}] is missing field")
        if not name:
            raise ValueError(f"{label}[{idx}] is missing name")
        out.append(KeyFieldSpec(name=name, field=field, required=required))

    return out


def parse_role_fields(raw_items: Any, *, label: str) -> List[RoleFieldSpec]:
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError(f"{label} must be a non-empty list")

    out: List[RoleFieldSpec] = []
    for idx, item in enumerate(raw_items, start=1):
        if isinstance(item, str):
            field = item.strip()
            name = field
        elif isinstance(item, dict):
            field = str(item.get("field") or item.get("source") or item.get("column") or "").strip()
            name = str(item.get("name") or item.get("label") or field).strip()
        else:
            raise ValueError(f"{label}[{idx}] must be a string or object")

        if not field:
            raise ValueError(f"{label}[{idx}] is missing field")
        if not name:
            raise ValueError(f"{label}[{idx}] is missing name")
        out.append(RoleFieldSpec(name=name, field=field))

    return out


def parse_job_specs(config_path: Path) -> List[JobSpec]:
    doc = load_yaml(config_path)
    cfg = doc.get("config") if isinstance(doc.get("config"), dict) else doc
    payload = cfg.get("payload") if isinstance(cfg, dict) and isinstance(cfg.get("payload"), dict) else {}
    raw_jobs = payload.get("jobs")
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise ValueError(f"No jobs defined in {config_path}")

    jobs: List[JobSpec] = []
    for item in raw_jobs:
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("id") or "").strip()
        if not job_id:
            raise ValueError("Each job requires an id")

        source_raw = str(item.get("source") or "").strip()
        output_raw = str(item.get("output") or "").strip()
        if not source_raw:
            inputs = item.get("inputs")
            if isinstance(inputs, list) and inputs:
                source_raw = str(inputs[0]).strip()
            elif inputs:
                source_raw = str(inputs).strip()
        if not output_raw:
            outputs = item.get("outputs")
            if isinstance(outputs, list) and outputs:
                output_raw = str(outputs[0]).strip()
            elif outputs:
                output_raw = str(outputs).strip()

        anchor_field = str(item.get("anchor_field") or item.get("anchor") or "").strip()
        if not source_raw:
            raise ValueError(f"Job '{job_id}' missing source")
        if not output_raw:
            raise ValueError(f"Job '{job_id}' missing output")
        if not anchor_field:
            raise ValueError(f"Job '{job_id}' missing anchor_field")

        jobs.append(
            JobSpec(
                job_id=job_id,
                enabled=bool(item.get("enabled", True)),
                source_path=vtx_path(source_raw, must_exist=True),
                output_path=vtx_path(output_raw, must_exist=False),
                anchor_field=anchor_field,
                key_fields=parse_key_fields(item.get("key_fields"), label=f"job '{job_id}' key_fields"),
                role_fields=parse_role_fields(item.get("role_fields"), label=f"job '{job_id}' role_fields"),
            )
        )
    return jobs


def read_source_frame(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path, dtype=object, keep_default_na=False)
    if suffix in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    raise ValueError(f"Unsupported input format for {path}")


def choose_default_identity(counts: Dict[str, int], display_map: Dict[str, str]) -> str:
    if not counts:
        return ""
    best_key = sorted(counts.keys(), key=lambda key: (-counts[key], key, display_map.get(key, key)))[0]
    return display_map.get(best_key, best_key)


def build_role_group_stats(series: pd.Series, member_indices: List[int]) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    display_map: Dict[str, str] = {}
    sole_counts: Dict[str, int] = {}
    non_null_rows = 0

    values_cache = series.tolist()
    for idx in member_indices:
        identities = split_multi_value_cell(values_cache[idx])
        if not identities:
            continue
        non_null_rows += 1
        for identity in identities:
            key = canonical_identity_key(identity)
            counts[key] = counts.get(key, 0) + 1
            display_map.setdefault(key, identity)
        if len(identities) == 1:
            sole_key = canonical_identity_key(identities[0])
            sole_counts[sole_key] = sole_counts.get(sole_key, 0) + 1

    sample_size = len(member_indices)
    chosen_display = choose_default_identity(counts, display_map)
    chosen_key = canonical_identity_key(chosen_display) if chosen_display else ""
    chosen_occurrences = counts.get(chosen_key, 0)
    sole_matches = sole_counts.get(chosen_key, 0)

    return {
        "default_role_identity": chosen_display,
        "utilization_score": round(chosen_occurrences / non_null_rows, 6) if non_null_rows else None,
        "sample_size": sample_size if sample_size else None,
        "validity_score": round(non_null_rows / sample_size, 6) if sample_size else None,
        "default_role_sole_assigned": round(sole_matches / non_null_rows, 6) if non_null_rows else None,
    }


def ensure_columns_exist(df: pd.DataFrame, fields: Iterable[str], *, label: str) -> None:
    missing = [field for field in fields if field not in df.columns]
    if missing:
        raise ValueError(f"{label} missing columns: {missing}")


def calculate_presence_score(row: pd.Series, key_fields: List[KeyFieldSpec]) -> float:
    if not key_fields:
        return 0.0
    populated = sum(1 for spec in key_fields if normalize_text(row.get(spec.field)))
    return round(populated / len(key_fields), 6)


def build_group_members(df: pd.DataFrame, row: pd.Series, key_fields: List[KeyFieldSpec]) -> List[int]:
    populated_specs = [spec for spec in key_fields if normalize_text(row.get(spec.field))]
    if not populated_specs:
        return []

    mask = pd.Series(True, index=df.index)
    for spec in populated_specs:
        current_value = normalize_text(row.get(spec.field))
        mask &= df[spec.field].map(normalize_text).eq(current_value)
    return list(df.index[mask])


def build_blank_role_metrics() -> Dict[str, Any]:
    return {
        "default_role_identity": "",
        "utilization_score": None,
        "sample_size": None,
        "validity_score": None,
        "default_role_sole_assigned": None,
        "key_field_presence_score": None,
    }


def build_output_frame(job: JobSpec) -> pd.DataFrame:
    logger.info("job=%s source=%s", job.job_id, job.source_path)
    df = read_source_frame(job.source_path)
    ensure_columns_exist(df, [job.anchor_field], label=f"Job '{job.job_id}' anchor")
    ensure_columns_exist(df, [spec.field for spec in job.key_fields], label=f"Job '{job.job_id}' key_fields")

    valid_roles: List[RoleFieldSpec] = []
    for spec in job.role_fields:
        if spec.field not in df.columns:
            logger.warning(
                "job=%s skipping missing role field '%s' (label '%s')",
                job.job_id,
                spec.field,
                spec.name,
            )
            continue
        valid_roles.append(spec)

    output_rows: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        out_row: Dict[str, Any] = {"Anchor": normalize_text(row.get(job.anchor_field))}
        for idx, key_spec in enumerate(job.key_fields, start=1):
            out_row[f"key{idx}_{sanitize_label(key_spec.name)}"] = normalize_text(row.get(key_spec.field))

        required_missing = any(
            key_spec.required and not normalize_text(row.get(key_spec.field))
            for key_spec in job.key_fields
        )
        presence_score = calculate_presence_score(row, job.key_fields)
        member_indices = [] if required_missing else build_group_members(df, row, job.key_fields)

        for role_spec in valid_roles:
            suffix = sanitize_label(role_spec.name)
            out_row[f"actual_role_value_{suffix}"] = normalize_text(row.get(role_spec.field))

            if required_missing or not member_indices:
                metrics = build_blank_role_metrics()
            else:
                metrics = build_role_group_stats(df[role_spec.field], member_indices)
                metrics["key_field_presence_score"] = presence_score

            out_row[f"default_role_identity_{suffix}"] = metrics["default_role_identity"]
            out_row[f"utilization_score_{suffix}"] = metrics["utilization_score"]
            out_row[f"sample_size_{suffix}"] = metrics["sample_size"]
            out_row[f"validity_score_{suffix}"] = metrics["validity_score"]
            out_row[f"default_role_sole_assigned_{suffix}"] = metrics["default_role_sole_assigned"]
            out_row[f"key_field_presence_score_{suffix}"] = metrics["key_field_presence_score"]

        output_rows.append(out_row)

    return pd.DataFrame(output_rows)


def write_output_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def run_job(job: JobSpec) -> Path:
    output_df = build_output_frame(job)
    write_output_csv(output_df, job.output_path)
    logger.info("job=%s rows=%d output=%s", job.job_id, len(output_df), job.output_path)
    return job.output_path


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Default role engine estimator")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--job", default=None, help="Run only the specified job id")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    config_path = vtx_path(args.config, must_exist=True)
    logger.info("VTX_ROOT=%s", VTX_ROOT)
    logger.info("Config=%s", config_path)
    jobs = parse_job_specs(config_path)
    if args.job:
        jobs = [job for job in jobs if job.job_id == args.job]
        if not jobs:
            raise ValueError(f"No job found with id '{args.job}'")

    ran = 0
    for job in jobs:
        if not job.enabled:
            logger.info("job=%s skipped (disabled)", job.job_id)
            continue
        run_job(job)
        ran += 1

    if ran == 0:
        logger.warning("no_enabled_jobs_ran")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
