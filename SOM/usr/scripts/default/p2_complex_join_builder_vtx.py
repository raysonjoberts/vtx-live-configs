#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
complex_join_builder_vtx.py
---------------------------
Staged table builder with composite join-key support.

Stages per job:
1) load base canonical table (raw_application_table/raw_server_table)
2) apply configured joins/additive fields (default include all fields)
3) apply inferred decisions with iterative fixpoint loop
4) apply coalesce with non-null precedence
5) write outputs (parquet required, csv optional)

Schema contracts supported:
- raw_application_table.parquet
- raw_server_table.parquet
- application_approximation.parquet
- master_applications.parquet
- master_servers.parquet
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import yaml


def resolve_vtx_root() -> Path:
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


VTX_ROOT = resolve_vtx_root()
DEFAULT_CONFIG_PATH = VTX_ROOT / "usr" / "config" / "run" / "p2_complex_join_builder_vtx.yaml"


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


logger = get_logger("complex_join_builder_vtx")


def load_yaml(path: Path) -> Dict[str, Any]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = doc.get("config") if isinstance(doc.get("config"), dict) else doc
    if not isinstance(cfg, dict):
        raise ValueError(f"Invalid config root: {path}")
    return cfg


def extract_jobs(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    payload = cfg.get("payload") if isinstance(cfg.get("payload"), dict) else {}
    jobs = payload.get("jobs") if isinstance(payload.get("jobs"), list) else cfg.get("jobs")
    return [j for j in (jobs or []) if isinstance(j, dict)]


def read_table(path: Path) -> pd.DataFrame:
    s = str(path).lower()
    if s.endswith(".csv"):
        df = pd.read_csv(path, dtype=str, keep_default_na=False).fillna("")
    elif s.endswith(".tsv"):
        df = pd.read_csv(path, sep="\t", dtype=str, keep_default_na=False).fillna("")
    elif s.endswith(".parquet"):
        df = pd.read_parquet(path).fillna("")
    else:
        raise ValueError(f"Unsupported source format: {path}")
    for c in df.columns:
        df[c] = df[c].map(normalize_scalar)
    return df


def normalize_scalar(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    if text.endswith(".0"):
        try:
            n = float(text)
            if n.is_integer():
                return str(int(n))
        except Exception:
            pass
    return text


def write_table(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if str(path).lower().endswith(".csv"):
        df.to_csv(path, index=False)
    elif str(path).lower().endswith(".parquet"):
        df.to_parquet(path, index=False)
    else:
        raise ValueError(f"Unsupported output format: {path}")


def split_semicolon_keep_blanks(v: Any, delimiter: str = ";") -> List[str]:
    s = "" if v is None else str(v)
    if s == "":
        return []
    return [x.strip() for x in s.split(delimiter)]


def join_semicolon(values: List[str]) -> str:
    return ";".join(values)


_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _num(v: Any) -> Optional[float]:
    s = "" if v is None else str(v).strip()
    if not s or not _NUM_RE.match(s):
        return None
    try:
        return float(s)
    except Exception:
        return None


def eval_condition(value: Any, op: str, target: Any) -> bool:
    op = (op or "").strip()
    if op == "=":
        op = "=="

    sval = "" if value is None else str(value)
    stgt = "" if target is None else str(target)

    if op == "isnull":
        return sval.strip() == ""
    if op == "isnotnull":
        return sval.strip() != ""
    if op == "regex":
        try:
            return re.search(stgt, sval) is not None
        except re.error:
            return False

    ln = _num(sval)
    rn = _num(stgt)
    if ln is not None and rn is not None and op in ("<", ">", "<=", ">="):
        if op == "<":
            return ln < rn
        if op == ">":
            return ln > rn
        if op == "<=":
            return ln <= rn
        if op == ">=":
            return ln >= rn

    if op == "==":
        return sval == stgt
    if op == "!=":
        return sval != stgt
    if op == "<":
        return sval < stgt
    if op == ">":
        return sval > stgt
    if op == "<=":
        return sval <= stgt
    if op == ">=":
        return sval >= stgt
    return False


def _normalize_key_list(value: Any, delimiter: str) -> List[str]:
    return [normalize_scalar(v) for v in split_semicolon_keep_blanks(value, delimiter=delimiter)]


def normalize_case(value: str) -> str:
    return value.lower()


def force_short_hostname(value: str) -> str:
    return value.split(".", 1)[0] if "." in value else value


JOIN_NORMALIZERS = {
    "normalize_case": normalize_case,
    "force_short_hostname": force_short_hostname,
}


def parse_join_normalizations(normalizations: Any) -> List[str]:
    if not isinstance(normalizations, list):
        return []

    ordered: List[str] = []
    for item in normalizations:
        if isinstance(item, str):
            name = item.strip()
            if name in JOIN_NORMALIZERS:
                ordered.append(name)
            continue
        if not isinstance(item, dict):
            continue
        for name, enabled in item.items():
            if name in JOIN_NORMALIZERS and bool(enabled):
                ordered.append(name)
    return ordered


def apply_join_normalizations(value: Any, ordered_normalizations: List[str]) -> str:
    normalized = normalize_scalar(value)
    for name in ordered_normalizations:
        func = JOIN_NORMALIZERS.get(name)
        if func:
            normalized = func(normalized)
    return normalized


def _filter_value_list(values: Any) -> List[str]:
    if isinstance(values, list):
        return [normalize_scalar(v) for v in values if normalize_scalar(v)]
    if values is None:
        return []
    normalized = normalize_scalar(values)
    return [normalized] if normalized else []


def evaluate_filter_condition(row_value: Any, operator: str, value: Any = None, values: Any = None) -> bool:
    op = (operator or "").strip().lower()
    source_value = normalize_scalar(row_value)
    if op in {"=", "==", "!=", "<", ">", "<=", ">=", "isnull", "isnotnull", "regex"}:
        return eval_condition(source_value, op, value)
    if op == "in":
        candidates = _filter_value_list(values if values is not None else value)
        return source_value in candidates
    if op == "not_in":
        candidates = _filter_value_list(values if values is not None else value)
        return source_value not in candidates
    return False


def format_join_filters(filters: Any) -> str:
    if not isinstance(filters, list) or not filters:
        return "[]"
    parts: List[str] = []
    for item in filters:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip()
        operator = str(item.get("operator") or "").strip()
        values = item.get("values")
        value = item.get("value")
        if isinstance(values, list):
            rendered = "[" + ", ".join(normalize_scalar(v) for v in values) + "]"
        else:
            rendered = normalize_scalar(value)
        parts.append(f"{field} {operator} {rendered}".strip())
    return "[" + "; ".join(parts) + "]"


def apply_table_filters(
    df: pd.DataFrame,
    filters: Any,
    *,
    alias: str,
    side: str,
) -> Tuple[pd.DataFrame, pd.Series]:
    if not isinstance(filters, list) or not filters:
        mask = pd.Series([True] * len(df), index=df.index, dtype=bool)
        return df, mask

    mask = pd.Series([True] * len(df), index=df.index, dtype=bool)
    active_filters: List[str] = []
    for item in filters:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip()
        operator = str(item.get("operator") or "").strip()
        if not field or not operator:
            continue
        resolved_field = resolve_column_name(list(df.columns), field)
        if not resolved_field:
            logger.warning(
                "join_filter_field_missing,alias=%s,side=%s,field=%s",
                alias,
                side,
                field,
            )
            mask &= False
            continue
        active_filters.append(field)
        value = item.get("value")
        values = item.get("values")
        condition_mask = df[resolved_field].map(
            lambda row_value: evaluate_filter_condition(row_value, operator, value, values)
        )
        mask &= condition_mask.astype(bool)

    filtered = df[mask].copy()
    logger.info(
        "join_filter_applied,alias=%s,side=%s,original_rows=%d,filtered_rows=%d,filters=%s",
        alias,
        side,
        len(df),
        len(filtered),
        format_join_filters(filters),
    )
    return filtered, mask


def resolve_column_name(columns: List[str], wanted: str) -> Optional[str]:
    if wanted in columns:
        return wanted
    wanted_lc = wanted.strip().lower()
    for column in columns:
        if column.strip().lower() == wanted_lc:
            return column
    return None


def parse_join_include_fields(
    include_fields: Any,
    r_df: pd.DataFrame,
    right_keys: List[str],
) -> Tuple[List[str], Dict[str, str]]:
    right_key_lc = {key.strip().lower() for key in right_keys}
    resolved_fields: List[str] = []
    output_names: Dict[str, str] = {}

    def add_field(source_raw: Any, output_raw: Any = None) -> None:
        source_name = str(source_raw or "").strip()
        if not source_name or source_name.lower() in right_key_lc:
            return
        resolved = resolve_column_name(list(r_df.columns), source_name)
        if not resolved:
            logger.warning("join_include_field_missing,source_field=%s", source_name)
            return
        output_name = str(output_raw or "").strip() or resolved
        if not output_name:
            output_name = resolved
        if resolved not in resolved_fields:
            resolved_fields.append(resolved)
        output_names[resolved] = output_name

    if not include_fields or include_fields == "*":
        for column in r_df.columns:
            if column.strip().lower() not in right_key_lc:
                add_field(column, column)
        return resolved_fields, output_names

    if isinstance(include_fields, dict):
        for source, output in include_fields.items():
            add_field(source, output)
        return resolved_fields, output_names

    if isinstance(include_fields, list):
        for item in include_fields:
            if isinstance(item, dict):
                source = (
                    item.get("include_field")
                    or item.get("source_field")
                    or item.get("field")
                    or item.get("name")
                )
                output = item.get("output_field") or item.get("as") or item.get("target_field")
                add_field(source, output)
            else:
                add_field(item)
        return resolved_fields, output_names

    add_field(include_fields)
    return resolved_fields, output_names


NO_MATCH_KEY = "__VTX_COMPLEX_JOIN_NO_MATCH__"
VALID_UNMAPPED_VALUE_BEHAVIORS = {"use_original", "no_match"}


def normalize_key_value(value: Any, ordered_normalizations: List[str]) -> str:
    return apply_join_normalizations(value, ordered_normalizations)


def normalize_value_map(value_map: Any, ordered_normalizations: List[str], alias: str, left_key: str, right_key: str) -> Dict[str, str]:
    if value_map is None:
        return {}
    if not isinstance(value_map, dict):
        raise ValueError(
            f"[{alias}] invalid value_map for {left_key}->{right_key}: expected mapping/dict"
        )
    normalized_map: Dict[str, str] = {}
    for raw_left, raw_right in value_map.items():
        left_value = normalize_key_value(raw_left, ordered_normalizations)
        right_value = normalize_key_value(raw_right, ordered_normalizations)
        if not left_value:
            continue
        if left_value in normalized_map and normalized_map[left_value] != right_value:
            raise ValueError(
                f"[{alias}] contradictory value_map for {left_key}->{right_key}: {raw_left}"
            )
        normalized_map[left_value] = right_value
    return normalized_map


def parse_match_keys(join_cfg: Dict[str, Any], alias: str) -> List[Dict[str, Any]]:
    raw_match_keys = join_cfg.get("match_keys")
    if raw_match_keys is None:
        raise ValueError(f"[{alias}] missing match_keys")

    if not isinstance(raw_match_keys, list) or not raw_match_keys:
        raise ValueError(f"[{alias}] missing or empty match_keys")

    parsed: List[Dict[str, Any]] = []
    seen_pairs = set()
    left_to_right: Dict[str, str] = {}
    right_to_left: Dict[str, str] = {}
    for index, item in enumerate(raw_match_keys, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"[{alias}] invalid match_keys[{index}]: expected mapping/dict")
        left_key = str(item.get("left_key") or "").strip()
        right_key = str(item.get("right_key") or "").strip()
        if not left_key:
            raise ValueError(f"[{alias}] match_keys[{index}] missing left_key")
        if not right_key:
            raise ValueError(f"[{alias}] match_keys[{index}] missing right_key")

        pair = (left_key.lower(), right_key.lower())
        if pair in seen_pairs:
            raise ValueError(f"[{alias}] duplicate match key pair: {left_key}->{right_key}")
        seen_pairs.add(pair)

        previous_right = left_to_right.get(left_key.lower())
        if previous_right and previous_right != right_key.lower():
            raise ValueError(f"[{alias}] contradictory match key definitions for left_key={left_key}")
        left_to_right[left_key.lower()] = right_key.lower()

        previous_left = right_to_left.get(right_key.lower())
        if previous_left and previous_left != left_key.lower():
            raise ValueError(f"[{alias}] contradictory match key definitions for right_key={right_key}")
        right_to_left[right_key.lower()] = left_key.lower()

        unmapped_behavior = str(item.get("unmapped_value_behavior") or "use_original").strip().lower()
        if unmapped_behavior not in VALID_UNMAPPED_VALUE_BEHAVIORS:
            raise ValueError(
                f"[{alias}] invalid unmapped_value_behavior for {left_key}->{right_key}: {unmapped_behavior}"
            )
        normalizations = parse_join_normalizations(item.get("normalizations", join_cfg.get("normalizations")))
        parsed.append(
            {
                "left_key": left_key,
                "right_key": right_key,
                "normalizations": normalizations,
                "value_map": normalize_value_map(item.get("value_map"), normalizations, alias, left_key, right_key),
                "unmapped_value_behavior": unmapped_behavior,
            }
        )
    return parsed


def resolve_match_key_columns(
    match_keys: List[Dict[str, Any]],
    left_columns: List[str],
    right_columns: List[str],
    alias: str,
) -> List[Dict[str, Any]]:
    resolved: List[Dict[str, Any]] = []
    for item in match_keys:
        left_key = item["left_key"]
        right_key = item["right_key"]
        resolved_left = resolve_column_name(left_columns, left_key)
        if not resolved_left:
            raise ValueError(f"[{alias}] configured left-side key column does not exist: {left_key}")
        resolved_right = resolve_column_name(right_columns, right_key)
        if not resolved_right:
            raise ValueError(f"[{alias}] configured right-side key column does not exist: {right_key}")
        resolved_item = dict(item)
        resolved_item["resolved_left_key"] = resolved_left
        resolved_item["resolved_right_key"] = resolved_right
        resolved.append(resolved_item)
    return resolved


def expand_key_values(row: pd.Series, column: str, delimiter: str) -> List[str]:
    values = split_semicolon_keep_blanks(row.get(column, ""), delimiter=delimiter)
    return values if values else [""]


def build_composite_keys(row: pd.Series, match_keys: List[Dict[str, Any]], delimiter: str, *, side: str) -> List[Tuple[str, ...]]:
    value_lists: List[List[str]] = []
    max_count = 1
    for item in match_keys:
        column = item["resolved_right_key"] if side == "right" else item["resolved_left_key"]
        values = expand_key_values(row, column, delimiter)
        normalized_values: List[str] = []
        for value in values:
            normalized = normalize_key_value(value, item["normalizations"])
            if side == "left" and item["value_map"]:
                if normalized in item["value_map"]:
                    normalized = item["value_map"][normalized]
                elif item["unmapped_value_behavior"] == "no_match":
                    normalized = NO_MATCH_KEY
            normalized_values.append(normalized)
        value_lists.append(normalized_values)
        max_count = max(max_count, len(normalized_values))

    keys: List[Tuple[str, ...]] = []
    for index in range(max_count):
        parts: List[str] = []
        for values in value_lists:
            if len(values) == 1:
                parts.append(values[0])
            elif index < len(values):
                parts.append(values[index])
            else:
                parts.append("")
        if not all(parts) or NO_MATCH_KEY in parts:
            continue
        keys.append(tuple(parts))
    return keys


def format_match_keys(match_keys: List[Dict[str, Any]]) -> str:
    parts = []
    for item in match_keys:
        details = f"{item['resolved_left_key']}->{item['resolved_right_key']}"
        if item["normalizations"]:
            details += f",normalizations={item['normalizations']}"
        if item["value_map"]:
            details += f",value_map_count={len(item['value_map'])},unmapped={item['unmapped_value_behavior']}"
        parts.append(details)
    return "[" + "; ".join(parts) + "]"


def _build_join_index(
    r_df: pd.DataFrame,
    match_keys: List[Dict[str, Any]],
    include_fields: List[str],
    delimiter: str,
) -> Dict[Tuple[str, ...], Dict[str, List[str]]]:
    idx: Dict[Tuple[str, ...], Dict[str, List[str]]] = {}
    for _, r in r_df.iterrows():
        keys = build_composite_keys(r, match_keys, delimiter, side="right")
        if not keys:
            continue
        for key in keys:
            bucket = idx.setdefault(key, {})
            for c in include_fields:
                bucket.setdefault(c, []).append(normalize_scalar(r.get(c, "")))
    return idx


def apply_joins(base: pd.DataFrame, joins: List[Dict[str, Any]]) -> pd.DataFrame:
    out = base.copy()
    for j in joins:
        alias = str(j.get("alias") or "join").strip()
        path = vtx_path(str(j.get("path") or ""), must_exist=True)
        delimiter = str(j.get("delimiter") or ";")
        include_fields = j.get("include_fields")
        left_table_filters = j.get("left_table_filters")
        right_table_filters = j.get("right_table_filters")

        match_keys = parse_match_keys(j, alias)
        r_df = read_table(path)
        match_keys = resolve_match_key_columns(match_keys, list(out.columns), list(r_df.columns), alias)

        filtered_right_df, _ = apply_table_filters(r_df, right_table_filters, alias=alias, side="right")
        eligible_left_df, eligible_left_mask = apply_table_filters(out, left_table_filters, alias=alias, side="left")

        right_key_columns = [item["resolved_right_key"] for item in match_keys]
        inc, configured_output_names = parse_join_include_fields(include_fields, filtered_right_df, right_key_columns)

        output_names: Dict[str, str] = {}
        existing_cols = set(out.columns)
        for c in inc:
            out_name = configured_output_names.get(c) or c
            if out_name in existing_cols:
                out_name = f"{alias}_{out_name}"
            output_names[c] = out_name
            if out_name not in out.columns:
                out[out_name] = ""

        idx = _build_join_index(filtered_right_df, match_keys, inc, delimiter)

        new_rows: List[Dict[str, Any]] = []
        successful_matches = 0
        unmatched_eligible_rows = 0
        for _, row in out.iterrows():
            if not bool(eligible_left_mask.get(row.name, True)):
                rec = row.to_dict()
                for c in inc:
                    rec[output_names[c]] = rec.get(output_names[c], "")
                new_rows.append(rec)
                continue

            key_list = build_composite_keys(row, match_keys, delimiter, side="left")
            if not key_list:
                rec = row.to_dict()
                for c in inc:
                    rec[output_names[c]] = rec.get(output_names[c], "")
                new_rows.append(rec)
                unmatched_eligible_rows += 1
                continue

            rec = row.to_dict()
            for c in inc:
                rec[output_names[c]] = ""

            row_matched = False
            for c in inc:
                values: List[str] = []
                for key in key_list:
                    if not key:
                        values.append("")
                        continue
                    field_values = idx.get(key, {}).get(c, [])
                    if not field_values:
                        values.append("")
                    elif len(field_values) == 1:
                        values.append(field_values[0])
                        if field_values[0]:
                            row_matched = True
                    else:
                        values.append(join_semicolon(field_values))
                        if any(v for v in field_values):
                            row_matched = True
                rec[output_names[c]] = join_semicolon(values)

            if row_matched:
                successful_matches += 1
            else:
                unmatched_eligible_rows += 1
            new_rows.append(rec)

        out = pd.DataFrame(new_rows)
        value_maps_applied = any(bool(item["value_map"]) for item in match_keys)
        logger.info(
            "stage_join_complete,alias=%s,key_pairs=%s,source_rows=%d,right_rows=%d,rows=%d,cols=%d,successful_matches=%d,unmatched_eligible_rows=%d,value_maps_applied=%s,right_filters=%s,left_filters=%s",
            alias,
            format_match_keys(match_keys),
            len(base),
            len(filtered_right_df),
            len(out),
            len(out.columns),
            successful_matches,
            unmatched_eligible_rows,
            value_maps_applied,
            format_join_filters(right_table_filters),
            format_join_filters(left_table_filters),
        )
    return out


def parse_decision_rules(path: Path, required_status: str) -> Dict[str, List[Dict[str, Any]]]:
    df = pd.read_csv(path, dtype=str).fillna("")
    needed = ["id", "condition", "input_field", "operator", "value", "output_field", "set", "status"]
    miss = [c for c in needed if c not in df.columns]
    if miss:
        raise ValueError(f"Decision csv missing columns: {miss}")

    req = required_status.strip().lower()
    if req:
        df = df[df["status"].str.strip().str.lower() == req].copy()

    grouped: Dict[str, List[Dict[str, Any]]] = {}

    def key_sort(v: str) -> Tuple[int, str]:
        try:
            return (int(float(str(v).strip())), str(v).strip())
        except Exception:
            return (10**9, str(v).strip())

    for rid, g in df.groupby("id", dropna=False):
        output_field = next((str(x).strip() for x in g["output_field"].tolist() if str(x).strip()), "")
        if not output_field:
            continue
        cond_mode = next((str(x).strip().lower() for x in g["condition"].tolist() if str(x).strip()), "all")
        if cond_mode not in {"all", "any"}:
            cond_mode = "all"
        rule_mode = next((str(x).strip().lower() for x in g.get("mode", []).tolist() if str(x).strip()), "element")
        if rule_mode in {"anchor", "group"}:
            rule_mode = "aggregate"
        if rule_mode not in {"element", "aggregate"}:
            rule_mode = "element"
        set_value = next((str(x) for x in g["set"].tolist() if str(x).strip() != ""), "")

        conds: List[Dict[str, Any]] = []
        for _, r in g.iterrows():
            field = str(r.get("input_field") or "").strip()
            op = str(r.get("operator") or "").strip()
            if not field or not op:
                continue
            conds.append({"field": field, "op": op, "value": r.get("value", "")})

        if not conds:
            continue
        grouped.setdefault(output_field, []).append(
            {
                "id": str(rid),
                "_sort": key_sort(str(rid)),
                "condition": cond_mode,
                "mode": rule_mode,
                "set": set_value,
                "conditions": conds,
            }
        )

    for k in list(grouped.keys()):
        grouped[k] = sorted(grouped[k], key=lambda x: x["_sort"])
    return grouped


def apply_inferred_decisions(df: pd.DataFrame, cfg: Dict[str, Any]) -> pd.DataFrame:
    if not cfg:
        return df
    path = vtx_path(str(cfg.get("path") or ""), must_exist=True)
    required_status = str(cfg.get("required_status") or "approved")
    max_iterations = int(cfg.get("max_iterations") or 10)

    rules_by_output = parse_decision_rules(path, required_status)
    out = df.copy()
    for of in rules_by_output.keys():
        if of not in out.columns:
            out[of] = ""

    def _field_list(row_idx: int, field: str) -> List[str]:
        if field not in out.columns:
            return []
        return split_semicolon_keep_blanks(out.at[row_idx, field], delimiter=";")

    def _any_blank(values: List[str]) -> bool:
        return any(str(v).strip() == "" for v in values)

    def _all_non_blank(values: List[str]) -> bool:
        return bool(values) and all(str(v).strip() != "" for v in values)

    def eval_aggregate(values: List[str], op: str, target: Any) -> bool:
        op = (op or "").strip().lower()
        t = "" if target is None else str(target).strip()
        value_set = {str(v).strip() for v in values if str(v).strip() != ""}
        if op in {"contains"}:
            return t in value_set
        if op in {"not_contains"}:
            return t not in value_set
        if op == "isnull":
            return _any_blank(values) or not value_set
        if op == "isnotnull":
            return _all_non_blank(values)
        return eval_condition(";".join(values), op, target)

    for it in range(1, max_iterations + 1):
        assignments = 0
        for i in range(len(out)):
            for of, rules in rules_by_output.items():
                for rule in rules:
                    mode = str(rule.get("mode") or "element").strip().lower()
                    if mode == "aggregate":
                        if str(out.at[i, of]).strip() != "":
                            continue
                        results: List[bool] = []
                        for cond in rule.get("conditions", []):
                            f = str(cond.get("field") or "").strip()
                            op = str(cond.get("op") or "").strip()
                            val = cond.get("value", "")
                            values = _field_list(i, f)
                            results.append(eval_aggregate(values, op, val))
                        ok = all(results) if str(rule.get("condition") or "all") == "all" else any(results)
                        if ok:
                            out.at[i, of] = str(rule.get("set") or "")
                            assignments += 1
                            break
                    else:
                        cond_fields = [str(c.get("field") or "").strip() for c in rule.get("conditions", [])]
                        max_len = 0
                        field_lists: Dict[str, List[str]] = {}
                        for f in cond_fields:
                            values = _field_list(i, f)
                            field_lists[f] = values
                            max_len = max(max_len, len(values))
                        if max_len == 0:
                            max_len = 1
                        out_vals = split_semicolon_keep_blanks(out.at[i, of], delimiter=";") if out.at[i, of] else []
                        if len(out_vals) < max_len:
                            out_vals = out_vals + [""] * (max_len - len(out_vals))

                        for idx in range(max_len):
                            if out_vals[idx].strip() != "":
                                continue
                            results: List[bool] = []
                            for cond in rule.get("conditions", []):
                                f = str(cond.get("field") or "").strip()
                                op = str(cond.get("op") or "").strip()
                                val = cond.get("value", "")
                                values = field_lists.get(f, [])
                                v = values[idx] if idx < len(values) else ""
                                results.append(eval_condition(v, op, val))
                            ok = all(results) if str(rule.get("condition") or "all") == "all" else any(results)
                            if ok:
                                out_vals[idx] = str(rule.get("set") or "")
                                assignments += 1

                        out.at[i, of] = ";".join(out_vals)
        logger.info("stage_inferred_iteration,iteration=%d,new_assignments=%d", it, assignments)
        if assignments == 0:
            logger.info("stage_inferred_converged,iteration=%d", it)
            break
    return out


def apply_coalesce(df: pd.DataFrame, rules: List[Dict[str, Any]]) -> pd.DataFrame:
    out = df.copy()
    for r in rules:
        target = str(r.get("target_field") or "").strip()
        sources = [str(x).strip() for x in (r.get("source_fields") or []) if str(x).strip()]
        if not target or not sources:
            continue
        if target not in out.columns:
            out[target] = ""
        for i in range(len(out)):
            cur = str(out.at[i, target]).strip()
            if cur:
                continue
            for s in sources:
                if s in out.columns:
                    val = str(out.at[i, s]).strip()
                    if val:
                        out.at[i, target] = val
                        break
    return out


@dataclass
class Options:
    config_path: Path
    job: Optional[str]


def parse_args(argv: Optional[List[str]] = None) -> Options:
    p = argparse.ArgumentParser(description="Staged complex join table builder")
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    p.add_argument("--job", default=None)
    args = p.parse_args(argv)
    return Options(config_path=vtx_path(args.config, must_exist=True), job=args.job)


def run_job(job: Dict[str, Any]) -> int:
    jid = str(job.get("id") or "job")

    base = job.get("base") if isinstance(job.get("base"), dict) else {}
    base_path = vtx_path(str(base.get("path") or ""), must_exist=True)
    anchor_field = str(base.get("anchor_field") or base.get("key_field") or "ID").strip()

    df = read_table(base_path)
    if anchor_field not in df.columns:
        raise ValueError(f"[{jid}] base anchor/key field missing: {anchor_field}")

    logger.info("stage_base_complete,id=%s,rows=%d,cols=%d,anchor=%s", jid, len(df), len(df.columns), anchor_field)

    joins = [x for x in (job.get("joins") or []) if isinstance(x, dict)]
    if joins:
        df = apply_joins(df, joins)

    dec = job.get("inferred_decisions") if isinstance(job.get("inferred_decisions"), dict) else {}
    if dec:
        df = apply_inferred_decisions(df, dec)

    coal = [x for x in (job.get("coalesce") or []) if isinstance(x, dict)]
    if coal:
        df = apply_coalesce(df, coal)
        logger.info("stage_coalesce_complete,id=%s", jid)

    outputs = job.get("outputs") if isinstance(job.get("outputs"), dict) else {}
    out_parquet_raw = str(outputs.get("parquet") or "").strip()
    out_csv_raw = str(outputs.get("csv") or "").strip()
    if not out_parquet_raw and not out_csv_raw:
        raise ValueError(f"[{jid}] no output path configured")

    out_parquet = None
    if out_parquet_raw:
        out_parquet = vtx_path(out_parquet_raw)
        write_table(df, out_parquet)
    if out_csv_raw:
        write_table(df, vtx_path(out_csv_raw))

    logger.info(
        "job_complete,id=%s,rows=%d,cols=%d,parquet=%s,csv=%s",
        jid,
        len(df),
        len(df.columns),
        out_parquet or "",
        vtx_path(out_csv_raw) if out_csv_raw else "",
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    opt = parse_args(argv)
    cfg = load_yaml(opt.config_path)
    jobs = extract_jobs(cfg)
    if not jobs:
        logger.error("no_jobs_defined,config=%s", opt.config_path)
        return 1

    selected = jobs
    if opt.job:
        selected = [j for j in jobs if str(j.get("id") or "") == opt.job]
        if not selected:
            logger.error("job_not_found,wanted=%s", opt.job)
            return 2

    for j in selected:
        if j.get("enabled", True) is False:
            continue
        run_job(j)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
