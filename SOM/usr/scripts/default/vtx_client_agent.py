# File: usr/scripts/default/vtx_client_agent.py
# Purpose: VTX client-side sync agent (Phase 1)
# - PUSH: client var/customerdata -> server var/customerdata (local path copy in Phase 1)
# - PULL DIRS: server var/tables & var/reporting -> client mirrors (one-way, latest-wins)
# - PULL FILES: specific files like usr/config/default/scheduler.yaml -> client
# Notes:
# - No interaction with scheduler.py.
# - Poll interval controlled by vtx_client.ini [client] poll_seconds.
# - Safe to run alongside scheduler.py --role client.

from __future__ import annotations
import os
import time
import shutil
import hashlib
import threading
import configparser
from pathlib import Path

# --------------------------
# Environment & configuration
# --------------------------
BTDM_ROOT = os.environ.get("BTDM_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
CFG_PATH  = os.path.join(BTDM_ROOT, "usr", "config", "default", "vtx_client.ini")


def load_cfg() -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    if not os.path.isfile(CFG_PATH):
        raise FileNotFoundError(f"Client config not found: {CFG_PATH}")
    # Read as UTF-8 (handle optional BOM)
    with open(CFG_PATH, "r", encoding="utf-8-sig") as f:
        cp.read_file(f)
    return cp



# -------------
# File utilities
# -------------

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_if_changed(src: Path, dst: Path) -> bool:
    """Copy src -> dst if content or timestamp differs. Return True if copied/updated."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and src.stat().st_size == dst.stat().st_size and int(src.stat().st_mtime) == int(dst.stat().st_mtime):
        return False
    if dst.exists() and src.stat().st_size == dst.stat().st_size:
        if sha256(src) == sha256(dst):
            # align timestamps for future quick checks
            os.utime(dst, (src.stat().st_atime, src.stat().st_mtime))
            return False
    tmp = dst.with_suffix(dst.suffix + ".part")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)  # atomic
    return True


# ------------------
# Sync loop routines
# ------------------

def push_customerdata_loop(cp: configparser.ConfigParser, stop_evt: threading.Event):
    client_cd = Path(cp.get("paths", "client_customerdata"))
    server_cd = Path(cp.get("paths", "server_customerdata"))
    delete_after = cp.getboolean("client", "push_delete_after_upload", fallback=True)
    poll = cp.getint("client", "poll_seconds", fallback=15)

    client_cd.mkdir(parents=True, exist_ok=True)
    server_cd.mkdir(parents=True, exist_ok=True)

    while not stop_evt.is_set():
        try:
            for p in list(client_cd.glob("*")):
                if p.is_file():
                    dst = server_cd / p.name
                    try:
                        tmp = dst.with_suffix(dst.suffix + ".part")
                        shutil.copy2(p, tmp)
                        os.replace(tmp, dst)  # atomic publish on server
                        if delete_after:
                            p.unlink(missing_ok=True)
                        print(f"[push] {p.name} -> server")
                    except Exception as e:
                        print(f"[push] failed for {p}: {e}")
        except Exception as e:
            print(f"[push] loop error: {e}")
        stop_evt.wait(poll)


def pull_dirs_loop(cp: configparser.ConfigParser, stop_evt: threading.Event):
    server_root = Path(cp.get("client", "server_root"))
    client_root = Path(cp.get("paths", "client_root"))
    pull_dirs = [d.strip().replace("/", os.sep) for d in cp.get("sync", "pull_dirs").split(",") if d.strip()]
    poll = cp.getint("client", "poll_seconds", fallback=15)

    while not stop_evt.is_set():
        try:
            for rel in pull_dirs:
                sdir = server_root / rel
                cdir = client_root / rel
                if not sdir.exists():
                    continue
                for spath in sdir.rglob("*"):
                    if spath.is_file():
                        relp = spath.relative_to(sdir)
                        dpath = cdir / relp
                        try:
                            if copy_if_changed(spath, dpath):
                                print(f"[pull] {rel}{os.sep}{relp} updated")
                        except Exception as e:
                            print(f"[pull] failed {rel}{os.sep}{relp}: {e}")
        except Exception as e:
            print(f"[pull] loop error: {e}")
        stop_evt.wait(poll)


def pull_files_loop(cp: configparser.ConfigParser, stop_evt: threading.Event):
    """Pull specific files (e.g., scheduler.yaml) from server to client."""
    server_root = Path(cp.get("client", "server_root"))
    client_root = Path(cp.get("paths", "client_root"))
    raw = cp.get("sync", "pull_files", fallback="").strip()
    if not raw:
        return  # nothing to do
    files = [f.strip().replace("/", os.sep) for f in raw.split(",") if f.strip()]
    poll = cp.getint("client", "poll_seconds", fallback=15)

    while not stop_evt.is_set():
        try:
            for rel in files:
                spath = server_root / rel
                dpath = client_root / rel
                if spath.exists() and spath.is_file():
                    try:
                        if copy_if_changed(spath, dpath):
                            print(f"[pull-file] {rel} updated")
                    except Exception as e:
                        print(f"[pull-file] failed {rel}: {e}")
        except Exception as e:
            print(f"[pull-file] loop error: {e}")
        stop_evt.wait(poll)


# ---------
# Entrypoint
# ---------

def main():
    cp = load_cfg()
    stop_evt = threading.Event()
    try:
        t_push = threading.Thread(target=push_customerdata_loop, args=(cp, stop_evt), daemon=True)
        t_pull_dirs = threading.Thread(target=pull_dirs_loop,  args=(cp, stop_evt), daemon=True)
        t_pull_files = threading.Thread(target=pull_files_loop, args=(cp, stop_evt), daemon=True)
        t_push.start(); t_pull_dirs.start(); t_pull_files.start()
        print("VTX Client Agent (sync-only) running. Ctrl+C to stop.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        stop_evt.set()


if __name__ == "__main__":
    main()
