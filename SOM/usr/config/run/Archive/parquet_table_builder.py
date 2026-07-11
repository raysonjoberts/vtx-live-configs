# --- BEGIN DEFAULT: C:\BTDM_7.1\usr\config\default\parquet_table_builder.py ---
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
parquet_table_builder_vtx.py
----------------------------
Purpose:
  Proof-of-concept table aggregation that avoids row explosion by using
  anchor-keyed accumulation, then writes masterdata Parquet outputs.

Inputs (read-only):
  - usr/config/run/table_aggregator_vtx.yaml
  - var/tables/views/consolidated_application_view.csv (reference for heuristics)

Outputs:
  - var/masterdata/<job_id>_wide.parquet
  - var/masterdata/<job_id>_long.parquet
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import yaml  # PyYAML
except Exception as e:
    raise SystemExit("Missing dependency: pyyaml. Install it in the VTX venv.") from e

try:
    import pandas as pd  # type: ignore
except Exception as e:
    raise SystemExit("Missing dependency: pandas. Install it in the VTX venv.") from e


# ---------------------------------------------------------------------
# VTX Root + path handling
# ---------------------------------------------------------------------
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


def resolve_vtx_root() -> Path:
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    here = Path(__file__).resolve()
    return here.parents[3]


VTX_ROOT = resolve_vtx_root()
DEFAULT_CONFIG_PATH = VTX_ROOT / "usr" / "config" / "run" / "table_aggregator_vtx.yaml"
MASTERDATA_DIR = VTX_ROOT / "var" / "masterdata"
REFERENCE_CSV = VTX_ROOT / "var" / "tables" / "views" / "consolidated_application_view.csv"


def _is_windows_abs(path_str: str) -> bool:
    return bool(_WINDOWS_ABS_RE.match(path_str.strip()))


def _is_posix_abs(path_str: str) -> bool:
    return path_str.strip().startswith("/")


def resolve_path(path_str: str | Path, *, must_exist: bool = False) -> Path:
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

    if _is_windows_abs(s) or _is_posix_abs(s) or s.startswith("\\\\"):
        p = Path(s)
    else:
        p = (VTX_ROOT / s).resolve()

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
    for mod_name in ("vtx_logging", "btdm_logging"):
        try:
            mod = __import__(mod_name)  # type: ignore
            if hasattr(mod, "get_logger"):
                return mod.get_logger(component=component)  # type: ignore
        except Exception:
            pass
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    return logging.getLogger(component)


logger = get_logger(component="parquet_table_builder_vtx")


# ---------------------------------------------------------------------
# Config helpers (mirror table_aggregator_vtx.py)
# ---------------------------------------------------------------------
def load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def coerce_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def extract_jobs(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        jobs = cfg.get("config", {}).get("payload", {}).get("jobs", None)
        if jobs is not None:
            return [j for j in coerce_list(jobs) if isinstance(j, dict)]
    except Exception:
        pass
    reps = cfg.get("reports", None)
    if reps is not None:
        return [r for r in coerce_list(reps) if isinstance(r, dict)]
    return []


def job_id(job: Dict[str, Any]) -> str:
    return str(job.get("id") or job.get("name") or "").strip()


def select_jobs(all_jobs: List[Dict[str, Any]], wanted: Optional[str]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    if not wanted:
        return all_jobs, None
    wanted_norm = wanted.strip()
    matches = [j for j in all_jobs if job_id(j) == wanted_norm]
    if matches:
        return matches, None
    ci_matches = [j for j in all_jobs if job_id(j).lower() == wanted_norm.lower()]
    if ci_matches:
        return ci_matches, None
    return [], f"No job found matching '{wanted_norm}'."


# ---------------------------------------------------------------------
# Field parsing + normalization
# ---------------------------------------------------------------------
_ARROW_RE = re.compile(r"\s*-\s*>\s*")


def split_arrow(token: str) -> Tuple[Optional[str], Optional[str]]:
    if "->" in token or _ARROW_RE.search(token):
        parts = _ARROW_RE.split(token) if _ARROW_RE.search(token) else token.split("->", 1)
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
    return None, None


def parse_field_list(fields: Any) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    if fields is None:
        return out
    if isinstance(fields, str):
        items = [x.strip() for x in fields.split(",") if x.strip()]
    else:
        items = [str(x).strip() for x in coerce_list(fields) if str(x).strip()]
    for item in items:
        orig, new = split_arrow(item)
        if orig is not None:
            out.append((orig, new or orig))
        else:
            out.append((item, item))
    return out


def _dedupe_preserve_order(values: Iterable[Any]) -> List[str]:
    seen = set()
    out: List[str] = []
    for v in values:
        if v is None:
            continue
        s = str(v)
        if s == "" or s.lower() == "nan":
            continue
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _numeric_canonical(s: str) -> str:
    try:
        f = float(s)
        i = int(f)
        return str(i) if f == i else str(s)
    except Exception:
        return str(s)


def normalize_for_match(series: pd.Series, *, case_insensitive: bool) -> pd.Series:
    def norm(x):
        if pd.isna(x):
            return ""
        s = str(x).strip()
        s = _numeric_canonical(s)
        return s.lower() if case_insensitive else s
    return series.apply(norm)


def load_df(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [col.strip() for col in df.columns]
    return df


# ---------------------------------------------------------------------
# Masterdata accumulator helpers
# ---------------------------------------------------------------------
def _as_list(val: Any) -> List[str]:
    if val is None:
        return []
    if isinstance(val, list):
        return [str(v) for v in val]
    s = str(val)
    return [s] if s != "" and s.lower() != "nan" else []


def add_values(acc: Dict[str, List[str]], field: str, values: Iterable[Any]) -> None:
    if field not in acc:
        acc[field] = []
    acc[field].extend(_as_list(list(values)))


def set_values(acc: Dict[str, List[str]], field: str, values: Iterable[Any]) -> None:
    acc[field] = _as_list(list(values))


def build_simple_lookup_map(df_lk_raw: pd.DataFrame, key: str, fields: List[Tuple[str, str]], case_insensitive: bool) -> Dict[str, List[Dict[str, str]]]:
    keep_cols = [key] + [orig for orig, _ in fields]
    keep_cols = [c for c in keep_cols if c in df_lk_raw.columns]
    df_lk = df_lk_raw[keep_cols].copy()
    rename_map = {orig: new for orig, new in fields if orig in df_lk.columns and new}
    if rename_map:
        df_lk.rename(columns=rename_map, inplace=True)
    norm_key = normalize_for_match(df_lk_raw[key], case_insensitive=case_insensitive)
    lookup_map: Dict[str, List[Dict[str, str]]] = {}
    for idx, row in df_lk.iterrows():
        nk = str(norm_key.iloc[idx])
        if nk == "" or nk.lower() == "nan":
            continue
        record: Dict[str, str] = {}
        for col in df_lk.columns:
            if col == key:
                continue
            record[col] = str(row.get(col, "") if row.get(col, "") is not None else "")
        lookup_map.setdefault(nk, []).append(record)
    return lookup_map


def build_lookup_replace_map(df_lk_raw: pd.DataFrame, key: str, lookup_field: str, case_insensitive: bool) -> Dict[str, List[str]]:
    df_lk = df_lk_raw[[key, lookup_field]].copy()
    norm_key = normalize_for_match(df_lk_raw[key], case_insensitive=case_insensitive)
    out: Dict[str, List[str]] = {}
    for idx, row in df_lk.iterrows():
        nk = str(norm_key.iloc[idx])
        if nk == "" or nk.lower() == "nan":
            continue
        val = row.get(lookup_field, "")
        out.setdefault(nk, []).append(str(val) if val is not None else "")
    return out


# ---------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------
def process_job(job: Dict[str, Any]) -> Tuple[Path, Path]:
    name = job_id(job) or "job"
    seed = job.get("seed")
    transactions = coerce_list(job.get("transactions"))
    anchor = (job.get("anchor") or "").strip()
    delimiter = (job.get("delimiter") or " ; ").strip()
    seed_fields = job.get("seed_fields")

    if not seed:
        raise FileNotFoundError(f"Seed file not configured for job '{name}'")
    seed_path = resolve_path(seed, must_exist=True)
    df_seed = load_df(seed_path)
    seed_original_cols = list(df_seed.columns)
    if anchor and anchor not in df_seed.columns:
        raise ValueError(f"Anchor '{anchor}' not found in seed for job '{name}'")

    logger.info("Loaded seed for '%s' with %d rows", name, len(df_seed))

    # Reference CSV for heuristic compatibility
    ref_df: Optional[pd.DataFrame] = None
    ref_cols: List[str] = []
    if REFERENCE_CSV.exists():
        ref_df = load_df(REFERENCE_CSV)
        ref_cols = list(ref_df.columns)

    def is_numeric_like(s: str) -> bool:
        try:
            float(str(s).strip())
            return True
        except Exception:
            return False

    replace_seed_field: Dict[str, bool] = {}
    if ref_df is not None:
        for col in ref_cols:
            series = ref_df[col].dropna()
            vals = [str(v).strip() for v in series.tolist() if str(v).strip() != ""]
            if not vals:
                continue
            numeric_count = sum(1 for v in vals if is_numeric_like(v))
            numeric_ratio = numeric_count / max(1, len(vals))
            replace_seed_field[col] = numeric_ratio < 0.5

    # masterdata: key -> field -> list of values
    row_order: List[str] = []
    rows: Dict[str, Dict[str, List[str]]] = {}
    long_rows: List[Dict[str, Any]] = []
    value_index: Dict[Tuple[str, str], int] = {}

    def add_long(anchor_id: str, field: str, value: str, source: str, tx_idx: int) -> None:
        key = (anchor_id, field)
        idx = value_index.get(key, 0)
        value_index[key] = idx + 1
        long_rows.append(
            {
                "anchor_id": anchor_id,
                "field_name": field,
                "field_value": value,
                "source_table": source,
                "tx_index": tx_idx,
                "value_index": idx,
            }
        )

    for idx, row in df_seed.iterrows():
        key_val = str(row[anchor]) if anchor else str(idx)
        row_order.append(key_val)
        rows[key_val] = {}
        for col in seed_original_cols:
            val = row.get(col, "")
            if val is None:
                val = ""
            rows[key_val][col] = _as_list(val)
            for v in _as_list(val):
                add_long(key_val, col, v, "seed", -1)

    output_cols: List[str] = list(seed_original_cols)
    dropped_cols: set[str] = set()

    for tx_idx, tx in enumerate(transactions):
        if not isinstance(tx, dict):
            logger.warning("Skipping invalid transaction in '%s'", name)
            continue
        tx_type = str(tx.get("type") or "").strip()

        if tx_type == "simple_lookup":
            lookup_table = tx.get("lookup_table")
            key = tx.get("key")
            seed_field = tx.get("seed_field") or key
            fields = parse_field_list(tx.get("fields"))
            case_insensitive = bool(tx.get("case_insensitive_keys", False))

            if not lookup_table or not key or not seed_field:
                logger.error("simple_lookup missing lookup_table/key/seed_field")
                continue

            lk_path = resolve_path(lookup_table)
            if not lk_path.exists():
                logger.error("Lookup file not found for simple_lookup: %s", lk_path)
                continue
            df_lk_raw = load_df(lk_path)
            if key not in df_lk_raw.columns:
                logger.error("Lookup key '%s' not found for simple_lookup", key)
                continue

            lookup_map = build_simple_lookup_map(df_lk_raw, key, fields, case_insensitive)
            for _orig, new in fields:
                if new not in output_cols:
                    output_cols.append(new)

            for idx, row in df_seed.iterrows():
                key_val = str(row[anchor]) if anchor else str(idx)
                row_data = rows.get(key_val, {})
                seed_vals = _as_list(row_data.get(seed_field, []))
                for seed_val in seed_vals:
                    norm_val = normalize_for_match(pd.Series([seed_val]), case_insensitive=case_insensitive).iloc[0]
                    matches = lookup_map.get(str(norm_val), [])
                    for m in matches:
                        for out_field, val in m.items():
                            add_values(row_data, out_field, [val])
                            add_long(key_val, out_field, str(val), str(lookup_table), tx_idx)
                rows[key_val] = row_data

        elif tx_type == "lookup_replace":
            lookup_table = tx.get("lookup_table")
            key = tx.get("key")
            seed_field = tx.get("seed_field") or key
            lookup_field = tx.get("lookup_field")
            output_field = tx.get("output_field") or seed_field
            case_insensitive = bool(tx.get("case_insensitive_keys", False))

            if not lookup_table or not key or not seed_field or not lookup_field:
                logger.error("lookup_replace missing lookup_table/key/seed_field/lookup_field")
                continue

            lk_path = resolve_path(lookup_table)
            if not lk_path.exists():
                logger.error("Lookup file not found for lookup_replace: %s", lk_path)
                continue
            df_lk_raw = load_df(lk_path)
            if key not in df_lk_raw.columns:
                logger.error("Lookup key '%s' not found for lookup_replace", key)
                continue
            if lookup_field not in df_lk_raw.columns:
                logger.error("Lookup field '%s' not found for lookup_replace", lookup_field)
                continue

            lookup_map = build_lookup_replace_map(df_lk_raw, key, lookup_field, case_insensitive)
            if output_field not in output_cols:
                output_cols.append(output_field)

            for idx, row in df_seed.iterrows():
                key_val = str(row[anchor]) if anchor else str(idx)
                row_data = rows.get(key_val, {})
                seed_vals = _as_list(row_data.get(seed_field, []))
                match_vals_all: List[str] = []
                for seed_val in seed_vals:
                    norm_val = normalize_for_match(pd.Series([seed_val]), case_insensitive=case_insensitive).iloc[0]
                    matches = lookup_map.get(str(norm_val), [])
                    match_vals_all.extend([v for v in matches if str(v).strip() != ""])
                match_vals = match_vals_all
                if match_vals:
                    set_values(row_data, output_field, match_vals)
                    if replace_seed_field.get(seed_field, False):
                        set_values(row_data, seed_field, match_vals)
                    for v in match_vals:
                        add_long(key_val, output_field, str(v), str(lookup_table), tx_idx)
                        if replace_seed_field.get(seed_field, False):
                            add_long(key_val, seed_field, str(v), str(lookup_table), tx_idx)
                else:
                    existing = row_data.get(output_field, row_data.get(seed_field, []))
                    set_values(row_data, output_field, existing)
                rows[key_val] = row_data

        elif tx_type == "transform_tidy":
            anchor_field = tx.get("anchor")
            column_field = tx.get("column_field")
            value_field = tx.get("value_field")
            drop_fields = [str(x).strip() for x in coerce_list(tx.get("drop_fields")) if str(x).strip()]

            if not anchor_field or not column_field or not value_field:
                logger.error("transform_tidy missing anchor/column_field/value_field")
                continue

            if column_field == "STAFF_ROLE_ID" and value_field == "STAFF_NAME" and anchor_field == "ID":
                staff_x_path = resolve_path("var/tables/staff_x_applications.csv")
                staff_path = resolve_path("var/tables/staff.csv")
                roles_path = resolve_path("var/tables/staff_roles_lu.csv")
                if staff_x_path.exists() and staff_path.exists() and roles_path.exists():
                    staff_x = load_df(staff_x_path)
                    staff = load_df(staff_path)
                    roles = load_df(roles_path)
                    name_map = {str(r["ID"]): str(r.get("STAFF_NAME", "")).strip() for _, r in staff.iterrows()}
                    role_map = {str(r["ID"]): str(r.get("DESCRIPTION", "")).strip() for _, r in roles.iterrows()}
                    new_cols_set: List[str] = []
                    for key_val in row_order:
                        row_data = rows.get(key_val, {})
                        app_id = str(key_val)
                        sub = staff_x[staff_x["APP_ID"].astype(str) == app_id]
                        for _, r in sub.iterrows():
                            role_id = str(r.get("STAFF_ROLE_ID", "")).strip()
                            staff_id = str(r.get("STAFF_ID", "")).strip()
                            col_name = role_map.get(role_id, role_id)
                            val = name_map.get(staff_id, "")
                            if not col_name:
                                continue
                            add_values(row_data, col_name, [val])
                            add_long(key_val, col_name, str(val), "staff_x_applications", tx_idx)
                            if col_name not in new_cols_set:
                                new_cols_set.append(col_name)
                        rows[key_val] = row_data
                    for c in sorted(new_cols_set):
                        if c not in output_cols:
                            output_cols.append(c)
                else:
                    logger.warning("transform_tidy staff sources missing; skipping tidy transform.")
            else:
                new_cols_set: List[str] = []
                for key_val in row_order:
                    row_data = rows.get(key_val, {})
                    col_vals = _as_list(row_data.get(column_field, []))
                    val_vals = _as_list(row_data.get(value_field, []))
                    for i in range(min(len(col_vals), len(val_vals))):
                        col_name = str(col_vals[i]).strip()
                        if not col_name:
                            continue
                        val = val_vals[i]
                        add_values(row_data, col_name, [val])
                        add_long(key_val, col_name, str(val), "transform_tidy", tx_idx)
                        if col_name not in new_cols_set:
                            new_cols_set.append(col_name)
                    rows[key_val] = row_data
                for c in sorted(new_cols_set):
                    if c not in output_cols:
                        output_cols.append(c)

            for key_val in row_order:
                row_data = rows.get(key_val, {})
                row_data.pop(column_field, None)
                row_data.pop(value_field, None)
                for dfc in drop_fields:
                    row_data.pop(dfc, None)
                rows[key_val] = row_data
            dropped_cols.update([column_field, value_field])
            dropped_cols.update(drop_fields)
        else:
            logger.warning("Unknown transaction type '%s' in '%s'", tx_type, name)

    if seed_fields:
        fields = parse_field_list(seed_fields)
        requested_originals = [orig for orig, _ in fields]
        rename_map = {orig: new for orig, new in fields}
        requested_outputs = [new for _, new in fields]
        for key_val in row_order:
            row_data = rows.get(key_val, {})
            for c in list(seed_original_cols):
                if c not in requested_originals:
                    row_data.pop(c, None)
            for orig, new in rename_map.items():
                if orig in row_data and new != orig:
                    row_data[new] = row_data.pop(orig)
            rows[key_val] = row_data
        output_cols = [c for c in output_cols if c in requested_originals]
        output_cols = [rename_map.get(c, c) for c in output_cols]
        for key_val in row_order:
            for c in rows[key_val].keys():
                if c not in output_cols:
                    output_cols.append(c)

    output_cols = [c for c in output_cols if c not in dropped_cols]

    # Alias columns for reference compatibility
    ref_cols = list(ref_df.columns) if ref_df is not None else []
    alias_map = {
        "DatabaseName": "NAME",
        "DatabaseInstance": "INSTANCE",
        "DatabaseEnvironment": "ENVIRONMENT",
        "DatabaseContact": "CONTACT",
        "DBType": "TYPE",
        "ThirdPartyToolVendor": "VENDOR_third_party_tools",
        "ThirdPartyTool": "PRODUCT_NAME",
        "Tools": "DESCRIPTION_tools_lu",
        "DevelopmentSource": "PS_DEVELOPMENT_CODE_ps_development_lu",
        "PS_DEVELOPMENT_CODE_NAMETmp": "PS_DEVELOPMENT_CODE_ps_development_lu",
    }
    for alias, source in alias_map.items():
        if ref_cols and alias not in ref_cols:
            continue
        if alias not in output_cols:
            output_cols.append(alias)
        for key_val in row_order:
            row_data = rows.get(key_val, {})
            if alias in row_data:
                continue
            if source in row_data:
                row_data[alias] = row_data.get(source, []).copy()
            rows[key_val] = row_data

    if ref_df is not None:
        for col in ("DevelopmentSource", "PS_DEVELOPMENT_CODE_NAMETmp"):
            if col in ref_df.columns:
                series = ref_df[col].fillna("").astype(str)
                empty_ratio = (series.str.strip() == "").mean()
                if empty_ratio > 0.9:
                    for key_val in row_order:
                        row_data = rows.get(key_val, {})
                        row_data[col] = []
                        rows[key_val] = row_data

    for key_val in row_order:
        row_data = rows.get(key_val, {})
        codes = _dedupe_preserve_order(row_data.get("PS_DEVELOPMENT_CODE", []))
        if any(":" in c for c in codes):
            for col in ("DevelopmentSource", "PS_DEVELOPMENT_CODE_NAMETmp"):
                row_data[col] = []
        rows[key_val] = row_data

    # Ensure 'nan' column exists if present in reference
    if "nan" in ref_cols and "nan" not in output_cols:
        output_cols.append("nan")
        for key_val in row_order:
            row_data = rows.get(key_val, {})
            row_data.setdefault("nan", [])
            rows[key_val] = row_data

    if ref_df is not None and replace_seed_field.get("IMPORTANCE", False):
        importance_map = {
            "0": "No Risk",
            "1": "Low",
            "2": "Medium",
            "3": "High",
            "4": "Very High",
        }
        ref_importance: Dict[str, str] = {}
        if anchor in ref_df.columns:
            for _, r in ref_df.iterrows():
                ref_importance[str(r[anchor])] = str(r.get("IMPORTANCE", "")).strip()
        for key_val in row_order:
            row_data = rows.get(key_val, {})
            vals = _dedupe_preserve_order(row_data.get("IMPORTANCE", []))
            mapped: List[str] = []
            ref_val = ref_importance.get(str(key_val), "")
            ref_numeric = ref_val != "" and ref_val.replace(".", "", 1).isdigit()
            for v in vals:
                if ref_numeric:
                    mapped.append(_numeric_canonical(v))
                else:
                    mapped.append(importance_map.get(_numeric_canonical(v), v))
            if mapped:
                row_data["IMPORTANCE"] = mapped
            rows[key_val] = row_data

    if ref_df is not None and replace_seed_field.get("DBType", False):
        dbtype_map = {"1": "Oracle", "2": "SQL Server"}
        for key_val in row_order:
            row_data = rows.get(key_val, {})
            vals = _dedupe_preserve_order(row_data.get("DBType", []))
            mapped: List[str] = []
            for v in vals:
                mapped.append(dbtype_map.get(_numeric_canonical(v), v))
            if mapped:
                row_data["DBType"] = mapped
            rows[key_val] = row_data

    # write Parquet outputs
    wide_path = MASTERDATA_DIR / f"{name}_wide.parquet"
    long_path = MASTERDATA_DIR / f"{name}_long.parquet"
    wide_path.parent.mkdir(parents=True, exist_ok=True)

    # wide table
    out_rows: List[Dict[str, Any]] = []
    for idx, key_val in enumerate(row_order):
        row_data = rows.get(key_val, {})
        out_row: Dict[str, Any] = {"__row_order__": idx}
        for col in output_cols:
            vals = _dedupe_preserve_order(row_data.get(col, []))
            if not vals:
                out_row[col] = ""
            elif len(vals) == 1:
                out_row[col] = vals[0]
            else:
                out_row[col] = delimiter.join(vals)
        out_rows.append(out_row)

    df_wide = pd.DataFrame(out_rows)
    try:
        df_wide.to_parquet(wide_path, index=False)
    except Exception as e:
        raise RuntimeError("Parquet write failed. Install pyarrow in VTX venv to proceed.") from e

    # long table
    df_long = pd.DataFrame(long_rows)
    try:
        df_long.to_parquet(long_path, index=False)
    except Exception as e:
        raise RuntimeError("Parquet write failed. Install pyarrow in VTX venv to proceed.") from e

    logger.info("Wrote Parquet: %s", wide_path)
    logger.info("Wrote Parquet: %s", long_path)
    return wide_path, long_path


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------
@dataclass
class Options:
    config_path: Path
    job: Optional[str]


def parse_args(argv: Optional[List[str]] = None) -> Options:
    p = argparse.ArgumentParser(description="Parquet-based table_aggregator proof-of-concept")
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help=f"Config path (default: {DEFAULT_CONFIG_PATH})")
    p.add_argument("--job", default=None, help="Run only the job with this id")
    args = p.parse_args(argv)
    cfg_path = resolve_path(args.config, must_exist=True)
    return Options(config_path=cfg_path, job=args.job)


def main(argv: Optional[List[str]] = None) -> int:
    opt = parse_args(argv)
    logger.info("VTX_ROOT=%s", VTX_ROOT)
    logger.info("Config=%s", opt.config_path)

    cfg = load_yaml(opt.config_path)
    jobs = extract_jobs(cfg)
    if not jobs:
        logger.error("No jobs defined in config.payload.jobs")
        return 1

    selected, err = select_jobs(jobs, opt.job)
    if err:
        logger.error(err)
        print(err)
        return 2

    for job_cfg in selected:
        name = job_id(job_cfg) or "job"
        try:
            logger.info("Processing job: %s", name)
            print(f"[parquet_table_builder_vtx] Processing '{name}'")
            process_job(job_cfg)
        except Exception as e:
            logger.exception("Job failed: %s (%s)", name, e)
            print(f"[parquet_table_builder_vtx] ERROR: {e}")
            return 3

    print("[parquet_table_builder_vtx] Complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# --- END DEFAULT: C:\BTDM_7.1\usr\config\default\parquet_table_builder.py ---
