#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
master_table_builder_vtx.py
---------------------------
Staged master table builder.

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
DEFAULT_CONFIG_PATH = VTX_ROOT / "usr" / "config" / "run" / "master_table_builder_vtx.yaml"


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


logger = get_logger("master_table_builder_vtx")


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
    right_key: str,
) -> Tuple[List[str], Dict[str, str]]:
    right_key_lc = right_key.strip().lower()
    resolved_fields: List[str] = []
    output_names: Dict[str, str] = {}

    def add_field(source_raw: Any, output_raw: Any = None) -> None:
        source_name = str(source_raw or "").strip()
        if not source_name or source_name.lower() == right_key_lc:
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
            if column.strip().lower() != right_key_lc:
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


def _build_join_index(
    r_df: pd.DataFrame,
    right_key: str,
    include_fields: List[str],
    delimiter: str,
    ordered_normalizations: List[str],
) -> Dict[str, Dict[str, List[str]]]:
    idx: Dict[str, Dict[str, List[str]]] = {}
    for _, r in r_df.iterrows():
        raw_key = r.get(right_key, "")
        keys = split_semicolon_keep_blanks(raw_key, delimiter=delimiter)
        if not keys:
            continue
        for key in keys:
            norm_key = apply_join_normalizations(key, ordered_normalizations)
            if not norm_key:
                continue
            bucket = idx.setdefault(norm_key, {})
            for c in include_fields:
                bucket.setdefault(c, []).append(normalize_scalar(r.get(c, "")))
    return idx


def apply_joins(base: pd.DataFrame, joins: List[Dict[str, Any]]) -> pd.DataFrame:
    out = base.copy()
    for j in joins:
        alias = str(j.get("alias") or "join").strip()
        path = vtx_path(str(j.get("path") or ""), must_exist=True)
        left_key = str(j.get("left_key") or "").strip()
        right_key = str(j.get("right_key") or "").strip()
        delimiter = str(j.get("delimiter") or ";")
        include_fields = j.get("include_fields")
        ordered_normalizations = parse_join_normalizations(j.get("normalizations"))
        left_table_filters = j.get("left_table_filters")
        right_table_filters = j.get("right_table_filters")

        resolved_left_key = resolve_column_name(list(out.columns), left_key) if left_key else None
        if not resolved_left_key:
            logger.warning("join_skip,alias=%s,reason=left_key_missing,left_key=%s", alias, left_key)
            continue

        r_df = read_table(path)
        resolved_right_key = resolve_column_name(list(r_df.columns), right_key) if right_key else None
        if not resolved_right_key:
            logger.warning("join_skip,alias=%s,reason=right_key_missing,right_key=%s", alias, right_key)
            continue

        filtered_right_df, _ = apply_table_filters(r_df, right_table_filters, alias=alias, side="right")
        eligible_left_df, eligible_left_mask = apply_table_filters(out, left_table_filters, alias=alias, side="left")

        inc, configured_output_names = parse_join_include_fields(include_fields, filtered_right_df, resolved_right_key)

        output_names: Dict[str, str] = {}
        existing_cols = set(out.columns)
        for c in inc:
            out_name = configured_output_names.get(c) or c
            if out_name in existing_cols:
                out_name = f"{alias}_{out_name}"
            output_names[c] = out_name
            if out_name not in out.columns:
                out[out_name] = ""

        idx = _build_join_index(filtered_right_df, resolved_right_key, inc, delimiter, ordered_normalizations)

        new_rows: List[Dict[str, Any]] = []
        successful_matches = 0
        for _, row in out.iterrows():
            if not bool(eligible_left_mask.get(row.name, True)):
                rec = row.to_dict()
                for c in inc:
                    rec[output_names[c]] = rec.get(output_names[c], "")
                new_rows.append(rec)
                continue

            key_list = [
                apply_join_normalizations(key, ordered_normalizations)
                for key in _normalize_key_list(row.get(resolved_left_key, ""), delimiter=delimiter)
            ]
            if not key_list:
                rec = row.to_dict()
                for c in inc:
                    rec[output_names[c]] = rec.get(output_names[c], "")
                new_rows.append(rec)
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
            new_rows.append(rec)

        out = pd.DataFrame(new_rows)
        logger.info(
            "stage_join_complete,alias=%s,rows=%d,cols=%d,successful_matches=%d,normalizations=%s,right_filters=%s,left_filters=%s",
            alias,
            len(out),
            len(out.columns),
            successful_matches,
            ordered_normalizations,
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
    p = argparse.ArgumentParser(description="Staged master table builder")
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
    out_parquet = vtx_path(str(outputs.get("parquet") or ""))
    out_csv_raw = str(outputs.get("csv") or "").strip()

    write_table(df, out_parquet)
    if out_csv_raw:
        write_table(df, vtx_path(out_csv_raw))

    logger.info("job_complete,id=%s,rows=%d,cols=%d,parquet=%s", jid, len(df), len(df.columns), out_parquet)
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
