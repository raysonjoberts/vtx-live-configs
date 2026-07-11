# ==========================================================
#
# Description:
#   This script exports specific sheets from the CCoE
#   inventory Excel workbook into CSV files for BTDM.
#   A temporary copy of the Excel file is created to
#   avoid issues with the source file being open or locked.
#
# Functionality:
#   - Reads from SOURCE_FILE (Excel workbook).
#   - Exports defined sheets to DESTINATION_FOLDER.
#   - Saves as CSV with predefined filenames.
#   - Logs success, warnings for missing sheets, and errors.
#   - Cleans up temporary copy after execution.
#
# Usage:
#   python export_excel_sheets.py
#
# Output:
#   - CCoE_consolidated_application_view.csv
#   - CCoE_consolidated_server_view.csv
#
# Author: BTDM Development Team
# ==========================================================


import os
import shutil
import tempfile
import pandas as pd

# === CONFIGURATION ===
SOURCE_FILE = r"C:\Users\Jayson.Roberts\State of Maine\DAFS-MaineIT CCOE - Data Dog\Correlated_Data\CCoE_Inventory\dev\CCOE_View.xlsx"
DESTINATION_FOLDER = r"C:\BTDM_7.1\var\customerdata"

# Create destination folder if it doesn't exist
os.makedirs(DESTINATION_FOLDER, exist_ok=True)

# === SHEETS TO EXPORT ===
sheets_to_export = {
    "consolidated_application_view": "CCoE_consolidated_application_view.csv",
    "consolidated_server_view": "CCoE_consolidated_server_view.csv"
}

def export_from_temp_copy(source_file, sheet_mapping, destination_folder):
    try:
        # Make a temporary copy of the source file to bypass any Excel lock
        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
            shutil.copy2(source_file, tmp.name)
            temp_file = tmp.name

        # Load the Excel file from the temporary location
        xl = pd.ExcelFile(temp_file)

        for sheet_name, output_filename in sheet_mapping.items():
            if sheet_name not in xl.sheet_names:
                print(f"[WARN] Sheet '{sheet_name}' not found in the Excel file. Skipping.")
                continue

            df = xl.parse(sheet_name)
            output_path = os.path.join(destination_folder, output_filename)
            df.to_csv(output_path, index=False)
            print(f"[SUCCESS] Saved: {output_path}")

    except Exception as e:
        print(f"[ERROR] {e}")

    finally:
        if os.path.exists(temp_file):
            os.remove(temp_file)

if __name__ == "__main__":
    export_from_temp_copy(SOURCE_FILE, sheets_to_export, DESTINATION_FOLDER)
