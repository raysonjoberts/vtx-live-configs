
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
    Returns a list of (pattern, source_folder, destination_folder, action, destination_file, append_date)
    """
    rules = []
    for pattern in config.sections():
        source = config[pattern].get("source_folder", "").strip()
        destination = config[pattern].get("destination_folder", "").strip()
        action = config[pattern].get("action", "move").strip().lower()
        dest_file = config[pattern].get("destination_file", "").strip()
        append_date = config[pattern].get("append_date", "false").strip().lower() == "true"
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
