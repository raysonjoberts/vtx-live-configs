"""
Transformer: reformat_custom_ads2.py

This transformer normalizes server names and aggregates application-level data
from a raw ADS2 dataset. It ensures server identifiers are consistently formatted,
then groups all fields by 'Application', returning unique, space-separated values
per field.

Typical Use Case:
- Collapses duplicate application rows into a single summary row.
- Normalizes server names for consistency.
- Ensures all fields are aggregated with deduplicated content.

Input Columns Required:
- Application
- Server Name

Output:
- One row per Application with unique values per field, space-separated.
- 'Server' column is normalized and cleaned of special characters and domain suffixes.
"""


import pandas as pd
from datetime import datetime

def log_message(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)

def log_exception(e):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] ERROR: {type(e).__name__}: {e}", flush=True)


def reformat_custom_ads2(df: pd.DataFrame) -> pd.DataFrame:
    if "Application" not in df.columns:
        raise ValueError("Required column 'Application' not found in input data.")

    # Step 1: Normalize 'Server' field
    df["Server"] = df["Server Name"].where(df["Server Name"].notna(), "")
    df["Server"] = (
        df["Server"]
        .str.lower()
        .str.replace(r"\..*$", "", regex=True)
        .str.replace(r"[^a-zA-Z0-9-_]", "", regex=True)
    )
    # Ensure consistent string representation
    df = df.fillna("").astype(str)

    # Group by 'Application' and aggregate all fields with unique space-separated values
    def aggregate_values(series):
        unique_vals = sorted(set(series))
        return " ".join(unique_vals)

    grouped = df.groupby("Application").agg(aggregate_values).reset_index()

    return grouped
