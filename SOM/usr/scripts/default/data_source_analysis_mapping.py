import os
import time
import re
import sys
import yaml
import pandas as pd
import numpy as np  # kept for consistency with other scripts
import sys

# ------------------------------------------------------------
# Globals / Paths
# ------------------------------------------------------------
VTX_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
MAPPING_YAML_PATH = os.path.join(
    VTX_ROOT, "usr", "config", "run", "data_source_analysis_mapping.yaml"
)
DEFAULT_DICTIONARY_YAML = os.path.join(
    VTX_ROOT, "usr", "config", "run", "data_source_analysis.yaml"
)

# ------------------------------------------------------------
# Logging (BTDM logging)
# ------------------------------------------------------------
try:
    sys.path.append(os.path.join(VTX_ROOT, "usr", "lib"))
    import btdm_logging  # type: ignore

    logger = btdm_logging.get_logger(component="data_source_analysis_mapping")
except Exception:
    import logging

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    logger = logging.getLogger("data_source_analysis_mapping")


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def safe_mtime(path: str) -> float | None:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def report_needs_update(output_file: str, source_files: list[str]) -> bool:
    """
    Re-run if:
    - output file does not exist, or
    - any source file is missing, or
    - any source file is newer than the output.
    """
    out_mtime = safe_mtime(output_file)
    if out_mtime is None:
        return True

    for src in source_files:
        src_mtime = safe_mtime(src)
        if src_mtime is None:
            return True
        if src_mtime >= out_mtime:
            return True

    return False


def resolve_path(p: str | None) -> str | None:
    #"""
    #Resolve paths relative to VTX_ROOT, and handle:
    #- "VTX_ROOT/..." placeholders
    #- Windows absolute (C:\...) and UNC (\\server\share)
    #- POSIX-style paths starting with "/" as relative to VTX_ROOT
    #"""
    if not p:
        return p

    p = p.strip()

    # VTX_ROOT placeholder
    if p.startswith("VTX_ROOT"):
        return os.path.normpath(p.replace("VTX_ROOT", VTX_ROOT))

    # Windows drive letter or UNC
    if re.match(r"^[A-Za-z]:[\\/]", p) or p.startswith("\\\\"):
        return os.path.normpath(p)

    # Treat leading / or \ as relative to VTX_ROOT
    if p.startswith("/") or p.startswith("\\"):
        rel = p.lstrip("/\\")
    else:
        rel = p

    rel = rel.replace("\\", "/")
    full = os.path.join(VTX_ROOT, *[part for part in rel.split("/") if part])
    return os.path.normpath(full)


def compute_field_metrics(series: pd.Series):
    non_null = series.dropna().astype(str)
    total = len(series)
    used_pct = len(non_null) / total if total > 0 else 0
    unique_non_null = non_null.nunique(dropna=True)
    unique_value_pct = non_null.nunique() / len(non_null) if len(non_null) > 0 else 0
    unique_count = unique_non_null
    return non_null, total, used_pct, unique_value_pct, unique_count

def apply_filters(df: pd.DataFrame, filters: str | None) -> pd.DataFrame:
    """
    Apply simple equality filters of the form:
      "ACTIVE = Y" or "ACTIVE = Y, TYPE = APP"

    - Case-insensitive comparison on values.
    - Leaves the DataFrame unchanged if filters is None/empty.
    """
    if not filters:
        return df

    result = df
    for clause in filters.split(","):
        clause = clause.strip()
        if not clause:
            continue
        if "=" not in clause:
            continue

        key, val = clause.split("=", 1)
        key = key.strip()
        val = val.strip()

        if key not in result.columns:
            logger.warning("filter_column_not_found,column=%s", key)
            continue

        # Normalize to string and compare case-insensitively
        result = result[result[key].astype(str).str.upper() == val.upper()]

    return result

def load_lookup_table(attr: dict):
    lookup = attr.get("lookup_table")
    if not lookup:
        return None
    file_path = lookup.get("file")
    field = lookup.get("field")
    if not file_path or not field:
        return None
    if file_path.startswith("VTX_ROOT"):
        file_path = file_path.replace("VTX_ROOT", VTX_ROOT)
    try:
        df_lookup = pd.read_csv(file_path)
        return set(df_lookup[field].dropna().astype(str).str.upper())
    except Exception:
        logger.exception("failed_to_load_lookup_table,file=%s,field=%s", file_path, field)
        return None


def text_length_stats(series: pd.Series) -> dict:
    lengths = series.dropna().astype(str).str.len()
    if lengths.empty:
        return {}
    return {
        "Minimum Length": lengths.min(),
        "Maximum Length": lengths.max(),
        "Average Length": round(lengths.mean(), 2),
        "Length stddev": round(lengths.std(ddof=0), 2),
    }


# ------------------------------------------------------------
# Attribute loading
# ------------------------------------------------------------
def _load_attributes_allow_empty(dictionary_yaml: str | None = None) -> list[dict]:
    """
    Load attribute criteria from the given dictionary YAML.

    If dictionary_yaml is None or fails, falls back to DEFAULT_DICTIONARY_YAML.
    """
    yaml_path = dictionary_yaml or DEFAULT_DICTIONARY_YAML
    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            attributes = yaml.safe_load(f)
    except Exception:
        logger.exception("failed_to_load_dictionary_yaml,path=%s", yaml_path)
        attributes = []
    if isinstance(attributes, dict):
        attributes = attributes.get("criteria", [])
    return attributes or []


def _load_attributes_by_name(dictionary_yaml: str | None) -> dict[str, dict]:
    """
    Return a mapping: attribute name -> attribute dict
    """
    attrs = _load_attributes_allow_empty(dictionary_yaml)
    by_name: dict[str, dict] = {}
    for a in attrs:
        if isinstance(a, dict):
            name = a.get("attribute")
            if name:
                by_name[name] = a
    return by_name


# ------------------------------------------------------------
# Mapped evaluation (no auto-detection)
# ------------------------------------------------------------
def evaluate_mapped_field(series: pd.Series, attribute: dict) -> dict | None:
    """
    Evaluate a field that is explicitly mapped to a Program Attribute.

    - Uses the same metrics as data_source_analysis (utilization, unique %, length stats).
    - Uses value_heuristics + lookup_table (if present) to compute Value Match Ratio.
    - DOES NOT filter out rows if they don't meet match ratios / name patterns.
      (We always return metrics so you get a "report card" on the mapping.)
    """
    non_null, total, used_pct, unique_value_pct, unique_count = compute_field_metrics(series)
    if total == 0:
        return None

    # Recompute unique_count ignoring empty strings
    non_null = non_null[non_null.str.strip() != ""]
    unique_count = non_null.nunique()

    attr_name = attribute.get("attribute", "")

    # -----------------------
    # Flags
    # -----------------------
    flags: list[str] = []

    # Low Utilization
    if used_pct <= 0.20:
        flags.append("Low Utilization")

    # High Generic Values (for the catch-all attribute)
    if attr_name == "General Unknown/Custom/Other fields":
        flags.append("High Generic Values")

    # High Unique Values Warning (same logic as your script)
    excluded_attr_names = {
        "General Description Field",
        "General ID Field",
        "General Date Field",
        "IP Address",
        "General Link",
        "General Link Field",
        "General Third Party Relationship",
    }

    value_heuristics = attribute.get("value_heuristics", [])
    has_min_unique_ratio = attribute.get("min_unique_ratio") is not None

    PERSON_NAME_SNIPPET = "[A-Z][a-z]+, [A-Z][a-z]+"
    has_person_name_pattern = any(
        isinstance(h, dict) and PERSON_NAME_SNIPPET in str(h.get("pattern", ""))
        for h in value_heuristics
    )

    non_null_count = len(non_null)

    if (
        attr_name not in excluded_attr_names
        and not has_min_unique_ratio
        and not has_person_name_pattern
        and non_null_count > 0
    ):
        if unique_count > non_null_count * 0.33:
            flags.append("High Unique Values Warning")

    flags_str = ", ".join(flags)

    # -----------------------
    # Value Match Ratio
    # -----------------------
    values = non_null.astype(str)
    match_ratio: float | None = None

    if isinstance(value_heuristics, list) and value_heuristics:
        candidate_mask = pd.Series(False, index=values.index)

        for heuristic in value_heuristics:
            if not isinstance(heuristic, dict):
                continue
            pattern = heuristic.get("pattern")
            if not pattern:
                continue
            try:
                regex = re.compile(pattern, re.IGNORECASE)
            except re.error:
                logger.warning("invalid_regex_pattern,pattern=%s,attribute=%s", pattern, attr_name)
                continue

            this_mask = values.apply(lambda x: bool(regex.match(x)))
            candidate_mask |= this_mask

        candidates = values[candidate_mask]

        lookup_values = load_lookup_table(attribute)
        if lookup_values:
            def candidate_in_lookup(val: str) -> bool:
                tokens = re.split(r"[^A-Za-z0-9]+", str(val).upper())
                tokens = [t for t in tokens if t]
                return any(t in lookup_values for t in tokens)

            candidates = candidates[candidates.apply(candidate_in_lookup)]

        if len(values) > 0:
            match_ratio = len(candidates) / len(values)

    len_stats = text_length_stats(non_null)

    return {
        "Total Rows": total,
        "Used %": round(used_pct, 2),
        "Unique Value %": round(unique_value_pct, 3),
        "Unique Count": unique_count,
        "Flags": flags_str,
        "Value Match Ratio": round(match_ratio, 3) if match_ratio is not None else None,
        **len_stats,
    }


# ------------------------------------------------------------
# Load mapping YAML
# ------------------------------------------------------------
def load_reports_from_mapping(mapping_yaml_path: str):
    """
    Load the mapping YAML that defines:
      - name
      - input_csvN / input_csvN_filters pairs
      - output_csv
      - dictionary_yaml
      - program_attribute_mapping {Program Attribute: Field Name}
    """
    try:
        with open(mapping_yaml_path, "r", encoding="utf-8") as f:
            conf = yaml.safe_load(f) or {}
    except Exception:
        logger.exception("failed_to_load_mapping_yaml,path=%s", mapping_yaml_path)
        return []

    reports = conf.get("reports", []) or []
    result = []

    for rep in reports:
        if not isinstance(rep, dict):
            continue

        name = rep.get("name")
        raw_output_csv = rep.get("output_csv")
        raw_dict_yaml = rep.get("dictionary_yaml")

        # Collect all input_csvN and input_csvN_filters pairs
        sources: list[tuple[str, str | None]] = []
        for key, value in rep.items():
            if not isinstance(key, str):
                continue
            if key.startswith("input_csv") and not key.endswith("_filters"):
                raw_input = value
                if not raw_input:
                    continue

                input_path = resolve_path(str(raw_input))
                filter_key = f"{key}_filters"
                filters = rep.get(filter_key)
                if isinstance(filters, str):
                    filters_str: str | None = filters
                else:
                    filters_str = None

                sources.append((input_path, filters_str))

        if not name or not raw_output_csv or not sources:
            logger.warning(
                "invalid_report_block_in_mapping_yaml,report=%s",
                rep,
            )
            continue

        output_csv = resolve_path(str(raw_output_csv))
        dict_yaml = resolve_path(str(raw_dict_yaml)) if raw_dict_yaml else DEFAULT_DICTIONARY_YAML

        mapping = rep.get("program_attribute_mapping", {}) or {}

        result.append(
            {
                "name": name,
                "sources": sources,  # list of (path, filters)
                "output_csv": output_csv,
                "dictionary_yaml": dict_yaml,
                "mapping": mapping,
            }
        )

    return result


# ------------------------------------------------------------
# Main analysis
# ------------------------------------------------------------
def analyze_mappings():
    reports = load_reports_from_mapping(MAPPING_YAML_PATH)
    if not reports:
        logger.warning("no_reports_found_in_mapping_yaml,path=%s", MAPPING_YAML_PATH)
        return

    for rep in reports:
        name = rep["name"]
        sources: list[tuple[str, str | None]] = rep["sources"]
        output_csv = rep["output_csv"]
        dictionary_yaml = rep["dictionary_yaml"]
        mapping: dict = rep["mapping"]

        logger.info(
            "mapping_report_start,name=%s,output=%s,dictionary=%s",
            name,
            output_csv,
            dictionary_yaml,
        )

        # Determine whether we need to re-run
        source_files = [src_path for (src_path, _filters) in sources]
        if dictionary_yaml:
            source_files.append(dictionary_yaml)
        source_files.append(MAPPING_YAML_PATH)

        if not report_needs_update(output_csv, source_files):
            logger.info("mapping_report_skipped_up_to_date,output=%s", output_csv)
            continue

        # Load all usable sources into memory (with filters applied)
        df_by_source: dict[str, pd.DataFrame] = {}
        for src_path, filters in sources:
            if not src_path or not os.path.isfile(src_path):
                logger.warning("input_csv_missing,report=%s,path=%s", name, src_path)
                continue

            try:
                df_src = pd.read_csv(src_path)
            except Exception:
                logger.exception("failed_to_read_input_csv,report=%s,path=%s", name, src_path)
                continue

            df_src = apply_filters(df_src, filters)
            df_by_source[src_path] = df_src

        if not df_by_source:
            logger.warning("no_valid_sources_for_report,report=%s", name)
            continue

        # Load attribute definitions
        attr_by_name = _load_attributes_by_name(dictionary_yaml)

        rows: list[dict] = []

        for program_attr, field_name in mapping.items():
            # Skip unmapped attributes (blank / None)
            if field_name is None:
                continue
            field_name = str(field_name).strip()
            if not field_name:
                continue

            attribute_def = attr_by_name.get(program_attr)

            # Skip attributes whose type is "general"
            if attribute_def and str(attribute_def.get("type", "")).lower() == "general":
                continue

            if not attribute_def:
                # If not found in dictionary, still compute metrics but with empty attribute config
                logger.warning(
                    "attribute_not_in_dictionary,report=%s,attribute=%s,dictionary=%s",
                    name,
                    program_attr,
                    dictionary_yaml,
                )
                attribute_def = {}

            found_any = False

            # Check each source for this field
            for src_path, df_src in df_by_source.items():
                if field_name not in df_src.columns:
                    continue

                series = df_src[field_name]
                metrics = evaluate_mapped_field(series, attribute_def)
                if not metrics:
                    continue

                # Derive Priority and Phase from the attribute definition
                priority = attribute_def.get("priority_level")
                if priority is None:
                    priority = attribute_def.get("priority")

                blocks = attribute_def.get("blocks")
                if isinstance(blocks, list):
                    phase = ", ".join(str(b) for b in blocks)
                else:
                    phase = blocks

                recommended_source = attribute_def.get("recommended source")

                row = {
                    "Program Attribute": program_attr,
                    "Field Name": field_name,
                    "Priority": priority,
                    "Phase": phase,
                    "Recommended Source": recommended_source,
                    "Data Source": os.path.basename(src_path),
                    **metrics,
                }
                rows.append(row)
                found_any = True

            if not found_any:
                logger.warning(
                    "mapped_field_not_in_any_input,report=%s,attribute=%s,field=%s",
                    name,
                    program_attr,
                    field_name,
                )
        # ----------------------------------------------------
        # Add "Not Mapped" rows for attributes in dictionary
        # that are NOT mapped to any data source field
        # ----------------------------------------------------
        all_attr_names = {
            n for n, a in attr_by_name.items()
            if str(a.get("type", "")).lower() != "general"
        }

        # Program Attributes that have a non-empty mapping in YAML
        mapped_attr_names = set()
        for program_attr, field_name in mapping.items():
            if field_name is None:
                continue
            if str(field_name).strip():
                mapped_attr_names.add(program_attr)

        unmapped_attr_names = all_attr_names - mapped_attr_names

        for attr_name in sorted(unmapped_attr_names):
            # Get attribute definition so we can pull Priority / Phase
            attribute_def = attr_by_name.get(attr_name, {})

            priority = attribute_def.get("priority_level")
            if priority is None:
                priority = attribute_def.get("priority")

            blocks = attribute_def.get("blocks")
            if isinstance(blocks, list):
                phase = ", ".join(str(b) for b in blocks)
            else:
                phase = blocks

            # For unmapped attributes, we show Program Attribute, "Not Mapped"
            # and leave the rest of the metrics blank.
            recommended_source = attribute_def.get("recommended source")
            rows.append(
                {
                    "Program Attribute": attr_name,
                    "Field Name": "Not Mapped",
                    "Priority": priority,
                    "Phase": phase,
                    "Recommended Source": recommended_source,
                    "Data Source": "",
                    "Total Rows": None,
                    "Used %": None,
                    "Unique Value %": None,
                    "Unique Count": None,
                    "Flags": "",
                    "Value Match Ratio": None,
                    "Minimum Length": None,
                    "Maximum Length": None,
                    "Average Length": None,
                    "Length stddev": None,
                }
            )   

        if not rows:
            logger.info("no_rows_generated_for_report,report=%s", name)
            continue

        out_df = pd.DataFrame(rows)

        # Column order: Program Attribute, Field Name, then everything else
        preferred_order = [
            "Program Attribute",
            "Field Name",
            "Priority",
            "Phase",
            "Recommended Source",
            "Data Source",
            "Total Rows",
            "Used %",
            "Unique Value %",
            "Unique Count",
            "Flags",
            "Value Match Ratio",
        ]

        existing_preferred = [c for c in preferred_order if c in out_df.columns]
        remaining = [c for c in out_df.columns if c not in existing_preferred]
        out_df = out_df[existing_preferred + remaining]

        os.makedirs(os.path.dirname(output_csv), exist_ok=True)
        out_df.to_csv(output_csv, index=False)
        logger.info(
            "mapping_report_written,report=%s,output=%s,rows=%d",
            name,
            output_csv,
            len(out_df),
        )


if __name__ == "__main__":
    logger.info("data_source_analysis_mapping_start")
    try:
        analyze_mappings()
    except Exception:
        logger.exception("data_source_analysis_mapping_failed")
        raise
    else:
        logger.info("data_source_analysis_mapping_complete")