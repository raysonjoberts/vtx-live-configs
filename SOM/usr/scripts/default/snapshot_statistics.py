import os
import sys
import logging

import pandas as pd
import yaml

# ------------------------------------------------------------
# Globals / Paths
# ------------------------------------------------------------
VTX_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
SNAPSHOT_DIR = os.path.join(VTX_ROOT, "var", "dailysnapshot")  # NOTE: plural
STATS_DIR = os.path.join(VTX_ROOT, "var", "statistics")
STATS_CONFIG_PATH = os.path.join(VTX_ROOT, "usr", "config", "run", "snapshot_statistics.yaml")

BASE_COLUMNS = [
    "Date",
    "Field Name",
    "Count",
    "Unique Count",
    "Active Count",
    "Active Unique Count",
    "Inactive Count",
    "Inactive Unique Count",
]

# ------------------------------------------------------------
# Logging (BTDM/VTX style)
# ------------------------------------------------------------
try:
    sys.path.append(os.path.join(VTX_ROOT, "usr", "lib"))
    import btdm_logging  # type: ignore

    logger = btdm_logging.get_logger(component="snapshot_statistics")
except Exception:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    logger = logging.getLogger("snapshot_statistics")


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def parse_snapshot_filename(filename: str):
    """
    Expect filenames like:
      tablename_MMDDYYYY.csv

    Returns (table_name, date_str) or (None, None) if it doesn't match.
    """
    name, ext = os.path.splitext(filename)
    if ext.lower() != ".csv":
        return None, None

    if "_" not in name:
        return None, None

    base, date_part = name.rsplit("_", 1)
    if len(date_part) != 8 or not date_part.isdigit():
        return None, None

    return base, date_part  # e.g. ("server_inventory", "12082025")


def load_existing_stats(stats_path: str) -> pd.DataFrame:
    """
    Load an existing stats CSV if present, otherwise return empty DF with expected columns.
    Handles older files that only had the first 4 columns.
    """
    if os.path.exists(stats_path):
        try:
            df = pd.read_csv(stats_path, dtype={"Date": str, "Field Name": str})
            # Ensure all expected columns exist
            for col in BASE_COLUMNS:
                if col not in df.columns:
                    df[col] = ""
            return df[BASE_COLUMNS]
        except Exception as e:
            logger.warning(f"Failed to read existing stats file {stats_path}: {e}")
            # Fall through to empty DF

    return pd.DataFrame(columns=BASE_COLUMNS)


def load_status_config(config_path: str) -> dict:
    """
    Load snapshot_stats.yaml and return mapping:
      { table_name: status_cfg_dict }

    status_cfg_dict format:
      { "column": "...", "active_values": [...], "inactive_values": [...] }
    """
    table_status_map: dict[str, dict] = {}

    if not os.path.exists(config_path):
        logger.info(f"No status config found at {config_path}; running without Active/Inactive splits.")
        return table_status_map

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"Failed to load status config {config_path}: {e}")
        return table_status_map

    tables = cfg.get("tables", [])
    if not isinstance(tables, list):
        logger.warning(f"Invalid 'tables' section in {config_path}; expected a list.")
        return table_status_map

    for entry in tables:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        status_cfg = entry.get("status")
        if not name or not status_cfg:
            continue
        table_status_map[str(name)] = status_cfg

    if table_status_map:
        logger.info(
            f"Loaded status config for tables: {', '.join(sorted(table_status_map.keys()))}"
        )
    else:
        logger.info("Status config loaded but no tables defined with 'status' section.")

    return table_status_map


def compute_snapshot_stats(df: pd.DataFrame, date_str: str, status_cfg: dict | None = None) -> pd.DataFrame:
    """
    Compute stats rows for a single snapshot DataFrame for a given date.
    Returns a DataFrame with columns in BASE_COLUMNS.

    If status_cfg is provided and valid, also computes Active/Inactive counts.
    """
    rows = []

    total_rows = len(df)
    total_cols = len(df.columns)

    # Precompute Active/Inactive subsets if configured
    active_df = None
    inactive_df = None

    if status_cfg:
        status_col = status_cfg.get("column")
        if status_col and status_col in df.columns:
            status_series = df[status_col].astype(str)

            active_values = {str(v) for v in status_cfg.get("active_values", [])}
            inactive_values = {str(v) for v in status_cfg.get("inactive_values", [])}

            if active_values:
                active_mask = status_series.isin(active_values)
                active_df = df[active_mask]
            if inactive_values:
                inactive_mask = status_series.isin(inactive_values)
                inactive_df = df[inactive_mask]
        else:
            logger.warning(
                f"Status config provided for date {date_str} but column "
                f"'{status_cfg.get('column')}' not found; skipping Active/Inactive split."
            )

    # Synthetic rows & columns entries (Active/Inactive not applicable)
    rows.append(
        {
            "Date": date_str,
            "Field Name": "rows",
            "Count": int(total_rows),
            "Unique Count": "",
            "Active Count": "",
            "Active Unique Count": "",
            "Inactive Count": "",
            "Inactive Unique Count": "",
        }
    )
    rows.append(
        {
            "Date": date_str,
            "Field Name": "columns",
            "Count": int(total_cols),
            "Unique Count": "",
            "Active Count": "",
            "Active Unique Count": "",
            "Inactive Count": "",
            "Inactive Unique Count": "",
        }
    )

    # Per-field non-null/unique + Active/Inactive variants
    for col in df.columns:
        series = df[col]
        non_null = series[series.notna()]
        total_non_null = int(non_null.shape[0])
        unique_total = int(non_null.nunique(dropna=True))

        active_count = ""
        active_unique = ""
        inactive_count = ""
        inactive_unique = ""

        if active_df is not None:
            s_active = active_df[col]
            non_null_active = s_active[s_active.notna()]
            if len(non_null_active) > 0:
                active_count = int(non_null_active.shape[0])
                active_unique = int(non_null_active.nunique(dropna=True))

        if inactive_df is not None:
            s_inactive = inactive_df[col]
            non_null_inactive = s_inactive[s_inactive.notna()]
            if len(non_null_inactive) > 0:
                inactive_count = int(non_null_inactive.shape[0])
                inactive_unique = int(non_null_inactive.nunique(dropna=True))

        rows.append(
            {
                "Date": date_str,
                "Field Name": str(col),
                "Count": total_non_null,
                "Unique Count": unique_total,
                "Active Count": active_count,
                "Active Unique Count": active_unique,
                "Inactive Count": inactive_count,
                "Inactive Unique Count": inactive_unique,
            }
        )

    return pd.DataFrame(rows, columns=BASE_COLUMNS)


def process_snapshots():
    logger.info(f"VTX_ROOT={VTX_ROOT}")
    logger.info(f"Scanning snapshot directory: {SNAPSHOT_DIR}")

    if not os.path.isdir(SNAPSHOT_DIR):
        logger.error(f"Snapshot directory does not exist: {SNAPSHOT_DIR}")
        return

    # Load status config (Active/Inactive definitions per table)
    table_status_map = load_status_config(STATS_CONFIG_PATH)

    # Group snapshot files by table name
    table_files: dict[str, list[tuple[str, str]]] = {}
    for fname in os.listdir(SNAPSHOT_DIR):
        table_name, date_str = parse_snapshot_filename(fname)
        if table_name is None:
            continue

        full_path = os.path.join(SNAPSHOT_DIR, fname)
        table_files.setdefault(table_name, []).append((date_str, full_path))

    if not table_files:
        logger.info("No snapshot files found to process.")
        return

    os.makedirs(STATS_DIR, exist_ok=True)

    for table_name, items in table_files.items():
        stats_path = os.path.join(STATS_DIR, f"{table_name}_stats.csv")
        logger.info(f"Processing table '{table_name}' -> stats file '{stats_path}'")

        # Load existing stats and find already processed dates
        stats_df = load_existing_stats(stats_path)
        existing_dates = set(stats_df["Date"].astype(str).unique())

        new_rows = []

        # Determine status config for this table, if any
        status_cfg = table_status_map.get(table_name)

        # Sort by date string for predictable processing order
        for date_str, csv_path in sorted(items, key=lambda x: x[0]):
            if date_str in existing_dates:
                logger.info(f"  Skipping {os.path.basename(csv_path)} (date {date_str} already processed)")
                continue

            logger.info(f"  Computing stats for snapshot {os.path.basename(csv_path)} (date {date_str})")

            try:
                # Read as strings to avoid type issues; we only care about counts
                df_snapshot = pd.read_csv(csv_path, dtype=str)
            except Exception as e:
                logger.error(f"    Failed to read snapshot {csv_path}: {e}")
                continue

            try:
                snapshot_stats = compute_snapshot_stats(df_snapshot, date_str, status_cfg=status_cfg)
                new_rows.append(snapshot_stats)
            except Exception as e:
                logger.error(f"    Failed to compute stats for {csv_path}: {e}")
                continue

        # If there are new rows, append and write out
        if new_rows:
            combined_new = pd.concat(new_rows, ignore_index=True)
            updated_stats = pd.concat([stats_df, combined_new], ignore_index=True)

            # Sort by Date then Field Name for readability
            updated_stats["Date"] = updated_stats["Date"].astype(str)
            updated_stats = updated_stats.sort_values(by=["Date", "Field Name"])

            try:
                updated_stats.to_csv(stats_path, index=False)
                logger.info(f"  Updated stats written to {stats_path}")
            except Exception as e:
                logger.error(f"  Failed to write stats file {stats_path}: {e}")
        else:
            logger.info(f"  No new dates to process for table '{table_name}'")


if __name__ == "__main__":
    process_snapshots()