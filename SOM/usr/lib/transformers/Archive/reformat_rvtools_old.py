import os
import pandas as pd
from reformat_sds import reformat_sds
from datetime import datetime


def log_message(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)

def log_exception(e):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] ERROR: {type(e).__name__}: {e}", flush=True)


def reformat_rvtools(file_path: str, output_dir: str, prefix: str = "output"):
    try:
        xls = pd.ExcelFile(file_path)
        base = prefix

        # Process vInfo
        if "vInfo" in xls.sheet_names:
            vinfo_df = xls.parse("vInfo")
            if "VM" in vinfo_df.columns:
                vinfo_df = reformat_sds(vinfo_df, source_field="VM")
                vinfo_out = os.path.join(output_dir, f"{base}_vinfo_server_audit.csv")
                vinfo_df.to_csv(vinfo_out, index=False)
                print(f"Saved: {vinfo_out}")
            else:
                ("Warning: 'VM' column not found in vInfo")

        # Process vPartition with normalization and calculations
        if "vPartition" in xls.sheet_names:
            vpart_df = xls.parse("vPartition")

            if "VM" not in vpart_df.columns:
                print("Warning: 'VM' column missing in vPartition — cannot apply SDS transformation.")
                return

            vpart_df = reformat_sds(vpart_df, source_field="VM")

            # Debug inspection of the Server column
            #print(f"[DEBUG] Columns after reformat_sds: {list(vpart_df.columns)}")
            if "Server" in vpart_df.columns:
                log_message(f"[DEBUG] First 5 entries in 'Server':\n{vpart_df['Server'].head()}")
                #log_message(f"[DEBUG] Server column dtype: {vpart_df['Server'].dtype}")
                #log_message(f"[DEBUG] Null count in 'Server': {vpart_df['Server'].isnull().sum()}")
            else:
                log_message("[ERROR] 'Server' column not created after reformat_sds()")
                return

            # Normalize column names
            vpart_df.columns = vpart_df.columns.str.strip().str.replace(" ", "_").str.lower()

            cap_col = "capacity_mib"
            used_col = "consumed_mib"



            #log_message(f"[DEBUG] Normalized columns in vPartition: {list(vpart_df.columns)}")
            #log_message(f"[DEBUG] Aggregating with cap_col='{cap_col}', used_col='{used_col}'")

            if cap_col in vpart_df.columns and used_col in vpart_df.columns:
                vpart_df[cap_col] = pd.to_numeric(vpart_df[cap_col], errors="coerce")
                vpart_df[used_col] = pd.to_numeric(vpart_df[used_col], errors="coerce")
                vpart_df = vpart_df.dropna(subset=[cap_col, used_col])

                if vpart_df["server"].isnull().all():
                    log_message("[ERROR] All entries in 'server' are null. Cannot group.")
                    return

                vpart_grouped = vpart_df.groupby("server").agg({
                    cap_col: "sum",
                    used_col: "sum"
                }).reset_index()

                vpart_grouped["capacity_(gb)"] = vpart_grouped[cap_col] / 1024
                vpart_grouped["used_(gb)"] = vpart_grouped[used_col] / 1024
                vpart_grouped["free_(gb)"] = vpart_grouped["capacity_(gb)"] - vpart_grouped["used_(gb)"]
                vpart_grouped["%_used"] = (vpart_grouped["used_(gb)"] / vpart_grouped["capacity_(gb)"]) * 100
                vpart_grouped["%_free"] = 100 - vpart_grouped["%_used"]

                vpart_grouped.rename(columns={"server": "Server"}, inplace=True)
                vpartition_out = os.path.join(output_dir, f"{base}_vpartition_server_audit.csv")
                vpart_grouped.to_csv(vpartition_out, index=False)
                log_message(f"Saved: {vpartition_out}")
            else:
                log_message(f"Warning: Required vPartition columns not found. Available: {list(vpart_df.columns)}")

    except Exception as e:
        log_message(f"Error processing RVTools file: {e}")
