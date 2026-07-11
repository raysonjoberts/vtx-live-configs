import os
import pandas as pd
from datetime import datetime

# Reuse global variables like the reference script
BTDM_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TABLES_DIR = os.path.join(BTDM_ROOT, "var", "tables")
LOG_FILE = os.path.join(BTDM_ROOT, "var", "logs", "table_aggregator.log")

INPUT_FILE = os.path.join(TABLES_DIR, "consolidated_staff_table.csv")
OUTPUT_FILE = os.path.join(TABLES_DIR, "consolidated_staff_view.csv")

SCRIPT_NAME = os.path.splitext(os.path.basename(__file__))[0]  # e.g., 'custom_view'

def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"{timestamp},{SCRIPT_NAME},{msg}\n")

def main():
    try:
        df = pd.read_csv(INPUT_FILE)

        # Drop unnecessary fields
        drop_fields = ["STAFF_ID", "STAFF_ROLE_ID", "LAST_UPDATED_BY", "LAST_UPDATED_DATE", "ID_x", "ID_y"]
        df.drop(columns=drop_fields, inplace=True, errors='ignore')

        # Ensure all values are strings before aggregation
        df["STAFF_NAME"] = df["STAFF_NAME"].astype(str)
        df["TITLE"] = df["TITLE"].astype(str)

        # Aggregate duplicates and pivot the data
        staff_name_pivot = df.groupby(["APP_ID", "DESCRIPTION"])["STAFF_NAME"].agg(lambda x: "; ".join(x.unique())).unstack()
        title_pivot = df.groupby(["APP_ID", "DESCRIPTION"])["TITLE"].agg(lambda x: "; ".join(x.unique())).unstack()

        # Add suffix to distinguish titles
        title_pivot.columns = [f"{col} Title" for col in title_pivot.columns]

        # Combine both pivots side-by-side
        result = pd.concat([staff_name_pivot, title_pivot], axis=1).reset_index()

        # Write result
        result.to_csv(OUTPUT_FILE, index=False)
        log(f"[SUCCESS] Created custom staff view with {len(result)} rows")

    except Exception as e:
        log(f"[ERROR] Failed to generate custom view: {e}")

if __name__ == "__main__":
    main()