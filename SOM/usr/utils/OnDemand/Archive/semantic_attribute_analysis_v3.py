
import pandas as pd
import yaml
import re

# File paths (edit as needed)
csv_path = r"C:\BTDM_7.1\var\tables\consolidated_application_view.csv"
yaml_path = r"C:\BTDM_7.1\usr\config\default\clean_data_source_analysis.yaml"
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

def match_heuristics(series, heuristics):
    stats = {
        "Value Match Ratio": 0.0,
        "Value Unique Ratio": 0.0,
        "Value Max Length Violation Ratio": 0.0,
        "Value Min Length Violation Ratio": 0.0
    }

    for rule in heuristics:
        string_series = series.dropna().astype(str)
        if len(string_series) == 0:
            continue

        matched = True

        if "min_length" in rule:
            too_short = string_series.map(len).lt(rule["min_length"]).mean()
            stats["Value Min Length Violation Ratio"] = too_short
            if too_short > 0.95:
                matched = False

        if "max_length" in rule:
            too_long = string_series.map(len).gt(rule["max_length"]).mean()
            stats["Value Max Length Violation Ratio"] = too_long
            if too_long > 0.95:
                matched = False

        if "min_unique_ratio" in rule:
            unique_ratio = string_series.nunique() / len(string_series)
            stats["Value Unique Ratio"] = unique_ratio
            if unique_ratio < rule["min_unique_ratio"]:
                matched = False

        if "pattern" in rule:
            regex = re.compile(rule["pattern"])
            exclude_patterns = [re.compile(p) for p in rule.get("not_like", [])]
            def is_match(v):
                if any(ex.search(v) for ex in exclude_patterns):
                    return False
                return bool(regex.match(v))
            match_ratio = string_series.apply(is_match).mean()
            stats["Value Match Ratio"] = match_ratio
            if "min_match_ratio" in rule and match_ratio < rule["min_match_ratio"]:
                matched = False

        if matched:
            return True, stats

    return False, stats

def match_value_examples(series, example_config):
    stats = {"Example Match Ratio": 0.0}
    try:
        if not example_config or "path" not in example_config or "column" not in example_config:
            return False, stats
        if "match_threshold" not in example_config:
            return False, stats

        threshold = example_config["match_threshold"] / 100.0
        exact_match = example_config.get("exact_match", False)
        exclude_patterns = [re.compile(p) for p in example_config.get("not_like", [])]

        df_lookup = pd.read_csv(example_config["path"])
        column_name = example_config["column"]
        if column_name not in df_lookup.columns:
            return False, stats

        example_values = set(df_lookup[column_name].dropna().astype(str).str.strip().str.lower())
        string_series = series.dropna().astype(str).str.strip().str.lower()

        def is_match(v):
            if any(ex.search(v) for ex in exclude_patterns):
                return False
            if exact_match:
                return any(token in example_values for token in re.findall(r"\b\w+\b", v))
            else:
                return v in example_values

        match_ratio = string_series.apply(is_match).mean()
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

            name_match = match_field_name(col, crit.get("field_name_patterns", []))
            heuristic_match, heuristic_stats = match_heuristics(all_values, crit.get("value_heuristics", []))
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
