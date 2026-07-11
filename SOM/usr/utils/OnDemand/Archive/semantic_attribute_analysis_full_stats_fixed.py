import pandas as pd
import yaml
import re
match_ratio = 0
unique_ratio = 0
too_short = 0
too_long = 0
example_ratio = 0

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
    return any(pat.lower() in tokens for pat in patterns)

def apply_pre_filters(series, rule):
    if "min_length" in rule and series.astype(str).map(len).lt(rule["min_length"]).mean() > 0.95:
        return False
    if "max_length" in rule and series.astype(str).map(len).gt(rule["max_length"]).mean() > 0.95:
        return False
    return True

def match_heuristics(series, heuristics):
    match_ratio = unique_ratio = too_short = too_long = 0
    match_ratio = unique_ratio = too_short = too_long = 0

    for rule in heuristics:
        if rule["type"] == "string":
            string_series = series.dropna().astype(str)
            if len(string_series) == 0:
                continue
            matched = True
            if "min_length" in rule:
                too_short = string_series.map(len).lt(rule["min_length"]).mean()
                if too_short > 0.95:
                    continue
            if "max_length" in rule:
                too_long = string_series.map(len).gt(rule["max_length"]).mean()
                if too_long > 0.95:
                    continue
            if "min_unique_ratio" in rule:
                unique_ratio = string_series.nunique() / len(string_series)
                if unique_ratio < rule["min_unique_ratio"]:
                    continue
            if "pattern" in rule:
                regex = re.compile(rule["pattern"])
                match_ratio = string_series.apply(lambda v: bool(regex.match(v))).mean()
                if "min_match_ratio" in rule and match_ratio < rule["min_match_ratio"]:
                    continue
            return True
            string_series = series.dropna().astype(str)
            if len(string_series) == 0:
                continue

            if "min_length" in rule:
                too_short = string_series.map(len).lt(rule["min_length"]).mean()

            if "max_length" in rule:
                too_long = string_series.map(len).gt(rule["max_length"]).mean()

            if "min_unique_ratio" in rule:
                if "min_unique_ratio" in rule:
                    unique_ratio = string_series.nunique() / len(string_series)
                if "min_unique_ratio" in rule and unique_ratio < rule["min_unique_ratio"]:
                    continue
            else:
                if "min_unique_ratio" in rule:
                    unique_ratio = string_series.nunique() / len(string_series)

            if "pattern" in rule:
                regex = re.compile(rule["pattern"])
                match_ratio = string_series.apply(lambda v: bool(regex.match(v))).mean()
                if 'min_match_ratio' not in rule:
                    print("    Skipping regex match: no min_match_ratio defined in YAML.")
                    continue
                if match_ratio >= rule['min_match_ratio']:
                    return True
    return False

def match_value_examples(series, example_config):
    example_ratio = 0
    example_ratio = 0

    try:
        if not example_config or "path" not in example_config or "column" not in example_config:
            return False
        if "match_threshold" not in example_config:
            return False
        if "match_threshold" not in example_config:
            return False
        threshold = example_config["match_threshold"] / 100.0
        exact_match = example_config.get("exact_match", False)

        df_lookup = pd.read_csv(example_config["path"])
        column_name = example_config["column"]
        if column_name not in df_lookup.columns:
            return False

        example_values = set(df_lookup[column_name].dropna().astype(str).str.strip().str.lower())
        string_series = series.dropna().astype(str).str.strip().str.lower()

        if exact_match:
            example_ratio = string_series.apply(lambda v: any(token in example_values for token in re.findall(r"\b\w+\b", v))).mean()
        else:
            example_ratio = string_series.apply(lambda v: v in example_values).mean()

        return example_ratio >= threshold
    except Exception:
        return False

def should_declare_match(name_match, heuristic_match, example_match, requirement):
    criteria_map = {
        "name": name_match,
        "value": heuristic_match,
        "example": example_match
    }
    return all(criteria_map.get(key, False) for key in requirement)

def evaluate(df, criteria_list):
    global match_ratio, unique_ratio, too_short, too_long, example_ratio
    match_ratio = unique_ratio = too_short = too_long = example_ratio = 0
    results = []
    summary = []
    total_rows = len(df)

    for col in df.columns:
        name_stats = {
            "Name Match Ratio": 0.0,
            "Name Unique Ratio": None,
            "Name Max Length Violation Ratio": None,
            "Name Min Length Violation Ratio": None
        }
        value_stats = {
            "Value Match Ratio": 0.0,
            "Value Unique Ratio": 0.0,
            "Value Max Length Violation Ratio": 0.0,
            "Value Min Length Violation Ratio": 0.0
        }
        example_stats = {
            "Example Match Ratio": 0.0
        }
        all_values = df[col].dropna()
        is_used_pct = round(len(all_values) / total_rows * 100, 2)
        matched = False

        for crit in criteria_list:
            attr = crit["attribute"]
            requirement = crit.get("match_requirements", ["name", "value"])

            name_match = match_field_name(col, crit.get("field_name_patterns", []))
            heuristic_match = match_heuristics(all_values, crit.get("value_heuristics", []))
            example_match = match_value_examples(all_values, crit.get("field_value_examples", {}))

            if should_declare_match(name_match, heuristic_match, example_match, requirement):
                result = {
                    "Field Name": col,
                    **name_stats,
                    **value_stats,
                    **example_stats,
                    "Value Match Ratio": round(match_ratio * 100, 2),
                    "Unique Ratio": round(unique_ratio * 100, 2),
                    "Max Length Violation Ratio": round(too_long * 100, 2),
                    "Min Length Violation Ratio": round(too_short * 100, 2),
                    "Example Match Ratio": round(example_ratio * 100, 2),
                    "Program Attribute": attr,
                    "Match Type": "+".join([k for k, v in {"name": name_match, "value": heuristic_match, "example": example_match}.items() if v]),
                    "is_used_pct": is_used_pct,
                    "Value Match Ratio": round(match_ratio * 100, 2),
                    "Unique Ratio": round(unique_ratio * 100, 2),
                    "Max Length Violation Ratio": round(too_long * 100, 2),
                    "Min Length Violation Ratio": round(too_short * 100, 2),
                    "Example Match Ratio": round(example_ratio * 100, 2)
                }
                results.append(result)
                matched = True
                value_stats = {
                    "Value Match Ratio": round(match_ratio * 100, 2),
                    "Value Unique Ratio": round(unique_ratio * 100, 2),
                    "Value Max Length Violation Ratio": round(too_long * 100, 2),
                    "Value Min Length Violation Ratio": round(too_short * 100, 2)
                }
                example_stats = {
                    "Example Match Ratio": round(example_ratio * 100, 2)
                }
                name_stats = {
                    "Name Match Ratio": 100.0 if name_match else 0.0,
                    "Name Unique Ratio": None,
                    "Name Max Length Violation Ratio": None,
                    "Name Min Length Violation Ratio": None
                }

                if crit.get("required", False):
                    summary.append({
                        "Attribute": attr,
                        "Matched Field": col,
                        "Match Type": result["Match Type"]
                    })

        if not matched:
            results.append({
                "Field Name": col,
                    **name_stats,
                    **value_stats,
                    **example_stats,
                    "Value Match Ratio": round(match_ratio * 100, 2),
                    "Unique Ratio": round(unique_ratio * 100, 2),
                    "Max Length Violation Ratio": round(too_long * 100, 2),
                    "Min Length Violation Ratio": round(too_short * 100, 2),
                    "Example Match Ratio": round(example_ratio * 100, 2),
                "Program Attribute": "No Match",
                "Value Match Ratio": round(match_ratio * 100, 2),
                "Unique Ratio": round(unique_ratio * 100, 2),
                "Max Length Violation Ratio": round(too_long * 100, 2),
                "Min Length Violation Ratio": round(too_short * 100, 2),
                "Example Match Ratio": round(example_ratio * 100, 2),
                "Match Type": "None",
                "is_used_pct": is_used_pct,
                "Value Match Ratio": round(match_ratio * 100, 2),
                "Unique Ratio": round(unique_ratio * 100, 2),
                "Max Length Violation Ratio": round(too_long * 100, 2),
                "Min Length Violation Ratio": round(too_short * 100, 2),
                "Example Match Ratio": round(example_ratio * 100, 2)
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
