#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
application_raw_table_builder_v2.py
-----------------------------------
Build a fully expanded raw application parquet table from a table-aggregator
style YAML job definition and maintain an append-only field manifest YAML.
"""

from __future__ import annotations

import argparse
import itertools
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
import yaml


def resolve_vtx_root() -> Path:
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


VTX_ROOT = resolve_vtx_root()
DEFAULT_CONFIG_PATH = VTX_ROOT / "usr" / "config" / "run" / "table_aggregator_vtx.yaml"
DEFAULT_MANIFEST_PATH = VTX_ROOT / "usr" / "config" / "manifests" / "raw_application_table_manifest.yaml"
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_ARROW_RE = re.compile(r"\s*-\s*>\s*")
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")


def _is_windows_abs(path_str: str) -> bool:
    return bool(_WINDOWS_ABS_RE.match(path_str.strip()))


def vtx_path(path_str: str | Path, *, must_exist: bool = False) -> Path:
    if isinstance(path_str, Path):
        s = str(path_str)
    else:
        s = str(path_str).strip()
    if not s:
        raise ValueError("Empty path string")

    s = os.path.expandvars(s)
    s = os.path.expanduser(s)
    s = s.replace("VTX_ROOT" + os.sep, str(VTX_ROOT) + os.sep)
    s = s.replace("BTDM_ROOT" + os.sep, str(VTX_ROOT) + os.sep)
    s = s.replace("VTX_ROOT/", str(VTX_ROOT) + "/")
    s = s.replace("BTDM_ROOT/", str(VTX_ROOT) + "/")

    if _is_windows_abs(s) or s.startswith("/") or s.startswith("\\\\"):
        p = Path(s)
    else:
        p = (VTX_ROOT / s).resolve()
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


logger = get_logger("application_raw_table_builder_v2")


def load_yaml(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        doc = yaml.safe_load(handle) or {}
    cfg = doc.get("config") if isinstance(doc.get("config"), dict) else doc
    if not isinstance(cfg, dict):
        raise ValueError(f"Invalid config root: {path}")
    return cfg


def save_yaml(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, sort_keys=False)


def coerce_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def extract_jobs(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    payload = cfg.get("payload") if isinstance(cfg.get("payload"), dict) else {}
    jobs = payload.get("jobs") if isinstance(payload.get("jobs"), list) else cfg.get("jobs")
    return [job for job in (jobs or []) if isinstance(job, dict)]


def read_table(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        df = pd.read_csv(path, dtype=str, keep_default_na=False).fillna("")
    elif suffix == ".parquet":
        df = pd.read_parquet(path).fillna("")
    else:
        raise ValueError(f"Unsupported source format: {path}")
    df.columns = [str(col).strip() for col in df.columns]
    for col in df.columns:
        df[col] = df[col].map(lambda v: normalize_key(v, case_insensitive=False))
    return df


def parse_field_spec(item: str) -> Tuple[str, str]:
    source, output = None, None
    if "->" in item or _ARROW_RE.search(item):
        parts = _ARROW_RE.split(item) if _ARROW_RE.search(item) else item.split("->", 1)
        if len(parts) == 2:
            source, output = parts[0].strip(), parts[1].strip()
    return source or item.strip(), output or item.strip()


def source_stem(filename: str) -> str:
    return Path(filename).stem


def collision_name(source_file: str, base_name: str) -> str:
    return f"{source_stem(source_file)}_{base_name}"


def normalize_key(value: Any, *, case_insensitive: bool) -> str:
    text = "" if value is None else str(value).strip()
    if text.lower() == "nan":
        text = ""
    if re.fullmatch(r"-?\d+\.0", text):
        text = text[:-2]
    return text.lower() if case_insensitive else text


def split_values(value: Any, delimiters: List[str]) -> List[str]:
    text = "" if value is None else str(value).strip()
    if not text or text.lower() == "nan":
        return []
    for delimiter in delimiters:
        if delimiter and delimiter in text:
            return [part.strip() for part in text.split(delimiter)]
    return [text]


def sanitize_token(text: str) -> str:
    cleaned = _NON_ALNUM_RE.sub("_", text.strip()).strip("_")
    return cleaned or "value"


def dedupe_preserve_order(values: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text.lower() == "nan":
            continue
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def join_values(values: Iterable[str], delimiter: str) -> str:
    return delimiter.join(dedupe_preserve_order(values))


def default_delimiters(job: Dict[str, Any], tx: Dict[str, Any]) -> List[str]:
    ordered: List[str] = []
    for item in [
        str(tx.get("delimiter") or "").strip(),
        str(job.get("delimiter") or "").strip(),
        ";",
        " ; ",
        "; ",
    ]:
        if item and item not in ordered:
            ordered.append(item)
    return ordered or [";"]


def default_delimiter(job: Dict[str, Any], tx: Dict[str, Any]) -> str:
    return default_delimiters(job, tx)[0]


def cardinality_mode(job: Dict[str, Any], tx: Dict[str, Any]) -> str:
    mode = str(tx.get("cardinality_mode") or job.get("cardinality_mode") or "collapse").strip().lower()
    if mode not in {"collapse", "expand"}:
        return "collapse"
    return mode


def ensure_output_column(
    rows: List[Dict[str, str]],
    lineage: Dict[str, str],
    base_to_final: Dict[str, List[str]],
    source_file: str,
    base_name: str,
) -> str:
    finals = base_to_final.setdefault(base_name, [])
    for final_name in finals:
        if lineage.get(final_name) == source_file:
            return final_name

    if not finals:
        lineage[base_name] = source_file
        finals.append(base_name)
        return base_name

    new_final = collision_name(source_file, base_name)
    if new_final not in finals:
        lineage[new_final] = source_file
        finals.append(new_final)
    return new_final


def collect_include_fields(tx: Dict[str, Any], df_lookup: pd.DataFrame, right_key: str) -> List[Tuple[str, str]]:
    selected = tx.get("include_fields")
    if selected is None:
        selected = tx.get("fields")
    if selected is None:
        return [(col, col) for col in df_lookup.columns if col != right_key]
    return [parse_field_spec(str(item).strip()) for item in coerce_list(selected) if str(item).strip()]


def build_lookup_records(
    df_lookup: pd.DataFrame,
    tx: Dict[str, Any],
    right_key: str,
    include_fields: List[Tuple[str, str]],
) -> Dict[str, List[Dict[str, str]]]:
    case_insensitive = bool(tx.get("case_insensitive_keys", False))
    records: Dict[str, List[Dict[str, str]]] = {}
    for _, raw in df_lookup.iterrows():
        normalized = normalize_key(raw.get(right_key, ""), case_insensitive=case_insensitive)
        if not normalized:
            continue
        record: Dict[str, str] = {}
        for source_field, output_field in include_fields:
            record[output_field] = str(raw.get(source_field, "") or "")
        records.setdefault(normalized, []).append(record)
    return records


def apply_simple_lookup(
    rows: List[Dict[str, str]],
    lineage: Dict[str, str],
    base_to_final: Dict[str, List[str]],
    job: Dict[str, Any],
    tx: Dict[str, Any],
) -> List[Dict[str, str]]:
    lookup_path = vtx_path(str(tx.get("lookup_table") or tx.get("path") or ""), must_exist=True)
    left_key = str(tx.get("seed_field") or tx.get("left_key") or "").strip()
    right_key = str(tx.get("key") or tx.get("right_key") or "").strip()
    if not left_key or not right_key:
        raise ValueError(f"simple_lookup requires both seed/left and key/right fields: {tx}")

    df_lookup = read_table(lookup_path)
    include_fields = collect_include_fields(tx, df_lookup, right_key)
    missing = [source for source, _ in include_fields if source not in df_lookup.columns]
    if right_key not in df_lookup.columns or missing:
        raise ValueError(f"simple_lookup columns missing in {lookup_path}: key={right_key}, missing_fields={missing}")

    case_insensitive = bool(tx.get("case_insensitive_keys", False))
    records = build_lookup_records(df_lookup, tx, right_key, include_fields)
    lookup_file = lookup_path.name
    delimiters = default_delimiters(job, tx)
    delimiter = default_delimiter(job, tx)
    mode = cardinality_mode(job, tx)
    out_rows: List[Dict[str, str]] = []

    for row in rows:
        left_values = split_values(row.get(left_key, ""), delimiters)
        matches: List[Dict[str, str]] = []
        for left_value in left_values:
            matches.extend(records.get(normalize_key(left_value, case_insensitive=case_insensitive), []))

        if not matches:
            out_rows.append(row.copy())
            continue

        if mode != "expand":
            new_row = row.copy()
            grouped_values: Dict[str, List[str]] = {}
            for match in matches:
                for output_field, value in match.items():
                    grouped_values.setdefault(output_field, []).append(value)
            for output_field, values in grouped_values.items():
                final_name = ensure_output_column(out_rows + [new_row], lineage, base_to_final, lookup_file, output_field)
                new_row[final_name] = join_values(values, delimiter)
            out_rows.append(new_row)
            continue

        for match in matches:
            new_row = row.copy()
            for output_field, value in match.items():
                final_name = ensure_output_column(out_rows + [new_row], lineage, base_to_final, lookup_file, output_field)
                new_row[final_name] = value
            out_rows.append(new_row)
    return out_rows


def apply_lookup_replace(
    rows: List[Dict[str, str]],
    lineage: Dict[str, str],
    base_to_final: Dict[str, List[str]],
    job: Dict[str, Any],
    tx: Dict[str, Any],
) -> List[Dict[str, str]]:
    lookup_path = vtx_path(str(tx.get("lookup_table") or tx.get("path") or ""), must_exist=True)
    left_key = str(tx.get("seed_field") or tx.get("left_key") or "").strip()
    right_key = str(tx.get("key") or tx.get("right_key") or "").strip()
    lookup_field = str(tx.get("lookup_field") or "").strip()
    output_field = str(tx.get("output_field") or left_key).strip()
    if not left_key or not right_key or not lookup_field:
        raise ValueError(f"lookup_replace requires seed/left, key/right, and lookup_field: {tx}")

    df_lookup = read_table(lookup_path)
    if right_key not in df_lookup.columns or lookup_field not in df_lookup.columns:
        raise ValueError(f"lookup_replace columns missing in {lookup_path}: key={right_key}, lookup_field={lookup_field}")

    case_insensitive = bool(tx.get("case_insensitive_keys", False))
    records: Dict[str, List[str]] = {}
    for _, raw in df_lookup.iterrows():
        normalized = normalize_key(raw.get(right_key, ""), case_insensitive=case_insensitive)
        if normalized:
            records.setdefault(normalized, []).append(str(raw.get(lookup_field, "") or ""))

    lookup_file = lookup_path.name
    if output_field in base_to_final:
        final_name = output_field
        if final_name not in lineage:
            lineage[final_name] = lookup_file
        finals = base_to_final.setdefault(output_field, [])
        if final_name not in finals:
            finals.insert(0, final_name)
    else:
        final_name = ensure_output_column(rows, lineage, base_to_final, lookup_file, output_field)
    delimiters = default_delimiters(job, tx)
    delimiter = default_delimiter(job, tx)
    mode = cardinality_mode(job, tx)
    out_rows: List[Dict[str, str]] = []
    for row in rows:
        left_values = split_values(row.get(left_key, ""), delimiters)
        replacements: List[str] = []
        for left_value in left_values:
            replacements.extend(records.get(normalize_key(left_value, case_insensitive=case_insensitive), []))
        replacements = dedupe_preserve_order(replacements)
        if not replacements:
            out_rows.append(row.copy())
            continue
        if mode != "expand":
            new_row = row.copy()
            new_row[final_name] = join_values(replacements, delimiter)
            out_rows.append(new_row)
            continue
        for replacement in replacements:
            new_row = row.copy()
            new_row[final_name] = replacement
            out_rows.append(new_row)
    return out_rows


def apply_transform_tidy(
    rows: List[Dict[str, str]],
    lineage: Dict[str, str],
    base_to_final: Dict[str, List[str]],
    tx: Dict[str, Any],
) -> List[Dict[str, str]]:
    column_field = str(tx.get("column_field") or "").strip()
    value_field = str(tx.get("value_field") or "").strip()
    if not column_field or not value_field:
        raise ValueError(f"transform_tidy requires column_field and value_field: {tx}")

    delimiter = default_delimiter(tx, tx)
    mode = cardinality_mode(tx, tx)
    drop_fields = [str(item).strip() for item in coerce_list(tx.get("drop_fields")) if str(item).strip()]
    source_file = str(tx.get("source_file") or lineage.get(value_field) or "transform_tidy.csv")
    output_prefix = str(tx.get("output_prefix") or "").strip()

    group_fields: List[str] = []
    for row in rows:
        for key in row.keys():
            if key not in drop_fields and key not in (column_field, value_field) and key not in group_fields:
                group_fields.append(key)

    grouped: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    column_order: List[str] = []
    for row in rows:
        group_key = tuple(str(row.get(field, "") or "") for field in group_fields)
        bucket = grouped.setdefault(
            group_key,
            {"base": {field: str(row.get(field, "") or "") for field in group_fields}, "values": {}},
        )
        column_name = f"{output_prefix}{sanitize_token(str(row.get(column_field, '') or ''))}"
        if column_name not in column_order:
            column_order.append(column_name)
        bucket["values"].setdefault(column_name, []).append(str(row.get(value_field, "") or ""))

    out_rows: List[Dict[str, str]] = []
    for bucket in grouped.values():
        final_names: List[str] = []
        value_lists: List[List[str]] = []
        for column_name in column_order:
            final_name = ensure_output_column(out_rows, lineage, base_to_final, source_file, column_name)
            final_names.append(final_name)
            values = dedupe_preserve_order(bucket["values"].get(column_name, [])) or [""]
            value_lists.append(values)
        if mode != "expand":
            new_row = dict(bucket["base"])
            for final_name, values in zip(final_names, value_lists):
                new_row[final_name] = join_values(values, delimiter)
            out_rows.append(new_row)
            continue
        for combo in itertools.product(*value_lists):
            new_row = dict(bucket["base"])
            for final_name, value in zip(final_names, combo):
                new_row[final_name] = value
            out_rows.append(new_row)
    return out_rows


def preprocess_tidy_table(spec: Dict[str, Any], job: Dict[str, Any]) -> Path:
    source_path = vtx_path(str(spec.get("source_table") or ""), must_exist=True)
    output_path = vtx_path(str(spec.get("output_path") or ""), must_exist=False)
    anchor_field = str(spec.get("anchor_field") or "").strip()
    column_field = str(spec.get("column_field") or "").strip()
    value_field = str(spec.get("value_field") or "").strip()
    if not source_path or not anchor_field or not column_field or not value_field:
        raise ValueError(f"preprocess_tidy requires source_table, anchor_field, column_field, and value_field: {spec}")

    df = read_table(source_path)
    for col in [anchor_field, column_field, value_field]:
        if col not in df.columns:
            raise ValueError(f"preprocess_tidy columns missing in {source_path}: {col}")

    df = df[[anchor_field, column_field, value_field]].copy()

    column_lookup_path = str(spec.get("column_lookup_table") or "").strip()
    if column_lookup_path:
        key = str(spec.get("column_lookup_key") or "").strip()
        value = str(spec.get("column_lookup_value_field") or "").strip()
        lookup = read_table(vtx_path(column_lookup_path, must_exist=True))
        if key not in lookup.columns or value not in lookup.columns:
            raise ValueError(f"preprocess_tidy column lookup missing fields in {column_lookup_path}: key={key}, value={value}")
        mapping = {
            normalize_key(k, case_insensitive=False): str(v or "")
            for k, v in zip(lookup[key], lookup[value])
            if normalize_key(k, case_insensitive=False)
        }
        df[column_field] = df[column_field].map(lambda x: mapping.get(normalize_key(x, case_insensitive=False), str(x or "")))

    value_lookup_path = str(spec.get("value_lookup_table") or "").strip()
    if value_lookup_path:
        key = str(spec.get("value_lookup_key") or "").strip()
        value = str(spec.get("value_lookup_value_field") or "").strip()
        lookup = read_table(vtx_path(value_lookup_path, must_exist=True))
        if key not in lookup.columns or value not in lookup.columns:
            raise ValueError(f"preprocess_tidy value lookup missing fields in {value_lookup_path}: key={key}, value={value}")
        mapping = {
            normalize_key(k, case_insensitive=False): str(v or "")
            for k, v in zip(lookup[key], lookup[value])
            if normalize_key(k, case_insensitive=False)
        }
        df[value_field] = df[value_field].map(lambda x: mapping.get(normalize_key(x, case_insensitive=False), str(x or "")))

    mode = cardinality_mode(job, spec)
    delimiter = default_delimiter(job, spec)
    grouped = df.groupby(anchor_field, dropna=False)
    column_order = dedupe_preserve_order(df[column_field].tolist())

    rows: List[Dict[str, str]] = []
    for anchor_value, group in grouped:
        base_row: Dict[str, str] = {anchor_field: str(anchor_value or "")}
        value_lists: List[List[str]] = []
        for col_name in column_order:
            vals = dedupe_preserve_order(group.loc[group[column_field] == col_name, value_field].tolist()) or [""]
            value_lists.append(vals)
        if mode != "expand":
            row = dict(base_row)
            for col_name, values in zip(column_order, value_lists):
                row[sanitize_token(col_name)] = join_values(values, delimiter)
            rows.append(row)
            continue
        for combo in itertools.product(*value_lists):
            row = dict(base_row)
            for col_name, value in zip(column_order, combo):
                row[sanitize_token(col_name)] = value
            rows.append(row)

    out_df = pd.DataFrame(rows).fillna("")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_parquet(output_path, index=False)
    return output_path


def load_manifest(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        doc = yaml.safe_load(handle) or {}
    entries = doc.get("entries")
    if not isinstance(entries, list):
        return []
    out: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue

        source = str(entry.get("source") or "")
        nested_fields = entry.get("fields")
        if isinstance(nested_fields, list):
            for field_entry in nested_fields:
                if not isinstance(field_entry, dict):
                    continue
                out.append(
                    {
                        "source": source,
                        "field": str(field_entry.get("field") or ""),
                        "classification": field_entry.get("classification", ""),
                    }
                )
            continue

        out.append(
            {
                "source": source,
                "field": str(entry.get("field") or ""),
                "classification": entry.get("classification", ""),
            }
        )
    return out


def write_manifest(path: Path, lineage: Dict[str, str], columns: List[str]) -> None:
    existing = load_manifest(path)
    by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    ordered: List[Tuple[str, str]] = []
    for entry in existing:
        key = (entry["source"], entry["field"])
        if key not in by_key:
            by_key[key] = entry
            ordered.append(key)
    for column in columns:
        key = (lineage.get(column, ""), column)
        if key not in by_key:
            by_key[key] = {"source": key[0], "field": key[1], "classification": ""}
            ordered.append(key)

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    source_order: List[str] = []
    for source, field in ordered:
        if source not in grouped:
            grouped[source] = []
            source_order.append(source)
        grouped[source].append(
            {
                "field": field,
                "classification": by_key[(source, field)].get("classification", ""),
            }
        )

    payload = {
        "entries": [
            {
                "source": source,
                "fields": grouped[source],
            }
            for source in source_order
        ]
    }
    save_yaml(path, payload)


def build_seed_rows(df_seed: pd.DataFrame) -> Tuple[List[Dict[str, str]], Dict[str, str], Dict[str, List[str]]]:
    rows = [{str(k): str(v) for k, v in row.items()} for row in df_seed.to_dict(orient="records")]
    lineage: Dict[str, str] = {}
    base_to_final: Dict[str, List[str]] = {}
    return rows, lineage, base_to_final


def seed_lineage(
    rows: List[Dict[str, str]],
    lineage: Dict[str, str],
    base_to_final: Dict[str, List[str]],
    df_seed: pd.DataFrame,
    seed_file: str,
) -> None:
    for column in df_seed.columns:
        ensure_output_column(rows, lineage, base_to_final, seed_file, str(column))


def process_job(job: Dict[str, Any]) -> int:
    job_id = str(job.get("id") or "raw_application_table_v2").strip()
    seed_raw = str(job.get("seed") or job.get("source") or "").strip()
    if not seed_raw:
        raise ValueError(f"[{job_id}] seed is required")

    outputs = job.get("outputs") if isinstance(job.get("outputs"), dict) else {}
    out_parquet_raw = str(outputs.get("parquet") or "").strip()
    if not out_parquet_raw:
        raise ValueError(f"[{job_id}] outputs.parquet is required")
    out_parquet = vtx_path(out_parquet_raw)
    manifest_path = vtx_path(str(outputs.get("manifest_yaml") or DEFAULT_MANIFEST_PATH))

    seed_path = vtx_path(seed_raw, must_exist=True)
    df_seed = read_table(seed_path)
    rows, lineage, base_to_final = build_seed_rows(df_seed)
    seed_lineage(rows, lineage, base_to_final, df_seed, seed_path.name)

    for spec in [item for item in coerce_list(job.get("preprocess_tidy")) if isinstance(item, dict)]:
        preprocess_tidy_table(spec, job)

    for tx in [item for item in coerce_list(job.get("transactions")) if isinstance(item, dict)]:
        tx_type = str(tx.get("type") or "").strip()
        if tx_type == "simple_lookup":
            rows = apply_simple_lookup(rows, lineage, base_to_final, job, tx)
        elif tx_type == "lookup_replace":
            rows = apply_lookup_replace(rows, lineage, base_to_final, job, tx)
        elif tx_type == "transform_tidy":
            rows = apply_transform_tidy(rows, lineage, base_to_final, tx)
        else:
            raise ValueError(f"[{job_id}] unsupported transaction type: {tx_type}")

    column_order: List[str] = []
    for seed_column in df_seed.columns:
        finals = base_to_final.get(str(seed_column), [])
        if finals:
            final_name = finals[0]
            if final_name not in column_order:
                column_order.append(final_name)
    for row in rows:
        for column in row.keys():
            if column not in column_order:
                column_order.append(column)

    df_out = pd.DataFrame(rows).fillna("")
    if column_order:
        df_out = df_out.reindex(columns=column_order)
    out_parquet.parent.mkdir(parents=True, exist_ok=True)
    df_out.to_parquet(out_parquet, index=False)
    write_manifest(manifest_path, lineage, list(df_out.columns))

    logger.info(
        "job_complete,id=%s,seed=%s,rows=%d,cols=%d,parquet=%s,manifest=%s",
        job_id,
        seed_path,
        len(df_out),
        len(df_out.columns),
        out_parquet,
        manifest_path,
    )
    return 0


@dataclass
class Options:
    config_path: Path
    job: Optional[str]


def parse_args(argv: Optional[List[str]] = None) -> Options:
    parser = argparse.ArgumentParser(description="Build a fully expanded raw application parquet table")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--job", default=None)
    args = parser.parse_args(argv)
    return Options(config_path=vtx_path(args.config, must_exist=True), job=args.job)


def main(argv: Optional[List[str]] = None) -> int:
    options = parse_args(argv)
    cfg = load_yaml(options.config_path)
    jobs = extract_jobs(cfg)
    if not jobs:
        logger.error("no_jobs_defined,config=%s", options.config_path)
        return 1

    selected = jobs
    if options.job:
        selected = [job for job in jobs if str(job.get("id") or "").strip() == options.job]
        if not selected:
            logger.error("job_not_found,wanted=%s", options.job)
            return 2

    for job in selected:
        if job.get("enabled", True) is False:
            continue
        process_job(job)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
