import pandas as pd
from datetime import datetime

def log_message(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)

def log_exception(e):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] ERROR: {type(e).__name__}: {e}", flush=True)

def reformat_custom_ads3(df: pd.DataFrame) -> pd.DataFrame:
    log_message("[DEBUG] Columns in original DataFrame:", df.columns.tolist())

    # Normalize column names (strip and lower)
    df.columns = [col.strip() for col in df.columns]
    col_map = {col.lower(): col for col in df.columns}

    # Drop "Server Ip" if it exists
    if "server ip" in col_map:
        df = df.drop(columns=[col_map["server ip"]])
        log_message("[DEBUG] Dropped column: Server Ip")

    # Required fields
    try:
        title_col = col_map["title name"]
        env_col = col_map["db environment"]
        server_col = col_map["server name"]
    except KeyError as e:
        raise ValueError(f"Missing required field: {e}")
    
    # Fill blank DB Environment values with "NA"
    df[env_col] = df[env_col].fillna("NA").replace("", "NA")

    group_keys = [title_col, env_col]
    other_cols = [col for col in df.columns if col not in group_keys + [server_col]]

    # Perform aggregation
    grouped = df.groupby(group_keys, as_index=False).agg({
        server_col: lambda x: ' '.join(sorted(set(str(v).strip() for v in x if pd.notna(v)))),
        **{col: "first" for col in other_cols}
    })

    # Rename 'Title Name' to 'Application'
    grouped = grouped.rename(columns={title_col: "Application"})

    log_message("[DEBUG] Grouped result preview:")
    log_message(grouped.head(5).to_string())

    return grouped
