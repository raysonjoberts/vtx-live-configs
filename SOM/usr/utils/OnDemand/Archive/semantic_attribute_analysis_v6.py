import pandas as pd
import yaml
import re
from collections import defaultdict

# Hardcoded file paths
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

            if len(string_series) == 0:
                continue  # skip rules if the series is empty

            min_unique_ratio = rule.get("min_unique_ratio")
            if min_unique_ratio is not None:
                if string_series.nunique() / len(string_series) < min_unique_ratio:
                    return False

            min_length = rule.get("min_length")
            if min_length is not None and string_series.str.len().lt(min_length).all():
                continue

            max_length = rule.get("max_length")
            if max_length is not None and string_series.str.len().gt(max_length).any():
                continue

            min_unique = rule.get("min_unique")
            if min_unique is not None and string_series.nunique() < min_unique:
                continue

            if "contains_any" in rule:
                match = string_series.apply(
                    lambda v: any(token.lower() in v.lower() for token in rule["contains_any"])
                )
                min_ratio = rule.get("min_match_ratio")
                if min_ratio is not None:
                    if match.mean() >= min_ratio:
                        return True
                elif match.any():
                    return True

            if "pattern" in rule:
                regex = re.compile(rule["pattern"])
                match = string_series.apply(lambda v: bool(regex.match(v)))
                min_ratio = rule.get("min_match_ratio")
                if min_ratio is not None:
                    if match.mean() >= min_ratio:
                        return True
                elif match.any():
                    return True
    return False

def get_is_used_score(pct):
    if pct >= 90:
        return 5
    elif pct >= 80:
        return 4
    elif pct >= 70:
        return 3
    elif pct >= 60:
        return 2
    elif pct >= 50:
        return 1
    else:
        return 0

def get_difficulty_multiplier(pct):
    if pct >= 90:
        return 1
    elif pct >= 80:
        return 1.25
    elif pct >= 70:
        return 1.5
    elif pct >= 60:
        return 1.75
    elif pct >= 50:
        return 2
    else:
        return 2.5  # Optional: even higher multiplier for <50

def evaluate_fields(df, criteria):
    results = []

    for col in df.columns:
        all_values = df[col].dropna()
        is_used_pct = all_values.count() / len(df) * 100

        is_used_score = get_is_used_score(is_used_pct)
        multiplier = get_difficulty_multiplier(is_used_pct)

        best_result = {
            "Field Name": col,
            "Program Attribute": "No Match",
            "Identifier Method": "No Match",
            "Confidence": 0,
            "Score": 0.0,
            "Impact": 0,
            "Difficulty": 0,
            "Max Weight": 0,
            "Explanation": "Field did not match any defined attributes",
            "is_used (%)": round(is_used_pct, 2),
            "Difficulty Score": 0.0,
            "Impact Score": 0.0
        }

        for attr in criteria:
            attr_name = attr["attribute"]
            impact = attr.get("impact", 1)
            difficulty = attr.get("difficulty", 1)
            max_weight = impact + difficulty

            content_match = match_heuristics(all_values, attr.get("value_heuristics", []))
            if not content_match:
                continue

            name_match = match_field_name(col, attr.get("field_name_patterns", []))

            if name_match and content_match:
                method = "Content Match + Name Match"
                score = 1.0
                confidence = is_used_score * 2
            elif content_match:
                method = "Content Match"
                score = 0.5
                confidence = is_used_score * 1
            elif name_match:
                method = "Name Match"
                score = 0.5
                confidence = is_used_score * 1
            else:
                method = "No Match"
                score = 0.0
                confidence = 0

            if score > best_result["Score"]:
                difficulty_score = difficulty * multiplier
                impact_score = impact * multiplier
                best_result = {
                    "Field Name": col,
                    "Program Attribute": attr_name,
                    "Identifier Method": method,
                    "Confidence": round(confidence, 2),
                    "Score": score,
                    "Impact": impact,
                    "Difficulty": difficulty,
                    "Max Weight": max_weight,
                    "Explanation": attr["description"],
                    "is_used (%)": round(is_used_pct, 2),
                    "Difficulty Score": round(difficulty_score, 2),
                    "Impact Score": round(impact_score, 2)
                }

        results.append(best_result)

    return pd.DataFrame(results)

def main():
    df = pd.read_csv(csv_path)

    if "ACTIVE" in df.columns:
        df = df[df["ACTIVE"].astype(str).str.upper() == "Y"]

    yaml_data = load_yaml(yaml_path)
    criteria = yaml_data.get("criteria", [])
    results_df = evaluate_fields(df, criteria)

    active_count = len(df)
    results_df["Active_Applications"] = active_count

    results_df.to_csv(output_path, index=False)
    print(f"Filtered on ACTIVE = 'Y'. Found {active_count} active records. Analysis saved to {output_path}")

if __name__ == "__main__":
    main()
