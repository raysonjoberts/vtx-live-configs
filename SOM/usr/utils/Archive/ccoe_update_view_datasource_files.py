# ==========================================================
# Description:
#   Copies specific consolidated CSV files from the BTDM
#   tables directory into the CCoE Inventory development
#   folder for external use.
#
# Files Moved:
#   - C:\BTDM_7.1\var\tables\consolidated_application_view.csv
#       -> C:\Users\Jayson.Roberts\State of Maine\DAFS-MaineIT CCOE - Data Dog\Correlated_Data\CCoE_Inventory\dev\consolidated_application_view.csv
#
#   - C:\BTDM_7.1\var\tables\consolidated_server_view.csv
#       -> C:\Users\Jayson.Roberts\State of Maine\DAFS-MaineIT CCOE - Data Dog\Correlated_Data\CCoE_Inventory\dev\consolidated_server_view.csv
#
# Functionality:
#   - Ensures destination folders exist
#   - Copies files, overwriting existing ones if present
#   - Logs results to console as [INFO] or [ERROR]
#
# Usage:
#   python <this_script>.py
# ==========================================================


import shutil
import os

# === Define source and destination paths here ===
FILES_TO_MOVE = [
    {
        "source": r"C:\BTDM_7.1\var\tables\consolidated_application_view.csv",
        "destination": r"C:\Users\Jayson.Roberts\State of Maine\DAFS-MaineIT CCOE - Data Dog\Correlated_Data\CCoE_Inventory\dev\consolidated_application_view.csv"
    },
    {
        "source": r"C:\BTDM_7.1\var\tables\consolidated_server_view.csv",
        "destination": r"C:\Users\Jayson.Roberts\State of Maine\DAFS-MaineIT CCOE - Data Dog\Correlated_Data\CCoE_Inventory\dev\consolidated_server_view.csv"
    }
]

# === Core move logic ===
def move_file(source, destination):
    try:
        # Ensure destination folder exists
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        
        # Move (and overwrite if exists)
        shutil.copy2(source, destination)
        print(f"[INFO] Moved '{source}' -> '{destination}'")
    except Exception as e:
        print(f"[ERROR] Failed to move '{source}' -> '{destination}': {e}")

if __name__ == "__main__":
    for file_pair in FILES_TO_MOVE:
        move_file(file_pair["source"], file_pair["destination"])
