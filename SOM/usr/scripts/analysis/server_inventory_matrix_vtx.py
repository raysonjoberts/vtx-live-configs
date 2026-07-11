#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
server_inventory_matrix_vtx.py
------------------------------
Job-driven server inventory harmonizer.

Default config (no --config required):
  usr/config/run/server_inventory_matrix_vtx.yaml

Each job references one inventory and defines its own outputs:
  - inventory_table
  - presence_matrix_reports
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import json
from html import escape

import pandas as pd
import yaml


# ---------------------------------------------------------------------
# Globals / Paths (VTX style)
# ---------------------------------------------------------------------

def resolve_vtx_root() -> Path:
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    here = Path(__file__).resolve()
    return here.parents[3]


VTX_ROOT = resolve_vtx_root()
DEFAULT_CONFIG_PATH = VTX_ROOT / "usr" / "config" / "run" / "server_inventory_matrix_vtx.yaml"
FALLBACK_CONFIG_PATH = VTX_ROOT / "usr" / "config" / "default" / "server_inventory_matrix_vtx.yaml"


def vtx_path(path_str: str | Path, *, base_root: Optional[Path] = None, must_exist: bool = False) -> Path:
    if isinstance(path_str, Path):
        p = path_str
    else:
        s = str(path_str).strip()
        s = os.path.expandvars(s)
        s = os.path.expanduser(s)
        root = str(base_root or VTX_ROOT)
        s = s.replace("VTX_ROOT" + os.sep, root + os.sep)
        s = s.replace("BTDM_ROOT" + os.sep, root + os.sep)
        s = s.replace("VTX_ROOT/", root + "/")
        s = s.replace("BTDM_ROOT/", root + "/")
        p = Path(s)

    if not p.is_absolute():
        p = (base_root or VTX_ROOT) / p
    p = p.resolve()

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


logger = get_logger(component="server_inventory_matrix_vtx")


# ---------------------------------------------------------------------
# Config loading helpers
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
    for k in ("inventories", "jobs", "inputs", "outputs"):
        if k in cfg and cfg[k] is None:
            cfg[k] = []
    return cfg


# ---------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------

def _norm_str(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if s.lower() in {"nan", "none", "null", "n/a", "na", "unknown", "undef", "undefined", "-", "--"}:
        return ""
    return s


def _normalize_series(s: pd.Series, rules: Dict[str, Any]) -> pd.Series:
    s2 = s.astype(str).fillna("")
    if rules.get("strip", True):
        s2 = s2.str.strip()
    if rules.get("casefold", True):
        s2 = s2.str.lower()
    if rules.get("fqdn_to_shortname", False):
        s2 = s2.str.split(".", n=1).str[0].str.strip()
        if rules.get("casefold", True):
            s2 = s2.str.lower()
    s2 = s2.replace({"nan": "", "none": "", "null": ""})
    return s2


# ---------------------------------------------------------------------
# Union-Find for ANY/ALL matching across keys
# ---------------------------------------------------------------------

class UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            self.parent[ra] = rb
        elif self.rank[ra] > self.rank[rb]:
            self.parent[rb] = ra
        else:
            self.parent[rb] = ra
            self.rank[ra] += 1


# ---------------------------------------------------------------------
# Core loading + mapping
# ---------------------------------------------------------------------

def _read_source_df(vtx_root: Path, src: Dict[str, Any]) -> pd.DataFrame:
    path = vtx_path(src.get("path", ""), base_root=vtx_root, must_exist=True)
    fmt = (src.get("format") or "").strip().lower()
    if fmt == "csv":
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    if fmt == "xlsx":
        sheet = src.get("sheet", 0)
        return pd.read_excel(path, sheet_name=sheet, dtype=str).fillna("")
    raise ValueError(f"Unsupported format '{fmt}' for source '{src.get('id')}'")


def _resolve_mapping(raw_cols: List[str], mapping: Dict[str, Any]) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
    raw_cols_lc = {c.strip().lower(): c for c in raw_cols}
    resolved: Dict[str, str] = {}
    missing: List[Tuple[str, str]] = []

    for canon, src_col in (mapping or {}).items():
        want = str(src_col).strip() if src_col is not None else ""
        if not want:
            continue
        if want in raw_cols:
            resolved[canon] = want
        else:
            key = want.lower()
            if key in raw_cols_lc:
                resolved[canon] = raw_cols_lc[key]
            else:
                missing.append((canon, want))
    return resolved, missing


def _coerce_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _apply_filters(logger: logging.Logger, inv_id: str, source_id: str, df: pd.DataFrame, filters: List[Dict[str, Any]]) -> pd.DataFrame:
    if not filters:
        return df

    mask = pd.Series([True] * len(df), index=df.index)

    for i, f in enumerate(filters, start=1):
        field = str(f.get("field") or "").strip()
        op = str(f.get("operator") or "").strip()
        if not field or not op:
            logger.warning(f"[{inv_id}] Source '{source_id}': skipping invalid filter #{i} (missing field/operator)")
            continue

        if field not in df.columns:
            logger.warning(f"[{inv_id}] Source '{source_id}': filter #{i} references missing field '{field}'")
            continue

        s = df[field]
        op_lc = op.lower()
        if op_lc == "isnull":
            m = s.isna() | (s.astype(str).str.strip() == "")
        elif op_lc == "isnotnull":
            m = ~(s.isna() | (s.astype(str).str.strip() == ""))
        elif op_lc == "regex":
            pattern = f.get("regex")
            if pattern is None:
                logger.warning(f"[{inv_id}] Source '{source_id}': filter #{i} operator=regex missing regex pattern")
                continue
            m = s.astype(str).fillna("").str.contains(str(pattern), regex=True, na=False)
        elif op in {"=", "==", "!=", ">", "<", ">=", "<="}:
            value = f.get("value")
            if value is None:
                logger.warning(f"[{inv_id}] Source '{source_id}': filter #{i} operator={op} missing value")
                continue

            s_str = s.astype(str).fillna("").str.strip()
            v_str = str(value).strip()

            s_num = _coerce_numeric(s_str)
            v_num = pd.to_numeric(v_str, errors="coerce")

            use_numeric = not pd.isna(v_num) and s_num.notna().any()

            if use_numeric:
                if op in {"=", "=="}:
                    m = (s_num == v_num)
                elif op == "!=":
                    m = (s_num != v_num)
                elif op == ">":
                    m = (s_num > v_num)
                elif op == "<":
                    m = (s_num < v_num)
                elif op == ">=":
                    m = (s_num >= v_num)
                elif op == "<=":
                    m = (s_num <= v_num)
            else:
                if op in {"=", "=="}:
                    m = (s_str == v_str)
                elif op == "!=":
                    m = (s_str != v_str)
                elif op == ">":
                    m = (s_str > v_str)
                elif op == "<":
                    m = (s_str < v_str)
                elif op == ">=":
                    m = (s_str >= v_str)
                elif op == "<=":
                    m = (s_str <= v_str)
        else:
            logger.warning(f"[{inv_id}] Source '{source_id}': unsupported operator '{op}' in filter #{i}")
            continue

        mask &= m

    before = len(df)
    out = df[mask].copy()
    after = len(out)
    logger.info(f"[{inv_id}] Source '{source_id}': filters applied (rows {before:,} -> {after:,})")
    return out


def _canonicalize_source(
    vtx_root: Path,
    logger: logging.Logger,
    inv_id: str,
    src: Dict[str, Any],
    attributes: List[str],
) -> pd.DataFrame:
    sid = src.get("id", "<missing>")
    raw = _read_source_df(vtx_root, src)
    raw = _apply_filters(logger, inv_id, sid, raw, src.get("filters") or [])

    resolved_map, missing = _resolve_mapping(list(raw.columns), src.get("map") or {})
    if missing:
        logger.warning(
            f"[{inv_id}] Source '{sid}': missing mapped columns: " +
            ", ".join([f"{c}->{s}" for c, s in missing])
        )
        logger.info(f"[{inv_id}] Source '{sid}': available columns sample: {list(raw.columns)[:60]}")
    else:
        logger.info(f"[{inv_id}] Source '{sid}': all mapped columns resolved OK")

    out = pd.DataFrame()
    for a in attributes:
        if a in resolved_map and resolved_map[a] in raw.columns:
            out[a] = raw[resolved_map[a]].astype(str).fillna("").map(_norm_str)
        else:
            out[a] = ""

    out["__source_id__"] = sid
    out["__row_id__"] = [f"{sid}::{i}" for i in range(len(out))]
    return out


# ---------------------------------------------------------------------
# Matching + inventory building
# ---------------------------------------------------------------------

def _get_norm_rules(globals_cfg: Dict[str, Any], normalize_as: str) -> Dict[str, Any]:
    d = (globals_cfg.get("default_normalization") or {})
    rules = d.get(normalize_as) or {}
    if "strip" not in rules:
        rules["strip"] = True
    if normalize_as == "hostname":
        rules.setdefault("casefold", True)
        rules.setdefault("fqdn_to_shortname", True)
    return rules


def _build_components(
    globals_cfg: Dict[str, Any],
    inv: Dict[str, Any],
    logger: logging.Logger,
    canon_rows: pd.DataFrame,
) -> pd.Series:
    match = inv.get("match") or {}
    condition = (match.get("condition") or "any").strip().lower()
    if condition not in {"any", "all"}:
        raise ValueError(f"[{inv.get('id')}] match.condition must be any|all")

    keys = match.get("keys") or []
    if not keys:
        raise ValueError(f"[{inv.get('id')}] match.keys is required")

    key_specs: List[Tuple[str, str]] = []
    for k in keys:
        name = (k.get("name") or "").strip()
        if not name:
            raise ValueError(f"[{inv.get('id')}] match.keys entries require name")
        normalize_as = (k.get("type") or "").strip().lower() or "generic"
        key_specs.append((name, normalize_as))

    tmp = canon_rows.copy()
    for attr_name, normalize_as in key_specs:
        rules = _get_norm_rules(globals_cfg, normalize_as)
        if attr_name not in tmp.columns:
            tmp[attr_name] = ""
        tmp[f"__norm__{attr_name}"] = _normalize_series(tmp[attr_name], rules)

    n = len(tmp)
    uf = UnionFind(n)
    token_owner: Dict[str, int] = {}

    if condition == "any":
        for i in range(n):
            for attr_name, _normalize_as in key_specs:
                v = tmp.at[i, f"__norm__{attr_name}"]
                if not v:
                    continue
                token = f"{attr_name}::{v}"
                if token in token_owner:
                    uf.union(i, token_owner[token])
                else:
                    token_owner[token] = i
    else:
        for i in range(n):
            parts: List[str] = []
            all_present = True
            for attr_name, _normalize_as in key_specs:
                v = tmp.at[i, f"__norm__{attr_name}"]
                if not v:
                    all_present = False
                    break
                parts.append(f"{attr_name}::{v}")
            if not all_present:
                continue
            token = "||".join(parts)
            if token in token_owner:
                uf.union(i, token_owner[token])
            else:
                token_owner[token] = i

    root_to_comp: Dict[int, int] = {}
    comp_ids: List[int] = []
    for i in range(n):
        r = uf.find(i)
        if r not in root_to_comp:
            root_to_comp[r] = len(root_to_comp) + 1
        comp_ids.append(root_to_comp[r])
    return pd.Series(comp_ids, index=canon_rows.index, dtype=int)


def _trusted_source_order_for_attr(
    inv: Dict[str, Any],
    sources: List[Dict[str, Any]],
    attr: str,
) -> List[str]:
    tr = inv.get("trust_ranking") or {}
    if attr in tr and isinstance(tr[attr], list) and tr[attr]:
        return [str(x) for x in tr[attr]]
    ordered = sorted(sources, key=lambda s: int(s.get("priority", 999999)))
    return [str(s.get("id")) for s in ordered]


def _build_unified_inventory(
    inv: Dict[str, Any],
    logger: logging.Logger,
    sources: List[Dict[str, Any]],
    canon_rows: pd.DataFrame,
    comp_id: pd.Series,
) -> pd.DataFrame:
    inv_id = inv.get("id", "<missing>")
    outputs = inv.get("outputs") or {}
    it = outputs.get("inventory_table") or {}

    include_presence_flags = bool(it.get("include_presence_flags", True))
    include_value_provenance = bool(it.get("include_value_provenance", True))
    include_ranked_values = bool(it.get("include_ranked_values", True))
    ranked_delim = str(it.get("ranked_values_delimiter", ";"))

    attributes: List[str] = inv.get("attributes") or []
    if not attributes:
        raise ValueError(f"[{inv_id}] attributes is required")

    df = canon_rows.copy()
    df["__component_id__"] = comp_id.values

    src_ids = [str(s.get("id")) for s in sources if s.get("enabled", True)]
    pres = (
        df.groupby(["__component_id__", "__source_id__"], dropna=False)
          .size()
          .reset_index(name="n")
    )
    pres["present"] = True
    pres_pivot = pres.pivot(index="__component_id__", columns="__source_id__", values="present").fillna(False)

    rows_out: List[Dict[str, Any]] = []
    grouped = df.groupby("__component_id__", dropna=False)

    for _cid, g in grouped:
        out_row: Dict[str, Any] = {}

        if include_presence_flags:
            for sid in src_ids:
                out_row[f"present__{sid}"] = bool(pres_pivot.at[_cid, sid]) if sid in pres_pivot.columns else False

        for attr in attributes:
            per_source: Dict[str, str] = {}
            for sid in src_ids:
                sg = g[g["__source_id__"] == sid]
                v = ""
                if not sg.empty:
                    vals = [x for x in sg[attr].astype(str).map(_norm_str).tolist() if x]
                    v = vals[0] if vals else ""
                per_source[sid] = v

            order = _trusted_source_order_for_attr(inv, sources, attr)

            ranked_vals: List[str] = []
            ranked_srcs: List[str] = []
            for sid in order:
                if sid in per_source and per_source[sid]:
                    ranked_vals.append(per_source[sid])
                    ranked_srcs.append(sid)

            chosen_val = ranked_vals[0] if ranked_vals else ""
            out_row[attr] = chosen_val

            if include_value_provenance:
                out_row[f"{attr}__source"] = ranked_srcs[0] if ranked_srcs else ""

            if include_ranked_values:
                out_row[f"{attr}__ranked"] = ranked_delim.join(ranked_vals)

        rows_out.append(out_row)

    out = pd.DataFrame(rows_out)

    fields = it.get("fields")
    if not fields or fields == "*" or fields == ["*"]:
        return out

    if isinstance(fields, str):
        fields_list = [fields]
    else:
        fields_list = list(fields)

    keep = [c for c in fields_list if c in out.columns]
    missing = [c for c in fields_list if c not in out.columns]
    if missing:
        logger.warning("[%s] inventory_table.fields missing columns omitted: %s", inv_id, missing)
    return out[keep].copy()


def _build_presence_matrix(
    globals_cfg: Dict[str, Any],
    inv: Dict[str, Any],
    logger: logging.Logger,
    sources: List[Dict[str, Any]],
    canon_rows: pd.DataFrame,
    report: Dict[str, Any],
) -> pd.DataFrame:
    inv_id = inv.get("id", "<missing>")
    attr = (report.get("attribute") or "").strip()
    if not attr:
        raise ValueError(f"[{inv_id}] presence_matrix_reports entry missing attribute")

    normalize_as = (report.get("normalize_as") or "").strip().lower() or "generic"
    rules = _get_norm_rules(globals_cfg, normalize_as)

    src_ids = [str(s.get("id")) for s in sources if s.get("enabled", True)]
    display_attrs = report.get("display_attributes") or []
    display_attrs = [str(x).strip() for x in display_attrs if str(x).strip()]

    tmp = canon_rows.copy()
    if attr not in tmp.columns:
        tmp[attr] = ""
    tmp["__val__"] = _normalize_series(tmp[attr], rules)
    tmp = tmp[tmp["__val__"] != ""]

    display_lookup: Dict[str, Dict[str, Dict[str, str]]] = {}

    if display_attrs:
        tmp_all = canon_rows.copy()
        if attr not in tmp_all.columns:
            tmp_all[attr] = ""
        tmp_all["__key__"] = _normalize_series(tmp_all[attr], rules)
        tmp_all = tmp_all[tmp_all["__key__"] != ""]

        for a2 in display_attrs:
            display_lookup[a2] = {}
            for sid in src_ids:
                display_lookup[a2][sid] = {}
                sub = tmp_all[tmp_all["__source_id__"] == sid]
                for _idx, row in sub.iterrows():
                    key = str(row.get("__key__", "")).strip()
                    if not key:
                        continue
                    val = str(row.get(a2, "")).strip()
                    if key not in display_lookup[a2][sid] and val:
                        display_lookup[a2][sid][key] = val

    source_of_truth = report.get("source_of_truth")
    sot = str(source_of_truth) if source_of_truth else None

    per_source_vals: Dict[str, set[str]] = {sid: set() for sid in src_ids}
    for sid in src_ids:
        vals = tmp[tmp["__source_id__"] == sid]["__val__"].astype(str).str.strip()
        per_source_vals[sid] = set(v for v in vals if v)

    values = sorted(set(tmp["__val__"].tolist()))
    rows: List[Dict[str, Any]] = []
    for v in values:
        r: Dict[str, Any] = {attr: v}
        for a2 in display_attrs:
            order = _trusted_source_order_for_attr(inv, sources, a2)
            chosen = ""
            for sid in order:
                val = display_lookup.get(a2, {}).get(sid, {}).get(v, "")
                if val:
                    chosen = val
                    break
            r[a2] = chosen
        for sid in src_ids:
            r[sid] = (v in per_source_vals[sid])
        rows.append(r)

    return pd.DataFrame(rows)


def _build_presence_matrix_html(inv: Dict[str, Any], report: Dict[str, Any], pm_df: pd.DataFrame, out_csv_path: Path) -> str:
    attr = str(report.get("attribute") or "Hostname").strip() or "Hostname"
    bool_columns = [
        c for c in pm_df.columns
        if c not in {attr} and (pm_df[c].dropna().empty or pm_df[c].dropna().isin([True, False]).all())
    ]
    text_columns = [c for c in pm_df.columns if c not in {attr} and c not in bool_columns]
    records: List[Dict[str, Any]] = []
    for row in pm_df.to_dict(orient="records"):
        rec: Dict[str, Any] = {}
        for key, value in row.items():
            if key in bool_columns:
                rec[key] = bool(value)
            elif pd.isna(value):
                rec[key] = ""
            else:
                rec[key] = str(value)
        records.append(rec)

    total_hosts = len(pm_df)
    total_true = sum(int(pm_df[c].fillna(False).astype(bool).sum()) for c in bool_columns) if bool_columns else 0
    total_cells = len(pm_df) * len(bool_columns)
    avg_completeness = (total_true / total_cells) if total_cells else 0.0

    title = "Server Inventory Matrix"
    subtitle = str(report.get("id") or attr).replace("_", " ").title()
    csv_href = escape(out_csv_path.name)
    data_json = json.dumps(records)
    bool_cols_json = json.dumps(bool_columns)
    text_cols_json = json.dumps(text_columns)
    attr_json = json.dumps(attr)
    subtitle_html = escape(subtitle)

    return f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>{title}</title>
  <style>
    :root {{
      --bg: #0b1320;
      --bg-2: #101d2f;
      --panel: #122238;
      --line: #2b4667;
      --text: #edf4ff;
      --muted: #a9bdd7;
      --accent: #f0b24a;
      --good-bg: rgba(124, 199, 162, 0.18);
      --good-text: #c9f2db;
      --bad-bg: rgba(224, 112, 112, 0.16);
      --bad-text: #ffd4d4;
      --shadow: 0 18px 48px rgba(0, 0, 0, 0.28);
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; color: var(--text); background: radial-gradient(circle at top right, rgba(121, 184, 255, 0.14), transparent 32%), radial-gradient(circle at bottom left, rgba(240, 178, 74, 0.1), transparent 36%), linear-gradient(180deg, var(--bg), var(--bg-2)); font: 400 14px/1.5 \"Avenir Next\", \"Trebuchet MS\", \"Helvetica Neue\", sans-serif; }}
    a {{ color: inherit; }}
    .topbar {{ position: sticky; top: 0; z-index: 10; backdrop-filter: blur(8px); background: rgba(8, 16, 28, 0.82); border-bottom: 1px solid rgba(67, 100, 135, 0.5); }}
    .topbar-inner {{ width: min(1500px, calc(100% - 32px)); margin: 0 auto; min-height: 74px; display: flex; gap: 16px; align-items: center; padding: 12px 0; }}
    .brandmark {{ width: 48px; height: 48px; border-radius: 14px; border: 1px solid rgba(121, 184, 255, 0.35); background: linear-gradient(135deg, rgba(240, 178, 74, 0.22), rgba(121, 184, 255, 0.18)), rgba(17, 30, 49, 0.95); display: grid; place-items: center; font-weight: 800; letter-spacing: 0.08em; color: var(--accent); box-shadow: var(--shadow); flex: 0 0 auto; }}
    .eyebrow {{ margin: 0; font-size: 11px; text-transform: uppercase; letter-spacing: 0.18em; color: var(--muted); }}
    .brandline {{ margin: 2px 0 0; font-size: 24px; font-weight: 700; line-height: 1.2; }}
    .instance {{ margin: 4px 0 0; font-size: 13px; color: var(--muted); }}
    .toolbar {{ margin-left: auto; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }}
    .toolbtn {{ border: 1px solid rgba(67, 100, 135, 0.7); background: rgba(19, 34, 56, 0.92); color: var(--text); padding: 10px 14px; border-radius: 999px; text-decoration: none; font-size: 13px; font-weight: 600; }}
    .shell {{ width: min(1500px, calc(100% - 32px)); margin: 0 auto; padding: 22px 0 60px; }}
    .hero {{ display: grid; grid-template-columns: minmax(420px, 1.7fr) minmax(300px, 0.9fr); gap: 20px; margin-top: 18px; }}
    .panel {{ border: 1px solid rgba(58, 88, 121, 0.72); border-radius: 24px; background: linear-gradient(180deg, rgba(19, 35, 57, 0.96), rgba(15, 28, 45, 0.96)); box-shadow: var(--shadow); padding: 24px; }}
    .header {{ margin: 0 0 12px; font-size: 22px; font-weight: 700; }}
    .copy {{ color: #cfe0f6; margin: 0 0 14px; }}
    .subhead {{ margin: 18px 0 8px; font-size: 14px; text-transform: uppercase; letter-spacing: 0.12em; color: var(--accent); }}
    .summary-row {{ display: flex; justify-content: space-between; gap: 16px; padding: 8px 0; border-bottom: 1px solid rgba(43, 70, 103, 0.35); }}
    .summary-row:last-child {{ border-bottom: 0; }}
    .summary-label, .summary-value {{ font-size: 14px; color: var(--text); }}
    .summary-value {{ font-weight: 400; }}
    .table-shell {{ margin-top: 18px; border: 1px solid rgba(58, 88, 121, 0.72); border-radius: 24px; background: linear-gradient(180deg, rgba(19, 35, 57, 0.96), rgba(15, 28, 45, 0.96)); box-shadow: var(--shadow); overflow: hidden; }}
    .table-tools {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; padding: 16px 18px; border-bottom: 1px solid rgba(43, 70, 103, 0.4); }}
    .search-input {{ width: min(360px, 50vw); border: 1px solid rgba(67, 100, 135, 0.65); background: rgba(14, 24, 40, 0.92); color: var(--text); padding: 10px 12px; border-radius: 999px; outline: none; }}
    .table-wrap {{ overflow-x: auto; padding: 0 10px 18px; }}
    table {{ width: 100%; border-collapse: separate; border-spacing: 0 8px; font-size: 13px; min-width: 960px; }}
    th, td {{ text-align: left; padding: 10px 12px; border: 0; }}
    thead th {{ position: sticky; top: 0; z-index: 2; background: rgba(11, 19, 32, 0.96); vertical-align: bottom; }}
    th button {{ all: unset; cursor: pointer; font-weight: 700; color: var(--text); }}
    tbody tr {{ background: rgba(10, 19, 32, 0.7); }}
    tbody tr td:first-child, thead th:first-child {{ border-top-left-radius: 14px; border-bottom-left-radius: 14px; }}
    tbody tr td:last-child, thead th:last-child {{ border-top-right-radius: 14px; border-bottom-right-radius: 14px; }}
    .bool-cell {{ text-align: center; }}
    .bool-chip {{ display: inline-flex; align-items: center; gap: 6px; font-weight: 600; color: var(--text); }}
    .bool-dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; }}
    .bool-dot-true {{ background: #7cc7a2; box-shadow: 0 0 0 3px rgba(124, 199, 162, 0.12); }}
    .bool-dot-false {{ background: #e07070; box-shadow: 0 0 0 3px rgba(224, 112, 112, 0.1); }}
    .col-meta {{ margin-top: 8px; font-size: 11px; color: var(--muted); line-height: 1.45; }}
    .col-meta strong {{ color: var(--text); font-weight: 600; }}
    .col-filter {{ margin-top: 8px; width: 100%; border: 1px solid rgba(67, 100, 135, 0.55); background: rgba(14, 24, 40, 0.92); color: var(--text); border-radius: 10px; padding: 6px 8px; font-size: 11px; }}
    .footer-spacer {{ height: 48px; }}
    @media (max-width: 1080px) {{ .hero {{ grid-template-columns: 1fr; }} .table-tools {{ flex-direction: column; align-items: flex-start; }} .search-input {{ width: 100%; }} }}
  </style>
</head>
<body>
  <div class=\"topbar\">
    <div class=\"topbar-inner\">
      <div class=\"brandmark\">VTX</div>
      <div class=\"brandcopy\">
        <p class=\"eyebrow\">Vector-TX</p>
        <p class=\"brandline\">Server Inventory Matrix</p>
        <p class=\"instance\">{subtitle_html}</p>
      </div>
      <div class=\"toolbar\">
        <a class=\"toolbtn\" href=\"{csv_href}\" download>Download CSV</a>
        <a class=\"toolbtn\" href=\"/logout\">Logout</a>
      </div>
    </div>
  </div>
  <div class=\"shell\">
    <div class=\"hero\">
      <section class=\"panel\">
        <h1 class=\"header\">{title}</h1>
        <p class=\"copy\">This report shows a unified, deduplicated list of all server hostnames that exist in all of your inventories which contain server data. VTX creates this master list of hosts, then compares this list against each individual source to identify which hosts are present and missing in each data source.</p>
        <h2 class=\"subhead\">Next Steps</h2>
        <p class=\"copy\">Using this list, we can identify if any of your inventory systems or tools needs to be updated. This is especially important for Application to Server Mapping. If your servers are not accounted for, they cannot be mapped to applications.</p>
      </section>
      <aside class=\"panel\">
        <h2 class=\"header\">Data Summary</h2>
        <div class=\"summary-row\"><div class=\"summary-label\">Total Unique Hostnames</div><div class=\"summary-value\">{total_hosts:,}</div></div>
        <div class=\"summary-row\"><div class=\"summary-label\">Average Completeness of Each Source</div><div class=\"summary-value\">{avg_completeness * 100:0.1f}%</div></div>
      </aside>
    </div>
    <section class=\"table-shell\">
      <div class=\"table-tools\">
        <div>Search hostnames and compare source coverage in real time.</div>
        <input id=\"searchInput\" class=\"search-input\" type=\"search\" placeholder=\"Search hostnames...\" />
      </div>
      <div class=\"table-wrap\">
        <table id=\"matrixTable\">
          <thead id=\"matrixHead\"></thead>
          <tbody id=\"matrixBody\"></tbody>
        </table>
      </div>
    </section>
    <div class=\"footer-spacer\"></div>
  </div>
  <script>
    const ATTR_COL = {attr_json};
    const BOOL_COLS = {bool_cols_json};
    const TEXT_COLS = {text_cols_json};
    const ROWS = {data_json};
    const state = {{
      search: '',
      sortKey: ATTR_COL,
      sortDir: 'asc',
      filters: Object.fromEntries(BOOL_COLS.map((c) => [c, 'all'])),
    }};

    function passesFilters(row) {{
      const search = state.search.trim().toLowerCase();
      if (search && !String(row[ATTR_COL] || '').toLowerCase().includes(search)) return false;
      for (const col of BOOL_COLS) {{
        const mode = state.filters[col];
        if (mode === 'all') continue;
        if (mode === 'true' && row[col] !== true) return false;
        if (mode === 'false' && row[col] !== false) return false;
      }}
      return true;
    }}

    function compareValues(a, b) {{
      if (typeof a === 'boolean' && typeof b === 'boolean') {{
        if (a === b) return 0;
        return a ? 1 : -1;
      }}
      return String(a || '').localeCompare(String(b || ''), undefined, {{ sensitivity: 'base', numeric: true }});
    }}

    function getVisibleRows() {{
      const filtered = ROWS.filter(passesFilters);
      filtered.sort((a, b) => {{
        const cmp = compareValues(a[state.sortKey], b[state.sortKey]);
        return state.sortDir === 'asc' ? cmp : -cmp;
      }});
      return filtered;
    }}

    function columnCounts(rows, col) {{
      let t = 0;
      let f = 0;
      for (const row of rows) {{
        if (row[col] === true) t += 1;
        else if (row[col] === false) f += 1;
      }}
      return {{ t, f }};
    }}

    function setSort(col) {{
      if (state.sortKey === col) state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
      else {{ state.sortKey = col; state.sortDir = 'asc'; }}
      render();
    }}

    function renderHead(rows) {{
      const head = document.getElementById('matrixHead');
      const tr = document.createElement('tr');

      const baseTh = document.createElement('th');
      const baseBtn = document.createElement('button');
      baseBtn.textContent = ATTR_COL;
      baseBtn.addEventListener('click', () => setSort(ATTR_COL));
      baseTh.appendChild(baseBtn);
      tr.appendChild(baseTh);

      for (const col of TEXT_COLS) {{
        const th = document.createElement('th');
        const btn = document.createElement('button');
        btn.textContent = col;
        btn.addEventListener('click', () => setSort(col));
        th.appendChild(btn);
        tr.appendChild(th);
      }}

      for (const col of BOOL_COLS) {{
        const counts = columnCounts(rows, col);
        const th = document.createElement('th');

        const btn = document.createElement('button');
        btn.textContent = col;
        btn.addEventListener('click', () => setSort(col));
        th.appendChild(btn);

        const meta = document.createElement('div');
        meta.className = 'col-meta';
        meta.innerHTML = `<div><strong>TRUE:</strong> ${{counts.t}}</div><div><strong>FALSE:</strong> ${{counts.f}}</div>`;
        th.appendChild(meta);

        const select = document.createElement('select');
        select.className = 'col-filter';
        select.innerHTML = '<option value=\"all\">All</option><option value=\"true\">Only TRUE</option><option value=\"false\">Only FALSE</option>';
        select.value = state.filters[col] || 'all';
        select.addEventListener('change', (e) => {{ state.filters[col] = e.target.value; render(); }});
        th.appendChild(select);

        tr.appendChild(th);
      }}

      head.replaceChildren(tr);
    }}

    function renderBody(rows) {{
      const body = document.getElementById('matrixBody');
      const frag = document.createDocumentFragment();
      for (const row of rows) {{
        const tr = document.createElement('tr');

        const nameTd = document.createElement('td');
        nameTd.textContent = row[ATTR_COL] || '';
        tr.appendChild(nameTd);

        for (const col of TEXT_COLS) {{
          const td = document.createElement('td');
          td.textContent = row[col] || '';
          tr.appendChild(td);
        }}

        for (const col of BOOL_COLS) {{
          const td = document.createElement('td');
          td.className = 'bool-cell';
          const chip = document.createElement('span');
          chip.className = 'bool-chip';
          const truthy = row[col] === true;
          const dot = document.createElement('span');
          dot.className = truthy ? 'bool-dot bool-dot-true' : 'bool-dot bool-dot-false';
          const label = document.createElement('span');
          label.textContent = truthy ? 'TRUE' : 'FALSE';
          chip.appendChild(dot);
          chip.appendChild(label);
          td.appendChild(chip);
          tr.appendChild(td);
        }}
        frag.appendChild(tr);
      }}
      body.replaceChildren(frag);
    }}

    function render() {{
      const rows = getVisibleRows();
      renderHead(rows);
      renderBody(rows);
    }}

    document.getElementById('searchInput').addEventListener('input', (e) => {{
      state.search = e.target.value || '';
      render();
    }});

    render();
  </script>
</body>
</html>"""


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Server Inventory Matrix (VTX)")
    ap.add_argument("--job", default=None, help="Optional job id filter")
    args = ap.parse_args(argv)

    cfg_path = DEFAULT_CONFIG_PATH
    if not cfg_path.exists() and FALLBACK_CONFIG_PATH.exists():
        logger.warning("Default run config missing; falling back to %s", FALLBACK_CONFIG_PATH)
        cfg_path = FALLBACK_CONFIG_PATH

    if not cfg_path.exists():
        raise FileNotFoundError(f"Config file not found: {cfg_path}")

    cfg = load_yaml(cfg_path)
    payload = cfg.get("payload") if isinstance(cfg.get("payload"), dict) else {}
    payload = payload or {}

    globals_cfg = payload.get("globals") if isinstance(payload.get("globals"), dict) else cfg.get("globals") or {}

    vtx_root_override = None
    override_raw = str(globals_cfg.get("vtx_root") or "").strip()
    if override_raw:
        vtx_root_override = Path(override_raw).expanduser().resolve()

    inventories = payload.get("inventories") or cfg.get("inventories") or []
    if not inventories:
        logger.error("No inventories found in config.")
        return 2

    enabled_inventories = [i for i in inventories if i.get("enabled", True)]
    inv_by_id: Dict[str, Dict[str, Any]] = {}
    for inv in enabled_inventories:
        inv_id = str(inv.get("id") or "").strip()
        if not inv_id:
            logger.error("Encountered enabled inventory missing id.")
            return 2
        inv_by_id[inv_id] = inv

    jobs = payload.get("jobs") or cfg.get("jobs") or []
    enabled_jobs = [j for j in jobs if isinstance(j, dict) and j.get("enabled", True)]
    if not enabled_jobs:
        logger.error("No enabled jobs found in config.")
        return 2

    if args.job:
        target = str(args.job).strip()
        exact = [j for j in enabled_jobs if str(j.get("id") or "").strip() == target]
        if not exact:
            exact = [j for j in enabled_jobs if str(j.get("id") or "").strip().lower() == target.lower()]
        if not exact:
            logger.error("No enabled job matched --job=%s", target)
            return 2
        enabled_jobs = exact

    inv_to_jobs: Dict[str, int] = {}
    for job in enabled_jobs:
        inv_ref = str(job.get("inventory_id") or "").strip()
        jid = str(job.get("id") or "<missing>")
        if not inv_ref:
            logger.error("[job:%s] Missing required field: inventory_id", jid)
            return 2
        if inv_ref not in inv_by_id:
            logger.error("[job:%s] inventory_id '%s' does not match any enabled inventory", jid, inv_ref)
            return 2
        inv_to_jobs[inv_ref] = inv_to_jobs.get(inv_ref, 0) + 1

    missing_job_inventories = [iid for iid in inv_by_id.keys() if iid not in inv_to_jobs]
    if missing_job_inventories:
        logger.error("Enabled inventories missing a corresponding enabled job: %s", missing_job_inventories)
        return 2

    for job in enabled_jobs:
        jid = str(job.get("id") or "<missing>")
        inv_ref = str(job.get("inventory_id") or "").strip()
        inv = dict(inv_by_id[inv_ref])
        job_outputs = job.get("outputs") or {}
        if not isinstance(job_outputs, dict) or not job_outputs:
            logger.error("[job:%s] Missing required outputs block", jid)
            return 2
        inv["outputs"] = job_outputs

        inv_id = inv.get("id", "<missing>")
        logger.info("=== Job: %s | Inventory: %s ===", jid, inv_id)

        sources = [s for s in (inv.get("sources") or []) if s.get("enabled", True)]
        if not sources:
            logger.error("[job:%s][%s] No enabled sources.", jid, inv_id)
            continue

        sources = sorted(sources, key=lambda s: int(s.get("priority", 999999)))

        attributes: List[str] = inv.get("attributes") or []
        canon_parts: List[pd.DataFrame] = []
        for src in sources:
            sid = src.get("id", "<missing>")
            logger.info("[job:%s][%s] Loading source '%s'", jid, inv_id, sid)
            dfc = _canonicalize_source(vtx_root_override or VTX_ROOT, logger, inv_id, src, attributes)
            canon_parts.append(dfc)

        canon_rows = pd.concat(canon_parts, ignore_index=True)
        logger.info("[job:%s][%s] Total canonical rows across sources: %s", jid, inv_id, f"{len(canon_rows):,}")

        comp_id = _build_components(globals_cfg, inv, logger, canon_rows)
        logger.info("[job:%s][%s] Unified assets (components): %s", jid, inv_id, f"{comp_id.nunique():,}")

        outputs = inv.get("outputs") or {}
        wrote_any_output = False

        logger.info(
            "[job:%s][%s] Write phase starting. output_keys=%s",
            jid,
            inv_id,
            sorted(list(outputs.keys())) if isinstance(outputs, dict) else [],
        )
        it = outputs.get("inventory_table")
        if it:
            raw_path = it.get("path") or ""
            out_path = vtx_path(raw_path, base_root=vtx_root_override)
            if not str(out_path):
                raise ValueError(f"[{inv_id}] outputs.inventory_table.path is required")
            logger.info(
                "[job:%s][%s] inventory_table configured: raw_path='%s' resolved_path='%s'",
                jid,
                inv_id,
                raw_path,
                out_path,
            )
            out_path.parent.mkdir(parents=True, exist_ok=True)

            try:
                logger.info("[job:%s][%s] Building inventory_table dataframe...", jid, inv_id)
                inv_df = _build_unified_inventory(inv, logger, sources, canon_rows, comp_id)
                logger.info(
                    "[job:%s][%s] inventory_table dataframe built (rows=%s, cols=%s). Writing CSV...",
                    jid,
                    inv_id,
                    f"{len(inv_df):,}",
                    f"{len(inv_df.columns):,}",
                )
                inv_df.to_csv(out_path, index=False)
                wrote_any_output = True
                logger.info("[job:%s][%s] Wrote inventory_table: %s (rows=%s, cols=%s)", jid, inv_id, out_path, f"{len(inv_df):,}", f"{len(inv_df.columns):,}")
            except Exception:
                logger.exception("[job:%s][%s] Failed writing inventory_table to %s", jid, inv_id, out_path)
                return 2

        pm = outputs.get("presence_matrix_reports") or []
        logger.info("[job:%s][%s] presence_matrix_reports configured count=%s", jid, inv_id, len(pm))
        for r in pm:
            rid = r.get("id", "<missing>")
            raw_pm_path = r.get("path") or ""
            out_path = vtx_path(raw_pm_path, base_root=vtx_root_override)
            if not str(out_path):
                raise ValueError(f"[{inv_id}] presence_matrix_reports[{rid}].path is required")
            logger.info(
                "[job:%s][%s] presence_matrix '%s' configured: raw_path='%s' resolved_path='%s'",
                jid,
                inv_id,
                rid,
                raw_pm_path,
                out_path,
            )
            out_path.parent.mkdir(parents=True, exist_ok=True)

            try:
                logger.info("[job:%s][%s] Building presence_matrix '%s' dataframe...", jid, inv_id, rid)
                pm_df = _build_presence_matrix(globals_cfg, inv, logger, sources, canon_rows, r)
                logger.info(
                    "[job:%s][%s] presence_matrix '%s' dataframe built (rows=%s, cols=%s). Writing CSV...",
                    jid,
                    inv_id,
                    rid,
                    f"{len(pm_df):,}",
                    f"{len(pm_df.columns):,}",
                )
                pm_df.to_csv(out_path, index=False)
                wrote_any_output = True
                logger.info("[job:%s][%s] Wrote presence_matrix '%s': %s (rows=%s, cols=%s)", jid, inv_id, rid, out_path, f"{len(pm_df):,}", f"{len(pm_df.columns):,}")

                html_raw = str(r.get("html_path") or "").strip()
                html_path = vtx_path(html_raw, base_root=vtx_root_override) if html_raw else out_path.with_suffix('.html')
                logger.info(
                    "[job:%s][%s] Writing presence_matrix HTML '%s' to %s",
                    jid,
                    inv_id,
                    rid,
                    html_path,
                )
                html_path.parent.mkdir(parents=True, exist_ok=True)
                html_path.write_text(_build_presence_matrix_html(inv, r, pm_df, out_path), encoding="utf-8")
                wrote_any_output = True
                logger.info("[job:%s][%s] Wrote presence_matrix HTML '%s': %s", jid, inv_id, rid, html_path)
            except Exception:
                logger.exception("[job:%s][%s] Failed writing presence_matrix '%s'", jid, inv_id, rid)
                return 2

        if not wrote_any_output:
            logger.error(
                "[job:%s][%s] No outputs were written even though outputs were configured. "
                "Check output paths and job outputs schema.",
                jid,
                inv_id,
            )
            return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
