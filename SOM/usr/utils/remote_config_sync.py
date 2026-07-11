#!/usr/bin/env python3
"""
remote_config_sync.py

Mirror selected VTX files from the deployed VTX usr/ directory into a
customer-specific directory in the vtx-live-configs Git repository, then
commit and push any changes.

Only these file types are synchronized:
    .yaml
    .py
    .conf

Example:
    python usr/utils/remote_config_sync.py \
        --customer SOM \
        --repo-root /opt/repos/vtx-live-configs

Environment variables may be used instead:
    VTX_ROOT or BTDM_ROOT
    VTX_LIVE_CONFIGS_ROOT
    VTX_CUSTOMER_NAME
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Sequence

ALLOWED_EXTENSIONS = {".yaml", ".py", ".conf"}


def log(message: str) -> None:
    print(f"[remote_config_sync] {message}")


def error(message: str) -> None:
    print(f"[remote_config_sync] ERROR: {message}", file=sys.stderr)


def run_git(repo_root: Path, args: Sequence[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a Git command inside repo_root."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result
    except FileNotFoundError:
        error("git was not found on PATH.")
        raise SystemExit(1)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() or str(exc)
        error(f"git {' '.join(args)} failed:\n{stderr}")
        raise SystemExit(exc.returncode or 1)


def get_vtx_root(cli_value: str | None) -> Path:
    """Resolve the deployed VTX root."""
    if cli_value:
        return Path(cli_value).expanduser().resolve()

    env_value = os.getenv("VTX_ROOT") or os.getenv("BTDM_ROOT")
    if env_value:
        return Path(env_value).expanduser().resolve()

    if platform.system().lower() == "windows":
        return Path(r"C:\BTDM_7.1")

    return Path("/opt/vtx")


def get_repo_root(cli_value: str | None) -> Path:
    """Resolve the local checkout of vtx-live-configs."""
    value = cli_value or os.getenv("VTX_LIVE_CONFIGS_ROOT")
    if not value:
        error(
            "Repository root is not configured. Use --repo-root or set "
            "VTX_LIVE_CONFIGS_ROOT."
        )
        raise SystemExit(2)

    return Path(value).expanduser().resolve()


def get_customer(cli_value: str | None) -> str:
    """Resolve and validate the customer directory name."""
    value = cli_value or os.getenv("VTX_CUSTOMER_NAME")
    if not value:
        error(
            "Customer name is not configured. Use --customer or set "
            "VTX_CUSTOMER_NAME."
        )
        raise SystemExit(2)

    customer = value.strip()

    if not customer:
        error("Customer name cannot be empty.")
        raise SystemExit(2)

    if customer in {".", ".."}:
        error("Customer name is invalid.")
        raise SystemExit(2)

    if "/" in customer or "\\" in customer:
        error("Customer name must be a single directory name, not a path.")
        raise SystemExit(2)

    return customer


def validate_paths(vtx_root: Path, repo_root: Path) -> tuple[Path, Path]:
    """Validate source and repository paths."""
    source_usr = vtx_root / "usr"

    if not source_usr.is_dir():
        error(f"VTX usr directory does not exist: {source_usr}")
        raise SystemExit(2)

    if not repo_root.is_dir():
        error(f"Repository directory does not exist: {repo_root}")
        raise SystemExit(2)

    if not (repo_root / ".git").exists():
        error(f"Directory is not a Git repository: {repo_root}")
        raise SystemExit(2)

    return source_usr, repo_root


def iter_allowed_files(root: Path) -> Iterable[Path]:
    """Yield allowed files beneath root as paths relative to root."""
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in ALLOWED_EXTENSIONS:
            yield path.relative_to(root)


def copy_snapshot(source_usr: Path, target_usr: Path) -> tuple[int, int]:
    """
    Mirror allowed files from source_usr to target_usr.

    Returns:
        (copied_or_updated_count, deleted_count)
    """
    source_files = set(iter_allowed_files(source_usr))
    target_files = set(iter_allowed_files(target_usr)) if target_usr.exists() else set()

    copied = 0
    deleted = 0

    for relative_path in sorted(source_files):
        source = source_usr / relative_path
        destination = target_usr / relative_path

        destination.parent.mkdir(parents=True, exist_ok=True)

        if not destination.exists() or not files_match(source, destination):
            shutil.copy2(source, destination)
            copied += 1
            log(f"Copied: usr/{relative_path.as_posix()}")

    stale_files = target_files - source_files
    for relative_path in sorted(stale_files):
        destination = target_usr / relative_path
        destination.unlink()
        deleted += 1
        log(f"Deleted stale file: usr/{relative_path.as_posix()}")

    remove_empty_directories(target_usr)

    return copied, deleted


def files_match(left: Path, right: Path) -> bool:
    """Compare two files without changing either file's timestamps."""
    if left.stat().st_size != right.stat().st_size:
        return False

    chunk_size = 1024 * 1024

    with left.open("rb") as left_handle, right.open("rb") as right_handle:
        while True:
            left_chunk = left_handle.read(chunk_size)
            right_chunk = right_handle.read(chunk_size)

            if left_chunk != right_chunk:
                return False

            if not left_chunk:
                return True


def remove_empty_directories(root: Path) -> None:
    """Remove empty directories beneath root, leaving root itself intact."""
    if not root.exists():
        return

    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )

    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass


def ensure_git_identity(repo_root: Path) -> None:
    """Fail clearly when Git commit identity is not configured."""
    name = run_git(repo_root, ["config", "--get", "user.name"], check=False)
    email = run_git(repo_root, ["config", "--get", "user.email"], check=False)

    if name.returncode != 0 or not name.stdout.strip():
        error("Git user.name is not configured for this repository.")
        raise SystemExit(2)

    if email.returncode != 0 or not email.stdout.strip():
        error("Git user.email is not configured for this repository.")
        raise SystemExit(2)


def update_repository(repo_root: Path) -> None:
    """Update the local checkout before creating the customer snapshot."""
    log("Updating vtx-live-configs repository...")
    run_git(repo_root, ["pull", "--rebase", "--autostash"])


def stage_customer(repo_root: Path, customer: str) -> None:
    """Stage only the current customer's directory."""
    run_git(repo_root, ["add", "-A", "--", f"{customer}/usr"])


def staged_changes_exist(repo_root: Path) -> bool:
    """Return True when the Git index contains staged changes."""
    result = run_git(repo_root, ["diff", "--cached", "--quiet"], check=False)

    if result.returncode == 0:
        return False

    if result.returncode == 1:
        return True

    error(result.stderr.strip() or "Unable to inspect staged Git changes.")
    raise SystemExit(result.returncode)


def commit_and_push(repo_root: Path, customer: str) -> None:
    """Commit and push the staged customer snapshot."""
    commit_message = f"Sync live VTX usr files for {customer}"

    log("Committing changes...")
    run_git(repo_root, ["commit", "-m", commit_message])

    log("Pushing changes...")
    push_result = run_git(repo_root, ["push"], check=False)

    if push_result.returncode == 0:
        return

    log("Initial push was rejected; rebasing and retrying once...")
    run_git(repo_root, ["pull", "--rebase"])
    retry_result = run_git(repo_root, ["push"], check=False)

    if retry_result.returncode != 0:
        error(retry_result.stderr.strip() or "Git push failed.")
        raise SystemExit(retry_result.returncode)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Mirror .yaml, .py, and .conf files from VTX usr/ into a "
            "customer directory in vtx-live-configs, then commit and push."
        )
    )
    parser.add_argument(
        "--customer",
        help="Customer directory name, such as SOM. "
             "Defaults to VTX_CUSTOMER_NAME.",
    )
    parser.add_argument(
        "--repo-root",
        help="Local checkout of vtx-live-configs. "
             "Defaults to VTX_LIVE_CONFIGS_ROOT.",
    )
    parser.add_argument(
        "--vtx-root",
        help="Deployed VTX root. Defaults to VTX_ROOT, BTDM_ROOT, "
             "or the platform default.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    customer = get_customer(args.customer)
    vtx_root = get_vtx_root(args.vtx_root)
    repo_root = get_repo_root(args.repo_root)
    source_usr, repo_root = validate_paths(vtx_root, repo_root)
    target_usr = repo_root / customer / "usr"

    log(f"Customer: {customer}")
    log(f"Source: {source_usr}")
    log(f"Destination: {target_usr}")

    ensure_git_identity(repo_root)
    update_repository(repo_root)

    copied, deleted = copy_snapshot(source_usr, target_usr)
    stage_customer(repo_root, customer)

    if not staged_changes_exist(repo_root):
        log("No matching file changes detected. Nothing to commit.")
        return 0

    log(f"Snapshot changes: {copied} copied/updated, {deleted} deleted.")
    commit_and_push(repo_root, customer)

    log("Remote configuration sync complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
