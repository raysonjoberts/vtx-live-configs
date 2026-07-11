# Diagnostic dump: test raw token matching on known name fields
import pandas as pd
import re

USERNAME_LOOKUP_PATH = r"C:\BTDM_7.1\var\data\all_names.txt"
TEST_DATA_PATH = r"C:\BTDM_7.1\var\tables\consolidated_application_view.csv"
FIELDS_TO_CHECK = ["Security Approver", "Product Manager", "Product Manager", "IT Manager", "Executive Business Sponsor", "DISPOSITION"]  # <-- put known name fields here

# Load names
name_set = set()
with open(USERNAME_LOOKUP_PATH, 'r') as f:
    for line in f:
        parts = line.strip().split(',')
        if len(parts) >= 1:
            name = parts[0].strip()
            if name.isalpha():
                name_set.add(name.lower())

print(f"Loaded {len(name_set):,} unique first names from list.")

df = pd.read_csv(TEST_DATA_PATH)

# Inspect token matches directly
for field in FIELDS_TO_CHECK:
    if field not in df.columns:
        print(f"Field missing: {field}")
        continue

    values = df[field].dropna().astype(str)
    match_count = 0
    total = len(values)

    for val in values:
        tokens = re.findall(r"\b\w+\b", val)
        if any(t.lower() in name_set for t in tokens):
            match_count += 1

    match_pct = (match_count / total) * 100 if total else 0
    print(f"{field}: {match_count}/{total} match ({match_pct:.1f}%)")