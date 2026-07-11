import pandas as pd
import yaml
import re
from collections import defaultdict

# Hardcoded file paths
csv_path = r"C:\BTDM_7.1\var\tables\consolidated_application_view.csv"
yaml_path = r"C:\BTDM_7.1\usr\config\default\data_source_analysis.yaml"
output_path = r"C:\BTDM_7.1\var\analysis\semantic_analysis.csv"
summary_output_path = r"C:\BTDM_7.1\var\analysis\semantic_summary.csv"
username_list_path = r"C:\BTDM_7.1\var\data\all_names.txt"

def load_yaml(path):
    with open(path, 'r') as f:
        return yaml.safe_load(f)

def load_username_set(path):
    name_set = set()
    with open(path, 'r') as f:
        for line in f:
            name = line.strip()
            if name.isalpha():
                name_set.add(name.lower())
    return name_set

USERNAME_LOOKUP = load_username_set(username_list_path)



def match_value_examples(series: pd.Series, example_config):
    try:
        if not example_config or "path" not in example_config or "column" not in example_config:
            return False
        df_lookup = pd.read_csv(example_config["path"])
        column_name = example_config["column"]
        if column_name not in df_lookup.columns:
            return False

        example_values = set(df_lookup[column_name].dropna().astype(str).str.strip().str.lower())
        series_values = set(series.dropna().astype(str).str.strip().str.lower())
        overlap = example_values & series_values
        return len(overlap) > 0
    except Exception as e:
        print(f"Error reading example match file: {e}")
        return False

def should_declare_match(name_match, content_match, example_match, requirement):
    criteria_map = {
        "name": name_match,
        "value": content_match,
        "example": example_match
    }
    if isinstance(requirement, list):
        return all(criteria_map.get(key, False) for key in requirement)
    elif isinstance(requirement, str):
        criteria = [name_match, content_match, example_match]
        count = sum(criteria)
        if requirement == "all":
            return count == 3
        elif requirement == "2_of_3":
            return count >= 2
        else:  # "any" or fallback
            return count >= 1
    else:
        return False

def match_field_name(field, patterns):
    return any(pat.lower() in field.lower() for pat in patterns)

def apply_pre_filters(series, pre_filters):
    for rule in pre_filters:
        if rule["type"] == "reject_if":
            pattern = re.compile(rule["pattern"])
            matches = series.astype(str).apply(lambda v: bool(pattern.match(v)))
            if matches.mean() > 0.95:
                return False
    return True

def match_heuristics(series: pd.Series, heuristics):
    for rule in heuristics:
        string_series = series.dropna().astype(str)
        if len(string_series) == 0:
            continue

        if rule["type"] == "string":
            if "min_unique_ratio" in rule:
                unique_ratio = string_series.nunique() / len(string_series)
                if unique_ratio < rule["min_unique_ratio"]:
                    continue

            if "pattern" in rule:
                regex = re.compile(rule["pattern"])
                match = string_series.apply(lambda v: bool(regex.match(v)))
                min_ratio = rule.get("min_match_ratio")
                if min_ratio is not None:
                    if match.mean() >= min_ratio:
                        return True
                elif match.any():
                    return True

        elif rule["type"] == "external_us_name_match":
            def contains_valid_name(value):
                tokens = re.findall(r"\b\w+\b", value)
                return any(token.lower() in USERNAME_LOOKUP for token in tokens)

            match_flags = string_series.apply(contains_valid_name)
            match_ratio = match_flags.mean() * 100
            threshold = rule.get("match_threshold", 50)
            if match_ratio >= threshold:
                return True

        elif rule["type"] == "uniqueness_ratio":
            min_ratio = rule.get("min_unique_ratio", 0.0)
            unique_ratio = string_series.nunique() / len(string_series)
            if unique_ratio >= min_ratio:
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
        return 2.5

def evaluate_fields(df, criteria):
    results = []
    summary = {}
    total_rows = len(df)

    for col in df.columns:
        print(f"\nEvaluating column: {col}")
        all_values = df[col].dropna()
        pop_ratio = len(all_values) / total_rows
        if pop_ratio < 0.20:
            continue  # Skip fields with too little usable data

        is_used_pct = all_values.count() / total_rows * 100
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
            required = attr.get("required", True)
            must_match_name = attr.get("must_match_name", False)
            max_weight = impact + difficulty

            if must_match_name and not match_field_name(col, attr.get("field_name_patterns", [])):
                continue

            if not apply_pre_filters(all_values, attr.get("pre_filters", [])):
                continue


            name_match = match_field_name(col, attr.get("field_name_patterns", []))
            content_match = match_heuristics(all_values, attr.get("value_heuristics", []))
            example_match = match_value_examples(all_values, attr.get("field_value_examples", {}))
            requirement = attr.get("match_requirements", "any")
            print(f"Name match: {name_match}, Heuristic match: {content_match}, Example match: {example_match}")
            print(f"Required to match: {requirement}")

            if not should_declare_match(name_match, content_match, example_match, requirement):
                continue

            if name_match and content_match and example_match:
                method = "Name + Content + Example Match"
                score = 1.0
                confidence = is_used_score * 3
            elif (name_match and content_match) or (name_match and example_match) or (content_match and example_match):
                method = "Two of Three Match"
                score = 0.75
                confidence = is_used_score * 2
            elif name_match or content_match or example_match:
                method = "One Match"
                score = 0.5
                confidence = is_used_score * 1
            else:
                continue
                score = 0.5
                confidence = is_used_score * 1

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
                if required:
                    summary[attr_name] = {
                        "Attribute": attr_name,
                        "Best Match": col,
                        "Identifier Method": method,
                        "is_used (%)": round(is_used_pct, 2),
                        "Confidence": round(confidence, 2),
                        "Difficulty": difficulty,
                        "Difficulty Score": round(difficulty_score, 2),
                        "Impact": impact,
                        "Impact Score": round(impact_score, 2)
                    }

        results.append(best_result)

    matched_attrs = set(summary.keys())
    for attr in criteria:
        attr_name = attr["attribute"]
        required = attr.get("required", True)
        if required and attr_name not in matched_attrs:
            summary[attr_name] = {
                "Attribute": attr_name,
                "Best Match": "(no match)",
                "Identifier Method": "No Match",
                "is_used (%)": 0.0,
                "Confidence": 0,
                "Difficulty": attr.get("difficulty", 1),
                "Difficulty Score": 0,
                "Impact": attr.get("impact", 1),
                "Impact Score": 0
            }

    summary_df = pd.DataFrame(summary.values())
    return pd.DataFrame(results), summary_df

def main():
    df = pd.read_csv(csv_path)

    if "ACTIVE" in df.columns:
        df = df[df["ACTIVE"].astype(str).str.upper() == "Y"]

    yaml_data = load_yaml(yaml_path)
    criteria = yaml_data.get("criteria", [])
    results_df, summary_df = evaluate_fields(df, criteria)

    active_count = len(df)
    results_df["Active_Applications"] = active_count

    results_df.to_csv(output_path, index=False)
    summary_df.to_csv(summary_output_path, index=False)

    print(f"Filtered on ACTIVE = 'Y'. Found {active_count} active records.\n")
    print(f"Detailed analysis saved to {output_path}")
    print(f"Summary report saved to {summary_output_path}")

if __name__ == "__main__":
    main()
