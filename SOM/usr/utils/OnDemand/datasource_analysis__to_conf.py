# ==========================================================
# Description:
#   Converts the "Source_Analysis" sheet from AnalysisConfig.xlsx
#   into a structured YAML file for data source analysis.
#
# Input:
#   - C:\BTDM_7.1\bin\ui\AnalysisConfig.xlsx
#       Sheet: "Source_Analysis"
#       Expected columns include:
#         • attribute
#         • required
#         • description
#         • field_name_patterns (comma‑separated)
#         • heuristic_types
#         • patterns
#         • heuristic_pattern_match_explanation
#         • match_requirements (optional, comma‑separated)
#         • thresholds (optional numeric values):
#             min_viable_ratio, min_unique_ratio, max_unique_ratio,
#             min_match_ratio, min_length, max_length
#
# Output:
#   - C:\BTDM_7.1\usr\config\default\data_source_analysis_v2.yaml
#       Example block:
#         - attribute: Application ID
#           required: true
#           description: Unique identifier for applications
#           field_name_patterns: ["app_id", "applicationid"]
#           value_heuristics:
#             - type: string
#               pattern: ".*\\d{3,}.*"
#               explanation: Contains a 3+ digit number
#               min_length: 3
#               max_length: 10
#           match_requirements: ["name", "value"]
#
# Processing Rules:
#   - Splits field_name_patterns and match_requirements into lists.
#   - Wraps heuristics into value_heuristics[].
#   - Adds thresholds if provided (converted to float).
#   - Preserves attribute order (sort_keys=False in yaml.dump).
#
# Logging:
#   - Prints completion notice with full YAML path.
#
# Usage:
#   python analysisconfig_to_yaml.py
#   (Run whenever Source_Analysis sheet is updated.)
# ==========================================================


import os
import pandas as pd
import yaml

# Determine BTDM_ROOT from this script's location
BTDM_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# Define paths
xlsx_path = os.path.join(BTDM_ROOT, "bin", "ui", "AnalysisConfig.xlsx")
yaml_path = os.path.join(BTDM_ROOT, "usr", "config", "default", "data_source_analysis_v2.yaml")

def row_to_attribute(row):
    attribute_block = {
        "attribute": row["attribute"],
        "required": bool(row["required"]),
        "description": row["description"],
        "field_name_patterns": [p.strip() for p in str(row["field_name_patterns"]).split(",") if p.strip()],
        "value_heuristics": [{
            "type": row["heuristic_types"],
            "pattern": row["patterns"],
            "explanation": row["heuristic_pattern_match_explanation"]
        }],
    }

    # Add any optional thresholds if they exist
    optional_keys = [
        "min_viable_ratio", "min_unique_ratio", "max_unique_ratio",
        "min_match_ratio", "min_length", "max_length"
    ]
    for key in optional_keys:
        if pd.notnull(row[key]):
            attribute_block["value_heuristics"][0][key] = float(row[key])

    # Add match requirements if present
    if pd.notnull(row.get("match_requirements", None)):
        attribute_block["match_requirements"] = [m.strip() for m in row["match_requirements"].split(",")]

    return attribute_block

def main():
    df = pd.read_excel(xlsx_path, sheet_name="Source_Analysis")
    attributes = [row_to_attribute(row) for _, row in df.iterrows()]

    with open(yaml_path, "w") as f:
        yaml.dump(attributes, f, sort_keys=False)

    print(f"YAML written to: {yaml_path}")

if __name__ == "__main__":
    main()
