#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
data_source_analysis_vtx.py
---------------------------
Purpose:
  Analyze one or more CSV/Parquet sources per configured job, then generate:
    1) analysis CSV
    2) HTML visualization

Notes:
  - This is a unified replacement flow for:
      - data_source_analysis.py
      - dashboards/visual_data_source_analysis_vtx.py
  - Job config is YAML-based (no .conf required).
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import re
import sys
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import yaml


def resolve_vtx_root() -> Path:
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    here = Path(__file__).resolve()
    return here.parents[3]


VTX_ROOT = resolve_vtx_root()
DEFAULT_CONFIG_PATH = VTX_ROOT / "usr" / "config" / "run" / "data_source_analysis_reporting.yaml"
DEFAULT_DICTIONARY_YAML = VTX_ROOT / "usr" / "config" / "run" / "data_source_analysis.yaml"


def vtx_path(path_str: str | Path, *, must_exist: bool = False) -> Path:
    if isinstance(path_str, Path):
        p = path_str
    else:
        s = os.path.expandvars(os.path.expanduser(str(path_str).strip()))
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


def get_logger(component: str) -> logging.Logger:
    lib_dir = VTX_ROOT / "usr" / "lib"
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))

    for mod_name in ("vtx_logging", "btdm_logging"):
        try:
            mod = __import__(mod_name)  # type: ignore
            if hasattr(mod, "get_logger"):
                return mod.get_logger(component=component)  # type: ignore
        except Exception:
            pass

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    return logging.getLogger(component)


logger = get_logger("data_source_analysis_vtx")

# Technical metadata columns emitted by parquet_table_builder_* long parquet artifacts.
# These are structural fields, not business/source fields for attribute analysis.
PARQUET_TECHNICAL_FIELDS = {
    "anchor_id",
    "field_name",
    "field_value",
    "source_table",
    "tx_index",
    "value_index",
}
MULTIVALUE_SPLIT_RE = re.compile(r"\s*;\s*|\s*\|\s*")
MATCH_TIE_EPSILON = 0.01


def split_multivalue_tokens(value: str) -> List[str]:
    s = str(value).strip()
    if not s:
        return []
    parts = [p.strip() for p in MULTIVALUE_SPLIT_RE.split(s) if p.strip()]
    return parts if parts else [s]


def _match_ratio(result: Dict[str, Any]) -> float:
    val = result.get("Value Match Ratio")
    try:
        return float(val) if val is not None else 0.0
    except Exception:
        return 0.0


def _category_rank(attr: Dict[str, Any], result: Dict[str, Any]) -> int:
    category = str(attr.get("category", "") or "").strip().lower()
    attribute_name = str(result.get("Attribute", "") or "")
    if category == "program":
        return 2
    if category == "general" or attribute_name.startswith("General "):
        return 1
    return 0


def _strictness_score(attr: Dict[str, Any]) -> int:
    score = 0
    reqs = {str(r).strip().lower() for r in coerce_list(attr.get("match_requirements"))}
    if "name" in reqs and "value" in reqs:
        score += 2
    elif "name" in reqs or "value" in reqs:
        score += 1

    if isinstance(attr.get("lookup_table"), dict):
        score += 3

    for key in (
        "min_unique_ratio",
        "max_unique_ratio",
        "min_match_ratio",
        "min_viable_ratio",
        "min_length",
        "max_length",
    ):
        if attr.get(key) is not None:
            score += 1

    if attr.get("field_name_patterns"):
        score += 1

    return score


def _should_replace_best(
    *,
    best_result: Optional[Dict[str, Any]],
    best_attr: Optional[Dict[str, Any]],
    candidate_result: Dict[str, Any],
    candidate_attr: Dict[str, Any],
) -> bool:
    if best_result is None or best_attr is None:
        return True

    best_ratio = _match_ratio(best_result)
    cand_ratio = _match_ratio(candidate_result)

    if cand_ratio > best_ratio + MATCH_TIE_EPSILON:
        return True
    if best_ratio > cand_ratio + MATCH_TIE_EPSILON:
        return False

    best_cat = _category_rank(best_attr, best_result)
    cand_cat = _category_rank(candidate_attr, candidate_result)
    if cand_cat != best_cat:
        return cand_cat > best_cat

    best_strict = _strictness_score(best_attr)
    cand_strict = _strictness_score(candidate_attr)
    if cand_strict != best_strict:
        return cand_strict > best_strict

    # Stable dictionary-order fallback: keep the first match encountered.
    return False


def load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Config root must be a dict: {path}")
    if "config" in raw and isinstance(raw.get("config"), dict):
        cfg = raw["config"]
    else:
        cfg = raw
    return cfg


def coerce_list(v: Any) -> List[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def extract_jobs(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    payload = cfg.get("payload", {}) if isinstance(cfg.get("payload"), dict) else {}
    jobs = payload.get("jobs")
    return [j for j in coerce_list(jobs) if isinstance(j, dict)]


def job_id(job: Dict[str, Any]) -> str:
    return str(job.get("id") or "").strip()


def select_jobs(jobs: List[Dict[str, Any]], wanted: Optional[str]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    if not wanted:
        return jobs, None
    exact = [j for j in jobs if job_id(j) == wanted]
    if exact:
        return exact, None
    lower = wanted.lower()
    ci = [j for j in jobs if job_id(j).lower() == lower]
    if ci:
        return ci, None
    available = ", ".join([job_id(j) for j in jobs if job_id(j)]) or "<none>"
    return [], f"job_not_found,wanted={wanted},available={available}"


def safe_mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def output_is_fresh(output_path: Path, source_paths: List[Path]) -> bool:
    out_mtime = safe_mtime(output_path)
    if out_mtime is None:
        return False
    any_source = False
    for src in source_paths:
        src_mtime = safe_mtime(src)
        if src_mtime is None:
            continue
        any_source = True
        if src_mtime > out_mtime:
            return False
    return any_source


def load_attributes(dictionary_yaml: Path) -> List[Dict[str, Any]]:
    try:
        doc = yaml.safe_load(dictionary_yaml.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("dictionary_yaml_load_failed,path=%s,error=%s", dictionary_yaml, exc)
        return []
    if isinstance(doc, dict):
        attrs = doc.get("criteria", [])
    else:
        attrs = []
    return [a for a in coerce_list(attrs) if isinstance(a, dict)]


def compute_field_metrics(series: pd.Series) -> Tuple[pd.Series, int, float, float, int]:
    cleaned = series.dropna().astype(str).map(lambda x: x.strip())
    # Parquet wide tables often represent missing values as empty strings (not NaN).
    # Normalize those placeholders so CSV and Parquet score consistently.
    non_null = cleaned[~cleaned.str.lower().isin({"", "nan", "none", "null"})]
    total = len(series)
    used_pct = len(non_null) / total if total > 0 else 0.0
    unique_non_null = non_null.nunique(dropna=True)
    unique_value_pct = unique_non_null / len(non_null) if len(non_null) > 0 else 0.0
    unique_count = unique_non_null
    return non_null, total, used_pct, unique_value_pct, unique_count


def text_length_stats(series: pd.Series) -> Dict[str, Any]:
    lengths = series.dropna().astype(str).str.len()
    if lengths.empty:
        return {}
    return {
        "Minimum Length": int(lengths.min()),
        "Maximum Length": int(lengths.max()),
        "Average Length": round(float(lengths.mean()), 2),
        "Length stddev": round(float(lengths.std(ddof=0)), 2),
    }


def load_lookup_table(attr: Dict[str, Any]) -> Optional[set[str]]:
    lookup = attr.get("lookup_table")
    if not isinstance(lookup, dict):
        return None
    file_path = lookup.get("file")
    field = lookup.get("field")
    if not file_path or not field:
        return None
    try:
        p = vtx_path(str(file_path), must_exist=True)
        df = pd.read_csv(p)
        return set(df[str(field)].dropna().astype(str).str.upper())
    except Exception:
        return None


def evaluate_field(field_name: str, series: pd.Series, attr: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    non_null, total, used_pct, unique_value_pct, unique_count = compute_field_metrics(series)
    if total == 0:
        return None

    min_viable_ratio = attr.get("min_viable_ratio")
    if min_viable_ratio and used_pct < float(min_viable_ratio):
        return None

    reqs = coerce_list(attr.get("match_requirements", ["value"]))
    reqs = [str(r) for r in reqs]

    flags: List[str] = []
    if used_pct <= 0.20:
        flags.append("Low Utilization")
    if attr.get("attribute") == "General Unknown/Custom/Other fields":
        flags.append("High Generic Values")
    non_null_count = len(non_null)
    if non_null_count > 0 and unique_count > non_null_count * 0.33:
        flags.append("High Unique Values Warning")

    if "name" in reqs:
        patterns = attr.get("field_name_patterns", {})
        like_patterns = []
        not_like_patterns = []
        if isinstance(patterns, dict):
            like_patterns = coerce_list(patterns.get("like"))
            not_like_patterns = coerce_list(patterns.get("not_like"))
        else:
            like_patterns = coerce_list(patterns)

        field_lower = field_name.lower()
        name_match = any(str(p).lower() in field_lower for p in like_patterns) if like_patterns else False
        if any(str(p).lower() in field_lower for p in not_like_patterns):
            name_match = False
        if not name_match:
            return None

    match_ratio: Optional[float] = None
    if "value" in reqs:
        values = non_null.astype(str)
        candidate_mask = pd.Series(False, index=values.index)
        for heuristic in coerce_list(attr.get("value_heuristics")):
            if not isinstance(heuristic, dict):
                continue
            pattern = heuristic.get("pattern")
            if not pattern:
                continue
            try:
                regex = re.compile(str(pattern), re.IGNORECASE)
                # Support multi-value parquet cells (e.g., "a;b;c") by matching any token.
                this_mask = values.apply(
                    lambda x: any(bool(regex.match(tok)) for tok in split_multivalue_tokens(str(x)))
                )
                candidate_mask |= this_mask
            except Exception:
                continue
        if not candidate_mask.any():
            return None

        candidates = values[candidate_mask]
        lookup_values = load_lookup_table(attr)
        if lookup_values:
            def candidate_in_lookup(val: str) -> bool:
                all_tokens: List[str] = []
                for mv in split_multivalue_tokens(str(val)):
                    all_tokens.extend([t for t in re.split(r"[^A-Za-z]+", mv.upper()) if t])
                return any(t in lookup_values for t in all_tokens)

            candidates = candidates[candidates.apply(candidate_in_lookup)]
            if len(candidates) == 0:
                return None

        match_ratio = len(candidates) / len(values) if len(values) else 0.0
        unique_ratio = candidates.nunique() / len(candidates) if len(candidates) else 0.0
        min_match_ratio = attr.get("min_match_ratio")
        if min_match_ratio and match_ratio < float(min_match_ratio):
            return None
        min_unique_ratio = attr.get("min_unique_ratio")
        if min_unique_ratio and unique_ratio < float(min_unique_ratio):
            return None
        max_unique_ratio = attr.get("max_unique_ratio")
        if max_unique_ratio and unique_ratio > float(max_unique_ratio):
            return None

    category_raw = str(attr.get("category", "") or "")
    category = category_raw[:1].upper() + category_raw[1:] if category_raw else ""
    len_stats = text_length_stats(non_null)

    return {
        "Field Name": field_name,
        "Attribute": str(attr.get("attribute", "No Match")),
        "Category": category,
        "Total Rows": total,
        "Used %": round(used_pct, 2),
        "Unique Value %": round(unique_value_pct, 3),
        "Unique Count": int(unique_count),
        "Flags": ", ".join(flags),
        "Value Match Ratio": round(match_ratio, 3) if match_ratio is not None else None,
        **len_stats,
    }


def parse_filters(filter_cfg: Any) -> List[Tuple[str, str, str]]:
    clauses: List[Tuple[str, str, str]] = []
    if filter_cfg is None:
        return clauses

    def parse_clause_text(clause: str) -> Optional[Tuple[str, str, str]]:
        s = clause.strip()
        if not s:
            return None
        # VTX op format: "<field> -oper- <value>"
        # Example: "ACTIVE -eq- Y"
        m = re.match(r"^\s*(.+?)\s*-\s*([A-Za-z_]+)\s*-\s*(.+?)\s*$", s)
        if m:
            field = m.group(1).strip()
            oper = m.group(2).strip().lower()
            value = m.group(3).strip()
            return (field, oper, value)
        # Backward-compatible format: "field=value"
        if "=" in s:
            k, v = s.split("=", 1)
            return (k.strip(), "eq", v.strip())
        return None

    if isinstance(filter_cfg, str):
        parts = [p.strip() for p in filter_cfg.split(",") if p.strip()]
        for part in parts:
            parsed = parse_clause_text(part)
            if parsed:
                clauses.append(parsed)
        return clauses

    if isinstance(filter_cfg, list):
        for item in filter_cfg:
            if isinstance(item, str):
                parsed = parse_clause_text(item)
                if parsed:
                    clauses.append(parsed)
            elif isinstance(item, dict):
                for k, v in item.items():
                    clauses.append((str(k).strip(), "eq", str(v).strip()))
        return clauses

    if isinstance(filter_cfg, dict):
        for k, v in filter_cfg.items():
            clauses.append((str(k).strip(), "eq", str(v).strip()))
    return clauses


def apply_filters(df: pd.DataFrame, filters_cfg: Any) -> pd.DataFrame:
    clauses = parse_filters(filters_cfg)
    for key, oper, val in clauses:
        if key not in df.columns:
            continue
        series = df[key]
        s = series.astype(str)
        su = s.str.upper()
        vu = val.upper()
        if oper in ("eq", "=", "is"):
            mask = su == vu
        elif oper in ("ne", "!=", "not"):
            mask = su != vu
        elif oper in ("contains", "like"):
            mask = s.str.contains(val, case=False, na=False, regex=False)
        elif oper in ("not_contains", "not_like"):
            mask = ~s.str.contains(val, case=False, na=False, regex=False)
        elif oper in ("startswith", "starts_with"):
            mask = s.str.startswith(val, na=False)
        elif oper in ("endswith", "ends_with"):
            mask = s.str.endswith(val, na=False)
        elif oper in ("regex", "matches"):
            mask = s.str.contains(val, case=False, na=False, regex=True)
        elif oper in ("gt", "gte", "lt", "lte"):
            nums = pd.to_numeric(series, errors="coerce")
            try:
                cmp_val = float(val)
            except Exception:
                continue
            if oper == "gt":
                mask = nums > cmp_val
            elif oper == "gte":
                mask = nums >= cmp_val
            elif oper == "lt":
                mask = nums < cmp_val
            else:
                mask = nums <= cmp_val
        elif oper in ("in", "not_in"):
            raw_items = [x.strip() for x in str(val).split("|") if x.strip()]
            set_u = {x.upper() for x in raw_items}
            mask = su.isin(set_u)
            if oper == "not_in":
                mask = ~mask
        else:
            continue
        df = df[mask]
    return df


def expand_source_paths(pattern: str) -> List[Path]:
    p = vtx_path(pattern)
    has_glob = any(ch in str(pattern) for ch in ("*", "?", "["))
    if has_glob:
        parent = p.parent
        if not parent.exists():
            return []
        return sorted([x for x in parent.glob(p.name) if x.is_file()])
    return [p] if p.is_file() else []


def read_source_table(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path)
    if suffix in (".parquet", ".pq"):
        return pd.read_parquet(path)
    raise ValueError(f"Unsupported source type: {path}")


def iter_source_defs(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Normalize supported source schemas into a list of:
      {"path": <str>, "filters": <any>}
    Supported job["sources"] shapes:
      1) list[ {path, filters} ]
      2) dict: { paths: [...], filters: ... }
      3) dict: { path: "...", filters: ... }
    """
    raw = job.get("sources")
    out: List[Dict[str, Any]] = []

    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("path"):
                out.append({"path": str(item.get("path")), "filters": item.get("filters")})
        return out

    if isinstance(raw, dict):
        filters = raw.get("filters")
        if raw.get("path"):
            out.append({"path": str(raw.get("path")), "filters": filters})
        for p in coerce_list(raw.get("paths")):
            if p:
                out.append({"path": str(p), "filters": filters})
    return out


def analyze_job(job: Dict[str, Any]) -> Optional[pd.DataFrame]:
    jid = job_id(job) or "job"
    dictionary_yaml = vtx_path(job.get("dictionary_yaml") or DEFAULT_DICTIONARY_YAML, must_exist=True)
    attrs = load_attributes(dictionary_yaml)
    priority_attrs = [a for a in attrs if a.get("priority")]
    non_priority_attrs = [a for a in attrs if not a.get("priority")]

    rows: List[Dict[str, Any]] = []
    for src in iter_source_defs(job):
        src_path = str(src.get("path") or "").strip()
        if not src_path:
            continue
        filters = src.get("filters")
        matched_files = expand_source_paths(src_path)
        for file_path in matched_files:
            try:
                df = read_source_table(file_path)
            except Exception as exc:
                logger.warning("source_load_failed,job=%s,path=%s,error=%s", jid, file_path, exc)
                continue

            df = apply_filters(df, filters)
            matched_fields: set[str] = set()

            for col in df.columns:
                if str(col) in PARQUET_TECHNICAL_FIELDS:
                    continue
                best_match = None
                best_attr = None
                for attr in priority_attrs:
                    result = evaluate_field(col, df[col], attr)
                    if result and _should_replace_best(
                        best_result=best_match,
                        best_attr=best_attr,
                        candidate_result=result,
                        candidate_attr=attr,
                    ):
                        best_match = result
                        best_attr = attr
                if best_match:
                    best_match["Data Source"] = file_path.name
                    rows.append(best_match)
                    matched_fields.add(col)

            for col in df.columns:
                if str(col) in PARQUET_TECHNICAL_FIELDS:
                    continue
                if col in matched_fields:
                    continue
                best_match = None
                best_attr = None
                for attr in non_priority_attrs:
                    result = evaluate_field(col, df[col], attr)
                    if result and _should_replace_best(
                        best_result=best_match,
                        best_attr=best_attr,
                        candidate_result=result,
                        candidate_attr=attr,
                    ):
                        best_match = result
                        best_attr = attr
                if best_match:
                    best_match["Data Source"] = file_path.name
                    rows.append(best_match)
                else:
                    non_null, total, used_pct, unique_value_pct, unique_count = compute_field_metrics(df[col])
                    len_stats = text_length_stats(non_null)
                    flags: List[str] = []
                    if used_pct <= 0.20:
                        flags.append("Low Utilization")
                    if len(non_null) > 0 and unique_count > len(non_null) * 0.33:
                        flags.append("High Unique Values Warning")
                    avg_len = len_stats.get("Average Length")
                    std_len = len_stats.get("Length stddev")
                    if avg_len is not None and std_len is not None and std_len > avg_len:
                        flags.append("Variance Warning")
                    rows.append(
                        {
                            "Field Name": col,
                            "Data Source": file_path.name,
                            "Attribute": "No Match",
                            "Category": "",
                            "Total Rows": total,
                            "Used %": round(used_pct, 2),
                            "Unique Value %": round(unique_value_pct, 3),
                            "Unique Count": int(unique_count),
                            "Flags": ", ".join(flags),
                            "Value Match Ratio": 0.0,
                            **len_stats,
                        }
                    )

    if not rows:
        logger.warning("job_no_rows,job=%s", jid)
        return None

    out_df = pd.DataFrame(rows)
    preferred_order = [
        "Field Name",
        "Data Source",
        "Attribute",
        "Category",
        "Total Rows",
        "Used %",
        "Unique Value %",
        "Unique Count",
        "Flags",
        "Value Match Ratio",
    ]
    existing_preferred = [c for c in preferred_order if c in out_df.columns]
    remaining = [c for c in out_df.columns if c not in existing_preferred]
    return out_df[existing_preferred + remaining]


def normalize_flags(flag_value: Any) -> set[str]:
    if flag_value is None or (isinstance(flag_value, float) and math.isnan(flag_value)):
        return set()
    if not isinstance(flag_value, str):
        flag_value = str(flag_value)
    return {p.strip() for p in flag_value.split(",") if p.strip()}


FLAG_DEFS: Dict[str, Dict[str, str]] = {
    "Low Utilization": {"description": "Field is used 20% or less."},
    "High Generic Values": {"description": "Field is dominated by generic values."},
    "High Unique Values Warning": {"description": "Field has high unique-value ratio."},
    "Variance Warning": {"description": "Field value lengths vary significantly."},
}


HTML_STATUS_ORDER = {"green": 0, "yellow": 1, "red": 2}


def configured_thresholds(job: Dict[str, Any]) -> Dict[str, float]:
    dictionary_yaml = vtx_path(job.get("dictionary_yaml") or DEFAULT_DICTIONARY_YAML, must_exist=True)
    attrs = load_attributes(dictionary_yaml)
    viable: List[float] = []
    match: List[float] = []
    expected_program_attrs: set[str] = set()
    for attr in attrs:
        try:
            if attr.get("min_viable_ratio") is not None:
                viable.append(float(attr.get("min_viable_ratio")))
        except Exception:
            pass
        try:
            if attr.get("min_match_ratio") is not None:
                match.append(float(attr.get("min_match_ratio")))
        except Exception:
            pass
        category = str(attr.get("category", "") or "").strip().lower()
        rec_source = str(attr.get("recommended source", "") or "").strip().lower()
        attr_name = str(attr.get("attribute", "") or "").strip()
        if category == "program" and rec_source == "application inventory" and attr_name:
            expected_program_attrs.add(attr_name)
    return {
        "min_viable_ratio": min(viable) if viable else 0.20,
        "min_match_ratio": min(match) if match else 0.50,
        "expected_program_attributes": float(len(expected_program_attrs)),
    }


def summarize_report(df: pd.DataFrame, *, expected_program_attributes: int = 0) -> Dict[str, float]:
    category_series = df.get("Category", pd.Series([""] * len(df))).fillna("").astype(str)
    attr_series = df.get("Attribute", pd.Series([""] * len(df))).fillna("").astype(str)
    flags_series = df.get("Flags", pd.Series([""] * len(df))).fillna("").astype(str)
    value_match_series = pd.to_numeric(df.get("Value Match Ratio"), errors="coerce")
    used_pct_series = pd.to_numeric(df.get("Used %"), errors="coerce")
    is_program = category_series.str.strip().str.lower() == "program"
    has_program_match = is_program & (attr_series.str.strip().str.lower() != "no match")

    program_attrs = {
        attr.strip()
        for attr in attr_series[has_program_match].tolist()
        if attr.strip() and attr.strip().lower() != "no match"
    }

    low_util = 0
    high_unique_variance = 0
    for idx, is_prog in enumerate(is_program.tolist()):
        if not is_prog:
            continue
        flags = normalize_flags(flags_series.iloc[idx])
        if "Low Utilization" in flags:
            low_util += 1
        if "High Unique Values Warning" in flags and "Variance Warning" in flags:
            high_unique_variance += 1

    matched_used = used_pct_series[has_program_match].dropna()
    matched_ratio = value_match_series[has_program_match].dropna()
    avg_used = float(matched_used.mean()) if not matched_used.empty else 0.0
    avg_ratio = float(matched_ratio.mean()) if not matched_ratio.empty else 0.0
    missing_program = max(0, int(expected_program_attributes) - len(program_attrs))

    return {
        "total_fields": float(len(df)),
        "program_attribute_count": float(len(program_attrs)),
        "missing_program_attributes": float(missing_program),
        "program_low_utilization": float(low_util),
        "program_high_unique_variance": float(high_unique_variance),
        "avg_program_utilization": avg_used,
        "avg_program_match_ratio": avg_ratio,
    }


def status_for_flags(flags: set[str]) -> Tuple[str, List[str]]:
    issues: List[str] = []
    if "Low Utilization" in flags:
        issues.append("Low utilization")
    if "High Generic Values" in flags:
        issues.append("Generic values")
    if "High Unique Values Warning" in flags and "Variance Warning" in flags:
        issues.append("High unique values + variance")

    if "Low Utilization" in flags:
        return "red", issues
    if issues:
        return "yellow", issues
    return "green", []


def safe_get(row: pd.Series, col: str, default: Any = "") -> Any:
    return row[col] if col in row and pd.notna(row[col]) else default


def generate_html(
    df: pd.DataFrame,
    title: str,
    subtitle: str | None = None,
    *,
    thresholds: Optional[Dict[str, float]] = None,
    download_csv_href: str = "",
) -> str:
    required = ["Field Name", "Attribute", "Used %", "Unique Count", "Value Match Ratio", "Flags"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Missing expected column(s) for HTML: {missing}")

    thresholds = thresholds or {"min_viable_ratio": 0.20, "min_match_ratio": 0.50}
    summary = summarize_report(
        df,
        expected_program_attributes=int(thresholds.get("expected_program_attributes", 0)),
    )
    min_viable_pct = float(thresholds.get("min_viable_ratio", 0.20)) * 100.0
    min_match_pct = float(thresholds.get("min_match_ratio", 0.50)) * 100.0
    has_category = "Category" in df.columns

    def fmt_int(v: Any) -> str:
        try:
            return f"{int(v):,}"
        except Exception:
            return escape(str(v))

    def fmt_pct(v: Any) -> str:
        try:
            return f"{float(v) * 100:0.1f}%"
        except Exception:
            return escape(str(v))

    body_rows: List[str] = []
    for _, r in df.iterrows():
        flags = normalize_flags(safe_get(r, "Flags", ""))
        severity, issues = status_for_flags(flags)
        issue_html = "".join(f'<span class="issue-tag">{escape(issue)}</span>' for issue in issues)

        used_pct = safe_get(r, "Used %", "")
        unique_count = safe_get(r, "Unique Count", "")
        value_match_ratio = safe_get(r, "Value Match Ratio", "")
        field_name = escape(str(safe_get(r, "Field Name", "")))
        attribute = escape(str(safe_get(r, "Attribute", "")))
        raw_category = str(safe_get(r, "Category", ""))
        raw_attribute = str(safe_get(r, "Attribute", ""))
        if not raw_category.strip() and raw_attribute.strip().lower().startswith("general "):
            raw_category = "General"
        category = escape(raw_category)
        status_dot = {"red": "🔴", "yellow": "🟡", "green": "🟢"}[severity]

        cols = [f"<td>{field_name}</td>"]
        cols.append(f"<td>{attribute}</td>")
        if has_category:
            cols.append(f"<td>{category}</td>")
        cols.extend(
            [
                f'<td data-value="{escape(str(used_pct))}" class="numeric">{fmt_pct(used_pct)}</td>',
                f'<td data-value="{escape(str(unique_count))}" class="numeric">{fmt_int(unique_count)}</td>',
                f'<td data-value="{escape(str(value_match_ratio))}" class="numeric">{fmt_pct(value_match_ratio)}</td>',
                f'<td data-value="{HTML_STATUS_ORDER[severity]}" class="status-cell"><span class="status-dot">{status_dot}</span>{issue_html}</td>',
            ]
        )

        body_rows.append(
            f'<tr data-severity="{severity}" '
            f'data-field="{escape(str(safe_get(r, "Field Name", ""))).lower()}" '
            f'data-attribute="{escape(str(safe_get(r, "Attribute", ""))).lower()}" '
            f'data-category="{category.lower()}">'
            + "".join(cols)
            + "</tr>"
        )

    subtitle_html = f'<div class="muted">{escape(subtitle)}</div>' if subtitle else ""
    category_header = '<th data-type="string">Category</th>' if has_category else ""
    download_btn_html = (
        f'<a class="toolbtn" href="{escape(download_csv_href)}" download>Download CSV</a>'
        if download_csv_href
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escape(title)}</title>
  <style>
    :root {{
      --bg: #0b1320;
      --bg-2: #101d2f;
      --panel: #122238;
      --line: #2b4667;
      --text: #edf4ff;
      --muted: #a9bdd7;
      --accent: #f0b24a;
      --shadow: 0 18px 48px rgba(0, 0, 0, 0.24);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--text);
      background:
        radial-gradient(circle at top right, rgba(121, 184, 255, 0.14), transparent 32%),
        radial-gradient(circle at bottom left, rgba(240, 178, 74, 0.08), transparent 36%),
        linear-gradient(180deg, var(--bg), var(--bg-2));
      font: 400 15px/1.6 "Avenir Next", "Trebuchet MS", "Helvetica Neue", sans-serif;
    }}
    .topbar {{
      position: sticky;
      top: 0;
      z-index: 10;
      backdrop-filter: blur(8px);
      background: rgba(8, 16, 28, 0.82);
      border-bottom: 1px solid rgba(67, 100, 135, 0.5);
    }}
    .topbar-inner {{
      width: min(1240px, calc(100% - 32px));
      margin: 0 auto;
      min-height: 72px;
      display: flex;
      gap: 16px;
      align-items: center;
      padding: 12px 0;
    }}
    .brandmark {{
      width: 48px;
      height: 48px;
      border-radius: 14px;
      border: 1px solid rgba(121, 184, 255, 0.35);
      background: linear-gradient(135deg, rgba(240, 178, 74, 0.22), rgba(121, 184, 255, 0.18)), rgba(17, 30, 49, 0.95);
      display: grid;
      place-items: center;
      font-weight: 800;
      letter-spacing: 0.08em;
      color: var(--accent);
      box-shadow: var(--shadow);
      flex: 0 0 auto;
    }}
    .brandcopy p {{ margin: 0; }}
    .eyebrow {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.18em; color: var(--muted); }}
    .brandline {{ margin-top: 2px; font-size: 24px; font-weight: 700; line-height: 1.2; }}
    .instance {{ margin-top: 4px; font-size: 13px; color: var(--muted); }}
    .toolbar {{ margin-left: auto; display: flex; gap: 10px; align-items: center; }}
    .toolbtn {{
      border: 1px solid rgba(67, 100, 135, 0.7);
      background: rgba(19, 34, 56, 0.92);
      color: var(--text);
      padding: 10px 14px;
      border-radius: 999px;
      text-decoration: none;
      font-size: 13px;
      font-weight: 600;
    }}
    .toolbtn:hover {{ border-color: rgba(121, 184, 255, 0.7); background: rgba(27, 48, 77, 0.98); }}
    .shell {{ width: min(1480px, calc(100% - 24px)); margin: 0 auto; padding: 24px 0 28px; }}
    .hero-grid {{ display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, 0.95fr) minmax(0, 0.9fr); gap: 18px; align-items: start; }}
    .panel {{
      border: 1px solid rgba(58, 88, 121, 0.72);
      border-radius: 24px;
      background: linear-gradient(180deg, rgba(19, 35, 57, 0.96), rgba(15, 28, 45, 0.96));
      box-shadow: var(--shadow);
      padding: 24px;
    }}
    .header {{ font-size: 34px; font-weight: 700; margin: 0; line-height: 1.08; }}
    .muted {{ color: #9aa3b2; font-size: 13px; margin-top: 10px; }}
    .lede {{ margin: 16px 0 0; color: #d5e3f6; font-size: 15px; max-width: 68ch; }}
    .inline-link {{ color: #8fc1ff; text-decoration: none; border-bottom: 1px solid rgba(143,193,255,0.35); }}
    .inline-link:hover {{ color: #d9ecff; border-bottom-color: rgba(217,236,255,0.6); }}
    .callout {{ margin-top: 18px; padding: 16px 18px; border-radius: 18px; background: rgba(11, 21, 34, 0.7); border: 1px solid rgba(58, 88, 121, 0.56); }}
    .callout h3, .definitions h3, .summary-panel h3 {{ margin: 0; font-size: 14px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--accent); }}
    .callout p {{ margin: 10px 0 0; color: #c7d7ec; }}
    .definitions, .summary-panel {{ height: 100%; }}
    .definitions-section {{ margin-top: 18px; padding-top: 18px; border-top: 1px solid rgba(58, 88, 121, 0.45); }}
    .definitions-section:first-of-type {{ margin-top: 0; padding-top: 0; border-top: 0; }}
    .definitions ul {{ margin: 12px 0 0; padding-left: 18px; color: #c7d7ec; }}
    .definitions li {{ margin-bottom: 8px; }}
    .summary-list {{ margin-top: 10px; display: grid; gap: 10px; }}
    .summary-row {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px; padding: 10px 0; border-bottom: 1px solid rgba(58, 88, 121, 0.32); align-items: baseline; }}
    .summary-row:last-child {{ border-bottom: 0; }}
    .summary-label {{ color: #c7d7ec; font-size: 14px; }}
    .summary-value {{ font-size: 14px; font-weight: 400; color: var(--text); text-align: right; }}
    .tile {{ margin-top: 22px; border: 1px solid rgba(58, 88, 121, 0.72); border-radius: 24px; background: linear-gradient(180deg, rgba(19, 35, 57, 0.96), rgba(15, 28, 45, 0.96)); box-shadow: var(--shadow); padding: 18px; }}
    .table-wrapper {{ margin-top: 8px; max-height: 68vh; overflow: auto; border-radius: 18px; border: 1px solid rgba(58, 88, 121, 0.45); padding-bottom: 8px; }}
    table.data {{ width: 100%; border-collapse: collapse; font-size: 11px; table-layout: auto; }}
    table.data th, table.data td {{ padding: 7px 8px; border-bottom: 1px solid rgba(255,255,255,0.07); white-space: normal; vertical-align: middle; }}
    table.data th {{ font-weight: 600; color: #c5d0ff; position: sticky; top: 0; background: #151a30; cursor: pointer; }}
    .numeric {{ text-align: center !important; font-variant-numeric: tabular-nums; }}
    table.data th.sort-asc::after {{ content: " ▲"; font-size: 10px; }}
    table.data th.sort-desc::after {{ content: " ▼"; font-size: 10px; }}
    table.data tr:hover {{ background: rgba(255,255,255,0.02); }}
    .controls {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 10px;
      align-items: center;
    }}
    .search-input {{
      background: #0f1326;
      border: 1px solid rgba(255,255,255,0.12);
      border-radius: 999px;
      padding: 6px 10px;
      color: #eaeef8;
      font-size: 12px;
      min-width: 240px;
      outline: none;
    }}
    .search-input::placeholder {{ color: #6a7285; }}
    .pill-checkbox {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      background: #11172b;
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 11px;
      color: #c5d0ff;
      cursor: pointer;
      user-select: none;
    }}
    .pill-checkbox input {{ accent-color: #ffd86b; }}
    .status-cell {{ min-width: 170px; }}
    .status-dot {{ margin-right: 6px; font-size: 15px; }}
    .issue-tag {{ display: inline-block; margin: 2px 6px 2px 0; border-radius: 999px; padding: 4px 8px; font-size: 11px; background: rgba(17, 30, 49, 0.78); color: var(--text); border: 1px solid rgba(67, 100, 135, 0.38); }}
    .page-footer {{ padding: 18px 0 90px; color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; text-align: right; }}
    @media (max-width: 920px) {{ .hero-grid {{ grid-template-columns: 1fr; }} }}
    @media (max-width: 760px) {{ .topbar-inner {{ flex-direction: column; align-items: flex-start; }} .toolbar {{ margin-left: 0; width: 100%; }} .toolbtn {{ width: 100%; text-align: center; }} .search-input {{ width: 100%; min-width: 0; }} }}
  </style>
</head>
<body>
  <header class="topbar">
    <div class="topbar-inner">
      <div class="brandmark" aria-hidden="true">VTX</div>
      <div class="brandcopy">
        <p class="eyebrow">Vector Transformation</p>
        <p class="brandline">Consolidated Inventories</p>
        <p class="instance" id="instance-name">Instance: Loading...</p>
      </div>
      <div class="toolbar">
        {download_btn_html}
        <a class="toolbtn" href="/logout">Logout</a>
      </div>
    </div>
  </header>
  <main class="shell">
    <section class="hero-grid">
      <section class="panel">
        <div class="header">{escape(title)}</div>
        <p class="lede">This is an analysis of your source inventories in which VTX has identified application and server attributes, listed in the <strong>Field Name</strong> column, and matched the patterns of your data against known attributes, listed in the <strong>Attribute</strong> column. VTX identifies program attributes and general attributes to develop an understanding of what information is in your inventory, and, importantly, which attributes are missing and which attributes have gaps and discrepancies.</p>
        <p class="lede">For context on how these program attributes support migration planning, review the <a class="inline-link" href="/reports/static/program_timeline.html" target="_blank" rel="noopener">Program Timeline</a>.</p>
        <div class="callout">
          <h3>Next Steps</h3>
          <p>From here, we can take this report as guidance for how to map your data source attributes to program attributes. This is the bridge between understanding the shape of your inventory and preparing customer-specific attribute mappings in configuration.</p>
        </div>
      </section>
      <section class="panel definitions">
        <div class="definitions-section">
          <h3>Definitions</h3>
          <ul>
            <li><strong>Low utilization</strong> means {min_viable_pct:0.0f}% or less of the rows have a value present.</li>
            <li><strong>Generic values</strong> means a high percentage of field values are generic placeholders such as Unknown, Custom, or Other.</li>
            <li><strong>Freeform text fields</strong> are fields that show both high unique values and high variance, indicating values are present but not following a stable pattern.</li>
          </ul>
        </div>
        <div class="definitions-section">
          <h3>Methodology</h3>
          <ul>
            <li>At least {min_viable_pct:0.0f}% of rows in a field must contain data before VTX considers that field viable for matching.</li>
            <li>At least {min_match_pct:0.0f}% of populated values must match an attribute pattern before VTX assigns that attribute.</li>
            <li>There are roughly 50 attributes used for a typical migration program, but VTX does not expect all of them to exist in an inventory system. Some attributes are expected to live in architecture repositories or require manual collection.</li>
          </ul>
        </div>
      </section>
      <aside class="panel summary-panel">
        <h3>Data Summary</h3>
        <div class="summary-list">
          <div class="summary-row"><div class="summary-label">Total Fields Evaluated</div><div class="summary-value">{int(summary["total_fields"]):,}</div></div>
          <div class="summary-row"><div class="summary-label">Program Attributes Identified</div><div class="summary-value">{int(summary["program_attribute_count"]):,}</div></div>
          <div class="summary-row"><div class="summary-label">Missing Program Attributes</div><div class="summary-value">{int(summary["missing_program_attributes"]):,}</div></div>
          <div class="summary-row"><div class="summary-label">Program Fields With Low Utilization</div><div class="summary-value">{int(summary["program_low_utilization"]):,}</div></div>
          <div class="summary-row"><div class="summary-label">Program Fields With Freeform Text</div><div class="summary-value">{int(summary["program_high_unique_variance"]):,}</div></div>
          <div class="summary-row"><div class="summary-label">Average Utilization of Program Fields</div><div class="summary-value">{summary["avg_program_utilization"] * 100:0.1f}%</div></div>
          <div class="summary-row"><div class="summary-label">Average Value Match Ratio of Program Fields</div><div class="summary-value">{summary["avg_program_match_ratio"] * 100:0.1f}%</div></div>
        </div>
      </aside>
    </section>
    <div class="tile">
    <div class="controls">
      <input id="filter-input" class="search-input" type="text" placeholder="Filter by Field Name or Attribute..." />
      <label class="pill-checkbox">
        <input type="checkbox" id="flagged-only" />
        Show only flagged rows
      </label>
      <label class="pill-checkbox">
        <input type="checkbox" id="clean-only" />
        Show only clear rows
      </label>
      <label class="pill-checkbox">
        <input type="checkbox" id="hide-general" />
        Hide general attributes
      </label>
    </div>
    <div class="table-wrapper">
      <table class="data" id="quality-table">
        <thead>
          <tr>
            <th data-type="string">Field Name</th>
            <th data-type="string">Attribute</th>
            {category_header}
            <th data-type="number">Used %</th>
            <th data-type="number">Unique Count</th>
            <th data-type="number">Value Match Ratio</th>
            <th data-type="number">Status</th>
          </tr>
        </thead>
        <tbody>
          {"".join(body_rows)}
        </tbody>
      </table>
    </div>
    </div>
    <footer class="page-footer">Vector Transformation</footer>
  </main>
  <script>
    (function() {{
      const host = window.location.hostname || "local";
      document.getElementById("instance-name").textContent = `Instance: ${{host}}`;
      const table = document.getElementById("quality-table");
      const tbody = table.tBodies[0];
      const headers = Array.from(table.tHead.rows[0].children);
      const filterInput = document.getElementById("filter-input");
      const flaggedOnlyCheckbox = document.getElementById("flagged-only");
      const cleanOnlyCheckbox = document.getElementById("clean-only");
      const hideGeneralCheckbox = document.getElementById("hide-general");
      let currentSortIndex = null;
      let currentSortDir = "asc";

      function getCellValue(row, idx) {{
        const cell = row.children[idx];
        if (!cell) return "";
        const dataValue = cell.getAttribute("data-value");
        return dataValue !== null ? dataValue : (cell.textContent || "");
      }}

      function compareValues(a, b, type, direction) {{
        if (type === "number") {{
          const na = parseFloat(a);
          const nb = parseFloat(b);
          const A = Number.isNaN(na) ? -Infinity : na;
          const B = Number.isNaN(nb) ? -Infinity : nb;
          return direction === "asc" ? A - B : B - A;
        }}
        return direction === "asc" ? a.localeCompare(b) : b.localeCompare(a);
      }}

      function sortByColumn(index, type) {{
        const rows = Array.from(tbody.querySelectorAll("tr"));
        const direction = (currentSortIndex === index && currentSortDir === "asc") ? "desc" : "asc";
        currentSortIndex = index;
        currentSortDir = direction;
        rows.sort((ra, rb) =>
          compareValues(
            getCellValue(ra, index).toString().toLowerCase(),
            getCellValue(rb, index).toString().toLowerCase(),
            type,
            direction
          )
        ).forEach(r => tbody.appendChild(r));
        headers.forEach(h => h.classList.remove("sort-asc", "sort-desc"));
        headers[index].classList.add(direction === "asc" ? "sort-asc" : "sort-desc");
      }}

      headers.forEach((th, idx) => th.addEventListener("click", () => sortByColumn(idx, th.getAttribute("data-type"))));

      function applyFilters() {{
        const query = (filterInput.value || "").toLowerCase();
        const flaggedOnly = flaggedOnlyCheckbox.checked;
        const cleanOnly = cleanOnlyCheckbox.checked;
        const hideGeneral = hideGeneralCheckbox.checked;
        const rows = Array.from(tbody.querySelectorAll("tr"));
        rows.forEach(row => {{
          const fieldName = row.getAttribute("data-field") || "";
          const attribute = row.getAttribute("data-attribute") || "";
          const category = row.getAttribute("data-category") || "";
          const severity = row.getAttribute("data-severity") || "green";
          const matchesText = !query || fieldName.includes(query) || attribute.includes(query);
          let visible = matchesText;
          if (hideGeneral && category === "general") visible = false;
          if (flaggedOnly) visible = visible && severity !== "green";
          if (cleanOnly) visible = visible && severity === "green";
          row.style.display = visible ? "" : "none";
        }});
      }}

      filterInput.addEventListener("input", applyFilters);
      flaggedOnlyCheckbox.addEventListener("change", () => {{
        if (flaggedOnlyCheckbox.checked) cleanOnlyCheckbox.checked = false;
        applyFilters();
      }});
      cleanOnlyCheckbox.addEventListener("change", () => {{
        if (cleanOnlyCheckbox.checked) flaggedOnlyCheckbox.checked = false;
        applyFilters();
      }});
      hideGeneralCheckbox.addEventListener("change", applyFilters);
      applyFilters();
    }})();
  </script>
</body>
</html>"""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run data source analysis and build CSV + HTML outputs.")
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="YAML config path")
    p.add_argument("--job", default=None, help="Run only one job id")
    p.add_argument("--dry-run", action="store_true", help="Do not write files")
    return p.parse_args()


def run_job(job: Dict[str, Any], *, dry_run: bool = False) -> None:
    jid = job_id(job) or "job"
    if job.get("enabled") is False:
        logger.info("job_skipped_disabled,id=%s", jid)
        return

    output_csv = vtx_path(job.get("output_csv") or "")
    output_html = vtx_path(job.get("output_html") or "")
    title = "Consolidated Inventories"
    subtitle = job.get("subtitle")
    if not output_csv.name or not output_html.name:
        raise ValueError(f"Job missing output_csv or output_html: {jid}")

    thresholds = configured_thresholds(job)

    source_files: List[Path] = []
    for src in iter_source_defs(job):
        source_files.extend(expand_source_paths(str(src.get("path") or "")))

    if output_is_fresh(output_csv, source_files) and output_is_fresh(output_html, source_files):
        logger.info("job_skipped_up_to_date,id=%s", jid)
        return

    df = analyze_job(job)
    if df is None or df.empty:
        logger.warning("job_no_output,id=%s", jid)
        return

    html = generate_html(
        df,
        title=title,
        subtitle=subtitle if isinstance(subtitle, str) else None,
        thresholds=thresholds,
        download_csv_href=output_csv.name,
    )
    if dry_run:
        logger.info("dry_run_outputs,id=%s,csv=%s,html=%s,rows=%d", jid, output_csv, output_html, len(df))
        return

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    output_html.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_csv, index=False)
    output_html.write_text(html, encoding="utf-8")
    logger.info("job_complete,id=%s,csv=%s,html=%s,rows=%d", jid, output_csv, output_html, len(df))


def main() -> int:
    args = parse_args()
    cfg_path = vtx_path(args.config, must_exist=True)
    logger.info("data_source_analysis_vtx_start,config=%s", cfg_path)
    cfg = load_yaml(cfg_path)
    jobs = extract_jobs(cfg)
    if not jobs:
        logger.error("no_jobs_defined,config=%s", cfg_path)
        return 2
    selected, err = select_jobs(jobs, args.job)
    if err:
        logger.error(err)
        return 3
    for j in selected:
        try:
            run_job(j, dry_run=bool(args.dry_run))
        except Exception:
            logger.exception("job_failed,id=%s", job_id(j))
            return 4
    logger.info("data_source_analysis_vtx_complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
