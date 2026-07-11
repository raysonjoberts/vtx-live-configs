# ==========================================================
# Description:
#   Cleans up old files from specified directories based on
#   retention rules defined in folder_cleanup.conf.
#   All actions are logged using the BTDM logging utility.
#
# Configuration:
#   - C:\BTDM_7.1\usr\config\default\folder_cleanup.conf
#       • Sections must be named "directory: <path>"
#       • Keys:
#           - file_age: number of days to retain files
#           - file_name: optional, comma-separated list of filename patterns (e.g., *.csv, *.log)
#           - file_type: optional, comma-separated list of file extensions (e.g., csv, log)
#
# Logging:
#   - Uses btdm_logging.get_logger("folder_cleaner")
#   - Logs info, warnings, and errors to BTDM’s centralized log system
#
# Functionality:
#   - Reads cleanup rules from folder_cleanup.conf
#   - Iterates over each configured directory
#   - Identifies files older than the specified age (in days)
#   - Filters by filename patterns and/or file types if provided
#   - Deletes matching files and logs results
#   - Skips invalid directories and logs warnings
#
# Usage:
#   python <this_script>.py
# ==========================================================


import os
import glob
import time
import sys
import configparser

BTDM_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CONFIG_DIR = os.path.join(BTDM_ROOT, "usr", "config", "default")

# Add utils to sys.path using BTDM_ROOT
UTILS_PATH = os.path.join(BTDM_ROOT, "usr", "utils")
sys.path.append(UTILS_PATH)

# Import logger
from btdm_logging import get_logger

logger = get_logger("folder_cleaner")

# === CONFIGURATION ===
CONFIG_PATH = os.path.join(CONFIG_DIR, "folder_cleanup.conf")

def parse_config(path):
    config = configparser.ConfigParser()
    config.optionxform = str  # preserve case
    config.read(path)
    jobs = []
    for section in config.sections():
        if section.startswith("directory:"):
            folder = section.split("directory:")[1].strip()
            file_age = int(config[section].get("file_age", 0))
            name_patterns = config[section].get("file_name", "").split(",") if "file_name" in config[section] else []
            name_patterns = [p.strip() for p in name_patterns if p.strip()]
            file_types = config[section].get("file_type", "").split(",") if "file_type" in config[section] else []
            file_types = [f".{ft.strip().lower()}" for ft in file_types if ft.strip()]
            jobs.append({
                "folder": folder,
                "file_age": file_age,
                "name_patterns": name_patterns,
                "file_types": file_types,
            })
    return jobs

def delete_old_files(folder, file_age, name_patterns, file_types):
    now = time.time()
    cutoff = now - (file_age * 86400)
    deleted = []

    if not os.path.isdir(folder):
        logger.warning(f"Skipped invalid folder: {folder}")
        return deleted

    # Gather files to evaluate
    if name_patterns:
        files = []
        for pattern in name_patterns:
            files.extend(glob.glob(os.path.join(folder, pattern)))
    else:
        files = [os.path.join(folder, f) for f in os.listdir(folder)]

    for file_path in files:
        if not os.path.isfile(file_path):
            continue

        if file_types:
            _, ext = os.path.splitext(file_path)
            if ext.lower() not in file_types:
                continue

        file_mtime = os.path.getmtime(file_path)
        if file_mtime < cutoff:
            try:
                os.remove(file_path)
                logger.info(f"Deleted: {file_path}")
                deleted.append(file_path)
            except Exception as e:
                logger.error(f"Error deleting {file_path}: {e}")

    return deleted

def main():
    jobs = parse_config(CONFIG_PATH)
    logger.info("Started folder cleanup process")
    for job in jobs:
        logger.info(f"Cleaning folder: {job['folder']} (Age > {job['file_age']} days)")
        deleted = delete_old_files(**job)
        if not deleted:
            logger.info("No files deleted.")
    logger.info("Folder cleanup process complete")

if __name__ == "__main__":
    main()
