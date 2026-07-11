# ==========================================================
# Description:
#   Converts config_table.xlsx (Excel UI file) into individual
#   .conf files (INI-style). Each sheet in the workbook becomes
#   a separate configuration file, with rows converted into
#   stanzas and key=value pairs.
#
# Input:
#   - C:\BTDM_7.1\bin\ui\config_table.xlsx
#       Each sheet represents one configuration file.
#       Rules:
#         • First column → stanza type (renamed internally to "stanza_value")
#         • Remaining columns → key=value pairs
#
# Output:
#   - C:\BTDM_7.1\usr\config\default\<sheet>.conf
#       Example:
#         [pattern: wsid_001.csv]
#         source_folder = C:\BTDM_7.1\downloads
#         destination_folder = C:\BTDM_7.1\customerdata
#         action = move
#         append_date = true
#
# Processing Rules:
#   - Skips empty DataFrames/sheets.
#   - Drops rows that are completely empty.
#   - Normalizes boolean values for append_date:
#         "1", "1.0", "true" → true
#         "0", "0.0", "false" → false
#         any other → blank
#   - First column value becomes part of the stanza header:
#         e.g., [<first_col_name>: <row_value>]
#
# Logging:
#   - Writes daily logs to:
#         C:\BTDM_7.1\var\logs\write_conf_from_excel_<YYYYMMDD>.log
#   - Logs start, per‑file generation, and errors.
#
# Usage:
#   python write_conf_from_excel.py
#   (Run manually, or integrated into automated config refresh)
# ==========================================================


import pandas as pd
from configparser import ConfigParser
import json
import yaml
import os
import sys
from datetime import datetime

# Setup BTDM-style logging
BTDM_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
LOG_DIR = os.path.join(BTDM_ROOT, "var", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
log_date = datetime.now().strftime("%Y%m%d")
#LOG_FILE = os.path.join(LOG_DIR, f"write_conf_from_excel_{log_date}.log")
#sys.stdout = open(LOG_FILE, "a")
sys.stderr = sys.stdout

# ------------------------------------------------------------
# Logging (BTDM/VTX style)
# ------------------------------------------------------------
try:
    sys.path.append(os.path.join(BTDM_ROOT, "usr", "lib"))
    import btdm_logging  # type: ignore

    logger = btdm_logging.get_logger(component="config_tables_to_conf")
except Exception:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    logger = logging.getLogger("config_tables_to_conf")

def generate_conf_from_excel(df, output_path):
    try:
        if df.empty:
            logger.info(f"Skipped empty DataFrame for {output_path}")
            return

        first_col = df.columns[0]
        df = df.rename(columns={first_col: "stanza_value"})
        stanza_type = first_col

        logger.info(f"Generating config file: {output_path} with stanza_type: {stanza_type}")

        with open(output_path, "w") as f:
            for _, row in df.iterrows():
                if not row.get("stanza_value"):
                    continue
                stanza = f"[{stanza_type}: {row['stanza_value']}]"
                f.write(stanza + "\n")
                for col, val in row.items():
                    if col == "stanza_value" or val == "":
                        continue

                    # Normalize boolean values for specific fields
                    if col == "append_date":
                        if str(val).strip().lower() in ["1", "1.0", "true"]:
                            val = "true"
                        elif str(val).strip().lower() in ["0", "0.0", "false"]:
                            val = "false"
                        else:
                            val = ""

                    f.write(f"{col} = {val}\n")
                f.write("\n")
        logger.info(f"Successfully wrote: {output_path}")
    except Exception as e:
        logger.error("Failed to generate {output_path}: {str(e)}")

def main():
    input_excel = os.path.join(BTDM_ROOT, "bin", "ui", "config_table.xlsx")
    logger.info(f"Starting config writer using {input_excel}")

    xls = pd.ExcelFile(input_excel, engine="openpyxl")
    for sheet in xls.sheet_names:
        df = xls.parse(sheet).dropna(how="all").fillna("")
        output_file = os.path.join(BTDM_ROOT, "usr", "config", "default", f"{sheet}.conf")
        generate_conf_from_excel(df, output_file)

if __name__ == "__main__":
    main()
