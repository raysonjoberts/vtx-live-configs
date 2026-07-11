
import os
import shutil
import configparser
import fnmatch
from datetime import datetime
import sys

BTDM_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LOG_DIR = os.path.join(BTDM_ROOT, "var", "logs")
log_date = datetime.now().strftime("%Y%m%d")
LOG_FILE = os.path.join(LOG_DIR, f"daily_csv_mover_{log_date}.log")
sys.stdout = open(LOG_FILE, "a")
sys.stderr = sys.stdout
CONFIG_DIR = os.path.join(BTDM_ROOT, "usr", "config", "default")


def log_message(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)

# === CONFIGURATION ===
CONFIG_FILE = r"C:\BTDM\config\download_router.conf"

def load_config(config_path):
    config = configparser.ConfigParser()
    config.read(os.path.join(CONFIG_DIR, "daily_csv_transfers.conf"))
    return config

def build_routing_table(config):
    """
    Returns a list of (pattern, source_folder, destination_folder, action)
    """
    rules = []
    for pattern in config.sections():
        source = config[pattern].get("source_folder", "").strip()
        destination = config[pattern].get("destination_folder", "").strip()
        action = config[pattern].get("action", "move").strip().lower()
        if source and destination:
            rules.append((pattern, source, destination, action))
    return rules

def process_files(rules):
    for pattern, source_folder, destination_folder, action in rules:
        if not os.path.isdir(source_folder):
            log_message(f"[WARN] Source folder does not exist: {source_folder}")
            continue

        for filename in os.listdir(source_folder):
            if fnmatch.fnmatch(filename, pattern):
                src_path = os.path.join(source_folder, filename)
                dest_path = os.path.join(destination_folder, filename)
                os.makedirs(destination_folder, exist_ok=True)

                try:
                    if action == "copy":
                        shutil.copy2(src_path, dest_path)
                        log_message(f"Copied: {filename} to {destination_folder}")
                    else:
                        shutil.move(src_path, dest_path)
                        log_message(f"Moved: {filename} to {destination_folder}")
                except Exception as e:
                    log_message(f"[ERROR] Failed to {action} {filename}: {e}")

if __name__ == "__main__":
    config = load_config(CONFIG_FILE)
    routing_rules = build_routing_table(config)
    process_files(routing_rules)
