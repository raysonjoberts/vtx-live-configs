import pandas as pd
import yaml
import re
from collections import defaultdict

# Hardcoded paths
csv_path = r"C:\BTDM_7.1\var\tables\consolidated_application_view.csv"
yaml_path = r"C:\BTDM_7.1\usr\config\default\data_source_analysis.yaml"
output_path = r"C:\BTDM_7.1\var\analysis\semantic_analysis.csv"

def load_yaml(path):
    with open(path, 'r') as f:
        return yaml.safe_load(f)

def match_field_name(field, patterns):
    return any(pat.lower() in field.lower() for pat in patterns)

def match_heuristics(series: pd.Series, heuristics):
    for rule in heuristics:
        if rule["type"] == "string":
            string_series = series.dropna().astype(str)

            # Hard gate: min_unique_ratio
            min_unique_ratio = rule.get("min_unique_ratio")
            if min_unique_ratio is not None:
                if string_series.nunique() / len(string_series) < min_unique_ratio:
                    return False

            min_length = rule.get("min_length")
            if min_length is not None:
                if string_series.str.len().lt(min_length).all():
                    continue

            min_unique = rule.get("min_unique")
            if min_unique is not None:
                if string_series.nunique() < min_unique:
                    continue

            if "contains_any" in rule:
                match = string_series.apply(
                    lambda v: any(token.lower() in v.lower() for token in rule["contains_any"])
                )
                if match.any():
                    return True

            if "pattern" in rule:
                regex = re.compile(rule["pattern"])
                match = string_series.apply(lambda v: bool(regex.match(v)))
                if match.any():
                    return True
    return False

def evaluate_fields(df, criteria):
    results = []

    for col in df.columns:
        all_values = df[col].dropna()
        best_result = {
            "Field Name": col,
            "Program Attribute": "No Match",
            "Identifier Method": "No Match",
            "Confidence": "Low",
            "Score": 0.0,
            "Impact": 0,
            "Difficulty": 0,
            "Max Weight": 0,
            "Explanation": "Field did not match any defined attributes"
        }

        for attr in criteria:
            attr_name = attr["attribute"]
            impact = attr.get("impact", 1)
            difficulty = attr.get("difficulty", 1)
            max_weight = impact + difficulty

            # Heuristics as hard gate
            content_match = match_heuristics(all_values, attr.get("value_heuristics", []))
            if not content_match:
                continue

            name_match = match_field_name(col, attr.get("field_name_patterns", []))

            if name_match and content_match:
                score = 1.0
                method = "Content Match + Name Match"
                confidence = "High"
            elif content_match:
                score = 0.5
                method = "Content Match"
                confidence = "Medium"
            else:
                score = 0.0
                method = "No Match"
                confidence = "Low"

            if score > best_result["Score"]:
                best_result = {
                    "Field Name": col,
                    "Program Attribute": attr_name,
                    "Identifier Method": method,
                    "Confidence": confidence,
                    "Score": score,
                    "Impact": impact,
                    "Difficulty": difficulty,
                    "Max Weight": max_weight,
                    "Explanation": attr["description"]
                }

        results.append(best_result)

    return pd.DataFrame(results)

def main():
    df = pd.read_csv(csv_path)
    yaml_data = load_yaml(yaml_path)
    criteria = yaml_data.get("criteria", [])
    results_df = evaluate_fields(df, criteria)
    results_df.to_csv(output_path, index=False)
    print(f"Analysis complete. Output saved to: {output_path}")

if __name__ == "__main__":
    main()
