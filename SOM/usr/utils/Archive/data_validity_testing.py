import pandas as pd
import re
import math

# ========================
# User Configurable Inputs
# ========================
INPUT_FILE = r"C:\BTDM_7.1\var\tables\consolidated_application_view.csv"   # Path to your intake file
OUTPUT_REPORT = r"C:\BTDM_7.1\var\analysis\validity_report.csv"  # Path to write report


# ========================
# Canonical Pattern Categories
# ========================
PATTERNS = {
    "letters_only": r"^[A-Za-z]+$",
    "numbers_only": r"^\d+$",
    "alphanumeric": r"^[A-Za-z0-9]+$",
    "letters_with_spaces": r"^[A-Za-z ]+$",
    "alphanumeric_with_symbols": r"^[A-Za-z0-9 .,_-]+$",
}

# ========================
# Helper Functions
# ========================
def infer_type(series):
    """Infer a general type based on dominant regex pattern."""
    dominant, adherence, _ = find_dominant_pattern(series)
    if not dominant:
        return "Empty/Unknown"
    elif dominant == "numbers_only":
        return "Numeric"
    elif "letters" in dominant:
        return "String"
    elif "alphanumeric" in dominant:
        return "Alphanumeric"
    else:
        return "Mixed/Other"

def entropy(series):
    """Compute Shannon entropy of value distribution."""
    counts = series.value_counts(normalize=True)
    return -(counts * counts.apply(lambda p: math.log(p, 2))).sum()

def find_dominant_pattern(series):
    """Find the pattern category with the highest adherence (relative to non-empty values)."""
    total = len(series)
    if total == 0:
        return None, None, 0

    # Identify non-empty values correctly
    non_empty = series[series.notna() & (series.astype(str).str.strip() != "")]
    used_count = len(non_empty)

    if used_count == 0:
        return None, None, 0

    results = {}
    for name, pattern in PATTERNS.items():
        matches = non_empty.astype(str).str.match(pattern).sum()
        results[name] = matches / used_count

    dominant = max(results, key=results.get)
    adherence = results[dominant] * 100
    used_pct = (used_count / total) * 100 if total > 0 else 0

    return dominant, round(adherence, 2), round(used_pct, 2)

# ========================
# Main Analysis
# ========================
def analyze_file(input_file, output_file):
    df = pd.read_csv(input_file)

    report_rows = []

    for col in df.columns:
        series_raw = df[col]
        total = len(series_raw)

        # Convert only non-null values to string for analysis
        series = series_raw.dropna().astype(str)
        unique_count = series.nunique()

        inferred_type = infer_type(series)
        dominant_pattern, pattern_adherence, used_pct = find_dominant_pattern(series_raw)

        min_len = series.str.len().min() if not series.empty else None
        max_len = series.str.len().max() if not series.empty else None
        entropy_score = entropy(series) if not series.empty else None

        report_rows.append({
            "Field Name": col,
            "Inferred Type": inferred_type,
            "Dominant Pattern": dominant_pattern if dominant_pattern else "None",
            "Used %": used_pct,
            "Pattern Adherence %": pattern_adherence if pattern_adherence is not None else "N/A",
            "Min Length": min_len,
            "Max Length": max_len,
            "Unique Count": unique_count,
            "Uniqueness Ratio": round(unique_count / total, 2) if total else None,
            "Entropy Score": round(entropy_score, 3) if entropy_score else None,
            "Flagged": "YES" if pattern_adherence is not None and pattern_adherence < 90 else "NO"
        })

    report_df = pd.DataFrame(report_rows)
    report_df.to_csv(output_file, index=False)
    print(f"[INFO] Validity report written to {output_file}")

# ========================
# Run
# ========================
if __name__ == "__main__":
    analyze_file(INPUT_FILE, OUTPUT_REPORT)
