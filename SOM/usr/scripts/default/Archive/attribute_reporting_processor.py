from __future__ import annotations

import os
import sys
import logging
from typing import Any, Dict, List, Optional, Tuple

import yaml
import pandas as pd


# ------------------------------------------------------------
# Globals / Paths (VTX style)
# ------------------------------------------------------------
VTX_ROOT = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)

CONFIG_DIR_DEFAULT = os.path.join(VTX_ROOT, "usr", "config", "run")
CONFIG_PATH_DEFAULT = os.path.join(CONFIG_DIR_DEFAULT, "attribute_reporting.yaml")


# ------------------------------------------------------------
# Logging (BTDM / VTX style)
# ------------------------------------------------------------
try:
    sys.path.append(os.path.join(VTX_ROOT, "usr", "lib"))
    import btdm_logging  # type: ignore

    logger = btdm_logging.get_logger(component="reporting_processor")
except Exception:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    logger = logging.getLogger("reporting_processor")


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def load_yaml_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def apply_filters(df: pd.DataFrame, filters: List[Dict[str, Any]]) -> pd.DataFrame:
    for f in filters or []:
        field = f["field"]
        op = f["operator"]
        val = f.get("value")

        if field not in df.columns:
            logger.warning(f"Filter field not found in data: '{field}' (skipping this filter)")
            continue

        if op == "==":
            df = df[df[field] == val]
        elif op == "!=":
            df = df[df[field] != val]
        elif op == "contains":
            df = df[df[field].astype(str).str.contains(str(val), na=False)]
        elif op == "not_contains":
            df = df[~df[field].astype(str).str.contains(str(val), na=False)]
        elif op == "isnull":
            df = df[df[field].isnull() | (df[field].astype(str).str.strip() == "")]
        elif op == "notnull":
            df = df[~(df[field].isnull() | (df[field].astype(str).str.strip() == ""))]
        else:
            logger.warning(f"Unknown operator '{op}' for field '{field}' (skipping)")
    return df


def select_output_fields(df: pd.DataFrame, output: Dict[str, Any]) -> pd.DataFrame:
    """
    Optional: output.fields lets you control exactly which columns are written.
    - fields: [Server, IP Address, Location, OS]
    - fields: "*" (or omitted) => write all columns
    """
    fields = output.get("fields")
    if not fields or fields == "*" or fields == ["*"]:
        return df

    # Normalize to list
    if isinstance(fields, str):
        fields_list = [fields]
    else:
        fields_list = list(fields)

    missing = [c for c in fields_list if c not in df.columns]
    if missing:
        logger.warning(f"Requested output fields not found and will be omitted: {missing}")

    keep = [c for c in fields_list if c in df.columns]
    return df[keep]


def output_result(result_df: pd.DataFrame, count: int, output: Dict[str, Any]) -> None:
    fmt = (output.get("format") or "csv").lower()
    destination = output["destination"]
    include_rows = bool(output.get("include_row_data"))
    include_count = bool(output.get("include_count_summary"))

    os.makedirs(os.path.dirname(destination), exist_ok=True)

    df_to_write = result_df if include_rows else pd.DataFrame()
    df_to_write = select_output_fields(df_to_write, output)

    if fmt == "csv":
        if include_rows:
            df_to_write.to_csv(destination, index=False)
        else:
            pd.DataFrame([{"count": count}]).to_csv(destination, index=False)

    elif fmt == "json":
        if include_rows:
            df_to_write.to_json(destination, orient="records", lines=True)
        else:
            with open(destination, "w", encoding="utf-8") as f:
                f.write(f'{{"count": {count}}}\n')

    elif fmt == "html":
        title = output.get("title") or "Report"
        html = [
            "<html><body>",
            f"<h2>{title}</h2>",
        ]
        if include_count:
            html.append(f"<p><b>Count:</b> {count}</p>")
        if include_rows:
            html.append(df_to_write.to_html(index=False))
        html.append("</body></html>")
        with open(destination, "w", encoding="utf-8") as f:
            f.write("".join(html))

    else:
        raise ValueError(f"Unsupported output format: {fmt}")


def _normalize_series(s: pd.Series, *, is_hostname: bool) -> pd.Series:
    """
    Normalization rules:
    - Always: strip whitespace, lowercase
    - If is_hostname: force shortname-only (split at first '.'), then strip/lower
    Empty strings become "".
    """
    s2 = s.astype(str).fillna("").str.strip().str.lower()
    if is_hostname:
        # shortname = everything before first dot
        s2 = s2.str.split(".", n=1).str[0].str.strip().str.lower()
    return s2


def _parse_match_keys(
    report_name: str,
    report: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], str]:
    """
    Supports:
      New:
        comparison.match_keys: list of {source_field, target_field, is_hostname}
        comparison.match_condition: any|all
      Old:
        source.key + target.key  (single key pair)
        comparison.match_condition optional

    Returns: (match_keys, match_condition)
    where match_keys is always a list of dicts with:
      - source_field
      - target_field
      - is_hostname (bool)
    """
    comparison = report.get("comparison", {}) or {}
    match_condition = (comparison.get("match_condition") or "any").strip().lower()
    if match_condition not in ("any", "all"):
        raise ValueError(
            f"[{report_name}] comparison.match_condition must be 'any' or 'all' (got: {match_condition})"
        )

    match_keys = comparison.get("match_keys")

    # New format
    if isinstance(match_keys, list) and match_keys:
        normalized: List[Dict[str, Any]] = []
        for i, mk in enumerate(match_keys):
            if not isinstance(mk, dict):
                raise ValueError(f"[{report_name}] comparison.match_keys[{i}] must be a dict")
            sf = mk.get("source_field")
            tf = mk.get("target_field")
            if not sf or not tf:
                raise ValueError(
                    f"[{report_name}] comparison.match_keys[{i}] requires source_field and target_field"
                )
            normalized.append(
                {
                    "source_field": str(sf),
                    "target_field": str(tf),
                    "is_hostname": bool(mk.get("is_hostname", False)),
                }
            )
        return normalized, match_condition

    # Old format fallback: source.key + target.key
    source = report.get("source", {}) or {}
    target = report.get("target", {}) or {}
    source_key = source.get("key")
    target_key = target.get("key")
    if source_key and target_key:
        return (
            [
                {
                    "source_field": str(source_key),
                    "target_field": str(target_key),
                    "is_hostname": False,  # old configs had no hostname hint
                }
            ],
            match_condition,
        )

    # If we got here, nothing was defined correctly
    raise ValueError(
        f"[{report_name}] difference comparison requires either comparison.match_keys "
        f"or (source.key + target.key)"
    )


def _compute_row_match_mask(
    df_left: pd.DataFrame,
    df_right: pd.DataFrame,
    *,
    match_keys: List[Dict[str, Any]],
    match_condition: str,
    report_name: str,
    left_label: str,
    right_label: str,
) -> pd.Series:
    """
    Returns boolean mask aligned to df_left indicating whether each row in df_left
    has a "match" in df_right according to match_keys and match_condition.

    For each key pair:
      - normalize left and right columns (hostname shortname if is_hostname)
      - match = left_value is in set(right_values)

    Combine per-row matches using:
      - any: OR across key pairs
      - all: AND across key pairs
    """
    if df_left.empty:
        return pd.Series([], dtype=bool, index=df_left.index)

    masks: List[pd.Series] = []

    for mk in match_keys:
        lf = mk["source_field"] if left_label == "source" else mk["target_field"]
        rf = mk["target_field"] if right_label == "target" else mk["source_field"]
        is_hostname = bool(mk.get("is_hostname", False))

        if lf not in df_left.columns:
            raise ValueError(f"[{report_name}] {left_label} field '{lf}' not found in {left_label} columns")
        if rf not in df_right.columns:
            raise ValueError(f"[{report_name}] {right_label} field '{rf}' not found in {right_label} columns")

        left_norm = _normalize_series(df_left[lf], is_hostname=is_hostname)
        right_norm = _normalize_series(df_right[rf], is_hostname=is_hostname)

        right_set = set(v for v in right_norm.tolist() if v != "")
        mask = left_norm.apply(lambda v: (v != "") and (v in right_set))
        masks.append(mask)

    if not masks:
        # Should never happen given validation, but be safe.
        return pd.Series([False] * len(df_left), index=df_left.index)

    if match_condition == "all":
        out = masks[0].copy()
        for m in masks[1:]:
            out = out & m
        return out

    # default any
    out = masks[0].copy()
    for m in masks[1:]:
        out = out | m
    return out


def process_report(report: Dict[str, Any]) -> None:
    report_name = report.get("report_name", "UnnamedReport")
    logger.info(f"Processing report: {report_name}")

    source = report["source"]
    source_file = source["file"]
    source_filters = source.get("filter", [])

    df_source = pd.read_csv(source_file, dtype=str, keep_default_na=False, na_values=[""])
    df_source = apply_filters(df_source, source_filters)

    comparison = report.get("comparison", {}) or {}
    comp_type = (comparison.get("type") or "").strip().lower()
    output = report["output"]

    result_df = pd.DataFrame()
    count = 0

    if comp_type == "difference":
        target = report.get("target") or {}
        target_file = target.get("file")
        direction = (comparison.get("direction", "source_only") or "source_only").strip().lower()

        if not target_file:
            raise ValueError(f"[{report_name}] difference comparison requires target.file")

        df_target = pd.read_csv(target_file, dtype=str, keep_default_na=False, na_values=[""])

        # NEW: multiple match keys + any/all logic (backwards compatible)
        match_keys, match_condition = _parse_match_keys(report_name, report)

        # Compute match masks
        source_has_match = _compute_row_match_mask(
            df_source,
            df_target,
            match_keys=match_keys,
            match_condition=match_condition,
            report_name=report_name,
            left_label="source",
            right_label="target",
        )
        target_has_match = _compute_row_match_mask(
            df_target,
            df_source,
            match_keys=match_keys,
            match_condition=match_condition,
            report_name=report_name,
            left_label="target",
            right_label="source",
        )

        if direction == "source_only":
            result_df = df_source[~source_has_match].copy()

        elif direction == "target_only":
            result_df = df_target[~target_has_match].copy()

        elif direction == "both":
            in_source_not_target = df_source[~source_has_match].copy()
            in_target_not_source = df_target[~target_has_match].copy()

            # Optional: tag origin so "both" output is interpretable
            in_source_not_target.insert(0, "__diff_origin__", "source_only")
            in_target_not_source.insert(0, "__diff_origin__", "target_only")

            result_df = pd.concat([in_source_not_target, in_target_not_source], ignore_index=True)

        else:
            raise ValueError(f"[{report_name}] unknown difference direction: {direction}")

        count = len(result_df)

    elif comp_type == "filter_only":
        result_df = df_source
        count = len(result_df)

    elif comp_type == "count":
        count_type = (comparison.get("count_type", "total") or "total").strip().lower()
        count_field = comparison.get("count_field")
        if count_type == "distinct" and count_field:
            if count_field not in df_source.columns:
                raise ValueError(f"[{report_name}] count_field '{count_field}' not found in source columns")
            count = df_source[count_field].nunique()
        else:
            count = len(df_source)

        if output.get("include_row_data"):
            result_df = df_source

    else:
        raise ValueError(f"[{report_name}] Unsupported comparison.type: {comp_type}")

    output_result(result_df, count, output)

    if output.get("include_count_summary"):
        logger.info(f"[SUMMARY] {report_name}: {count} row(s) counted.")


def main() -> int:
    # Allow override: --config <path>
    config_path = CONFIG_PATH_DEFAULT
    if "--config" in sys.argv:
        i = sys.argv.index("--config")
        if i + 1 < len(sys.argv):
            config_path = sys.argv[i + 1]

    logger.info(f"Using config: {config_path}")
    config = load_yaml_config(config_path)

    reports = config.get("reports", [])
    if not reports:
        # handle single-report files
        reports = [config]

    for report in reports:
        process_report(report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())