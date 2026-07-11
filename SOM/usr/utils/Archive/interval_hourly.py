#!/usr/bin/env python3
"""
BTDM Interval Launcher
- Launch each script independently (detached)
- Sleep between launches
- BTDM_ROOT-aware (relative paths work across Windows/mac/Linux)
"""

import os, sys, time, shlex, subprocess, datetime, platform, pathlib

# --- Define BTDM_ROOT relative to this file ---
BTDM_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# --- Interval Settings ---
SLEEP_BETWEEN_LAUNCHES = 2  # seconds between launching scripts

# --- List of scripts to launch ---
# Use os.path.join(BTDM_ROOT, "relative", "path", "to", "script.py")
COMMANDS = [
    os.path.join(BTDM_ROOT, "usr", "utils", "hourly_file_mover.py"),
]
# --------------------------------


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def is_windows():
    return platform.system().lower().startswith("win")

def detach_popen(cmd_path):
    """Launch a script detached from this launcher."""
    try:
        if is_windows():
            args = [sys.executable, cmd_path]
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NO_WINDOW
            )
            with open(os.devnull, "w") as devnull:
                p = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=devnull,
                    stderr=devnull,
                    creationflags=creationflags,
                    close_fds=True,
                )
            return p, None
        else:
            args = [sys.executable, cmd_path]
            with open(os.devnull, "w") as devnull:
                p = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=devnull,
                    stderr=devnull,
                    start_new_session=True,
                    close_fds=True,
                )
            return p, None
    except Exception as e:
        return None, str(e)

def main():
    for idx, cmd in enumerate(COMMANDS, start=1):
        p, err = detach_popen(cmd)
        if err:
            print(f"[{now()}] LAUNCH-ERROR {idx}/{len(COMMANDS)} :: {cmd} :: {err}")
        else:
            print(f"[{now()}] LAUNCHED     {idx}/{len(COMMANDS)} :: {cmd} :: pid={p.pid}")
        if idx < len(COMMANDS):
            time.sleep(SLEEP_BETWEEN_LAUNCHES)

if __name__ == "__main__":
    sys.exit(main())
