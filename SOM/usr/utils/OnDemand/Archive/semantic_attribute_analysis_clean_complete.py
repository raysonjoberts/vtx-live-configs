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
    return any(pat.lower() in tokens for pat in patterns)

def apply_pre_filters(series, rule):
    if "min_length" in rule and series.dropna().astype(str).map(len).lt(rule["min_length"]).mean() > 0.95:
        return False
    if "max_length" in rule and series.dropna().astype(str).map(len).gt(rule["max_length"]).mean() > 0.95:
        return False
    return True


def match_heuristics(series, heuristics):
    for rule in heuristics:
        if rule["type"] == "string":
            string_series = series.dropna().astype(str)
            if len(string_series) == 0:
                print("  ⚠️ Empty series for heuristic check")
                continue

            if "min_length" in rule:
                too_short = string_series.map(len).lt(rule["min_length"]).mean()
                print(f"    Length < {rule['min_length']}: {too_short:.2f}")

            if "max_length" in rule:
                too_long = string_series.map(len).gt(rule["max_length"]).mean()
                print(f"    Length > {rule['max_length']}: {too_long:.2f}")

            if "min_unique_ratio" in rule:
                unique_ratio = string_series.nunique() / len(string_series)
                print(f"    Unique ratio: {unique_ratio:.2f} (required: {rule['min_unique_ratio']})")
                if unique_ratio < rule["min_unique_ratio"]:
                    continue

            if "pattern" in rule:
                regex = re.compile(rule["pattern"])
                match_ratio = string_series.apply(lambda v: bool(regex.match(v))).mean()
                print(f"    Pattern match ratio: {match_ratio:.2f} (required: {rule.get('min_match_ratio', 0.5)})")
                if match_ratio >= rule.get("min_match_ratio", 0.5):
                    return True
    return False

def match_value_examples(series, example_config):
    try:
        if not example_config or "path" not in example_config or "column" not in example_config:
            return False
        threshold = example_config.get("match_threshold", 50) / 100.0
        exact_match = example_config.get("exact_match", False)

        df_lookup = pd.read_csv(example_config["path"])
        column_name = example_config["column"]
        if column_name not in df_lookup.columns:
            return False

        example_values = set(df_lookup[column_name].dropna().astype(str).str.strip().str.lower())
        string_series = series.dropna().astype(str).str.strip().str.lower()

        if exact_match:
            match_ratio = string_series.apply(lambda v: any(token in example_values for token in re.findall(r"\b\w+\b", v))).mean()
        else:
            match_ratio = string_series.apply(lambda v: v in example_values).mean()

        return match_ratio >= threshold
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
    results = []
    summary = []
    total_rows = len(df)

    for col in df.columns:
        print(f"\nEvaluating column: {col}")
        all_values = df[col].dropna()
        if len(all_values) / total_rows < 0.2:
            continue

        for crit in criteria_list:
        matched = False
            attr = crit["attribute"]
            requirement = crit.get("match_requirements", ["name", "value"])
            name_match = match_field_name(col, crit.get("field_name_patterns", []))
            heuristic_match = match_heuristics(all_values, crit.get("value_heuristics", []))
            example_match = match_value_examples(all_values, crit.get("field_value_examples", {}))

            print(f"  Match Check: name={name_match}, value={heuristic_match}, example={example_match}")
            print(f"  Required: {requirement}")
            if not should_declare_match(name_match, heuristic_match, example_match, requirement):
                print("  → Skipped (did not meet match requirements)")
            if should_declare_match(name_match, heuristic_match, example_match, requirement):
                result = {
                    "Field Name": col,
                    "Program Attribute": attr,
                    "Match Type": "+".join([k for k, v in {"name": name_match, "value": heuristic_match, "example": example_match}.items() if v]),
                    "is_used_pct": round(len(all_values) / total_rows * 100, 2),
                    "Value Match Ratio": round(locals().get('match_ratio', 0) * 100, 2),
                    "Unique Ratio": round(locals().get('unique_ratio', 0) * 100, 2),
                    "Max Length Violation Ratio": round(locals().get('too_long', 0) * 100, 2),
                    "Min Length Violation Ratio": round(locals().get('too_short', 0) * 100, 2)
                }
                result = {
                    "Field Name": col,
                    "Program Attribute": attr,
                    "Match Type": "+".join([k for k, v in {"name": name_match, "value": heuristic_match, "example": example_match}.items() if v]),
                    "is_used_pct": round(len(all_values) / total_rows * 100, 2),
                    "Value Match Ratio": round(locals().get('match_ratio', 0) * 100, 2),
                    "Unique Ratio": round(locals().get('unique_ratio', 0) * 100, 2),
                    "Max Length Violation Ratio": round(locals().get('too_long', 0) * 100, 2),
                    "Min Length Violation Ratio": round(locals().get('too_short', 0) * 100, 2)
                }
                result = {
                    "Field Name": col,
                    "Program Attribute": attr,
                    "Match Type": "+".join([k for k, v in {"name": name_match, "value": heuristic_match, "example": example_match}.items() if v]),
                    "is_used_pct": round(len(all_values) / total_rows * 100, 2),
                    "Value Match Ratio": round(locals().get('match_ratio', 0) * 100, 2),
                    "Unique Ratio": round(locals().get('unique_ratio', 0) * 100, 2),
                    "Max Length Violation Ratio": round(locals().get('too_long', 0) * 100, 2),
                    "Min Length Violation Ratio": round(locals().get('too_short', 0) * 100, 2)
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
                "is_used_pct": round(len(all_values) / total_rows * 100, 2),
                "Value Match Ratio": round(locals().get('match_ratio', 0) * 100, 2),
                "Unique Ratio": round(locals().get('unique_ratio', 0) * 100, 2),
                "Max Length Violation Ratio": round(locals().get('too_long', 0) * 100, 2),
                "Min Length Violation Ratio": round(locals().get('too_short', 0) * 100, 2)
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
