# ==========================================================
#
# Description:
#   This script generates a consolidated server view for
#   BTDM by reading the consolidated_server_table.csv file,
#   collapsing duplicate records, and applying transformation
#   rules defined in field_rules.conf.
#
# Functionality:
#   - Reads input file: consolidated_server_table.csv
#   - Groups records by Server and RECORDID
#   - Collapses field values into unique, sorted lists
#   - Applies conditional rules from field_rules.conf
#   - Removes fields flagged in field_rules.conf
#   - Writes output file: consolidated_server_view.csv
#   - Logs results, warnings, and errors to table_aggregator.log
#
# Usage:
#   python table_aggregator.py
#
# Inputs:
#   - BTDM_ROOT/var/tables/consolidated_server_table.csv
#   - BTDM_ROOT/usr/config/default/field_rules.conf
#
# Output:
#   - BTDM_ROOT/var/tables/consolidated_server_view.csv
#
# Author: BTDM Development Team
# ==========================================================


import os
import pandas as pd
import configparser
import re
from datetime import datetime

BTDM_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TABLES_DIR = os.path.join(BTDM_ROOT, "var", "tables")
LOG_FILE = os.path.join(BTDM_ROOT, "var", "logs", "table_aggregator.log")
RULES_CONF = os.path.join(BTDM_ROOT, "usr", "config", "default", "field_rules.conf")

INPUT_FILE = os.path.join(TABLES_DIR, "consolidated_server_table.csv")
OUTPUT_FILE = os.path.join(TABLES_DIR, "consolidated_server_view.csv")

SCRIPT_NAME = os.path.splitext(os.path.basename(__file__))[0]  # e.g., 'ccoe_server_view'

def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"{timestamp},server_view,{msg}\n")

def apply_rules(df, rules_path):
    config = configparser.ConfigParser()
    config.read(rules_path)

    for section in config.sections():
        if not section.startswith(f"{SCRIPT_NAME}:"):
            continue

        _, new_field = section.split(":", 1)
        if new_field == "remove_fields":
            continue

        if new_field not in df.columns:
            df[new_field] = ""  # Create the field if it doesn't exist

        unmatched_mask = df[new_field] == ""

        for key in sorted(config[section]):  # process rules in order
            rule = config[section][key]
            match = re.match(rf'if (.+?) then {new_field} = (.+)', rule)
            if match:
                condition, value = match.groups()
                try:
                    if "CONTAINS" in condition:
                        cond_field, cond_value = re.split(r"\s+CONTAINS\s+", condition)
                        cond_field = cond_field.strip()
                        cond_value = cond_value.strip().lower()
                        mask = df[cond_field].astype(str).str.lower().str.contains(cond_value, na=False)
                    elif "NOT CONTAINS" in condition:
                        cond_field, cond_value = re.split(r"\s+NOT CONTAINS\s+", condition)
                        cond_field = cond_field.strip()
                        cond_value = cond_value.strip().lower()
                        mask = ~df[cond_field].astype(str).str.lower().str.contains(cond_value, na=False)
                    elif "MATCHES" in condition:
                        cond_field, pattern = re.split(r"\s+MATCHES\s+", condition)
                        cond_field = cond_field.strip()
                        pattern = pattern.strip().replace("*", ".*")
                        regex = f"(?i)^{pattern}$"
                        mask = df[cond_field].astype(str).str.match(regex, na=False)
                    elif "NOT MATCHES" in condition:
                        cond_field, pattern = re.split(r"\s+NOT MATCHES\s+", condition)
                        cond_field = cond_field.strip()
                        pattern = pattern.strip().replace("*", ".*")
                        regex = f"(?i)^{pattern}$"
                        mask = ~df[cond_field].astype(str).str.match(regex, na=False)
                    else:
                        for col in df.columns:
                            if re.search(rf'\b{col}\b', condition):
                                condition = re.sub(rf'\b{col}\b', f'df["{col}"]', condition)
                        mask = eval(condition)

                    final_mask = mask & unmatched_mask
                    df.loc[final_mask, new_field] = value.strip()
                    unmatched_mask = unmatched_mask & ~final_mask

                except Exception as e:
                    log(f"[WARN] Failed to apply rule '{rule}': {e}")
    return df

def get_fields_to_remove(config_path, script_name):
    config = configparser.ConfigParser()
    config.read(config_path)

    section = f"{script_name}:remove_fields"
    if config.has_section(section) and config.has_option(section, "fields"):
        return [f.strip() for f in config.get(section, "fields").split(",")]
    return []

def main():
    try:
        df = pd.read_csv(INPUT_FILE, low_memory=False)

        group_keys = ["Server", "RECORDID"]
        collapse_fields = [col for col in df.columns if col not in group_keys]

        df = df.fillna("")
        df[collapse_fields] = df[collapse_fields].astype(str)

        grouped = df.groupby(group_keys, dropna=False).agg(lambda x: '\n'.join(sorted(set(v for v in x if v))))
        grouped.reset_index(inplace=True)

        grouped = apply_rules(grouped, RULES_CONF)

        fields_to_remove = get_fields_to_remove(RULES_CONF, SCRIPT_NAME)
        grouped.drop(columns=[col for col in fields_to_remove if col in grouped.columns], inplace=True)

        grouped.to_csv(OUTPUT_FILE, index=False)
        log(f"[SUCCESS] Created consolidated view with {len(grouped)} rows")

    except Exception as e:
        log(f"[ERROR] View generation failed: {e}")

if __name__ == "__main__":
    main()