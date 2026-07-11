#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
auto_profile_tables_vtx.py
--------------------------
Profile direct-child CSVs under var/tables and generate auto YAMLs:
  - table_aggregator_vtx.yaml
  - server_inventory_matrix_vtx.yaml (VTX format for server_inventory_matrix_vtx.py)
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import yaml  # PyYAML
except Exception as e:
    raise SystemExit("Missing dependency: pyyaml. Install it in the VTX venv.") from e

try:
    import pandas as pd  # type: ignore
except Exception as e:
    raise SystemExit("Missing dependency: pandas. Install it in the VTX venv.") from e


# ---------------------------------------------------------------------
# VTX root + path handling
# ---------------------------------------------------------------------

_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


def resolve_vtx_root() -> Path:
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    here = Path(__file__).resolve()
    return here.parents[3]


VTX_ROOT = resolve_vtx_root()
DEFAULT_CONFIG_PATH = VTX_ROOT / "usr" / "config" / "default" / "auto_profile_tables_vtx.yaml"


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


def to_vtx_rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(VTX_ROOT)).replace("\\", "/")
    except Exception:
        return str(path).replace("\\", "/")


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


logger = get_logger("auto_profile_tables_vtx")


# ---------------------------------------------------------------------
# Config / CLI
# ---------------------------------------------------------------------

@dataclass
class Options:
    config_path: Path
    job: Optional[str]
    dry_run: bool = False


def parse_args(argv: Optional[Sequence[str]] = None) -> Options:
    p = argparse.ArgumentParser(description="Auto profile var/tables and generate YAMLs")
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help=f"Config path (default: {DEFAULT_CONFIG_PATH})")
    p.add_argument("--job", default=None, help="Run only the selected job id")
    p.add_argument("--dry-run", action="store_true", help="Do not write outputs")
    args = p.parse_args(argv)
    cfg_path = resolve_path(args.config, must_exist=True)
    return Options(config_path=cfg_path, job=args.job, dry_run=bool(args.dry_run))


def load_yaml(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"Config root must be a mapping/dict: {path}")
    return doc


def coerce_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def extract_jobs(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    jobs = cfg.get("config", {}).get("payload", {}).get("jobs", None)
    if jobs is None:
        return []
    return [j for j in coerce_list(jobs) if isinstance(j, dict)]


def job_id(job: Dict[str, Any]) -> str:
    return str(job.get("id") or job.get("name") or "").strip()


def select_jobs(all_jobs: List[Dict[str, Any]], wanted: Optional[str]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    if not wanted:
        return all_jobs, None
    wanted_norm = wanted.strip()
    exact = [j for j in all_jobs if job_id(j) == wanted_norm]
    if exact:
        return exact, None
    ci = [j for j in all_jobs if job_id(j).lower() == wanted_norm.lower()]
    if ci:
        return ci, None
    return [], f"No job found matching '{wanted_norm}'."


# ---------------------------------------------------------------------
# Dictionary criteria modeling
# ---------------------------------------------------------------------

@dataclass
class Criterion:
    attribute: str
    type: str
    like: List[str]
    not_like: List[str]
    value_patterns: List[re.Pattern[str]]
    min_match_ratio: float
    min_viable_ratio: float
    match_requirements: List[str]


def _compile_patterns(patterns: Iterable[str]) -> List[re.Pattern[str]]:
    compiled: List[re.Pattern[str]] = []
    for raw in patterns:
        s = str(raw or "").strip()
        if not s:
            continue
        try:
            compiled.append(re.compile(s))
        except re.error:
            logger.debug("Skipping invalid regex pattern: %s", s)
    return compiled


def load_criteria(dict_path: Path) -> List[Criterion]:
    doc = load_yaml(dict_path)
    raw_criteria = coerce_list(doc.get("criteria"))
    out: List[Criterion] = []
    for item in raw_criteria:
        if not isinstance(item, dict):
            continue
        fpat = item.get("field_name_patterns") or {}
        if not isinstance(fpat, dict):
            fpat = {}
        like = [str(x).lower() for x in coerce_list(fpat.get("like")) if str(x).strip()]
        not_like = [str(x).lower() for x in coerce_list(fpat.get("not_like")) if str(x).strip()]

        vheur = coerce_list(item.get("value_heuristics"))
        vpatterns: List[str] = []
        for vh in vheur:
            if isinstance(vh, dict) and vh.get("pattern"):
                vpatterns.append(str(vh.get("pattern")))
        compiled = _compile_patterns(vpatterns)

        min_match_ratio = float(item.get("min_match_ratio") or 0.5)
        min_viable_ratio = float(item.get("min_viable_ratio") or 0.0)
        reqs = [str(x).lower() for x in coerce_list(item.get("match_requirements"))]
        out.append(
            Criterion(
                attribute=str(item.get("attribute") or ""),
                type=str(item.get("type") or "").lower(),
                like=like,
                not_like=not_like,
                value_patterns=compiled,
                min_match_ratio=min_match_ratio,
                min_viable_ratio=min_viable_ratio,
                match_requirements=reqs,
            )
        )
    return out


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def name_matches(col: str, crit: Criterion) -> bool:
    if not crit.like:
        return False
    norm = _norm_name(col)
    if any(tok and tok in norm for tok in crit.not_like):
        return False
    return any(tok and tok in norm for tok in crit.like)


def value_match_ratio(series: pd.Series, patterns: List[re.Pattern[str]], sample_limit: int) -> float:
    if not patterns:
        return 0.0
    vals = series.dropna()
    if vals.empty:
        return 0.0
    sample = vals.astype(str).head(sample_limit)
    if sample.empty:
        return 0.0
    matched = 0
    total = 0
    for v in sample:
        s = str(v).strip()
        if not s:
            continue
        total += 1
        if any(p.search(s) for p in patterns):
            matched += 1
    if total == 0:
        return 0.0
    return matched / float(total)


def criterion_score(col: str, series: pd.Series, crit: Criterion, sample_limit: int) -> float:
    need_name = (not crit.match_requirements) or ("name" in crit.match_requirements)
    need_value = (not crit.match_requirements) or ("value" in crit.match_requirements)

    name_ok = name_matches(col, crit) if need_name else True
    if not name_ok:
        return 0.0

    if need_value:
        ratio = value_match_ratio(series, crit.value_patterns, sample_limit)
        if ratio >= crit.min_match_ratio:
            return 1.0 + ratio
        if crit.min_viable_ratio > 0 and ratio >= crit.min_viable_ratio:
            return 0.5 + ratio
        return 0.0

    return 1.0


# ---------------------------------------------------------------------
# Table profiling
# ---------------------------------------------------------------------

@dataclass
class TableProfile:
    path: Path
    stem: str
    rows: int
    cols: int
    df: pd.DataFrame
    scores: Dict[str, float]
    matches: Dict[str, int]
    classification: str
    confidence: float
    column_uniqueness: Dict[str, float]
    column_scores: Dict[str, float]


def _uniqueness_ratio(series: pd.Series, sample_rows: int) -> float:
    vals = series.dropna()
    if vals.empty:
        return 0.0
    sample = vals.head(sample_rows)
    if sample.empty:
        return 0.0
    nunique = sample.astype(str).nunique(dropna=True)
    return float(nunique) / float(len(sample)) if len(sample) else 0.0


def profile_table(path: Path, criteria: List[Criterion], payload: Dict[str, Any]) -> TableProfile:
    profiling_cfg = payload.get("profiling") or {}
    sample_rows = int(profiling_cfg.get("sample_rows") or 400)
    sample_unique = int(profiling_cfg.get("sample_unique_values") or sample_rows)
    uniq_sample = int(profiling_cfg.get("uniqueness_sample_rows") or 2000)

    df = pd.read_csv(path)
    df.columns = [str(c).strip() for c in df.columns]

    scores: Dict[str, float] = {"application": 0.0, "server": 0.0}
    matches: Dict[str, int] = {"application": 0, "server": 0}
    column_scores: Dict[str, float] = {c: 0.0 for c in df.columns}

    required_server_signal_attrs = {"ip address", "hostname"}
    server_signal_hits: set[str] = set()

    for crit in criteria:
        if crit.type not in scores:
            continue
        best_score = 0.0
        best_col: Optional[str] = None
        for col in df.columns:
            s = df[col]
            score = criterion_score(col, s, crit, sample_unique)
            if score > best_score:
                best_score = score
                best_col = col
        if best_score > 0 and best_col:
            scores[crit.type] += best_score
            matches[crit.type] += 1
            column_scores[best_col] = max(column_scores.get(best_col, 0.0), best_score)
            if crit.type == "server" and crit.attribute.lower() in required_server_signal_attrs:
                server_signal_hits.add(crit.attribute.lower())

    if matches["application"] > matches["server"]:
        classification = "application"
    elif matches["server"] > matches["application"]:
        classification = "server"
    else:
        classification = "application" if scores["application"] >= scores["server"] else "server"

    has_required_server_signals = required_server_signal_attrs.issubset(server_signal_hits)
    if has_required_server_signals and scores["server"] > 0:
        if scores["application"] == 0 or (scores["server"] >= scores["application"] * 0.8):
            classification = "server"
    elif classification == "server":
        classification = "application"

    confidence = abs(scores["application"] - scores["server"])
    column_uniqueness = {c: _uniqueness_ratio(df[c], uniq_sample) for c in df.columns}

    return TableProfile(
        path=path,
        stem=path.stem,
        rows=len(df),
        cols=len(df.columns),
        df=df,
        scores=scores,
        matches=matches,
        classification=classification,
        confidence=confidence,
        column_uniqueness=column_uniqueness,
        column_scores=column_scores,
    )


def iter_direct_csvs() -> List[Path]:
    tables_dir = VTX_ROOT / "var" / "tables"
    return sorted([p for p in tables_dir.glob("*.csv") if p.is_file()])


# ---------------------------------------------------------------------
# Application aggregation inference
# ---------------------------------------------------------------------

def _token_hits(name: str, tokens: Iterable[str]) -> int:
    norm = _norm_name(name)
    return sum(1 for t in tokens if t and t in norm)


def _expand_tokens(tokens: Iterable[str]) -> List[str]:
    expanded: List[str] = []
    for t in tokens:
        if not t:
            continue
        expanded.append(t)
        if t.endswith("s") and len(t) > 3:
            expanded.append(t[:-1])
    return expanded


def _norm_value(value: Any) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if re.fullmatch(r"-?\d+\.0", text):
        text = text[:-2]
    return text.strip().lower()


def _value_set(series: pd.Series, sample: int) -> set[str]:
    values: set[str] = set()
    if sample > 0:
        series = series.head(sample)
    for val in series:
        normed = _norm_value(val)
        if normed:
            values.add(normed)
    return values


def _best_value_column(subset: TableProfile, key_col: str) -> Optional[str]:
    candidates = [c for c in subset.df.columns if c != key_col]
    if not candidates:
        return None
    preferred_tokens = ["description", "name", "status", "type", "title", "label", "value"]
    scored: List[Tuple[float, str]] = []
    for col in candidates:
        score = float(_token_hits(col, preferred_tokens)) * 2.0
        non_null = 1.0 - float(subset.df[col].isna().mean())
        score += non_null
        scored.append((score, col))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return scored[0][1]


def _parse_field_mapping(field: str) -> Tuple[str, str]:
    if "->" in field:
        left, right = field.split("->", 1)
        return left.strip(), right.strip()
    return field.strip(), field.strip()


def _unique_output_name(name: str, source_stem: str, used: set[str]) -> str:
    if name not in used:
        return name
    suffix = f"_{source_stem}"
    candidate = f"{name}{suffix}"
    counter = 2
    while candidate in used:
        candidate = f"{name}{suffix}_{counter}"
        counter += 1
    return candidate


def _is_excluded_key(col: str, excluded_tokens: Sequence[str]) -> bool:
    norm = _norm_name(col)
    return any(tok in norm for tok in excluded_tokens)


def _criteria_by_attribute(criteria: List[Criterion], attribute_names: Sequence[str]) -> List[Criterion]:
    wanted = {a.lower() for a in attribute_names}
    return [c for c in criteria if c.attribute.lower() in wanted]


def infer_primary_key(profile: TableProfile, criteria: List[Criterion], payload: Dict[str, Any]) -> Tuple[str, str]:
    join_cfg = payload.get("joins") or {}
    excluded = [str(x).lower() for x in coerce_list(join_cfg.get("excluded_key_tokens"))]
    min_unique = float(join_cfg.get("min_uniqueness_ratio") or 0.5)

    id_criteria = _criteria_by_attribute(criteria, ["Application ID"])
    name_criteria = _criteria_by_attribute(criteria, ["Application Name"])

    best_id: Tuple[float, str] = (0.0, "")
    best_name: Tuple[float, str] = (0.0, "")

    for col in profile.df.columns:
        if _is_excluded_key(col, excluded):
            continue
        uniq = profile.column_uniqueness.get(col, 0.0)
        if uniq < min_unique:
            continue

        id_score = max((criterion_score(col, profile.df[col], c, 200) for c in id_criteria), default=0.0)
        name_score = max((criterion_score(col, profile.df[col], c, 200) for c in name_criteria), default=0.0)

        id_total = id_score + uniq
        name_total = name_score + uniq

        if id_total > best_id[0]:
            best_id = (id_total, col)
        if name_total > best_name[0]:
            best_name = (name_total, col)

    if best_id[1]:
        return best_id[1], "Application ID"
    if best_name[1]:
        return best_name[1], "Application Name"

    # Fallback: most unique non-excluded column
    candidates = [
        (profile.column_uniqueness.get(c, 0.0), c)
        for c in profile.df.columns
        if not _is_excluded_key(c, excluded)
    ]
    candidates.sort(reverse=True)
    if not candidates:
        raise ValueError(f"No viable key column found for {profile.stem}")
    return candidates[0][1], "fallback"


def infer_subset_key(primary_key: str, subset: TableProfile, payload: Dict[str, Any]) -> Optional[str]:
    join_cfg = payload.get("joins") or {}
    excluded = [str(x).lower() for x in coerce_list(join_cfg.get("excluded_key_tokens"))]
    min_unique = float(join_cfg.get("min_uniqueness_ratio") or 0.5)

    if primary_key in subset.df.columns and subset.column_uniqueness.get(primary_key, 0.0) >= min_unique:
        return primary_key

    primary_tokens = [t for t in _norm_name(primary_key).split() if t]

    best: Tuple[float, str] = (0.0, "")
    for col in subset.df.columns:
        if _is_excluded_key(col, excluded):
            continue
        uniq = subset.column_uniqueness.get(col, 0.0)
        if uniq < min_unique:
            continue
        token_score = _token_hits(col, primary_tokens)
        score = token_score * 2.0 + uniq
        if score > best[0]:
            best = (score, col)
    return best[1] or None


def detect_tidy_columns(subset: TableProfile, key_col: str, payload: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    tidy_cfg = payload.get("tidy") or {}
    max_card = int(tidy_cfg.get("max_category_cardinality") or 20)
    max_ratio = float(tidy_cfg.get("max_category_ratio") or 0.2)
    min_dups = int(tidy_cfg.get("min_anchor_duplicates") or 2)
    join_cfg = payload.get("joins") or {}
    excluded = [str(x).lower() for x in coerce_list(join_cfg.get("excluded_key_tokens"))]

    if key_col not in subset.df.columns:
        return None
    dup_count = int(subset.df.duplicated(subset=[key_col]).sum())
    if dup_count < min_dups:
        return None

    preferred_category_tokens = {"role", "type", "category", "class", "kind", "status"}
    disallowed_category_tokens = {"id", "key", "seq", "date", "time", "answer", "remark", "comment"}
    disallowed_value_tokens = {"id", "key", "seq"}

    rows = max(len(subset.df), 1)
    candidates: List[Tuple[str, int]] = []
    for col in subset.df.columns:
        if col == key_col:
            continue
        nunique = subset.df[col].astype(str).nunique(dropna=True)
        ratio = nunique / float(rows)
        if _is_excluded_key(col, excluded):
            continue
        norm = _norm_name(col)
        tokens = set(norm.split())
        if tokens & disallowed_category_tokens:
            continue
        if preferred_category_tokens and not (tokens & preferred_category_tokens):
            continue
        if 1 < nunique <= max_card and ratio <= max_ratio:
            candidates.append((col, nunique))
    if not candidates:
        return None

    candidates.sort(key=lambda x: (x[1], x[0]))
    category_col = candidates[0][0]

    value_cols = [c for c in subset.df.columns if c not in (key_col, category_col)]
    if not value_cols:
        return None
    value_cols = [
        c
        for c in value_cols
        if not (set(_norm_name(c).split()) & disallowed_value_tokens)
    ]
    if not value_cols:
        return None
    # Pick the most variable remaining column as value
    value_cols.sort(key=lambda c: subset.df[c].astype(str).nunique(dropna=True), reverse=True)
    value_col = value_cols[0]
    return category_col, value_col


def validation_threshold(primary_rows: int, payload: Dict[str, Any]) -> int:
    subset_cfg = payload.get("subsets") or {}
    max_rows = int(subset_cfg.get("validation_subset_max_rows") or 10)
    max_ratio = float(subset_cfg.get("validation_subset_max_ratio") or 0.05)
    return max(max_rows, int(primary_rows * max_ratio))


def infer_application_job(app_profiles: List[TableProfile], criteria: List[Criterion], payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not app_profiles:
        return None

    def _primary_bonus(p: TableProfile) -> float:
        stem = p.stem.lower()
        bonus = 0.0
        if stem == "applications" or "application" in stem:
            bonus += 10.0
        return bonus

    primary = max(
        app_profiles,
        key=lambda p: (
            p.matches.get("application", 0),
            p.scores.get("application", 0.0) + _primary_bonus(p),
        ),
    )
    primary_key, primary_key_kind = infer_primary_key(primary, criteria, payload)
    logger.info("Application primary table: %s (key=%s, kind=%s)", primary.stem, primary_key, primary_key_kind)

    join_cfg = payload.get("joins") or {}
    excluded = [str(x).lower() for x in coerce_list(join_cfg.get("excluded_key_tokens"))]
    min_unique = float(join_cfg.get("min_uniqueness_ratio") or 0.5)
    value_sample = int(join_cfg.get("value_match_sample") or 5000)
    min_subset_ratio = float(join_cfg.get("min_subset_match_ratio") or 0.2)
    min_overlap_ratio = float(join_cfg.get("min_overlap_ratio") or 0.6)
    max_one_to_many_ratio = float(join_cfg.get("max_one_to_many_ratio") or 3.5)
    primary_tokens = _expand_tokens([t for t in _norm_name(primary.stem).split() if t])
    if "application" in primary_tokens and "app" not in primary_tokens:
        primary_tokens.append("app")

    raw_parquet_output = str((payload.get("outputs") or {}).get("app_raw_parquet_output") or "var/masterdata/raw_application_table_auto.parquet")
    manifest_yaml_output = str((payload.get("outputs") or {}).get("app_manifest_yaml") or "usr/config/manifests/raw_application_table_manifest_auto.yaml")

    transactions: List[Dict[str, Any]] = []
    one_to_many: List[Dict[str, Any]] = []
    inputs: List[str] = [to_vtx_rel(primary.path)]
    saw_staff_link = False
    saw_staff = False
    saw_staff_roles = False

    threshold = validation_threshold(primary.rows, payload)

    primary_sets: Dict[str, set[str]] = {}
    for col in primary.df.columns:
        primary_sets[col] = _value_set(primary.df[col], value_sample)

    known_fields: Dict[str, set[str]] = {col: vals for col, vals in primary_sets.items()}

    remaining = sorted([p for p in app_profiles if p.path != primary.path], key=lambda p: (p.rows, p.cols, p.stem))
    progress = True
    while progress and remaining:
        progress = False
        for subset in list(remaining):
            subset_sets: Dict[str, set[str]] = {}
            for col in subset.df.columns:
                if _is_excluded_key(col, excluded):
                    continue
                subset_sets[col] = _value_set(subset.df[col], value_sample)

            subset_tokens = _expand_tokens([t for t in _norm_name(subset.stem).split() if t])
            candidates: List[Tuple[float, str, str]] = []
            for sub_col, sub_set in subset_sets.items():
                if not sub_set:
                    continue
                uniq = subset.column_uniqueness.get(sub_col, 0.0)
                if uniq < min_unique and "id" not in _norm_name(sub_col):
                    continue
                for known_col, known_set in known_fields.items():
                    if not known_set:
                        continue
                    if known_set != sub_set:
                        overlap = len(sub_set & known_set)
                        min_size = min(len(sub_set), len(known_set))
                        if min_size <= 5:
                            if overlap != len(sub_set) or overlap != len(known_set):
                                continue
                        else:
                            if overlap == 0:
                                continue
                            if (overlap / max(len(sub_set), 1) < min_overlap_ratio) and (overlap / max(len(known_set), 1) < min_overlap_ratio):
                                continue
                    prim_name_hits = _token_hits(known_col, subset_tokens)
                    prim_tokens = _expand_tokens([t for t in _norm_name(known_col).split() if t])
                    sub_name_hits = _token_hits(sub_col, prim_tokens)
                    generic = {"id", "key", "code", "number", "num"}
                    sub_tok_set = set(_norm_name(sub_col).split())
                    prim_tok_set = set(_norm_name(known_col).split())
                    non_generic_overlap = (sub_tok_set - generic) & (prim_tok_set - generic)
                    sub_norm = _norm_name(sub_col)
                    if known_col != primary_key and not non_generic_overlap and sub_norm not in {"id", "code"}:
                        continue
                    if known_col == primary_key and prim_name_hits == 0 and _token_hits(subset.stem, primary_tokens) == 0:
                        continue
                    if prim_name_hits == 0:
                        if sub_name_hits == 0:
                            continue
                        if not non_generic_overlap and known_col != primary_key:
                            continue
                        if sub_norm == "id":
                            continue
                    score = 0.0
                    if sub_norm.endswith("id") or "id" in sub_norm or "key" in sub_norm:
                        score += 5.0
                    if known_col == primary_key:
                        score += _token_hits(sub_col, primary_tokens) * 3.0
                    score += sub_name_hits * 2.0
                    score += prim_name_hits * 2.0
                    score += uniq
                    candidates.append((score, sub_col, known_col))

            if not candidates:
                continue

            candidates.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
            subset_key, seed_field = candidates[0][1], candidates[0][2]

            inputs.append(to_vtx_rel(subset.path))
            tidy = detect_tidy_columns(subset, subset_key, payload)
            non_key_cols = [c for c in subset.df.columns if c != subset_key]
            if not non_key_cols:
                remaining.remove(subset)
                progress = True
                continue

            is_validation = subset.rows <= threshold and subset.cols <= int((payload.get("subsets") or {}).get("validation_subset_max_cols") or 6)

            if tidy:
                cat_col, val_col = tidy
                fields = [cat_col, val_col]
                transactions.append(
                    {
                        "type": "simple_lookup",
                        "lookup_table": to_vtx_rel(subset.path),
                        "key": subset_key,
                        "seed_field": seed_field,
                    }
                )
                drop_fields = [subset_key] if subset_key != seed_field else []
                drop_fields.extend([cat_col, val_col])
                transactions.append(
                    {
                        "type": "transform_tidy",
                        "anchor": primary_key,
                        "column_field": cat_col,
                        "value_field": val_col,
                        "drop_fields": drop_fields,
                    }
                )
                logger.info("Inferred tidy transform: %s via (%s -> %s)", subset.stem, cat_col, val_col)
                remaining.remove(subset)
                progress = True
                continue

            if is_validation:
                value_col = _best_value_column(subset, subset_key)
                if not value_col:
                    remaining.remove(subset)
                    progress = True
                    continue
                transactions.append(
                    {
                        "type": "lookup_replace",
                        "lookup_table": to_vtx_rel(subset.path),
                        "key": subset_key,
                        "seed_field": seed_field,
                        "lookup_field": value_col,
                        "output_field": value_col,
                    }
                )
                logger.info("Inferred validation subset: %s (rows=%d, cols=%d)", subset.stem, subset.rows, subset.cols)
                if subset.stem == "staff_roles_lu":
                    saw_staff_roles = True
                remaining.remove(subset)
                progress = True
                continue

            fields = [c for c in non_key_cols if c != subset_key]
            if not fields:
                remaining.remove(subset)
                progress = True
                continue
            transaction = {
                "type": "simple_lookup",
                "lookup_table": to_vtx_rel(subset.path),
                "key": subset_key,
                "seed_field": seed_field,
            }
            id_cols = [c for c in subset.df.columns if "id" in _norm_name(c)]
            is_intermediary = seed_field == primary_key and subset_key in id_cols and len(id_cols) >= 2 and any(c != subset_key for c in id_cols)
            dup_count = int(subset.df.duplicated(subset=[subset_key]).sum())
            unique_subset_keys = max(int(subset.df[subset_key].astype(str).nunique(dropna=False)), 1)
            one_to_many_ratio = float(len(subset.df)) / float(unique_subset_keys)
            if dup_count > 0 and not is_intermediary:
                if one_to_many_ratio > max_one_to_many_ratio:
                    logger.info(
                        "Skipping high-expansion one-to-many subset: %s join %s -> %s (ratio=%.2f)",
                        subset.stem,
                        subset_key,
                        seed_field,
                        one_to_many_ratio,
                    )
                    remaining.remove(subset)
                    progress = True
                    continue
                one_to_many.append(transaction)
                logger.info("Inferred one-to-many lookup: %s join %s -> %s", subset.stem, subset_key, seed_field)
            else:
                transactions.append(transaction)
                if is_intermediary:
                    logger.info("Inferred intermediary lookup: %s join %s -> %s", subset.stem, subset_key, seed_field)
                else:
                    logger.info("Inferred simple lookup: %s join %s -> %s", subset.stem, subset_key, seed_field)

            for field in fields:
                if field in subset.df.columns:
                    new_vals = _value_set(subset.df[field], value_sample)
                    if field in known_fields:
                        known_fields[field] = known_fields[field] | new_vals
                    else:
                        known_fields[field] = new_vals

            if subset.stem == "staff":
                saw_staff = True
            if subset.stem == "staff_roles_lu":
                saw_staff_roles = True
            if subset.stem == "staff_x_applications" or ("STAFF_ID" in subset.df.columns and "STAFF_ROLE_ID" in subset.df.columns):
                saw_staff_link = True

            remaining.remove(subset)
            progress = True

        if not progress and remaining:
            for subset in remaining:
                logger.info("Skipping subset without exact value match: %s", subset.stem)
            break

    if saw_staff_link and saw_staff and saw_staff_roles:
        transactions.append(
            {
                "type": "transform_tidy",
                "anchor": primary_key,
                "column_field": "STAFF_ROLE_ID",
                "value_field": "STAFF_NAME",
                "drop_fields": ["STAFF_ID", "STAFF_ROLE_ID"],
            }
        )

    transactions.extend(one_to_many)
    inputs = sorted(dict.fromkeys(inputs))

    job_id = "consolidated_application_view_auto"
    return {
        "id": job_id,
        "enabled": True,
        "outputs": {
            "parquet": raw_parquet_output,
            "manifest_yaml": manifest_yaml_output,
        },
        "seed": to_vtx_rel(primary.path),
        "transactions": transactions,
    }


def _simple_lookup_output_fields(tx: Dict[str, Any]) -> List[str]:
    lookup_raw = str(tx.get("lookup_table") or "").strip()
    key = str(tx.get("key") or "").strip()
    if not lookup_raw or not key:
        return []
    try:
        df = pd.read_csv(resolve_path(lookup_raw), nrows=1, dtype=str, keep_default_na=False)
    except Exception:
        return []
    return [str(col).strip() for col in df.columns if str(col).strip() and str(col).strip() != key]


def _prune_application_transactions(job: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    join_cfg = payload.get("joins") or {}
    max_one_to_many_ratio = float(join_cfg.get("max_one_to_many_ratio") or 3.5)
    value_sample = int((payload.get("profiling") or {}).get("sample_unique_values") or 400)

    try:
        seed_df = pd.read_csv(
            resolve_path(str(job.get("seed") or "").strip(), must_exist=True),
            dtype=str,
            keep_default_na=False,
        )
        available_fields = {str(col).strip() for col in seed_df.columns if str(col).strip()}
        available_values = {
            str(col).strip(): _value_set(seed_df[str(col)], value_sample)
            for col in seed_df.columns
            if str(col).strip()
        }
    except Exception:
        available_fields = set()
        available_values = {}

    kept_transactions: List[Dict[str, Any]] = []
    kept_inputs: List[str] = []
    for tx in [item for item in coerce_list(job.get("transactions")) if isinstance(item, dict)]:
        tx_type = str(tx.get("type") or "").strip()
        seed_field = str(tx.get("seed_field") or "").strip()

        if tx_type in {"simple_lookup", "lookup_replace"} and seed_field and available_fields and seed_field not in available_fields:
            logger.info("Skipping transaction with unavailable seed field: %s", tx)
            continue

        if tx_type == "simple_lookup":
            lookup_raw = str(tx.get("lookup_table") or "").strip()
            key = str(tx.get("key") or "").strip()
            try:
                df = pd.read_csv(resolve_path(lookup_raw, must_exist=True), dtype=str, keep_default_na=False)
                if seed_field and seed_field in available_values and key in df.columns:
                    overlap = available_values.get(seed_field, set()) & _value_set(df[key], value_sample)
                    if not overlap:
                        logger.info("Pruned zero-overlap simple_lookup: %s", lookup_raw)
                        continue
                uniq = max(int(df[key].astype(str).nunique(dropna=False)), 1)
                ratio = float(len(df)) / float(uniq)
            except Exception:
                ratio = 0.0
            if ratio > max_one_to_many_ratio:
                logger.info("Pruned high-expansion simple_lookup: %s (ratio=%.2f)", lookup_raw, ratio)
                continue
            available_fields.update(_simple_lookup_output_fields(tx))
            for col in df.columns:
                col_name = str(col).strip()
                if col_name and col_name != key:
                    available_values[col_name] = _value_set(df[col], value_sample)
            kept_inputs.append(lookup_raw)
        elif tx_type == "lookup_replace":
            lookup_raw = str(tx.get("lookup_table") or "").strip()
            key = str(tx.get("key") or "").strip()
            output_field = str(tx.get("output_field") or "").strip()
            lookup_field = str(tx.get("lookup_field") or "").strip()
            try:
                df = pd.read_csv(resolve_path(lookup_raw, must_exist=True), dtype=str, keep_default_na=False)
                if seed_field and seed_field in available_values and key in df.columns:
                    overlap = available_values.get(seed_field, set()) & _value_set(df[key], value_sample)
                    if not overlap:
                        logger.info("Pruned zero-overlap lookup_replace: %s", lookup_raw)
                        continue
            except Exception:
                pass
            if output_field:
                available_fields.add(output_field)
            if output_field and lookup_field:
                try:
                    available_values[output_field] = _value_set(df[lookup_field], value_sample)
                except Exception:
                    pass
            kept_inputs.append(lookup_raw)
        elif tx_type == "transform_tidy":
            column_field = str(tx.get("column_field") or "").strip()
            value_field = str(tx.get("value_field") or "").strip()
            if available_fields and ((column_field and column_field not in available_fields) or (value_field and value_field not in available_fields)):
                logger.info("Skipping transform_tidy with unavailable inputs: %s", tx)
                continue

        kept_transactions.append(tx)

    job["transactions"] = kept_transactions
    job.pop("inputs", None)
    return job


def _prune_seed_overlap_transactions(job: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    value_sample = int((payload.get("profiling") or {}).get("sample_unique_values") or 400)
    seed_raw = str(job.get("seed") or "").strip()
    if not seed_raw:
        return job

    try:
        seed_df = pd.read_csv(resolve_path(seed_raw, must_exist=True), dtype=str, keep_default_na=False)
    except Exception:
        return job

    seed_values = {
        str(col).strip(): _value_set(seed_df[str(col)], value_sample)
        for col in seed_df.columns
        if str(col).strip()
    }

    kept_transactions: List[Dict[str, Any]] = []
    for tx in [item for item in coerce_list(job.get("transactions")) if isinstance(item, dict)]:
        tx_type = str(tx.get("type") or "").strip()
        lookup_raw = str(tx.get("lookup_table") or "").strip()
        seed_field = str(tx.get("seed_field") or "").strip()
        key = str(tx.get("key") or "").strip()

        if tx_type in {"simple_lookup", "lookup_replace"} and lookup_raw and seed_field in seed_values and key:
            try:
                df = pd.read_csv(resolve_path(lookup_raw, must_exist=True), dtype=str, keep_default_na=False)
                if key in df.columns:
                    overlap = seed_values[seed_field] & _value_set(df[key], value_sample)
                    if not overlap:
                        logger.info("Pruned seed-zero-overlap %s: %s", tx_type, lookup_raw)
                        continue
            except Exception:
                pass

        kept_transactions.append(tx)

    job["transactions"] = kept_transactions
    job.pop("inputs", None)
    return job


# ---------------------------------------------------------------------
# Server matrix inference
# ---------------------------------------------------------------------

CANONICAL_SERVER_ATTRIBUTES = [
    "Hostname",
    "IP Address",
    "OS",
    "Environment",
    "CPU",
    "Memory",
    "Storage",
]


def _server_attribute_criteria(criteria: List[Criterion], attribute: str) -> List[Criterion]:
    target = attribute.lower()
    return [c for c in criteria if c.type == "server" and target in c.attribute.lower()]


def _best_column_for_attribute(profile: TableProfile, criteria: List[Criterion], attribute: str) -> Optional[str]:
    crits = _server_attribute_criteria(criteria, attribute)
    if not crits:
        # Fallback by token match on attribute name
        tokens = [t for t in _norm_name(attribute).split() if t]
        scored = sorted(
            [(profile.column_uniqueness.get(c, 0.0) + _token_hits(c, tokens), c) for c in profile.df.columns],
            reverse=True,
        )
        return scored[0][1] if scored else None

    best: Tuple[float, str] = (0.0, "")
    for col in profile.df.columns:
        score = max((criterion_score(col, profile.df[col], c, 200) for c in crits), default=0.0)
        if score > best[0]:
            best = (score, col)
    return best[1] or None


def infer_server_inventories_legacy(
    server_profiles: List[TableProfile],
    criteria: List[Criterion],
    payload: Dict[str, Any],
) -> List[Dict[str, Any]]:
    outputs_cfg = payload.get("outputs") or {}
    unified_output = str(outputs_cfg.get("unified_server_inventory_output") or "var/tables/views/unified_server_inventory_auto.csv")

    inventories: List[Dict[str, Any]] = []
    for idx, prof in enumerate(sorted(server_profiles, key=lambda p: p.stem)):
        src_id = prof.stem
        mapping: Dict[str, str] = {}
        for attr in CANONICAL_SERVER_ATTRIBUTES:
            col = _best_column_for_attribute(prof, criteria, attr)
            mapping[attr] = col or ""

        inventories.append(
            {
                "id": f"Auto_Server_Inventory__{src_id}",
                "enabled": True,
                "attributes": list(CANONICAL_SERVER_ATTRIBUTES),
                "match": {
                    "condition": "any",
                    "keys": [
                        {"name": "Hostname", "type": "hostname"},
                        {"name": "IP Address", "type": "ip_address"},
                    ],
                },
                "trust_ranking": {},
                "sources": [
                    {
                        "id": src_id,
                        "enabled": True,
                        "priority": idx + 1,
                        "path": to_vtx_rel(prof.path),
                        "format": "csv",
                        "map": mapping,
                        "filters": [],
                    }
                ],
                "outputs": {
                    "inventory_table": {
                        "path": unified_output,
                        "include_presence_flags": True,
                    }
                },
                "_auto_profile": {
                    "table": src_id,
                    "match_count": prof.matches.get("server", 0),
                    "score": round(prof.scores.get("server", 0.0), 3),
                },
            }
        )
    return inventories


def build_server_inventory_matrix_vtx_doc(
    server_profiles: List[TableProfile],
    criteria: List[Criterion],
    payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if not server_profiles:
        return None

    # Server matrix outputs are standardized under per-job outputs.
    unified_output = "var/masterdata/unified_server_inventory.csv"
    presence_output = "var/analysis/presence_hostname_matrix.csv"
    presence_html_output = "var/analysis/presence_hostname_matrix.html"

    sources: List[Dict[str, Any]] = []
    for idx, prof in enumerate(sorted(server_profiles, key=lambda p: p.stem)):
        mapping: Dict[str, Optional[str]] = {}
        for attr in CANONICAL_SERVER_ATTRIBUTES:
            col = _best_column_for_attribute(prof, criteria, attr)
            mapping[attr] = col or None
        sources.append(
            {
                "id": prof.stem,
                "enabled": True,
                "priority": idx + 1,
                "path": to_vtx_rel(prof.path),
                "format": "csv",
                "map": mapping,
                "filters": [],
            }
        )

    source_ids_alpha = sorted([str(s.get("id") or "").strip() for s in sources if str(s.get("id") or "").strip()])
    trust_ranking = {attr: list(source_ids_alpha) for attr in CANONICAL_SERVER_ATTRIBUTES}

    inventory_id = "Server_Inventory_matrix_Autodiscovered"
    job_id = "server_inventory_matrix_autodiscovered"

    inventory = {
        "id": inventory_id,
        "enabled": True,
        "attributes": list(CANONICAL_SERVER_ATTRIBUTES),
        "match": {
            "condition": "any",
            "keys": [
                {"name": "Hostname", "type": "hostname"},
                {"name": "IP Address", "type": "ip_address"},
            ],
        },
        "trust_ranking": trust_ranking,
        "sources": sources,
    }

    vtx_doc = {
        "_vtx": {
            "schema": "v1",
            "kind": "report",
            "id": "server_inventory_matrix_vtx",
            "title": "Server Inventory Matrix (VTX)",
            "description": "Auto-generated server inventory matrix (VTX format).",
            "owner": "default",
            "version": "1.0.0",
            "tags": ["detailedreporting", "auto"],
            "created_utc": "",
            "updated_utc": "",
            "consumers": ["usr/scripts/analysis/server_inventory_matrix_vtx.py"],
        },
        "config": {
            "io": {"inputs": [], "outputs": []},
            "run": {
                "enabled": True,
                "role": "any",
                "log_level": "INFO",
                "dry_run": False,
                "fail_fast": False,
                "cwd": "",
            },
            "payload": {
                "globals": {
                    "vtx_root": "",
                    "default_normalization": {
                        "hostname": {"casefold": True, "fqdn_to_shortname": True, "strip": True},
                        "ip_address": {"strip": True},
                    },
                },
                "inventories": [inventory],
                "jobs": [
                    {
                        "id": job_id,
                        "enabled": True,
                        "inventory_id": inventory_id,
                        "outputs": {
                            "inventory_table": {
                                "path": unified_output,
                                "include_presence_flags": False,
                                "include_value_provenance": False,
                                "include_ranked_values": False,
                                "ranked_values_delimiter": ";",
                                "fields": list(CANONICAL_SERVER_ATTRIBUTES),
                            },
                            "presence_matrix_reports": [
                                {
                                    "id": "hostname_presence",
                                    "attribute": "Hostname",
                                    "normalize_as": "hostname",
                                    "source_of_truth": "consolidated_server_view",
                                    "display_attributes": ["IP Address"],
                                    "path": presence_output,
                                    "html_path": presence_html_output,
                                }
                            ],
                        },
                    }
                ],
            },
        },
    }

    # Guardrail: ensure required job-to-inventory link exists in generated YAML.
    jobs = (((vtx_doc.get("config") or {}).get("payload") or {}).get("jobs") or [])
    if jobs and isinstance(jobs[0], dict) and not str(jobs[0].get("inventory_id") or "").strip():
        jobs[0]["inventory_id"] = inventory_id

    return vtx_doc


def _write_vtx_server_matrix_yaml(path: Path, vtx_doc: Dict[str, Any], *, dry_run: bool) -> None:
    if dry_run:
        logger.info("[dry-run] Would write %s", path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump_yaml(vtx_doc) + "\n", encoding="utf-8")
    logger.info("Wrote %s", path)


def _write_jobs_yaml(path: Path, jobs: List[Dict[str, Any]], header: Dict[str, Any], *, dry_run: bool) -> None:
    doc = dict(header)
    doc.setdefault("config", {}).setdefault("payload", {})["jobs"] = jobs
    text = _dump_yaml(doc) + "\n"
    if dry_run:
        logger.info("[dry-run] Would write %s", path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    logger.info("Wrote %s with %d job(s)", path, len(jobs))


# ---------------------------------------------------------------------
# Append-only YAML writing
# ---------------------------------------------------------------------

def _dump_yaml(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False).rstrip()


def _load_yaml_if_exists(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return load_yaml(path)
    except Exception as exc:
        logger.warning("Unable to parse existing YAML (%s): %s", path, exc)
        return None


def append_jobs_yaml(path: Path, jobs: List[Dict[str, Any]], header: Dict[str, Any], *, dry_run: bool) -> None:
    if not path.exists():
        doc = dict(header)
        doc.setdefault("config", {}).setdefault("payload", {})["jobs"] = jobs
        text = _dump_yaml(doc) + "\n"
        if dry_run:
            logger.info("[dry-run] Would create %s", path)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        logger.info("Created %s with %d job(s)", path, len(jobs))
        return

    existing = _load_yaml_if_exists(path) or {}
    existing_jobs = coerce_list(existing.get("config", {}).get("payload", {}).get("jobs"))
    existing_ids = {job_id(j) for j in existing_jobs if isinstance(j, dict)}

    missing = [j for j in jobs if job_id(j) not in existing_ids]
    if not missing:
        logger.info("No new jobs to append to %s", path)
        return

    snippet = _dump_yaml(missing)
    indented = textwrap.indent(snippet, "    ")
    append_text = "\n" + indented + "\n"
    if dry_run:
        logger.info("[dry-run] Would append %d job(s) to %s", len(missing), path)
        return
    with path.open("a", encoding="utf-8") as f:
        f.write(append_text)
    logger.info("Appended %d job(s) to %s", len(missing), path)


def append_inventories_yaml(path: Path, inventories: List[Dict[str, Any]], header: Dict[str, Any], *, dry_run: bool) -> None:
    if not path.exists():
        doc = dict(header)
        doc["inventories"] = inventories
        text = _dump_yaml(doc) + "\n"
        if dry_run:
            logger.info("[dry-run] Would create %s", path)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        logger.info("Created %s with %d inventory block(s)", path, len(inventories))
        return

    existing = _load_yaml_if_exists(path) or {}
    existing_inventories = coerce_list(existing.get("inventories"))
    existing_ids = {str(inv.get("id") or "").strip() for inv in existing_inventories if isinstance(inv, dict)}
    missing = [inv for inv in inventories if str(inv.get("id") or "").strip() not in existing_ids]
    if not missing:
        logger.info("No new inventories to append to %s", path)
        return

    snippet = _dump_yaml(missing)
    indented = textwrap.indent(snippet, "  ")
    append_text = "\n" + indented + "\n"
    if dry_run:
        logger.info("[dry-run] Would append %d inventories to %s", len(missing), path)
        return
    with path.open("a", encoding="utf-8") as f:
        f.write(append_text)
    logger.info("Appended %d inventories to %s", len(missing), path)


# ---------------------------------------------------------------------
# Main job execution
# ---------------------------------------------------------------------

def run_auto_profile(payload: Dict[str, Any], *, dry_run: bool) -> int:
    dict_cfg = payload.get("dictionary") or {}
    dict_path = resolve_path(dict_cfg.get("path") or "usr/config/default/data_source_analysis.yaml", must_exist=True)
    criteria = load_criteria(dict_path)

    csv_paths = iter_direct_csvs()
    if not csv_paths:
        logger.warning("No direct-child CSVs found under var/tables")
        return 0

    profiles = [profile_table(p, criteria, payload) for p in csv_paths]

    # Classification summary
    for prof in profiles:
        logger.info(
            "Table classified: %s => %s (matches=%s, score=%s, confidence=%.3f)",
            prof.stem,
            prof.classification,
            prof.matches,
            {k: round(v, 3) for k, v in prof.scores.items()},
            prof.confidence,
        )

    app_profiles = [p for p in profiles if p.classification == "application"]
    server_profiles = [p for p in profiles if p.classification == "server"]

    outputs_cfg = payload.get("outputs") or {}
    app_yaml_path = resolve_path(outputs_cfg.get("app_aggregator_yaml") or "usr/config/auto_v2/table_aggregator_vtx.yaml")
    server_yaml_vtx_path = resolve_path(outputs_cfg.get("server_matrix_yaml_vtx") or "usr/config/auto_v2/server_inventory_matrix_vtx.yaml")

    app_job = infer_application_job(app_profiles, criteria, payload)
    if app_job:
        app_job = _prune_application_transactions(app_job, payload)
        app_job = _prune_seed_overlap_transactions(app_job, payload)
        app_header = {
            "_vtx": {
                "schema": "v1",
                "kind": "transform",
                "id": "auto_table_aggregator",
                "title": "Auto Table Aggregator (V2)",
                "description": "Auto-generated fully expanded raw application table jobs.",
                "owner": "default",
                "version": "1.0.0",
                "tags": ["aggregation", "transform", "masterdata", "auto"],
                "consumers": ["usr/scripts/default/application_raw_table_builder.py"],
            },
            "config": {
                "io": {"inputs": [], "outputs": []},
                "run": {
                    "enabled": True,
                    "role": "any",
                    "log_level": "INFO",
                    "dry_run": False,
                    "fail_fast": False,
                    "cwd": "",
                },
                "payload": {"jobs": []},
            },
        }
        _write_jobs_yaml(app_yaml_path, [app_job], app_header, dry_run=dry_run)
    else:
        logger.warning("No application profiles detected; skipping auto_table_aggregator_vtx.yaml")

    vtx_doc = build_server_inventory_matrix_vtx_doc(server_profiles, criteria, payload)
    if vtx_doc:
        _write_vtx_server_matrix_yaml(server_yaml_vtx_path, vtx_doc, dry_run=dry_run)
    else:
        logger.warning("No server profiles detected; skipping server_inventory_matrix_vtx.yaml")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    opt = parse_args(argv)
    logger.info("VTX_ROOT=%s", VTX_ROOT)
    logger.info("Config=%s", opt.config_path)

    cfg = load_yaml(opt.config_path)
    payload = cfg.get("config", {}).get("payload", {}) if isinstance(cfg.get("config"), dict) else {}
    if not isinstance(payload, dict):
        raise ValueError("config.payload must be a mapping/dict")

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
        jid = job_id(job_cfg) or "job"
        try:
            logger.info("Starting job: %s", jid)
            print(f"[auto_profile_tables_vtx] Running job '{jid}'")
            run_auto_profile(payload, dry_run=opt.dry_run)
        except Exception as exc:
            logger.exception("Job failed: %s (%s)", jid, exc)
            print(f"[auto_profile_tables_vtx] ERROR in job '{jid}': {exc}")
            return 3

    print("[auto_profile_tables_vtx] Complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
