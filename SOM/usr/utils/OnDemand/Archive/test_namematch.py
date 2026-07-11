# FULL updated logic: apply ACTIVE == 'Y', 20% minimum value presence, match logic vs valid set
import pandas as pd
import re
import yaml

USERNAME_LOOKUP_PATH = r"C:\BTDM_7.1\var\data\all_names.txt"
TEST_DATA_PATH = r"C:\BTDM_7.1\var\tables\consolidated_application_view.csv"
YAML_PATH = r"C:\BTDM_7.1\usr\config\default\data_source_analysis_test.yaml"

# Load known names
def load_username_set(path):
    name_set = set()
    with open(path, 'r') as f:
        for line in f:
            parts = line.strip().split(',')
            name = parts[0].strip()
            if name.isalpha():
                name_set.add(name.lower())
    return name_set

USERNAME_LOOKUP = load_username_set(USERNAME_LOOKUP_PATH)

# Load YAML
def load_yaml(path):
    with open(path, 'r') as f:
        return yaml.safe_load(f)

# Pre-filter logic
def apply_pre_filters(series, pre_filters):
    for rule in pre_filters:
        if rule["type"] == "reject_if":
            pattern = re.compile(rule["pattern"])
            matches = series.astype(str).apply(lambda v: bool(pattern.match(v)))
            match_ratio = matches.mean()
            if match_ratio > 0.95:
                print(f"[REJECTED] >95% of values in field match reject_if pattern: {rule['reason']}")
                return False
    return True

# Name token match
def external_us_name_match(series, threshold):
    def contains_valid_name(value):
        tokens = re.findall(r"\b\w+\b", str(value))
        return any(token.lower() in USERNAME_LOOKUP for token in tokens)

    filtered = series.dropna().astype(str)
    if filtered.empty:
        return False, 0

    match_flags = filtered.apply(contains_valid_name)
    match_ratio = match_flags.mean() * 100
    return match_ratio >= threshold, match_ratio

# Capitalization pattern match
def capitalization_match(series, pattern):
    regex = re.compile(pattern)
    matches = series.dropna().astype(str).apply(lambda v: bool(regex.match(v)))
    return matches.mean() * 100

# MAIN
df = pd.read_csv(TEST_DATA_PATH)
if "ACTIVE" in df.columns:
    df = df[df["ACTIVE"].astype(str).str.upper() == "Y"]

total_active = len(df)
yaml_config = load_yaml(YAML_PATH)
criteria = [c for c in yaml_config.get("criteria", []) if c.get("semantic_type") == "user_name"]

print(f"Filtered to ACTIVE=Y ({total_active} entries)\n")

for attr in criteria:
    pre_filters = attr.get("pre_filters", [])
    heuristics = attr.get("value_heuristics", [])
    name_pattern = next((h["pattern"] for h in heuristics if h["type"] == "string" and "pattern" in h), None)
    threshold = next((h["match_threshold"] for h in heuristics if h["type"] == "external_us_name_match"), 50)

    for col in df.columns:
        col_values = df[col].dropna()
        pop_ratio = len(col_values) / total_active

        if pop_ratio < 0.20:
            print(f"SKIPPED: {col:30} | Only {len(col_values)} non-null values ({pop_ratio:.1%})")
            continue

        if not apply_pre_filters(col_values, pre_filters):
            print(f"REJECTED by pre-filters: {col}")
            continue

        name_match, name_pct = external_us_name_match(col_values, threshold)
        capital_pct = capitalization_match(col_values, name_pattern) if name_pattern else 0

        if name_match:
            print(f"MATCH:   {col:30} | {name_pct:5.1f}% US name match | {capital_pct:5.1f}% cap pattern")
        else:
            print(f"NO MATCH: {col:30} | {name_pct:5.1f}% name | {capital_pct:5.1f}% cap pattern")
