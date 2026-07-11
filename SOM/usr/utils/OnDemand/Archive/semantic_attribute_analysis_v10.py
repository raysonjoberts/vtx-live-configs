
import pandas as pd
import yaml
import re

# File paths (edit as needed)
csv_path = r"C:\BTDM_7.1\var\tables\consolidated_application_view.csv"
yaml_path = r"C:\BTDM_7.1\usr\config\default\data_source_analysis.yaml"
output_path = r"C:\BTDM_7.1\var\analysis\semantic_analysis.csv"
summary_output_path = r"C:\BTDM_7.1\var\analysis\semantic_summary.csv"

def load_yaml(path):
    with open(path, 'r') as f:
        return yaml.safe_load(f)

def match_field_name(field, patterns):
    tokens = re.split(r'[_\s\-]+', field.lower())
    includes = patterns.get("like", []) if isinstance(patterns, dict) else patterns
    excludes = patterns.get("not_like", []) if isinstance(patterns, dict) else []

    if any(ex.lower() in tokens for ex in excludes):
        return False
    return any(pat.lower() in tokens for pat in includes)

def match_heuristics(series, heuristics, global_config):
    string_series = series.dropna().astype(str)
    total = len(string_series)
    if total == 0:
        return False, {
            "Value Match Ratio": 0.0,
            "Value Unique Ratio": 0.0,
            "Value Max Length Violation Ratio": 0.0,
            "Value Min Length Violation Ratio": 0.0
        }

    if "min_viable_ratio" in global_config:
        viable_ratio = string_series.count() / len(series)
        if viable_ratio < global_config["min_viable_ratio"]:
            return False, {
                "Value Match Ratio": 0.0,
                "Value Unique Ratio": 0.0,
                "Value Max Length Violation Ratio": 0.0,
                "Value Min Length Violation Ratio": 0.0
            }

    matched_mask = pd.Series(False, index=string_series.index)

    for rule in heuristics:
        rule_matched_mask = pd.Series(True, index=string_series.index)

        if "min_length" in rule:
            rule_matched_mask &= string_series.map(len) >= rule["min_length"]

        if "max_length" in rule:
            rule_matched_mask &= string_series.map(len) <= rule["max_length"]

        if "pattern" in rule:
            regex = re.compile(rule["pattern"], re.IGNORECASE)
            exclude_patterns = [re.compile(p, re.IGNORECASE) for p in rule.get("not_like", [])]
            def valid_pattern(v):
                if any(ex.search(v) for ex in exclude_patterns):
                    return False
                return bool(regex.match(v))
            rule_matched_mask &= string_series.apply(valid_pattern)

        matched_mask |= rule_matched_mask

    match_ratio = matched_mask.mean()
    unique_ratio = string_series.nunique() / total
    too_short = string_series.map(len).lt(global_config.get("min_length", 0)).mean()
    too_long = string_series.map(len).gt(global_config.get("max_length", 999)).mean()

    stats = {
        "Value Match Ratio": match_ratio,
        "Value Unique Ratio": unique_ratio,
        "Value Max Length Violation Ratio": too_long,
        "Value Min Length Violation Ratio": too_short
    }

    if "min_match_ratio" in global_config and match_ratio < global_config["min_match_ratio"]:
        return False, stats

    if "min_unique_ratio" in global_config and unique_ratio < global_config["min_unique_ratio"]:
        return False, stats

    if "max_unique_ratio" in global_config and unique_ratio > global_config["max_unique_ratio"]:
        return False, stats

    return True, stats

def match_value_examples(series, example_config):
    stats = {"Example Match Ratio": 0.0}
    try:
        if not example_config or "path" not in example_config or "column" not in example_config:
            return False, stats
        if "match_threshold" not in example_config:
            return False, stats

        threshold = example_config["match_threshold"] / 100.0
        df_lookup = pd.read_csv(example_config["path"])
        example_values = set(df_lookup[example_config["column"]].dropna().astype(str).str.lower().str.strip())
        string_series = series.dropna().astype(str).str.lower().str.strip()

        def contains_token(value):
            tokens = re.split(r"[\s.,]+", value)  # <-- fix applied here
            return any(token in example_values for token in tokens)

        match_ratio = string_series.apply(contains_token).mean()
        stats["Example Match Ratio"] = match_ratio
        return match_ratio >= threshold, stats
    except Exception:
        return False, stats

def should_declare_match(name_match, heuristic_match, example_match, requirement):
    criteria_map = {
        "name": name_match,
        "value": heuristic_match,
        "example": example_match
    }
    return all(criteria_map.get(key, False) for key in requirement)

def evaluate(df, criteria_list):
    results = []
    summary = []
    total_rows = len(df)

    for col in df.columns:
        all_values = df[col].dropna()
        is_used_pct = round(len(all_values) / total_rows * 100, 2)
        matched = False

        for crit in criteria_list:
            attr = crit["attribute"]
            requirement = crit.get("match_requirements", ["name", "value"])
            global_heuristic_config = {
                "min_match_ratio": crit.get("min_match_ratio"),
                "min_unique_ratio": crit.get("min_unique_ratio"),
                "max_unique_ratio": crit.get("max_unique_ratio"),
                "min_viable_ratio": crit.get("min_viable_ratio"),
                "min_length":  crit.get("min_length", 0),
                "max_length": crit.get("max_length", 999)
            }

            name_match = match_field_name(col, crit.get("field_name_patterns", []))
            heuristic_match, heuristic_stats = match_heuristics(all_values, crit.get("value_heuristics", []), global_heuristic_config)
            example_match, example_stats = match_value_examples(all_values, crit.get("field_value_examples", {}))

            if should_declare_match(name_match, heuristic_match, example_match, requirement):
                result = {
                    "Field Name": col,
                    "Program Attribute": attr,
                    "Match Type": "+".join([k for k, v in {"name": name_match, "value": heuristic_match, "example": example_match}.items() if v]),
                    "is_used_pct": is_used_pct,
                    "Name Match Ratio": 100.0 if name_match else 0.0,
                    **heuristic_stats,
                    **example_stats
                }
                results.append(result)
                matched = True

                if crit.get("required", False):
                    summary.append({
                        "Attribute": attr,
                        "Matched Field": col,
                        "Match Type": result["Match Type"]
                    })

        if not matched:
            results.append({
                "Field Name": col,
                "Program Attribute": "No Match",
                "Match Type": "None",
                "is_used_pct": is_used_pct,
                "Name Match Ratio": 0.0,
                "Value Match Ratio": 0.0,
                "Value Unique Ratio": 0.0,
                "Value Max Length Violation Ratio": 0.0,
                "Value Min Length Violation Ratio": 0.0,
                "Example Match Ratio": 0.0
            })

    return pd.DataFrame(results), pd.DataFrame(summary)

def main():
    df = pd.read_csv(csv_path)
    if "ACTIVE" in df.columns:
        df = df[df["ACTIVE"].astype(str).str.upper() == "Y"]

    yaml_data = load_yaml(yaml_path)
    criteria = yaml_data.get("criteria", [])
    results_df, summary_df = evaluate(df, criteria)

    results_df.to_csv(output_path, index=False)
    summary_df.to_csv(summary_output_path, index=False)
    print(f"Saved results to {output_path} and summary to {summary_output_path}")

if __name__ == "__main__":
    main()
