import os
import configparser
import pandas as pd
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BTDM_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", ".."))
TABLES_DIR = os.path.join(BTDM_ROOT, "var", "tables")
CONFIG_PATH = os.path.join(BTDM_ROOT, "usr", "config", "default", "aggregations.conf")
REPORT_DIR = os.path.join(BTDM_ROOT, "var", "lib", "reporting")
REPORT_PATH = os.path.join(REPORT_DIR, "field_sources_report.csv")

os.makedirs(REPORT_DIR, exist_ok=True)

def parse_config():
    config = configparser.RawConfigParser()
    config.read(CONFIG_PATH)
    categorized = {"source": [], "additional": [], "audit": []}

    for section in config.sections():
        if section.startswith("aggregate:"):
            path = section.split("aggregate:", 1)[1].strip()
            meta = {
                k: v.split("#", 1)[0].strip()
                for k, v in config.items(section)
            }
            entry = {
                "path": path,
                "type": meta.get("type", ""),
                "key": meta.get("key", "application"),
                "fields": [f.strip() for f in meta.get("lookup_fields", "").split(",") if f.strip()]
            }
            if meta.get("is_source_data", "false").lower() == "true":
                categorized["source"].append(entry)
            elif meta.get("is_additional_data", "false").lower() == "true":
                categorized["additional"].append(entry)
            elif meta.get("is_audit_data", "false").lower() == "true":
                categorized["audit"].append(entry)
    return categorized

def load_master_fields(file_path):
    if os.path.exists(file_path):
        try:
            df = pd.read_csv(file_path, dtype=str)
            return set(df.columns)
        except Exception as e:
            print(f"[WARN] Failed to read master file {file_path}: {e}")
    return set()

def clean_data_source(filename):
    base = os.path.basename(filename)
    if base.lower().endswith(".csv"):
        base = base[:-4]
    base = base.replace("_ads", "").replace("_sds", "")
    return base

def main():
    conf = parse_config()
    report_rows = []

    for data_type, key, table_path in [
        ("SDS", "Server", os.path.join(TABLES_DIR, "consolidated_server_table.csv")),
        ("ADS", "Application", os.path.join(TABLES_DIR, "consolidated_application_table.csv"))
    ]:
        inventory_name = os.path.basename(table_path).replace(".csv", "")
        master_fields = load_master_fields(table_path)

        for entry in conf["source"] + conf["additional"]:
            if entry["type"].upper() != data_type:
                continue
            if not os.path.exists(entry["path"]):
                continue
            try:
                df = pd.read_csv(entry["path"], dtype=str)
                is_source = entry in conf["source"]
                for field in master_fields:
                    if field in df.columns or (not is_source and field in entry["fields"]):
                        report_rows.append({
                            "Data Field": field,
                            "Data Source": clean_data_source(entry["path"]),
                            "Type": "source" if is_source else "additional",
                            "Inventory Source": inventory_name
                        })
            except Exception as e:
                print(f"[WARN] Could not read {entry['path']}: {e}")

    report_df = pd.DataFrame(report_rows).drop_duplicates()
    report_df.to_csv(REPORT_PATH, index=False)
    print(f"[INFO] Report saved to: {REPORT_PATH}")

if __name__ == "__main__":
    main()
