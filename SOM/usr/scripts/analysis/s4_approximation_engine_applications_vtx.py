#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
approximation_engine_applications_vtx.py
----------------------------------------
Purpose:
  Approximate application attributes using similarity across configured
  comparison fields. One row per anchor value.

Inputs:
  - app_parquet (application data)
  - YAML config (targets + comparison fields)

Outputs:
  - VTX/var/masterdata/application_approximation.parquet
  - VTX/var/masterdata/application_approximation.csv
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

try:
    import yaml  # type: ignore
except Exception as e:
    raise SystemExit("Missing dependency: pyyaml. Install it in the VTX venv.") from e

try:
    import pandas as pd  # type: ignore
except Exception as e:
    raise SystemExit("Missing dependency: pandas. Install it in the VTX venv.") from e


# ---------------------------------------------------------------------
# Globals / Paths (VTX style)
# ---------------------------------------------------------------------

def resolve_vtx_root() -> Path:
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    here = Path(__file__).resolve()
    inferred = here.parents[3]  # analysis/<script>.py -> scripts -> usr -> VTX_ROOT
    return inferred


VTX_ROOT = resolve_vtx_root()


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


# ---------------------------------------------------------------------
# Logging (VTX style)
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


logger = get_logger(component="approximation_engine_applications_vtx")


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def normalize_text(val: Any) -> str:
    if val is None:
        return ""
    s = str(val).strip()
    if s.lower() in {"nan", "none", "null"}:
        return ""
    return s


def normalize_tokens(val: Any) -> Set[str]:
    text = normalize_text(val)
    if not text:
        return set()
    parts = [p.strip() for p in text.split(";") if p.strip()]
    if len(parts) > 1:
        return {p.lower() for p in parts}
    return set(TOKEN_RE.findall(text.lower()))


def score_mapping_row(row: Dict[str, Any]) -> Tuple[float, float, int]:
    vmr = float(row.get("Value Match Ratio") or 0)
    used = float(row.get("Used %") or 0)
    flags = row.get("Flags") or ""
    flags_n = len([p for p in str(flags).split(",") if p.strip()])
    return (vmr, used, -flags_n)


def compute_similarity(
    row_tokens: Dict[str, Set[str]],
    other_tokens: Dict[str, Set[str]],
    weights: Dict[str, float],
) -> float:
    total = 0.0
    max_total = 0.0
    for field, weight in weights.items():
        a = row_tokens.get(field, set())
        b = other_tokens.get(field, set())
        if not a or not b:
            continue
        max_total += weight
        if a.intersection(b):
            total += weight
    if max_total == 0:
        return 0.0
    return total / max_total


def predict_from_neighbors(
    similarities: List[Tuple[int, float]],
    actual_values: List[Set[str]],
    top_k: int,
) -> Tuple[str, float]:
    if not similarities:
        return ("", 0.0)
    top = sorted(similarities, key=lambda x: x[1], reverse=True)[:top_k]
    votes: Dict[str, float] = {}
    for idx, sim in top:
        if sim <= 0:
            continue
        vals = actual_values[idx]
        for v in vals:
            votes[v] = votes.get(v, 0.0) + sim
    if not votes:
        return ("", 0.0)
    sorted_votes = sorted(votes.items(), key=lambda x: x[1], reverse=True)
    top_val, top_score = sorted_votes[0]
    total = sum(v for _, v in sorted_votes)
    confidence = top_score / total if total else 0.0
    return (top_val, confidence)


# ---------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------

def load_yaml(path: Path) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    doc = yaml.safe_load(raw) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"Config root must be a mapping/dict: {path}")
    return doc


@dataclass
class Options:
    config_path: Optional[Path]
    app_parquet: Path
    out_parquet: Path
    out_csv: Path
    top_k: int
    min_peer_group_size: int
    anchor_field: str
    title_field: str
    targets: List[Dict[str, Any]]


def _load_runtime_config(path: Path) -> Dict[str, Any]:
    doc = load_yaml(path)
    cfg = doc.get("config") if isinstance(doc.get("config"), dict) else doc
    if not isinstance(cfg, dict):
        return {}
    payload = cfg.get("payload") if isinstance(cfg.get("payload"), dict) else {}
    return payload if isinstance(payload, dict) else {}


def parse_args(argv: Optional[Sequence[str]] = None) -> Options:
    p = argparse.ArgumentParser(description="Approximation engine (applications)")
    p.add_argument("--config", default="usr/config/run/approximation_engine_applications_vtx.yaml")
    p.add_argument("--app-parquet", default="var/masterdata/raw_application_table.parquet")
    p.add_argument("--out-parquet", default="var/masterdata/application_approximation.parquet")
    p.add_argument("--out-csv", default="var/masterdata/application_approximation.csv")
    p.add_argument("--top-k", default=50, type=int)
    p.add_argument("--min-peer-group-size", default=10, type=int)
    args = p.parse_args(argv)

    cfg_path = vtx_path(args.config, must_exist=False)
    payload = _load_runtime_config(cfg_path) if cfg_path.exists() else {}

    app_raw = str(payload.get("app_parquet") or args.app_parquet)
    out_parquet_raw = str(payload.get("out_parquet") or args.out_parquet)
    out_csv_raw = str(payload.get("out_csv") or args.out_csv)
    top_k = int(payload.get("top_k") or args.top_k)
    min_peer = int(payload.get("min_peer_group_size") or args.min_peer_group_size)
    anchor_field = str(payload.get("anchor_field") or "ID").strip()
    title_field = str(payload.get("title_field") or "TITLE_NAME").strip()
    targets = payload.get("targets") if isinstance(payload.get("targets"), list) else []

    return Options(
        config_path=cfg_path if cfg_path.exists() else None,
        app_parquet=vtx_path(app_raw, must_exist=True),
        out_parquet=vtx_path(out_parquet_raw, must_exist=False),
        out_csv=vtx_path(out_csv_raw, must_exist=False),
        top_k=top_k,
        min_peer_group_size=min_peer,
        anchor_field=anchor_field,
        title_field=title_field,
        targets=[t for t in targets if isinstance(t, dict)],
    )


# ---------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    opt = parse_args(argv)
    logger.info("VTX_ROOT=%s", VTX_ROOT)
    if opt.config_path:
        logger.info("Config=%s", opt.config_path)
    logger.info("AppParquet=%s", opt.app_parquet)

    if not opt.targets:
        logger.warning("no_targets_configured")
        return 0

    app_df = pd.read_parquet(opt.app_parquet)
    if opt.anchor_field not in app_df.columns:
        raise ValueError(f"anchor_field not found in app parquet: {opt.anchor_field}")
    if opt.title_field not in app_df.columns:
        raise ValueError(f"title_field not found in app parquet: {opt.title_field}")

    app_df[opt.anchor_field] = app_df[opt.anchor_field].apply(normalize_text)
    app_df[opt.title_field] = app_df[opt.title_field].apply(normalize_text)

    before_rows = len(app_df)
    app_df = app_df[app_df[opt.anchor_field].astype(str).str.strip() != ""].copy()
    if len(app_df) != before_rows:
        logger.warning("rows_with_blank_anchor_removed,count=%d", before_rows - len(app_df))

    dupe_count = app_df[opt.anchor_field].duplicated(keep="first").sum()
    if dupe_count:
        logger.warning("duplicate_anchor_rows_dropped,count=%d,anchor=%s", dupe_count, opt.anchor_field)
        app_df = app_df.drop_duplicates(subset=[opt.anchor_field], keep="first").copy()

    id_col = opt.anchor_field
    title_col = opt.title_field
    active_col = next((c for c in ["ACTIVE", "Active", "active"] if c in app_df.columns), None)

    base_rows: List[Dict[str, Any]] = []
    for i in range(len(app_df)):
        base_rows.append(
            {
                "ID": app_df.at[i, id_col],
                "TITLE_NAME": app_df.at[i, title_col],
                "ACTIVE": app_df.at[i, active_col] if active_col else "",
            }
        )

    for target in opt.targets:
        name = str(target.get("name") or target.get("field") or "").strip()
        field = str(target.get("field") or "").strip()
        compare_fields = target.get("compare_fields") if isinstance(target.get("compare_fields"), list) else []
        compare_fields = [str(f).strip() for f in compare_fields if str(f).strip()]

        if not field:
            logger.info("target_missing_field,name=%s", name)
            continue
        if field not in app_df.columns:
            logger.info("target_field_missing,field=%s", field)
            continue
        if not compare_fields:
            logger.info("no_compare_fields,field=%s", field)
            continue

        signal_fields: List[str] = []
        for f in compare_fields:
            if f in app_df.columns and f not in signal_fields:
                signal_fields.append(f)

        if not signal_fields:
            logger.info("no_signal_fields,field=%s", field)
            continue

        signal_tokens: Dict[str, List[Set[str]]] = {}
        weights: Dict[str, float] = {}
        for sig in signal_fields:
            series = app_df[sig].apply(normalize_tokens).tolist()
            signal_tokens[sig] = series
            distinct = app_df[sig].apply(lambda v: normalize_text(v).lower()).nunique()
            weights[sig] = 1.0 / math.log(2 + max(distinct, 1))

        actual_vals_raw = app_df[field].apply(normalize_text).tolist()
        actual_vals = [set([v.strip() for v in val.split(";") if v.strip()]) if val else set() for val in actual_vals_raw]

        labeled_idx = [i for i, v in enumerate(actual_vals) if v]
        if len(labeled_idx) < opt.min_peer_group_size:
            logger.info("insufficient_peers,field=%s,count=%s", field, len(labeled_idx))
            continue

        token_maps = []
        for i in range(len(app_df)):
            token_maps.append({f: signal_tokens[f][i] for f in signal_fields})

        correct = 0
        scored = 0

        for i in range(len(app_df)):
            actual_set = actual_vals[i]
            sims: List[Tuple[int, float]] = []
            for j in labeled_idx:
                if i == j:
                    continue
                sim = compute_similarity(token_maps[i], token_maps[j], weights)
                if sim > 0:
                    sims.append((j, sim))
            pred, conf = predict_from_neighbors(sims, actual_vals, opt.top_k)

            actual_value = "; ".join(sorted(actual_set)) if actual_set else ""
            approx_correct = ""
            if actual_set:
                scored += 1
                approx_correct = pred in actual_set if pred else False
                if approx_correct:
                    correct += 1

            safe_name = re.sub(r"[^A-Za-z0-9_]+", "_", field).strip("_")
            base_rows[i][f"ActualValue::{safe_name}"] = actual_value
            base_rows[i][f"ApproxValue::{safe_name}"] = pred
            base_rows[i][f"ApproxScore::{safe_name}"] = f"{round(conf * 100):.0f}%"
            base_rows[i][f"ApproxCorrect::{safe_name}"] = (
                "" if approx_correct == "" else str(bool(approx_correct))
            )

        accuracy = round((correct / scored) * 100) if scored else 0
        for i in range(len(app_df)):
            safe_name = re.sub(r"[^A-Za-z0-9_]+", "_", field).strip("_")
            base_rows[i][f"ApproxAccuracy::{safe_name}"] = f"{accuracy}%"

    detailed_df = pd.DataFrame(base_rows)
    opt.out_parquet.parent.mkdir(parents=True, exist_ok=True)
    detailed_df.to_parquet(opt.out_parquet, index=False)
    logger.info("wrote_parquet,path=%s,rows=%s", opt.out_parquet, len(detailed_df))

    detailed_df.to_csv(opt.out_csv, index=False)
    logger.info("wrote_csv,path=%s,rows=%s", opt.out_csv, len(detailed_df))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
