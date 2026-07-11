# ==========================================================
# Description:
#   Automates daily file movement or copying based on
#   configurable patterns defined in file_mover.conf.
#   All log output is written to a daily log file.
#
# Configuration:
#   - C:\BTDM_7.1\usr\config\default\file_mover.conf
#       • Sections must be named "pattern: <filename_pattern>"
#       • Keys:
#           - source_folder: where to look for files
#           - destination_folder: where to move/copy files
#           - action: "move" (default) or "copy"
#           - destination_file: optional, rename file on arrival
#           - append_date: true/false, append YYYYMMDD to filename
#           - interval: only "daily" is processed by this script
#
# Logging:
#   - Logs all actions to:
#       C:\BTDM_7.1\var\logs\daily_file_mover_<YYYYMMDD>.log
#
# Functionality:
#   - Reads routing rules from file_mover.conf
#   - Matches files in source folders against patterns
#   - Moves or copies matching files to destinations
#   - Renames files if destination_file is set
#   - Optionally appends current date to filenames
#   - Logs all successes, warnings, and errors
#
# Usage:
#   python <this_script>.py
# ==========================================================


import os
import shutil
import configparser
import fnmatch
import re
from datetime import datetime
import sys

BTDM_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LOG_DIR = os.path.join(BTDM_ROOT, "var", "logs")
log_date = datetime.now().strftime("%Y%m%d")
LOG_FILE = os.path.join(LOG_DIR, f"daily_file_mover_{log_date}.log")
sys.stdout = open(LOG_FILE, "a")
sys.stderr = sys.stdout
CONFIG_DIR = os.path.join(BTDM_ROOT, "usr", "config", "default")

def log_message(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)

# === CONFIGURATION ===
CONFIG_FILE = os.path.join(CONFIG_DIR, "file_mover.conf")

def load_config(config_path):
    config = configparser.ConfigParser()
    config.read(config_path)
    return config

def build_routing_table(config):
    """
    Returns a list of (pattern, source_folder, destination_folder, action, destination_file, append_date)
    """
    rules = []
    for section in config.sections():
        match = re.match(r"^pattern[:\s]+(.*)$", section, re.IGNORECASE)
        if not match:
            continue
        pattern = match.group(1).strip()

        interval = config[section].get("interval", "daily").strip().lower()
        if interval != "daily":  # Only include daily entries
            continue
        source = config[section].get("source_folder", "").strip()
        destination = config[section].get("destination_folder", "").strip()
        action = config[section].get("action", "move").strip().lower()
        dest_file = config[section].get("destination_file", "").strip()
        append_date = config[section].get("append_date", "false").strip().lower() == "true"
        if source and destination:
            rules.append((pattern, source, destination, action, dest_file, append_date))
    return rules

def process_files(rules):
    for pattern, source_folder, destination_folder, action, dest_file, append_date in rules:
        if not os.path.isdir(source_folder):
            log_message(f"[WARN] Source folder does not exist: {source_folder}")
            continue

        for filename in os.listdir(source_folder):
            if fnmatch.fnmatch(filename, pattern):
                src_path = os.path.join(source_folder, filename)

                # Determine destination filename
                base_name = dest_file if dest_file else filename
                if append_date:
                    name, ext = os.path.splitext(base_name)
                    date_str = datetime.now().strftime("%Y%m%d")
                    base_name = f"{name}_{date_str}{ext}"

                dest_path = os.path.join(destination_folder, base_name)
                os.makedirs(destination_folder, exist_ok=True)

                try:
                    if action == "copy":
                        shutil.copy2(src_path, dest_path)
                        log_message(f"Copied: {filename} to {dest_path}")
                    else:
                        shutil.move(src_path, dest_path)
                        log_message(f"Moved: {filename} to {dest_path}")
                except Exception as e:
                    log_message(f"[ERROR] Failed to {action} {filename}: {e}")

if __name__ == "__main__":
    config = load_config(CONFIG_FILE)
    routing_rules = build_routing_table(config)
    process_files(routing_rules)
