#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Tag Engine
----------
Purpose:
  Apply rule-driven tags to entities based on conditions over a master dataset.

Inputs:
  - Config YAML (usr/config/run/tag_engine.yaml)
  - Master dataset (parquet)

Outputs:
  - Tag assignments CSV
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import pandas as pd
except Exception as e:
    raise SystemExit("Missing dependency: pandas. Install it in the VTX venv.") from e

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
      3) infer relative to this file: <VTX_ROOT>/usr/scripts/default/<script>.py
    """
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    here = Path(__file__).resolve()
    inferred = here.parents[3]  # default/<script>.py -> scripts -> usr -> VTX_ROOT
    return inferred


VTX_ROOT = resolve_vtx_root()
DEFAULT_CONFIG_PATH = VTX_ROOT / "usr" / "config" / "run" / "tag_engine.yaml"


def vtx_path(path_str: str | Path, *, must_exist: bool = False) -> Path:
    """
    Resolve a path that may be VTX-relative or absolute.
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


logger = get_logger(component="tag_engine")


SOURCE_FIELD_ALIASES: Dict[str, List[str]] = {
    # Sandbox master_applications uses these physical columns while production YAML
    # may still refer to the user-facing labels.
    "OS Version": ["HOSTING_DETAILS"],
    "Inferred Server Location": ["HOSTING_LOCATION"],
}


# ---------------------------------------------------------------------
# Config loading
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

    for k in ("jobs", "inputs", "outputs", "tags", "rules"):
        if k in cfg and cfg[k] is None:
            cfg[k] = []

    return cfg


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

@dataclass
class Options:
    config_path: Path
    job_id: Optional[str] = None


def parse_args(argv: Optional[List[str]] = None) -> Options:
    p = argparse.ArgumentParser(description="Tag Engine")
    p.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Path to YAML config (default: {DEFAULT_CONFIG_PATH})",
    )
    p.add_argument("--job", default=None, help="Run a specific job id")
    args = p.parse_args(argv)

    cfg_path = vtx_path(args.config, must_exist=True)
    return Options(config_path=cfg_path, job_id=args.job)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def is_null_value(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, float) and pd.isna(val):
        return True
    s = str(val).strip()
    if s == "":
        return True
    if s.lower() in {"nan", "none", "null"}:
        return True
    return False


def normalize_text(val: Any) -> str:
    if is_null_value(val):
        return ""
    return str(val).strip()


def split_values(val: Any, delimiter: str) -> List[str]:
    if is_null_value(val):
        return []
    s = str(val)
    parts = s.split(delimiter)
    return [p.strip() for p in parts]


def coerce_number(val: Any) -> Optional[float]:
    if is_null_value(val):
        return None
    if isinstance(val, (int, float)) and not (isinstance(val, float) and pd.isna(val)):
        return float(val)
    s = str(val).strip().replace(",", "")
    try:
        return float(s)
    except Exception:
        return None


def normalize_values(values: List[str]) -> List[str]:
    return ["" if v is None else str(v) for v in values]


def align_values(values: List[str], length: int) -> List[str]:
    """
    Align a split field to the expanded row count.

    Multi-valued collapsed rows use `;`-delimited lists that are evaluated slot-by-slot.
    When one field expands to multiple slots but another field is scalar, the scalar value
    should apply to every expanded slot rather than only the first.
    """
    if len(values) >= length:
        return values
    if len(values) == 1 and length > 1:
        return values * length
    return values + [""] * (length - len(values))


def build_tag_value(tag_value: Any) -> str:
    return normalize_text(tag_value)


def agg_values(values: List[str], agg: str) -> Optional[float]:
    cleaned = [v for v in values if not is_null_value(v)]
    if agg == "count":
        return float(len(cleaned))
    if agg == "unique_count":
        return float(len(set(cleaned)))

    nums = [coerce_number(v) for v in cleaned]
    nums = [n for n in nums if n is not None]
    if not nums:
        return None
    if agg == "sum":
        return float(sum(nums))
    if agg == "avg":
        return float(sum(nums)) / float(len(nums))
    if agg == "min":
        return float(min(nums))
    if agg == "max":
        return float(max(nums))
    return None


def compare_list_values(values: List[str], operator: str, target: Any) -> bool:
    if operator in {"==", "!="}:
        if operator == "==":
            return any(v == str(target) for v in values)
        return all(v != str(target) for v in values)

    if operator in {"contains", "notcontains"}:
        if operator == "contains":
            return any(str(target) in v for v in values)
        return all(str(target) not in v for v in values)

    if operator in {">", ">=", "<", "<="}:
        t_num = coerce_number(target)
        if t_num is None:
            return False
        comparisons = []
        for v in values:
            v_num = coerce_number(v)
            if v_num is None:
                continue
            if operator == ">":
                comparisons.append(v_num > t_num)
            elif operator == ">=":
                comparisons.append(v_num >= t_num)
            elif operator == "<":
                comparisons.append(v_num < t_num)
            elif operator == "<=":
                comparisons.append(v_num <= t_num)
        return any(comparisons)

    if operator == "in_range":
        if not isinstance(target, (list, tuple)) or len(target) != 2:
            return False
        lo = coerce_number(target[0])
        hi = coerce_number(target[1])
        if lo is None or hi is None:
            return False
        for v in values:
            v_num = coerce_number(v)
            if v_num is None:
                continue
            if lo <= v_num <= hi:
                return True
        return False

    return False


def compare_scalar_value(value: Optional[float], operator: str, target: Any) -> bool:
    if operator in {"==", "!="}:
        if value is None:
            return False
        t_num = coerce_number(target)
        if t_num is None:
            return False
        if operator == "==":
            return value == t_num
        return value != t_num

    if operator in {">", ">=", "<", "<="}:
        if value is None:
            return False
        t_num = coerce_number(target)
        if t_num is None:
            return False
        if operator == ">":
            return value > t_num
        if operator == ">=":
            return value >= t_num
        if operator == "<":
            return value < t_num
        if operator == "<=":
            return value <= t_num

    if operator == "in_range":
        if value is None:
            return False
        if not isinstance(target, (list, tuple)) or len(target) != 2:
            return False
        lo = coerce_number(target[0])
        hi = coerce_number(target[1])
        if lo is None or hi is None:
            return False
        return lo <= value <= hi

    return False


def eval_condition(
    values: List[str],
    operator: str,
    target: Any,
    agg: Optional[str] = None,
) -> bool:
    op = operator.lower().strip()
    if op in {"isnull", "is_null"}:
        return all(is_null_value(v) for v in values)
    if op in {"isnotnull", "is_not_null"}:
        return any(not is_null_value(v) for v in values)

    if agg:
        agg_value = agg_values(values, agg)
        return compare_scalar_value(agg_value, op, target)

    return compare_list_values(values, op, target)


def normalize_logic(logic: Optional[str]) -> str:
    if not logic:
        return "all"
    l = logic.strip().lower()
    if l in {"and", "all"}:
        return "all"
    if l in {"or", "any"}:
        return "any"
    return "all"


def apply_source_field_aliases(df: pd.DataFrame, required_fields: List[str]) -> pd.DataFrame:
    missing = [field for field in required_fields if field not in df.columns]
    if not missing:
        return df

    df_out = df
    for field in missing:
        for alias in SOURCE_FIELD_ALIASES.get(field, []):
            if alias in df_out.columns:
                df_out[field] = df_out[alias]
                logger.info("Mapped missing source field '%s' from alias '%s'", field, alias)
                break
    return df_out


def read_source_table(path: Path) -> pd.DataFrame:
    suffix = str(path).lower()
    if suffix.endswith(".parquet"):
        return pd.read_parquet(path)
    if suffix.endswith(".csv"):
        return pd.read_csv(path, dtype=str, keep_default_na=False).fillna("")
    raise ValueError(f"Unsupported input dataset format: {path}")


def collect_fields(tags: List[Dict[str, Any]]) -> List[str]:
    fields: List[str] = []
    for tag in tags:
        for k in tag.get("key_fields", []) or []:
            if k not in fields:
                fields.append(k)
        for rule in tag.get("rules", []) or []:
            for cond in rule.get("conditions", []) or []:
                field = cond.get("field")
                if field and field not in fields:
                    fields.append(field)
    return fields


def collect_key_fields(tags: List[Dict[str, Any]]) -> List[str]:
    keys: List[str] = []
    for tag in tags:
        for k in tag.get("key_fields", []) or []:
            if k not in keys:
                keys.append(k)
    return keys


def collect_tag_columns(tags: List[Dict[str, Any]], derived_tags: List[Dict[str, Any]]) -> List[str]:
    columns: List[str] = []
    for definition in list(tags) + list(derived_tags):
        tag_column = normalize_text(definition.get("tag_column", ""))
        if tag_column and tag_column not in columns:
            columns.append(tag_column)
    return columns


def collect_match_columns(tags: List[Dict[str, Any]]) -> List[str]:
    columns: List[str] = []
    for tag in tags:
        for column in tag.get("match_columns", []) or []:
            col = normalize_text(column)
            if col and col not in columns:
                columns.append(col)
    return columns


def build_tag_group_index(
    rows: List[Dict[str, Any]],
    key_fields: List[str],
    match_fields: List[str],
) -> Tuple[
    Dict[Tuple[str, Tuple[str, ...]], List[Dict[str, str]]],
    Dict[Tuple[str, Tuple[str, ...], Tuple[str, ...]], List[Dict[str, str]]],
]:
    anchor_only: Dict[Tuple[str, Tuple[str, ...]], List[Dict[str, str]]] = {}
    anchor_key: Dict[Tuple[str, Tuple[str, ...], Tuple[str, ...]], List[Dict[str, str]]] = {}

    for row in rows:
        anchor = normalize_text(row.get("anchor", ""))
        if anchor == "":
            continue
        key_tuple = tuple(normalize_text(row.get(k, "")) for k in key_fields)
        match_tuple = tuple(normalize_text(row.get(m, "")) for m in match_fields)
        has_key = any(v != "" for v in key_tuple)
        tag_column = normalize_text(row.get("tag_column", ""))
        tag_value = normalize_text(row.get("tag_value", ""))
        if not tag_column or not tag_value:
            continue
        entry = {"tag_column": tag_column, "tag_value": tag_value}

        if has_key:
            anchor_key.setdefault((anchor, match_tuple, key_tuple), []).append(entry)
        else:
            anchor_only.setdefault((anchor, match_tuple), []).append(entry)

    return anchor_only, anchor_key


def tag_record_condition_match(record: Dict[str, str], cond: Dict[str, Any]) -> bool:
    field = normalize_text(cond.get("field", ""))
    operator = str(cond.get("operator", "==")).strip().lower()
    value = normalize_text(cond.get("value", ""))
    if field not in {"tag_column", "tag_value"}:
        return False
    record_value = normalize_text(record.get(field, ""))

    if operator in {"==", "=", "eq"}:
        return record_value == value
    if operator in {"!=", "ne"}:
        return record_value != value
    if operator == "contains":
        return value in record_value
    if operator == "notcontains":
        return value not in record_value
    if operator in {"isnull", "is_null"}:
        return record_value == ""
    if operator in {"isnotnull", "is_not_null"}:
        return record_value != ""

    return False


def build_derived_condition_groups(conditions: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    groups: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    for cond in conditions:
        field = normalize_text(cond.get("field", ""))
        if current and field == "tag_column":
            groups.append(current)
            current = [cond]
        else:
            current.append(cond)
    if current:
        groups.append(current)
    return groups


def tag_condition_groups_match(
    tag_records: List[Dict[str, str]],
    conditions: List[Dict[str, Any]],
    logic: str,
) -> bool:
    condition_groups = build_derived_condition_groups(conditions)
    if not condition_groups:
        return False

    group_results: List[bool] = []
    for group in condition_groups:
        group_results.append(any(all(tag_record_condition_match(record, cond) for cond in group) for record in tag_records))

    if logic == "any":
        return any(group_results)
    return all(group_results)


def collapse_tag_records(
    rows: List[Dict[str, Any]],
    match_fields: List[str],
    tag_columns: List[str],
    delimiter: str,
) -> pd.DataFrame:
    group_order: List[Tuple[str, Tuple[str, ...]]] = []
    group_anchor_values: Dict[Tuple[str, Tuple[str, ...]], Any] = {}
    collapsed: Dict[Tuple[str, Tuple[str, ...]], Dict[str, Any]] = {}

    for row in rows:
        anchor_key = normalize_text(row.get("anchor", ""))
        tag_column = normalize_text(row.get("tag_column", ""))
        tag_value = normalize_text(row.get("tag_value", ""))
        if not anchor_key or not tag_column or not tag_value:
            continue
        match_tuple = tuple(normalize_text(row.get(field, "")) for field in match_fields)
        group_key = (anchor_key, match_tuple)

        if group_key not in collapsed:
            collapsed[group_key] = {"anchor": row.get("anchor")}
            for idx, field in enumerate(match_fields):
                collapsed[group_key][field] = match_tuple[idx]
            for col in tag_columns:
                collapsed[group_key][col] = []
            group_order.append(group_key)
            group_anchor_values[group_key] = row.get("anchor")

        values = collapsed[group_key].setdefault(tag_column, [])
        if tag_value not in values:
            values.append(tag_value)

    output_rows: List[Dict[str, Any]] = []
    for group_key in group_order:
        row_out: Dict[str, Any] = {"anchor": group_anchor_values[group_key]}
        for field in match_fields:
            row_out[field] = collapsed[group_key].get(field, "")
        for tag_column in tag_columns:
            row_out[tag_column] = delimiter.join(collapsed[group_key].get(tag_column, []))
        output_rows.append(row_out)

    columns = ["anchor"] + match_fields + tag_columns
    return pd.DataFrame(output_rows, columns=columns)


# ---------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------

def run_job(job: Dict[str, Any]) -> pd.DataFrame:
    job_id = job.get("id", "tag_engine")
    inputs = job.get("inputs", []) or []
    outputs = job.get("outputs", []) or []
    if not inputs:
        raise ValueError(f"Job '{job_id}' missing inputs")
    if not outputs:
        raise ValueError(f"Job '{job_id}' missing outputs")

    input_path = vtx_path(inputs[0], must_exist=True)
    output_path = vtx_path(outputs[0])

    anchor_field = job.get("anchor_field")
    if not anchor_field:
        raise ValueError(f"Job '{job_id}' missing anchor_field")

    key_delimiter = str(job.get("key_delimiter", ";"))

    tags = job.get("tags", []) or []
    derived_tags = job.get("derived_tags", []) or []
    if not tags:
        raise ValueError(f"Job '{job_id}' has no tags defined")

    df = read_source_table(input_path)

    if anchor_field not in df.columns:
        raise ValueError(f"Anchor field '{anchor_field}' not in input dataset")

    match_field_names = collect_match_columns(tags)
    all_fields = list(dict.fromkeys(collect_fields(tags) + match_field_names))
    df = apply_source_field_aliases(df, all_fields)
    missing_fields = [f for f in all_fields if f not in df.columns]
    if missing_fields:
        raise ValueError(f"Missing fields in input dataset: {missing_fields}")

    tag_records: List[Dict[str, Any]] = []
    key_field_names = collect_key_fields(tags)
    output_tag_columns = collect_tag_columns(tags, derived_tags)

    for tag in tags:
        tag_column = normalize_text(tag.get("tag_column", ""))
        tag_source = tag.get("source", "rule")
        tag_key_fields = tag.get("key_fields", []) or []
        tag_match_fields = [normalize_text(c) for c in (tag.get("match_columns", []) or []) if normalize_text(c)]
        condition_logic = normalize_logic(tag.get("condition_logic"))
        first_match_only = bool(tag.get("first_match_only", True))

        if not tag_column:
            logger.warning("Skipping tag '%s' in job '%s': missing tag_column", tag.get("id", ""), job_id)
            continue

        context_fields = list(dict.fromkeys(tag_match_fields + tag_key_fields))
        tag_fields = list(dict.fromkeys(context_fields + collect_fields([tag])))
        groups: Dict[Tuple[str, Tuple[str, ...]], List[Dict[str, str]]] = {}
        anchor_values: Dict[Tuple[str, Tuple[str, ...]], Any] = {}

        for _, row in df.iterrows():
            anchor_value = row.get(anchor_field)
            if is_null_value(anchor_value):
                continue

            field_values: Dict[str, List[str]] = {}
            max_len = 1
            for field in tag_fields:
                values = split_values(row.get(field), key_delimiter)
                field_values[field] = values
                max_len = max(max_len, len(values))

            aligned_values = {field: align_values(values, max_len) for field, values in field_values.items()}
            for idx in range(max_len):
                context_tuple = tuple(aligned_values.get(field, [""] * max_len)[idx] for field in context_fields)
                group_key = (normalize_text(anchor_value), context_tuple)
                slot = {field: aligned_values.get(field, [""] * max_len)[idx] for field in tag_fields}
                groups.setdefault(group_key, []).append(slot)
                anchor_values.setdefault(group_key, anchor_value)

        for group_key, slots in groups.items():
            context_tuple = group_key[1]
            matched_any_rule = False
            for rule in tag.get("rules", []) or []:
                conditions = rule.get("conditions", []) or []
                rule_logic = normalize_logic(rule.get("condition_logic"))
                if not conditions:
                    continue

                results: List[bool] = []
                for cond in conditions:
                    field = cond.get("field")
                    if not field:
                        results.append(False)
                        continue
                    operator = cond.get("operator", "==")
                    target = cond.get("value")
                    agg = cond.get("agg") or cond.get("aggregate")
                    group_values = [slot.get(field, "") for slot in slots]
                    results.append(eval_condition(group_values, operator, target, agg))

                logic = rule_logic or condition_logic
                if logic == "any":
                    is_match = any(results)
                else:
                    is_match = all(results)

                if not is_match:
                    continue

                matched_any_rule = True
                tag_value = build_tag_value(rule.get("tag_value", ""))
                if not tag_value:
                    logger.warning(
                        "Skipping tag assignment in job '%s', tag '%s', rule '%s': missing tag_column/tag_value",
                        job_id,
                        tag.get("id", ""),
                        rule.get("id", ""),
                    )
                    continue
                row_out = {
                    "anchor": anchor_values[group_key],
                    "tag_column": tag_column,
                    "tag_value": tag_value,
                    "source": rule.get("source", tag_source),
                    "rule_id": rule.get("id"),
                }

                for key_field in key_field_names:
                    row_out[key_field] = ""
                for match_field in match_field_names:
                    row_out[match_field] = ""
                context_map = {context_fields[i]: key_val for i, key_val in enumerate(context_tuple)}
                for key_field in key_field_names:
                    row_out[key_field] = context_map.get(key_field, "")
                for match_field in match_field_names:
                    row_out[match_field] = context_map.get(match_field, "")

                tag_records.append(row_out)

                if first_match_only:
                    break

            if not matched_any_rule:
                continue

    if derived_tags:
        anchor_only, anchor_key = build_tag_group_index(tag_records, key_field_names, match_field_names)
        existing: Dict[Tuple[str, Tuple[str, ...], Tuple[str, ...]], set[Tuple[str, str]]] = {}

        for (anchor, match_tuple), tags_list in anchor_only.items():
            key_tuple = tuple("" for _ in key_field_names)
            existing[(anchor, match_tuple, key_tuple)] = {(entry["tag_column"], entry["tag_value"]) for entry in tags_list}
        for (anchor, match_tuple, key_tuple), tags_list in anchor_key.items():
            existing[(anchor, match_tuple, key_tuple)] = {(entry["tag_column"], entry["tag_value"]) for entry in tags_list}

        for derived in derived_tags:
            derived_column = normalize_text(derived.get("tag_column", ""))
            derived_source = derived.get("source", "rule")
            derived_scope = str(derived.get("scope", "anchor")).strip().lower()
            include_anchor_tags = bool(derived.get("include_anchor_tags", False))
            derived_logic = normalize_logic(derived.get("condition_logic"))
            derived_first_match = bool(derived.get("first_match_only", True))

            if not derived_column:
                logger.warning("Skipping derived tag '%s' in job '%s': missing tag_column", derived.get("id", ""), job_id)
                continue

            if derived_scope not in {"anchor", "anchor_key"}:
                raise ValueError(f"Invalid derived scope '{derived_scope}' for tag '{derived_column}'")

            if derived_scope == "anchor":
                groups = [
                    (anchor, match_tuple, tuple("" for _ in key_field_names), list(tag_list))
                    for (anchor, match_tuple), tag_list in anchor_only.items()
                ]
            else:
                groups = []
                for (anchor, match_tuple, key_tuple), tag_list in anchor_key.items():
                    combined = list(tag_list)
                    if include_anchor_tags:
                        combined.extend(anchor_only.get((anchor, match_tuple), []))
                    groups.append((anchor, match_tuple, key_tuple, combined))

            for anchor, match_tuple, key_tuple, tag_list in groups:
                matched_any = False
                for rule in derived.get("rules", []) or []:
                    conditions = rule.get("conditions", []) or []
                    if not conditions:
                        continue

                    rule_logic = normalize_logic(rule.get("condition_logic")) or derived_logic
                    is_match = tag_condition_groups_match(tag_list, conditions, rule_logic)
                    if not is_match:
                        continue

                    exclude_conditions = rule.get("exclude", []) or []
                    if exclude_conditions:
                        if tag_condition_groups_match(tag_list, exclude_conditions, "any"):
                            continue

                    matched_any = True
                    tag_value = build_tag_value(rule.get("tag_value", ""))
                    if not tag_value:
                        continue

                    group_key = (anchor, match_tuple, key_tuple)
                    existing.setdefault(group_key, set())
                    if (derived_column, tag_value) in existing[group_key]:
                        continue
                    existing[group_key].add((derived_column, tag_value))

                    row_out = {
                        "anchor": anchor,
                        "tag_column": derived_column,
                        "tag_value": tag_value,
                        "source": rule.get("source", derived_source),
                        "rule_id": rule.get("id"),
                    }
                    for key_field in key_field_names:
                        row_out[key_field] = ""
                    for match_field in match_field_names:
                        row_out[match_field] = ""
                    for idx, key_val in enumerate(key_tuple):
                        if idx < len(key_field_names):
                            row_out[key_field_names[idx]] = key_val
                    for idx, match_val in enumerate(match_tuple):
                        if idx < len(match_field_names):
                            row_out[match_field_names[idx]] = match_val
                    tag_records.append(row_out)

                    if derived_first_match:
                        break

                if not matched_any:
                    continue

    output_df = collapse_tag_records(tag_records, match_field_names, output_tag_columns, key_delimiter)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_df.to_csv(output_path, index=False)
    logger.info("Wrote %s rows to %s", len(output_df), output_path)

    return output_df


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    opt = parse_args(argv)
    logger.info("VTX_ROOT=%s", VTX_ROOT)
    logger.info("Config=%s", opt.config_path)

    cfg = load_yaml(opt.config_path)
    payload = cfg.get("payload") if isinstance(cfg.get("payload"), dict) else {}
    jobs = payload.get("jobs", []) or cfg.get("jobs", []) or []
    if not isinstance(jobs, list):
        raise ValueError("Config 'jobs' must be a list")

    if opt.job_id:
        jobs = [j for j in jobs if j.get("id") == opt.job_id]
        if not jobs:
            raise ValueError(f"Job id not found: {opt.job_id}")

    for job in jobs:
        if not job.get("enabled", True):
            continue
        run_job(job)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
