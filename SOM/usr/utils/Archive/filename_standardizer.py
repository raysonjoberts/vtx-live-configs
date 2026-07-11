# ==========================================================
# Description:
#   Standardizes filenames in specified directories based on
#   configurable patterns defined in filename_standardize.conf.
#   All actions are logged to a daily log file.
#
# Configuration:
#   - C:\BTDM_7.1\usr\config\default\filename_standardize.conf
#       • Sections must be named "location: <directory_path>"
#       • Keys:
#           - pattern: file pattern to match (e.g., *.csv)
#           - output: new filename template
#                     use <extension> as placeholder for file’s extension
#
# Logging:
#   - Logs all rename operations, warnings, and errors to:
#       C:\BTDM_7.1\var\logs\filename_standardizer_<YYYYMMDD>.log
#
# Functionality:
#   - Reads configuration rules from filename_standardize.conf
#   - Scans directories listed in the config
#   - Identifies files matching the defined pattern
#   - Renames files according to the output template
#   - Preserves original extension via <extension> placeholder
#   - Logs all operations for auditing
#
# Usage:
#   python <this_script>.py
# ==========================================================


import os
import sys
import configparser
import fnmatch
import re
from pathlib import Path
from datetime import datetime

# Define BTDM_ROOT dynamically
BTDM_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CONFIG_DIR = os.path.join(BTDM_ROOT, "usr", "config", "default")
CONF_PATH = os.path.join(CONFIG_DIR, "filename_standardize.conf")
LOG_DIR = os.path.join(BTDM_ROOT, "var", "logs")
log_date = datetime.now().strftime("%Y%m%d")
LOG_FILE = os.path.join(LOG_DIR, f"filename_standardizer_{log_date}.log")
sys.stdout = open(LOG_FILE, "a")
sys.stderr = sys.stdout

def log_message(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)

def standardize_filenames():
    config = configparser.ConfigParser()
    config.read(CONF_PATH)
    log_message(f">>> Loaded filename standardize config from: {CONF_PATH}")

    for section in config.sections():
        match = re.match(r"^location[:\s]+(.*)$", section, re.IGNORECASE)
        if not match:
            continue
        directory = match.group(1).strip()

        pattern = config[section].get("pattern", "")
        output = config[section].get("output", "")

        if not pattern or not output:
            log_message(f"[WARN] Skipping section {section} due to missing pattern/output.")
            continue

        dir_path = Path(directory)
        if not dir_path.exists():
            log_message(f"[WARN] Directory does not exist: {dir_path}")
            continue

        for file in dir_path.iterdir():
            if fnmatch.fnmatch(file.name.lower(), pattern.lower()):
                new_name = output.replace("<extension>", file.suffix.lstrip("."))
                new_path = dir_path / new_name
                try:
                    os.rename(file, new_path)
                    log_message(f"[RENAME] {file.name} → {new_name}")
                except Exception as e:
                    log_message(f"[ERROR] Could not rename {file.name}: {e}")

if __name__ == '__main__':
    standardize_filenames()
