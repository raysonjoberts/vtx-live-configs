#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
master_view_builder_vtx.py
--------------------------
Build CSV views from master tables with optional deterministic collapse.

Supports:
- source master table selection (applications/servers)
- field projection in YAML order (default all)
- optional anchor collapse with fallback anchor fields
- deterministic linked-field collapse that preserves row alignment
"""

from __future__ import annotations

import argparse
import logging
import os
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
DEFAULT_CONFIG_PATH = VTX_ROOT / "usr" / "config" / "run" / "s5_master_view_builder_vtx.yaml"


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
        if s.startswith("VTX/"):
            s = str(VTX_ROOT) + "/" + s[len("VTX/") :]
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


logger = get_logger("master_view_builder_vtx_v2")


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


def job_id(job: Dict[str, Any]) -> str:
    return str(job.get("id") or "").strip()


def read_table(path: Path) -> pd.DataFrame:
    suffix = str(path).lower()
    if suffix.endswith(".csv"):
        df = pd.read_csv(path, dtype=str, keep_default_na=False).fillna("")
    elif suffix.endswith(".parquet"):
        df = pd.read_parquet(path).fillna("")
    else:
        raise ValueError(f"Unsupported source format: {path}")
    for column in df.columns:
        df[column] = df[column].map(normalize)
    return df


def write_table(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = str(path).lower()
    if suffix.endswith(".csv"):
        df.to_csv(path, index=False)
        return
    raise ValueError(f"Unsupported output format: {path}")


def normalize(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.lower() in {"nan", "none", "null"} else s


TAG_META_COLUMNS = {"entity_type", "anchor", "tag_category", "tag_key", "tag_value", "tag", "source", "rule_id"}


def read_tags(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, keep_default_na=False).fillna("")
    for column in df.columns:
        df[column] = df[column].map(normalize)
    return df


def build_tag_index(
    tags_df: pd.DataFrame,
    match_columns: List[str],
) -> Tuple[List[str], Dict[str, List[Dict[str, Any]]]]:
    value_columns = [c for c in tags_df.columns if c not in ({"anchor"} | set(match_columns) | TAG_META_COLUMNS)]
    by_anchor: Dict[str, List[Dict[str, Any]]] = {}
    for _, row in tags_df.iterrows():
        anchor = normalize(row.get("anchor", ""))
        if anchor == "":
            continue
        key_map = {col: normalize(row.get(col, "")) for col in match_columns}
        value_map = {col: normalize(row.get(col, "")) for col in value_columns}
        entry = {
            "keys": key_map,
            "values": value_map,
        }
        by_anchor.setdefault(anchor, []).append(entry)
    return value_columns, by_anchor


def apply_tags(
    df: pd.DataFrame,
    tag_path: Path,
    anchor_field: str,
    match_columns: List[str],
) -> pd.DataFrame:
    tags_df = read_tags(tag_path)
    if "anchor" not in tags_df.columns:
        raise ValueError(f"tag_source_table is missing required column 'anchor': {tag_path}")

    missing_tag_columns = [col for col in match_columns if col not in tags_df.columns]
    if missing_tag_columns:
        raise ValueError(f"tag_source_table is missing tag_match_columns {missing_tag_columns}: {tag_path}")

    missing_view_columns = [col for col in match_columns if col not in df.columns]
    if missing_view_columns:
        raise ValueError(f"tag_match_columns not found in view columns: {missing_view_columns}")

    value_columns, tags_by_anchor = build_tag_index(tags_df, match_columns)

    def row_tag_values(row: pd.Series) -> Dict[str, str]:
        row_out = {col: "" for col in value_columns}
        if not value_columns:
            return row_out

        anchor_val = normalize(row.get(anchor_field, ""))
        if anchor_val == "":
            return row_out
        candidates = tags_by_anchor.get(anchor_val, [])
        if not candidates:
            return row_out
        collected: Dict[str, List[str]] = {col: [] for col in value_columns}
        for entry in candidates:
            matched = True
            for key_col in match_columns:
                key_val = entry["keys"].get(key_col, "")
                if key_val == "":
                    continue
                if normalize(row.get(key_col, "")) != key_val:
                    matched = False
                    break
            if matched:
                for value_col in value_columns:
                    value = entry["values"].get(value_col, "")
                    if value and value not in collected[value_col]:
                        collected[value_col].append(value)
        for value_col in value_columns:
            row_out[value_col] = ";".join(collected[value_col])
        return row_out

    tag_values_df = df.apply(row_tag_values, axis=1, result_type="expand")
    for value_col in value_columns:
        df[value_col] = tag_values_df[value_col]
    return df


def collapse_scalar(values: List[str], delimiter: str) -> str:
    vals = [normalize(v) for v in values]
    if not vals:
        return ""
    if all(v == vals[0] for v in vals):
        return vals[0]
    return delimiter.join(vals)


def collapse_linked_group(rows: pd.DataFrame, fields: List[str], delimiter: str) -> Dict[str, str]:
    tuples: List[Tuple[str, ...]] = []
    for _, row in rows.iterrows():
        tuples.append(tuple(normalize(row.get(field, "")) for field in fields))
    if not tuples:
        ordered: List[Tuple[str, ...]] = []
    elif all(item == tuples[0] for item in tuples):
        ordered = [tuples[0]]
    else:
        ordered = tuples

    output: Dict[str, str] = {}
    for index, field in enumerate(fields):
        output[field] = delimiter.join(item[index] for item in ordered) if ordered else ""
    return output


def split_semicolon(value: Any, delimiter: str) -> List[str]:
    s = normalize(value)
    if s == "":
        return []
    return [normalize(v) for v in s.split(delimiter)]


def expand_view(
    df: pd.DataFrame,
    anchor_fields: List[str],
    expand_by: str,
    linked_groups: List[List[str]],
    delimiter: str,
) -> pd.DataFrame:
    if expand_by not in df.columns:
        raise ValueError(f"expand_by field not found in source columns: {expand_by}")

    group_map: Dict[str, int] = {}
    resolved_groups: List[List[str]] = []
    expand_group_index: Optional[int] = None
    for group in linked_groups:
        present = [field for field in group if field in df.columns]
        if len(present) < 2:
            continue
        group_index = len(resolved_groups)
        resolved_groups.append(present)
        for field in present:
            group_map[field] = group_index
        if expand_by in present and expand_group_index is None:
            expand_group_index = group_index

    if expand_group_index is None:
        raise ValueError("expand_by must be included in a linked_field_group")

    rows_out: List[Dict[str, str]] = []
    key_group = resolved_groups[expand_group_index]

    for _, row in df.iterrows():
        key_values_raw = split_semicolon(row.get(expand_by, ""), delimiter)
        if not key_values_raw:
            record = {field: normalize(row.get(field, "")) for field in df.columns}
            rows_out.append(record)
            continue

        max_len = 0
        arrays: Dict[str, List[str]] = {}
        for group in resolved_groups:
            for field in group:
                values = split_semicolon(row.get(field, ""), delimiter)
                arrays[field] = values
                if field in key_group:
                    max_len = max(max_len, len(values))

        if max_len == 0:
            record = {field: normalize(row.get(field, "")) for field in df.columns}
            rows_out.append(record)
            continue

        for field in arrays:
            values = arrays[field]
            if len(values) < max_len:
                arrays[field] = values + [""] * (max_len - len(values))

        for idx in range(max_len):
            record: Dict[str, str] = {}
            for field in anchor_fields:
                record[field] = normalize(row.get(field, ""))

            for group in resolved_groups:
                for field in group:
                    record[field] = arrays[field][idx] if idx < len(arrays[field]) else ""

            for field in df.columns:
                if field in anchor_fields:
                    continue
                if field in group_map:
                    continue
                if field not in record:
                    record[field] = normalize(row.get(field, ""))
            rows_out.append(record)

    out_df = pd.DataFrame(rows_out)
    ordered_columns = [column for column in df.columns if column in out_df.columns]
    for column in out_df.columns:
        if column not in ordered_columns:
            ordered_columns.append(column)
    out_df = out_df[ordered_columns]
    return collapse_expanded_view(out_df, anchor_fields, expand_by, resolved_groups, delimiter)


def collapse_expanded_view(
    df: pd.DataFrame,
    anchor_fields: List[str],
    expand_by: str,
    linked_groups: List[List[str]],
    delimiter: str,
) -> pd.DataFrame:
    group_fields = [field for field in anchor_fields + [expand_by] if field in df.columns]
    if not group_fields:
        return df

    group_map: Dict[str, int] = {}
    for group_index, group in enumerate(linked_groups):
        for field in group:
            if field in df.columns:
                group_map[field] = group_index

    rows_out: List[Dict[str, str]] = []
    groupby_key: List[str] | str = group_fields[0] if len(group_fields) == 1 else group_fields
    for key, subframe in df.groupby(groupby_key, sort=False, dropna=False):
        key_values = [normalize(key)] if not isinstance(key, tuple) else [normalize(value) for value in key]
        record = {field: key_values[idx] for idx, field in enumerate(group_fields)}
        handled_groups: set[int] = set()

        for column in df.columns:
            if column in group_fields:
                continue
            if column in group_map:
                group_index = group_map[column]
                if group_index in handled_groups:
                    continue
                for linked_field in linked_groups[group_index]:
                    if linked_field in group_fields or linked_field not in df.columns:
                        continue
                    record[linked_field] = collapse_scalar(subframe[linked_field].tolist(), delimiter)
                handled_groups.add(group_index)
                continue
            record[column] = collapse_scalar(subframe[column].tolist(), delimiter)
        rows_out.append(record)

    out_df = pd.DataFrame(rows_out)
    ordered_columns = [column for column in df.columns if column in out_df.columns]
    for column in out_df.columns:
        if column not in ordered_columns:
            ordered_columns.append(column)
    return out_df[ordered_columns]


def collapse_view(
    df: pd.DataFrame,
    anchor_fields: List[str],
    order_fields: List[str],
    linked_groups: List[List[str]],
    delimiter: str,
) -> pd.DataFrame:
    missing_anchor = [field for field in anchor_fields if field not in df.columns]
    if missing_anchor:
        raise ValueError(f"collapse anchor fields not found: {missing_anchor}")

    sort_fields = [field for field in order_fields if field in df.columns]
    if sort_fields:
        df = df.sort_values(by=sort_fields, kind="mergesort")

    group_map: Dict[str, int] = {}
    resolved_groups: List[List[str]] = []
    for group in linked_groups:
        present = [field for field in group if field in df.columns]
        if len(present) < 2:
            continue
        group_index = len(resolved_groups)
        resolved_groups.append(present)
        for field in present:
            group_map[field] = group_index

    rows_out: List[Dict[str, str]] = []
    groupby_key: List[str] | str
    if len(anchor_fields) == 1:
        groupby_key = anchor_fields[0]
    else:
        groupby_key = anchor_fields

    for anchor, subframe in df.groupby(groupby_key, sort=False, dropna=False):
        if isinstance(anchor, tuple):
            anchor_values = [normalize(v) for v in anchor]
        else:
            anchor_values = [normalize(anchor)]
        record: Dict[str, str] = {
            field: anchor_values[idx]
            for idx, field in enumerate(anchor_fields)
        }
        handled_groups: set[int] = set()
        for column in df.columns:
            if column in anchor_fields:
                continue
            if column in group_map:
                group_index = group_map[column]
                if group_index in handled_groups:
                    continue
                record.update(collapse_linked_group(subframe, resolved_groups[group_index], delimiter))
                handled_groups.add(group_index)
                continue
            record[column] = collapse_scalar(subframe[column].tolist(), delimiter)
        rows_out.append(record)

    out_df = pd.DataFrame(rows_out)
    ordered_columns = [column for column in df.columns if column in out_df.columns]
    for column in out_df.columns:
        if column not in ordered_columns:
            ordered_columns.append(column)
    return out_df[ordered_columns]


def resolve_anchor_fields(collapse_cfg: Dict[str, Any], df: pd.DataFrame) -> List[str]:
    preferred_fields: List[str] = []
    anchor_fields = collapse_cfg.get("anchor_fields")
    if isinstance(anchor_fields, list):
        preferred_fields.extend(str(value).strip() for value in anchor_fields if str(value).strip())

    anchor_field = str(collapse_cfg.get("anchor_field") or "").strip()
    if anchor_field:
        preferred_fields.append(anchor_field)

    resolved: List[str] = []
    for candidate in preferred_fields:
        if candidate in df.columns and candidate not in resolved:
            resolved.append(candidate)
    if resolved:
        return resolved
    raise ValueError(f"collapse anchor field(s) not found in source columns: {preferred_fields}")


@dataclass
class Options:
    config_path: Path
    job: Optional[str]


def parse_args(argv: Optional[List[str]] = None) -> Options:
    parser = argparse.ArgumentParser(description="Build CSV views from master tables")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--job", default=None)
    args = parser.parse_args(argv)
    return Options(config_path=vtx_path(args.config, must_exist=True), job=args.job)


def process_job(job: Dict[str, Any]) -> int:
    jid = job_id(job) or "job"

    source = str(job.get("source_table") or "").strip()
    if not source:
        inputs = job.get("inputs") if isinstance(job.get("inputs"), list) else []
        source = str(inputs[0]).strip() if inputs else ""
    src_path = vtx_path(source, must_exist=True)

    outputs = job.get("outputs") if isinstance(job.get("outputs"), dict) else {}
    out_csv_raw = str(outputs.get("csv") or "").strip()
    if not out_csv_raw:
        raise ValueError(f"[{jid}] csv output is required")

    fields = job.get("fields")
    if not isinstance(fields, list) or not fields:
        fields = job.get("select_fields") if isinstance(job.get("select_fields"), list) else []

    source_df = read_table(src_path)
    selected_fields = list(fields) if fields else list(source_df.columns)
    collapse_cfg = job.get("collapse") if isinstance(job.get("collapse"), dict) else {}
    if collapse_cfg.get("enabled", False):
        delimiter = str(collapse_cfg.get("delimiter") or ";")
        mode = str(collapse_cfg.get("mode") or "collapse").strip().lower()
        order_fields = [str(value).strip() for value in (collapse_cfg.get("order_fields") or []) if str(value).strip()]
        linked_groups: List[List[str]] = []
        for group in (collapse_cfg.get("linked_field_groups") or []):
            if isinstance(group, list):
                linked_groups.append([str(value).strip() for value in group if str(value).strip()])

        helper_fields: List[str] = []
        anchor_fields = collapse_cfg.get("anchor_fields")
        if isinstance(anchor_fields, list):
            helper_fields.extend(str(value).strip() for value in anchor_fields if str(value).strip())
        anchor_field = str(collapse_cfg.get("anchor_field") or "").strip()
        if anchor_field:
            helper_fields.append(anchor_field)
        helper_fields.extend(order_fields)
        for group in linked_groups:
            helper_fields.extend(group)

        working_fields: List[str] = []
        for field in selected_fields + helper_fields:
            if field in source_df.columns and field not in working_fields:
                working_fields.append(field)

        df = source_df[working_fields].copy()
        anchors = resolve_anchor_fields(collapse_cfg, df)
        if mode == "expand":
            expand_by = str(collapse_cfg.get("expand_by") or "").strip()
            if not expand_by:
                raise ValueError(f"[{jid}] expand_by is required when collapse.mode=expand")
            df = expand_view(df, anchors, expand_by, linked_groups, delimiter)
            logger.info(
                "expand_complete,id=%s,anchors=%s,expand_by=%s,rows=%d,cols=%d",
                jid,
                anchors,
                expand_by,
                len(df),
                len(df.columns),
            )
        else:
            df = collapse_view(df, anchors, order_fields, linked_groups, delimiter)
            logger.info("collapse_complete,id=%s,anchors=%s,rows=%d,cols=%d", jid, anchors, len(df), len(df.columns))
        df = df[[field for field in selected_fields if field in df.columns]]
    else:
        missing = [field for field in selected_fields if field not in source_df.columns]
        if missing:
            raise ValueError(f"[{jid}] missing fields in source: {missing}")
        df = source_df[selected_fields].copy()

    tag_source = str(job.get("tag_source_table") or "").strip()
    if tag_source:
        tag_anchor = str(job.get("tag_anchor_field") or "").strip()
        if not tag_anchor:
            raise ValueError(f"[{jid}] tag_anchor_field is required when tag_source_table is set")
        if tag_anchor not in df.columns:
            raise ValueError(f"[{jid}] tag_anchor_field not found in view columns: {tag_anchor}")
        tag_match_columns = [
            str(value).strip()
            for value in (job.get("tag_match_columns") or [])
            if str(value).strip()
        ]
        tag_path = vtx_path(tag_source, must_exist=True)
        df = apply_tags(df, tag_path, tag_anchor, tag_match_columns)

    out_csv = vtx_path(out_csv_raw)
    write_table(df, out_csv)
    logger.info("wrote_csv,id=%s,path=%s,rows=%d,cols=%d", jid, out_csv, len(df), len(df.columns))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    options = parse_args(argv)
    config = load_yaml(options.config_path)
    jobs = extract_jobs(config)
    if not jobs:
        logger.error("No jobs defined")
        return 1

    selected = jobs
    if options.job:
        selected = [job for job in jobs if job_id(job) == options.job]
        if not selected:
            logger.error("job_not_found,wanted=%s", options.job)
            return 2

    for job in selected:
        if job.get("enabled", True) is False:
            continue
        process_job(job)

    print("[master_view_builder_vtx] Complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
