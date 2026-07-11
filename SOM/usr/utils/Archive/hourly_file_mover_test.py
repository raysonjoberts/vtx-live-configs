# ==========================================================
# Description:
#   Moves or copies files from source to destination folders
#   according to routing rules defined in file_mover.conf.
#   This script is designed to run on an *hourly* schedule.
#
# Configuration:
#   - C:\BTDM_7.1\usr\config\default\file_mover.conf
#       • Sections must be named "pattern: <filename_pattern>"
#       • Keys:
#           - interval: must be "hourly" for this script to act
#           - source_folder: folder where files originate
#           - destination_folder: folder where files will be sent
#           - action: "move" (default) or "copy"
#           - destination_file: optional, rename file on arrival
#           - append_date: "true" to append YYYYMMDD before extension
#
# Logging:
#   - Creates log file: C:\BTDM_7.1\var\logs\hourly_file_mover_<date>.log
#   - Logs all moves/copies, warnings, and errors with timestamps
#
# Functionality:
#   - Loads rules from file_mover.conf
#   - Filters sections where interval = hourly
#   - Matches filenames against the defined pattern (supports wildcards)
#   - If append_date is true, appends current date to destination filename
#   - Copies or moves the file to the destination folder
#   - Creates destination directories if missing
#
# Usage:
#   python <this_script>.py
#   (Typically scheduled hourly via Task Scheduler or cron)
# ==========================================================

import os
import shutil
import configparser
import fnmatch
import re
from datetime import datetime
import sys
from btdm_logging import get_logger

BTDM_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LOG_DIR = os.path.join(BTDM_ROOT, "var", "logs")
log_date = datetime.now().strftime("%Y%m%d")
LOG_FILE = os.path.join(LOG_DIR, f"hourly_file_mover_{log_date}.log")
CONFIG_DIR = os.path.join(BTDM_ROOT, "usr", "config", "default")

logger = get_logger("hourly_file_mover")
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

        interval = config[section].get("interval", "hourly").strip().lower()
        if interval != "hourly":
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
            logger.warning(f"Source folder does not exist: {source_folder}")
            continue

        for filename in os.listdir(source_folder):
            if fnmatch.fnmatch(filename, pattern):
                src_path = os.path.join(source_folder, filename)
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
                        logger.info(f"Copied: {filename} to {dest_path}")
                    else:
                        shutil.move(src_path, dest_path)
                        logger.info(f"Moved: {filename} to {dest_path}")
                except Exception as e:
                    logger.error(f"Failed to {action} {filename}: {e}")

if __name__ == "__main__":
    config = load_config(CONFIG_FILE)
    routing_rules = build_routing_table(config)
    process_files(routing_rules)

