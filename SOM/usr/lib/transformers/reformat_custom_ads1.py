"""
Transformer: reformat_custom_ads1.py

This transformer pivots staff-to-application assignment data. It groups input rows
by 'Application' and reshapes the data so each staff role becomes its own column,
containing the associated staff name. Metadata fields like 'Dept', 'Title', and
'Title Name' are preserved for each application. Missing expected columns such as
'App Id' are added as None to maintain consistency in output format.

Typical Use Case:
- Converts long-form staff role assignments into a wide-format application view
  for reporting and correlation with other ADS datasets.

Input Columns Required:
- Application
- Staff Role
- Staff Name
- Dept
- Title
- Title Name

Output:
- One row per Application with separate columns for each staff role and metadata.
"""


import pandas as pd
from datetime import datetime

def log_message(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)

def log_exception(e):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] ERROR: {type(e).__name__}: {e}", flush=True)

#def reformat_custom_ads1(df_ads2: pd.DataFrame) -> pd.DataFrame:
def reformat_custom_ads1(df_ads2: pd.DataFrame, transforms: dict) -> pd.DataFrame:
   
    #log_message(f"[DEBUG] Columns in input df: {list(df_ads2.columns)}")

    df_ads2["Staff Role"] = df_ads2["Staff Role"].str.strip()
    df_ads2["Application"] = df_ads2["Application"].astype(str).str.strip()

    pivot_df = df_ads2.pivot_table(
        index="Application",
        columns="Staff Role",
        values="Staff Name",
        aggfunc="first"
    ).reset_index()

    metadata = df_ads2.groupby("Application")[[
        # "App Id",
        "Dept", "Title", "Title Name"
    ]].first().reset_index()

    formatted_df = pd.merge(metadata, pivot_df, on="Application", how="left")

    role_fields = sorted(pivot_df.columns.drop("Application").tolist())
    column_order = ["App Id", "Application"] + role_fields + ["Dept", "Title", "Title Name"]

    for col in column_order:
        if col not in formatted_df.columns:
            formatted_df[col] = None

    #log_message(f"[DEBUG] Final formatted df columns: {list(formatted_df.columns)}")
    return formatted_df[column_order]
