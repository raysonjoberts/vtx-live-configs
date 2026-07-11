#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
server_raw_table_builder.py
---------------------------
Build a coalesced raw server inventory parquet from the server profile YAML.

Primary contract:
  usr/config/run/server_profile_tables.yaml

Legacy fallback is retained for older server_inventory_matrix_vtx/raw-table
configs so existing behavior is not broken if that older config is still used.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
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
DEFAULT_CONFIG_PATH = VTX_ROOT / "usr" / "config" / "run" / "server_profile_tables.yaml"
LEGACY_CONFIG_PATH = VTX_ROOT / "usr" / "config" / "run" / "server_inventory_matrix_vtx.yaml"


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


logger = get_logger("server_raw_table_builder")


def load_yaml(path: Path) -> Dict[str, Any]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = doc.get("config") if isinstance(doc.get("config"), dict) else doc
    if not isinstance(cfg, dict):
        raise ValueError(f"Invalid config root: {path}")
    return cfg


def read_table(path: Path) -> pd.DataFrame:
    if str(path).lower().endswith(".parquet"):
        df = pd.read_parquet(path).fillna("")
    else:
        df = pd.read_csv(path, dtype=str, keep_default_na=False).fillna("")
    for col in df.columns:
        df[col] = df[col].map(_norm_str)
    return df


def _norm_str(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if s.lower() in {"nan", "none", "null", "n/a", "na", "unknown", "undef", "undefined", "-", "--"}:
        return ""
    if s.endswith(".0"):
        try:
            n = float(s)
            if n.is_integer():
                return str(int(n))
        except Exception:
            pass
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
    return s2.replace({"nan": "", "none": "", "null": ""})


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
        elif want.lower() in raw_cols_lc:
            resolved[canon] = raw_cols_lc[want.lower()]
        else:
            missing.append((canon, want))
    return resolved, missing


def _coerce_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _apply_filters(inv_id: str, source_id: str, df: pd.DataFrame, filters: List[Dict[str, Any]]) -> pd.DataFrame:
    if not filters:
        return df

    mask = pd.Series([True] * len(df), index=df.index)
    for i, filt in enumerate(filters, start=1):
        field = str(filt.get("field") or "").strip()
        op = str(filt.get("operator") or "").strip().lower()
        if not field or not op or field not in df.columns:
            logger.warning("[%s] Source '%s': invalid filter #%d", inv_id, source_id, i)
            continue
        series = df[field]
        if op == "isnull":
            keep = series.isna() | (series.astype(str).str.strip() == "")
        elif op == "isnotnull":
            keep = ~(series.isna() | (series.astype(str).str.strip() == ""))
        elif op == "regex":
            pattern = str(filt.get("regex") or "")
            keep = series.astype(str).fillna("").str.contains(pattern, regex=True, na=False)
        elif op in {"=", "==", "!=", ">", "<", ">=", "<="}:
            value = filt.get("value")
            if value is None:
                continue
            s_str = series.astype(str).fillna("").str.strip()
            v_str = str(value).strip()
            s_num = _coerce_numeric(s_str)
            v_num = pd.to_numeric(v_str, errors="coerce")
            use_numeric = not pd.isna(v_num) and s_num.notna().any()
            if use_numeric:
                if op in {"=", "=="}:
                    keep = s_num == v_num
                elif op == "!=":
                    keep = s_num != v_num
                elif op == ">":
                    keep = s_num > v_num
                elif op == "<":
                    keep = s_num < v_num
                elif op == ">=":
                    keep = s_num >= v_num
                else:
                    keep = s_num <= v_num
            else:
                if op in {"=", "=="}:
                    keep = s_str == v_str
                elif op == "!=":
                    keep = s_str != v_str
                elif op == ">":
                    keep = s_str > v_str
                elif op == "<":
                    keep = s_str < v_str
                elif op == ">=":
                    keep = s_str >= v_str
                else:
                    keep = s_str <= v_str
        else:
            logger.warning("[%s] Source '%s': unsupported operator '%s'", inv_id, source_id, op)
            continue
        mask &= keep

    out = df[mask].copy()
    logger.info("[%s] Source '%s': filters applied (rows %s -> %s)", inv_id, source_id, f"{len(df):,}", f"{len(out):,}")
    return out


def _get_norm_rules(globals_cfg: Dict[str, Any], normalize_as: str) -> Dict[str, Any]:
    rules = ((globals_cfg.get("default_normalization") or {}).get(normalize_as) or {}).copy()
    rules.setdefault("strip", True)
    if normalize_as == "hostname":
        rules.setdefault("casefold", True)
        rules.setdefault("fqdn_to_shortname", True)
    return rules


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


def _canonicalize_source(inv_id: str, src: Dict[str, Any], attributes: List[str]) -> pd.DataFrame:
    sid = str(src.get("id") or "<missing>")
    raw = read_table(vtx_path(str(src.get("path") or ""), must_exist=True))
    raw = _apply_filters(inv_id, sid, raw, src.get("filters") or [])

    resolved_map, missing = _resolve_mapping(list(raw.columns), src.get("map") or {})
    if missing:
        logger.warning("[%s] Source '%s': missing mapped columns: %s", inv_id, sid, ", ".join(f"{c}->{s}" for c, s in missing))

    out = pd.DataFrame()
    for attr in attributes:
        if attr in resolved_map and resolved_map[attr] in raw.columns:
            out[attr] = raw[resolved_map[attr]].astype(str).fillna("").map(_norm_str)
        else:
            out[attr] = ""
    out["__source_id__"] = sid
    out["__row_id__"] = [f"{sid}::{i}" for i in range(len(out))]
    return out


def _nonblank_values(series: pd.Series) -> List[str]:
    return [x for x in series.astype(str).map(_norm_str).tolist() if x]


def _build_inventory_from_hostname_anchors(
    globals_cfg: Dict[str, Any],
    inv: Dict[str, Any],
    sources: List[Dict[str, Any]],
    canon_rows: pd.DataFrame,
    output_cfg: Dict[str, Any],
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    attributes = _inventory_fields(output_cfg, inv)
    source_ids = [str(s.get("id") or "") for s in sources if s.get("enabled", True)]
    hostname_rules = _get_norm_rules(globals_cfg, "hostname")
    ip_rules = _get_norm_rules(globals_cfg, "ip_address")

    df = canon_rows.copy()
    if "Hostname" not in df.columns:
        df["Hostname"] = ""
    if "IP Address" not in df.columns:
        df["IP Address"] = ""
    df["__norm_hostname__"] = _normalize_series(df["Hostname"], hostname_rules)
    df["__norm_ip__"] = _normalize_series(df["IP Address"], ip_rules)

    anchor_keys: List[str] = []
    seen: set[str] = set()
    for hostname in df["__norm_hostname__"].astype(str).tolist():
        if hostname and hostname not in seen:
            seen.add(hostname)
            anchor_keys.append(hostname)

    inventory_rows: List[Dict[str, Any]] = []
    ranking_rows: List[Dict[str, Any]] = []
    component_id = 0
    for anchor in anchor_keys:
        component_id += 1
        host_matches = df[df["__norm_hostname__"] == anchor]
        known_ips = {v for v in host_matches["__norm_ip__"].astype(str).tolist() if v}
        matched_by_source: Dict[str, pd.DataFrame] = {}
        for sid in source_ids:
            subset = host_matches[host_matches["__source_id__"] == sid]
            if subset.empty and known_ips:
                subset = df[(df["__source_id__"] == sid) & (df["__norm_ip__"].isin(known_ips))]
            matched_by_source[sid] = subset
            if not subset.empty:
                known_ips |= {v for v in subset["__norm_ip__"].astype(str).tolist() if v}

        out_row: Dict[str, Any] = {}
        for attr in attributes:
            ranked_vals: List[str] = []
            ranked_srcs: List[str] = []
            for sid in _trusted_source_order_for_attr(inv, sources, attr):
                subset = matched_by_source.get(sid)
                if subset is None or subset.empty or attr not in subset.columns:
                    continue
                vals = _nonblank_values(subset[attr])
                if not vals:
                    continue
                ranked_vals.append(vals[0])
                ranked_srcs.append(sid)

            chosen_val = ranked_vals[0] if ranked_vals else ""
            chosen_src = ranked_srcs[0] if ranked_srcs else ""
            if attr == "Hostname" and not chosen_val:
                chosen_val = anchor
            out_row[attr] = chosen_val
            ranking_rows.append(
                {
                    "__component_id__": component_id,
                    "Hostname": out_row.get("Hostname") or chosen_val or anchor,
                    "Attribute": attr,
                    "Chosen Value": chosen_val,
                    "Chosen Source": chosen_src,
                    "Ranked Values": ";".join(ranked_vals),
                    "Ranked Sources": ";".join(ranked_srcs),
                }
            )

        if not out_row.get("Hostname"):
            continue
        inventory_rows.append(out_row)

    return pd.DataFrame(inventory_rows), pd.DataFrame(ranking_rows)


def _build_components(globals_cfg: Dict[str, Any], inv: Dict[str, Any], canon_rows: pd.DataFrame) -> pd.Series:
    match = inv.get("match") or {}
    condition = str(match.get("condition") or "any").strip().lower()
    if condition not in {"any", "all"}:
        raise ValueError(f"[{inv.get('id')}] match.condition must be any|all")

    keys = match.get("keys") or []
    if not keys:
        raise ValueError(f"[{inv.get('id')}] match.keys is required")

    specs: List[Tuple[str, str]] = []
    for k in keys:
        name = str(k.get("name") or "").strip()
        normalize_as = str(k.get("type") or "generic").strip().lower()
        if not name:
            raise ValueError(f"[{inv.get('id')}] match.keys entries require name")
        specs.append((name, normalize_as))

    tmp = canon_rows.copy()
    for attr_name, normalize_as in specs:
        if attr_name not in tmp.columns:
            tmp[attr_name] = ""
        tmp[f"__norm__{attr_name}"] = _normalize_series(tmp[attr_name], _get_norm_rules(globals_cfg, normalize_as))

    uf = UnionFind(len(tmp))
    token_owner: Dict[str, int] = {}

    if condition == "any":
        for i in range(len(tmp)):
            for attr_name, _ in specs:
                value = tmp.at[i, f"__norm__{attr_name}"]
                if not value:
                    continue
                token = f"{attr_name}::{value}"
                if token in token_owner:
                    uf.union(i, token_owner[token])
                else:
                    token_owner[token] = i
    else:
        for i in range(len(tmp)):
            parts: List[str] = []
            complete = True
            for attr_name, _ in specs:
                value = tmp.at[i, f"__norm__{attr_name}"]
                if not value:
                    complete = False
                    break
                parts.append(f"{attr_name}::{value}")
            if not complete:
                continue
            token = "||".join(parts)
            if token in token_owner:
                uf.union(i, token_owner[token])
            else:
                token_owner[token] = i

    root_to_comp: Dict[int, int] = {}
    comp_ids: List[int] = []
    for i in range(len(tmp)):
        root = uf.find(i)
        if root not in root_to_comp:
            root_to_comp[root] = len(root_to_comp) + 1
        comp_ids.append(root_to_comp[root])
    return pd.Series(comp_ids, index=canon_rows.index, dtype=int)


def _trusted_source_order_for_attr(inv: Dict[str, Any], sources: List[Dict[str, Any]], attr: str) -> List[str]:
    tr = inv.get("trust_ranking") or {}
    ranked = tr.get(attr)
    if isinstance(ranked, list) and ranked:
        return [str(x) for x in ranked]
    ordered = sorted([s for s in sources if s.get("enabled", True)], key=lambda s: int(s.get("priority", 999999)))
    return [str(s.get("id") or "") for s in ordered]


def _inventory_fields(output_cfg: Dict[str, Any], inv: Dict[str, Any]) -> List[str]:
    fields = output_cfg.get("fields")
    attrs = [str(x) for x in (inv.get("attributes") or []) if str(x).strip()]
    if not fields or fields == "*" or fields == ["*"]:
        return attrs
    if isinstance(fields, str):
        return [fields]
    return [str(x) for x in fields if str(x).strip()]


def _build_inventory_and_rankings(inv: Dict[str, Any], sources: List[Dict[str, Any]], canon_rows: pd.DataFrame, comp_id: pd.Series, output_cfg: Dict[str, Any]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    df = canon_rows.copy()
    df["__component_id__"] = comp_id.values
    attributes = _inventory_fields(output_cfg, inv)
    source_ids = [str(s.get("id") or "") for s in sources if s.get("enabled", True)]

    inventory_rows: List[Dict[str, Any]] = []
    ranking_rows: List[Dict[str, Any]] = []
    for component_id, group in df.groupby("__component_id__", dropna=False):
        out_row: Dict[str, Any] = {"__component_id__": int(component_id)}
        chosen_hostname = ""
        for attr in attributes:
            per_source: Dict[str, str] = {}
            for sid in source_ids:
                subset = group[group["__source_id__"] == sid]
                value = ""
                if not subset.empty and attr in subset.columns:
                    vals = [x for x in subset[attr].astype(str).map(_norm_str).tolist() if x]
                    value = vals[0] if vals else ""
                per_source[sid] = value

            order = _trusted_source_order_for_attr(inv, sources, attr)
            ranked_vals: List[str] = []
            ranked_srcs: List[str] = []
            for sid in order:
                value = per_source.get(sid, "")
                if value:
                    ranked_vals.append(value)
                    ranked_srcs.append(sid)

            chosen_val = ranked_vals[0] if ranked_vals else ""
            chosen_src = ranked_srcs[0] if ranked_srcs else ""
            out_row[attr] = chosen_val
            if attr == "Hostname" and chosen_val:
                chosen_hostname = chosen_val
            ranking_rows.append(
                {
                    "__component_id__": int(component_id),
                    "Hostname": chosen_hostname,
                    "Attribute": attr,
                    "Chosen Value": chosen_val,
                    "Chosen Source": chosen_src,
                    "Ranked Values": ";".join(ranked_vals),
                    "Ranked Sources": ";".join(ranked_srcs),
                }
            )
        if not out_row.get("Hostname"):
            continue
        inventory_rows.append(out_row)

    inv_df = pd.DataFrame(inventory_rows)
    rank_df = pd.DataFrame(ranking_rows)
    if not inv_df.empty and "Hostname" in inv_df.columns:
        rank_lookup = inv_df.set_index("__component_id__")["Hostname"].to_dict()
        if not rank_df.empty:
            rank_df["Hostname"] = rank_df["__component_id__"].map(rank_lookup).fillna("")
    public_inv_df = inv_df.drop(columns=["__component_id__"], errors="ignore")
    return public_inv_df, rank_df


def _write_df(df: pd.DataFrame, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if str(out_path).lower().endswith(".parquet"):
        df.to_parquet(out_path, index=False)
    else:
        df.to_csv(out_path, index=False)


def extract_jobs(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    payload = cfg.get("payload") if isinstance(cfg.get("payload"), dict) else {}
    jobs = payload.get("raw_table_jobs") if isinstance(payload.get("raw_table_jobs"), list) else None
    if jobs is None:
        jobs = payload.get("jobs") if isinstance(payload.get("jobs"), list) else cfg.get("jobs")
    return [j for j in (jobs or []) if isinstance(j, dict)]


def _get_inventories(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    payload = cfg.get("payload") if isinstance(cfg.get("payload"), dict) else {}
    inventories = payload.get("inventories") if isinstance(payload.get("inventories"), list) else []
    return [inv for inv in inventories if isinstance(inv, dict)]


def _resolve_inventory(cfg: Dict[str, Any], job: Dict[str, Any]) -> Dict[str, Any]:
    wanted = str(job.get("inventory_id") or "").strip()
    inventories = _get_inventories(cfg)
    if not inventories:
        raise ValueError("No inventories defined in config.payload.inventories")
    if wanted:
        for inv in inventories:
            if str(inv.get("id") or "").strip() == wanted:
                return inv
        raise ValueError(f"inventory_id not found: {wanted}")
    return inventories[0]


def process_job_server_profile(job: Dict[str, Any], cfg: Dict[str, Any]) -> int:
    jid = str(job.get("id") or "server_raw_table_auto")
    payload = cfg.get("payload") if isinstance(cfg.get("payload"), dict) else {}
    globals_cfg = payload.get("globals") if isinstance(payload.get("globals"), dict) else {}
    inv = _resolve_inventory(cfg, job)
    inv_id = str(inv.get("id") or "<missing>")
    attributes = [str(x) for x in (inv.get("attributes") or []) if str(x).strip()]
    sources = [s for s in (inv.get("sources") or []) if isinstance(s, dict) and s.get("enabled", True)]
    sources = sorted(sources, key=lambda s: int(s.get("priority", 999999)))
    if not sources:
        raise ValueError(f"[{inv_id}] No enabled sources")

    canonical_frames = [_canonicalize_source(inv_id, src, attributes) for src in sources]
    canon_rows = pd.concat(canonical_frames, ignore_index=True) if canonical_frames else pd.DataFrame(columns=attributes)

    outputs = job.get("outputs") if isinstance(job.get("outputs"), dict) else {}
    inventory_cfg = outputs.get("inventory_table") if isinstance(outputs.get("inventory_table"), dict) else {}
    if not inventory_cfg or not str(inventory_cfg.get("path") or "").strip():
        raise ValueError(f"[{jid}] outputs.inventory_table.path is required")

    inv_df, rankings_df = _build_inventory_from_hostname_anchors(globals_cfg, inv, sources, canon_rows, inventory_cfg)
    out_inventory = vtx_path(str(inventory_cfg.get("path") or ""))
    _write_df(inv_df, out_inventory)

    rankings_cfg = outputs.get("inventory_rankings") if isinstance(outputs.get("inventory_rankings"), dict) else {}
    rankings_path_raw = str(rankings_cfg.get("path") or "").strip()
    if rankings_path_raw:
        _write_df(rankings_df, vtx_path(rankings_path_raw))

    logger.info(
        "job_complete,id=%s,inventory_id=%s,rows=%d,cols=%d,sources=%d,parquet=%s",
        jid,
        inv_id,
        len(inv_df),
        len(inv_df.columns),
        len(sources),
        out_inventory,
    )
    return 0


def _resolve_server_matrix_job(cfg: Dict[str, Any], wanted_job: str) -> Tuple[str, Dict[str, Any]]:
    payload = cfg.get("payload") if isinstance(cfg.get("payload"), dict) else {}
    jobs = payload.get("jobs") if isinstance(payload.get("jobs"), list) else []
    jobs = [j for j in jobs if isinstance(j, dict) and j.get("enabled", True)]
    if not jobs:
        return "", {}
    if wanted_job:
        for j in jobs:
            if str(j.get("id") or "").strip() == wanted_job:
                return wanted_job, j
    j0 = jobs[0]
    return str(j0.get("id") or "").strip(), j0


def resolve_source_from_server_matrix(job: Dict[str, Any], cfg: Dict[str, Any], cfg_path: Path) -> Tuple[Path, str]:
    if str(job.get("source") or "").strip():
        p = vtx_path(str(job.get("source")), must_exist=True)
        return p, "explicit_source"

    wanted_job = str(job.get("server_matrix_job_id") or "").strip()
    run_if_missing = bool(job.get("run_server_matrix_if_missing", True))
    resolved_job_id, resolved_job = _resolve_server_matrix_job(cfg, wanted_job)
    if not resolved_job:
        raise ValueError(f"Could not resolve enabled server_inventory_matrix job from config: {cfg_path}")

    outputs = resolved_job.get("outputs") if isinstance(resolved_job.get("outputs"), dict) else {}
    inv = outputs.get("inventory_table") if isinstance(outputs.get("inventory_table"), dict) else {}
    inv_path_raw = str(inv.get("path") or "").strip()
    if not inv_path_raw:
        raise ValueError(f"Resolved server matrix job '{resolved_job_id}' has no outputs.inventory_table.path")
    source = vtx_path(inv_path_raw, must_exist=False)

    if (not source.exists()) and run_if_missing:
        script = vtx_path("usr/scripts/analysis/server_inventory_matrix_vtx.py", must_exist=True)
        rc = os.system(f'"{sys.executable}" "{script}" --job "{resolved_job_id}"')
        if rc != 0:
            raise RuntimeError(f"server_inventory_matrix_vtx.py failed (rc={rc}) while building '{resolved_job_id}'")

    if not source.exists():
        raise FileNotFoundError(f"Expected server inventory table not found: {source}")
    return source, f"server_inventory_matrix:{resolved_job_id}"


def process_job_legacy(job: Dict[str, Any], cfg: Dict[str, Any], cfg_path: Path) -> int:
    jid = str(job.get("id") or "servers_raw")
    source, source_mode = resolve_source_from_server_matrix(job, cfg, cfg_path)
    out_parquet = vtx_path(str((job.get("outputs") or {}).get("parquet") or ""))
    out_csv_raw = str((job.get("outputs") or {}).get("csv") or "").strip()
    out_csv = vtx_path(out_csv_raw) if out_csv_raw else None
    anchor_field = str(job.get("anchor_field") or "Hostname").strip()
    row_key_field = str(job.get("row_key_field") or "__row_key__").strip()

    df = read_table(source)
    if anchor_field not in df.columns:
        raise ValueError(f"[{jid}] anchor_field not found in source: {anchor_field}")

    counters: Dict[str, int] = {}
    keys: List[str] = []
    for anchor in df[anchor_field].astype(str).tolist():
        counters[anchor] = counters.get(anchor, 0) + 1
        keys.append(f"{anchor}::{counters[anchor]}")
    df[row_key_field] = keys

    _write_df(df, out_parquet)
    if out_csv is not None:
        _write_df(df, out_csv)

    logger.info(
        "legacy_job_complete,id=%s,rows=%d,cols=%d,anchor=%s,source_mode=%s,source=%s,parquet=%s",
        jid,
        len(df),
        len(df.columns),
        anchor_field,
        source_mode,
        source,
        out_parquet,
    )
    return 0


class Options(argparse.Namespace):
    config_path: Path
    job: Optional[str]


def parse_args(argv: Optional[List[str]] = None) -> Options:
    ap = argparse.ArgumentParser(description="Build canonical raw server table")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    ap.add_argument("--job", default=None)
    args = ap.parse_args(argv, namespace=Options())
    config_candidate = vtx_path(args.config, must_exist=False)
    if not config_candidate.exists() and args.config == str(DEFAULT_CONFIG_PATH) and LEGACY_CONFIG_PATH.exists():
        config_candidate = LEGACY_CONFIG_PATH
    args.config_path = vtx_path(config_candidate, must_exist=True)
    return args


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

    use_server_profile = bool(_get_inventories(cfg))
    for job in selected:
        if job.get("enabled", True) is False:
            continue
        if use_server_profile:
            process_job_server_profile(job, cfg)
        else:
            process_job_legacy(job, cfg, opt.config_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
