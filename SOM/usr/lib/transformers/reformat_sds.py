
import pandas as pd
import re
from datetime import datetime

def log_message(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)

def log_exception(e):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] ERROR: {type(e).__name__}: {e}", flush=True)


def reformat_sds(df: pd.DataFrame, source_field: str) -> pd.DataFrame:
    if source_field not in df.columns:
        raise ValueError(f"Source field '{source_field}' not found in the input data.")

    # Step 1: Lowercase the source server name
    df["Server"] = df[source_field].astype(str).str.lower()

    # Step 2: Remove FQDN - keep only the short name
    df["Server"] = df["Server"].str.replace(r"\..*$", "", regex=True)

    # Step 3: Remove special characters except a-zA-Z0-9-_
    df["Server"] = df["Server"].str.replace(r"[^a-zA-Z0-9-_]", "", regex=True)

    return df
