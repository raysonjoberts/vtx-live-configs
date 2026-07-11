"""
Transformer: reformat_custom_ads4.py

This transformer cleans and aggregates ADS application data by title and DB environment.
It removes unwanted columns, standardizes missing values in the 'DB Environment' field,
and groups the data by 'Title Name' and 'DB Environment'.

The 'Server Name' field is aggregated using a space-separated list of unique entries,
while all other fields retain their first observed value in each group.

Typical Use Case:
- Removes duplicate rows and unwanted columns.
- Aggregates servers and metadata per application and environment.
- Ensures consistent handling of blank DB Environment values.

Input Columns Required:
- Title Name
- DB Environment
- Server Name

Output:
- One row per unique (Title Name, DB Environment) pair with servers and metadata.
"""

import pandas as pd
from datetime import datetime

def log_message(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)

def concat_unique(values):
    return " ".join(pd.Series(values).dropna().astype(str).unique())

def reformat_custom_ads4(df, transforms):
    log_message(f"[DEBUG] Columns in original DataFrame: {df.columns.tolist()}")

    # Strip whitespace from column names
    df.columns = [col.strip() for col in df.columns]

    # Drop "Server Ip" column if it exists
    if "Server Ip" in df.columns:
        df = df.drop(columns=["Server Ip"])
        log_message("[DEBUG] Dropped column: Server Ip")

    # Fix blank or missing DB Environment
    df["DB Environment"] = df["DB Environment"].replace("", pd.NA).fillna("NA")

    # Fields to aggregate using space-delimited merge
    concat_fields = ["Server Name"]  # Add others as needed
    non_concat_fields = [col for col in df.columns if col not in concat_fields + ["Title Name", "DB Environment"]]

    # Build aggregation functions
    agg_funcs = {col: concat_unique for col in concat_fields}
    for col in non_concat_fields:
        agg_funcs[col] = "first"

    grouped = df.groupby(["Title Name", "DB Environment"], as_index=False).agg(agg_funcs)

    # Restore blank DB Environment
    grouped["DB Environment"] = grouped["DB Environment"].replace("NA", "")

    log_message("[DEBUG] Grouped result preview:")
    log_message(grouped.head(5).to_string())

    return grouped
