#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
visual_tasks_dashboard_vtx.py
-----------------------------
Purpose:
  Render a tasks dashboard HTML from CSV sources using the VTX universal config.

Inputs:
  - Config YAML (VTX universal template)
  - CSV files defined per job

Outputs:
  - HTML dashboard(s) written to configured output paths
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from html import escape
from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

try:
    import yaml  # PyYAML
except Exception as e:
    raise SystemExit("Missing dependency: pyyaml. Install it in the VTX venv.") from e


# ---------------------------------------------------------------------
# Globals / Paths (VTX style)
# ---------------------------------------------------------------------

def resolve_vtx_root() -> Path:
    """
    Determine VTX_ROOT in a cross-platform way.
    Priority:
      1) env VTX_ROOT
      2) env BTDM_ROOT (legacy)
      3) infer relative to this file: <VTX_ROOT>/usr/scripts/default/<script>.py
    """
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    here = Path(__file__).resolve()
    inferred = here.parents[3]  # default/<script>.py -> scripts -> usr -> VTX_ROOT
    return inferred


VTX_ROOT = resolve_vtx_root()

DEFAULT_CONFIG_DIR = VTX_ROOT / "usr" / "config" / "default"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "visual_tasks_dashboard_vtx.yaml"


def vtx_path(path_str: str | Path, *, must_exist: bool = False) -> Path:
    """
    Resolve a path that may be:
      - VTX-relative: "var/tables/x.csv"
      - absolute: "/opt/vtx/..." or "C:\\BTDM_7.1\\..."
      - contains env vars: "$VTX_ROOT/var/..." or "%VTX_ROOT%\\var\\..."
      - contains a literal prefix "VTX_ROOT/" or "BTDM_ROOT/" (legacy)
    """
    if isinstance(path_str, Path):
        p = path_str
    else:
        s = str(path_str).strip()

        # Expand env vars and ~
        s = os.path.expandvars(s)
        s = os.path.expanduser(s)

        # Support literal "VTX_ROOT/..." and "BTDM_ROOT/..."
        s = s.replace("VTX_ROOT" + os.sep, str(VTX_ROOT) + os.sep)
        s = s.replace("BTDM_ROOT" + os.sep, str(VTX_ROOT) + os.sep)
        s = s.replace("VTX_ROOT/", str(VTX_ROOT) + "/")
        s = s.replace("BTDM_ROOT/", str(VTX_ROOT) + "/")

        p = Path(s)

    if not p.is_absolute():
        p = (VTX_ROOT / p).resolve()

    if must_exist and not p.exists():
        raise FileNotFoundError(f"Path does not exist: {p}")
    return p


# ---------------------------------------------------------------------
# Logging (VTX style)
# ---------------------------------------------------------------------

def get_logger(component: str) -> logging.Logger:
    """
    Prefer vtx_logging if present; fall back to stdlib.
    """
    lib_dir = VTX_ROOT / "usr" / "lib"
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))

    try:
        import vtx_logging  # type: ignore

        return vtx_logging.get_logger(component=component)
    except Exception:
        logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
        return logging.getLogger(component)


logger = get_logger(component="visual_tasks_dashboard_vtx")


# ---------------------------------------------------------------------
# Config loading helpers
# ---------------------------------------------------------------------

def load_yaml(path: Path) -> Dict[str, Any]:
    """
    Loads YAML and returns dict.
    Supports the universal template: top-level _vtx + config.
    If you pass a legacy YAML (no _vtx/config), it returns it as-is.
    """
    raw = path.read_text(encoding="utf-8")
    doc = yaml.safe_load(raw) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"Config root must be a mapping/dict: {path}")

    if "config" in doc and isinstance(doc.get("config"), dict):
        cfg = doc["config"]
    else:
        cfg = doc

    for k in ("reports", "jobs", "stretches", "inputs", "outputs"):
        if k in cfg and cfg[k] is None:
            cfg[k] = []

    return cfg


# ---------------------------------------------------------------------
# Path helpers (legacy-compatible)
# ---------------------------------------------------------------------

def resolve_path(path: str | None) -> str | None:
    """
    Resolve a path relative to VTX_ROOT.

    - If path is None -> return None
    - If path is an absolute Windows or UNC path -> return as-is
    - If path starts with "/" -> treat as VTX_ROOT-relative
    - Otherwise -> concatenate to VTX_ROOT
    """
    if not path:
        return None

    if re.match(r"^[A-Za-z]:", path) or path.startswith("\\\\"):
        return path

    normalized = path.lstrip("/\\")
    return os.path.join(str(VTX_ROOT), normalized)


def resolve_vtx_path(path: str | None) -> str | None:
    """
    Resolve a path that may use the literal prefix 'VTX_ROOT/'.
    """
    if path is None:
        return None
    if path.startswith("VTX_ROOT/"):
        return os.path.join(str(VTX_ROOT), path[len("VTX_ROOT/"):])
    return resolve_path(path)


# ---------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------

def format_datetime(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""

    try:
        ts = pd.to_datetime(value, errors="coerce")
    except Exception:
        return ""

    if pd.isna(ts):
        return ""

    s = ts.strftime("%m/%d/%Y %I:%M%p")
    try:
        date_part, time_part = s.split(" ")
        month, day, year = date_part.split("/")
        month = str(int(month))
        day = str(int(day))

        ampm = time_part[-2:].lower()
        time_main = time_part[:-2].lstrip("0")
        if time_main.startswith(":"):
            time_main = "12" + time_main

        return f"{month}/{day}/{year} {time_main}{ampm}"
    except Exception:
        return s


def normalize_status(raw: Any) -> str:
    if raw is None:
        return "unknown"
    text = str(raw).strip().lower()

    mapping = {
        "backlog": "backlog",
        "todo": "backlog",
        "to do": "backlog",
        "in progress": "in_progress",
        "in-progress": "in_progress",
        "in_progress": "in_progress",
        "working": "in_progress",
        "doing": "in_progress",
        "complete": "completed",
        "completed": "completed",
        "done": "completed",
        "closed": "completed",
        "blocked": "blocked",
        "on hold": "on_hold",
        "on-hold": "on_hold",
        "on_hold": "on_hold",
        "paused": "on_hold",
    }
    return mapping.get(text, "other")


def status_display_label(raw: Any) -> str:
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    lower = text.lower()
    if lower in {"in-progress", "in_progress"}:
        return "In Progress"
    if lower in {"on-hold", "on_hold"}:
        return "On Hold"
    return text[:1].upper() + text[1:]


def compute_status_counts(df: pd.DataFrame, status_field: str | None) -> Dict[str, int]:
    counts = {
        "total": len(df),
        "backlog": 0,
        "in_progress": 0,
        "completed": 0,
        "blocked": 0,
        "on_hold": 0,
        "other": 0,
    }
    if not status_field or status_field not in df.columns:
        return counts

    for val in df[status_field]:
        key = normalize_status(val)
        if key not in counts:
            key = "other"
        counts[key] += 1

    return counts


def enrich_with_log(
    tasks_df: pd.DataFrame,
    cfg: Dict[str, Any],
) -> Tuple[pd.DataFrame, Dict[str, List[Dict[str, str]]]]:
    log_csv = resolve_vtx_path(cfg.get("log_csv"))
    logs_by_key: Dict[str, List[Dict[str, str]]] = {}

    if not log_csv:
        return tasks_df, logs_by_key

    if not os.path.exists(log_csv):
        logger.warning("tasks_log_csv_not_found,path=%s", log_csv)
        return tasks_df, logs_by_key

    key_field = cfg.get("log_key_field", "task_id")
    ts_field = cfg.get("log_timestamp_field")
    out_field = cfg.get("log_last_updated_column", "last_updated")

    try:
        log_df = pd.read_csv(log_csv)
    except Exception as exc:
        logger.error("failed_to_read_tasks_log_csv,path=%s,error=%s", log_csv, exc)
        return tasks_df, logs_by_key

    if not ts_field or ts_field not in log_df.columns or key_field not in log_df.columns:
        logger.warning(
            "tasks_log_missing_columns,path=%s,key_field=%s,ts_field=%s,columns=%s",
            log_csv,
            key_field,
            ts_field,
            list(log_df.columns),
        )
        return tasks_df, logs_by_key

    try:
        if key_field in tasks_df.columns:
            tasks_df = tasks_df.copy()
            tasks_df[key_field] = tasks_df[key_field].astype("string")
        else:
            logger.warning(
                "tasks_df_missing_key_field,key_field=%s,columns=%s",
                key_field,
                list(tasks_df.columns),
            )
            return tasks_df, logs_by_key

        log_df[key_field] = log_df[key_field].astype("string")

        original_rows = len(log_df)

        log_df["_parsed_ts"] = pd.to_datetime(log_df[ts_field], errors="coerce")

        if log_df["_parsed_ts"].notna().any():
            ts_for_group = log_df["_parsed_ts"]
        else:
            ts_for_group = log_df[ts_field]

        tmp = log_df[[key_field]].copy()
        tmp["_ts_for_group"] = ts_for_group
        last_updates = tmp.groupby(key_field)["_ts_for_group"].max().reset_index()
        last_updates.rename(columns={"_ts_for_group": out_field}, inplace=True)

        merged = tasks_df.merge(last_updates, how="left", on=key_field)

        for _, row in log_df.iterrows():
            key_val = row.get(key_field)
            if pd.isna(key_val):
                continue

            key_str = str(key_val)

            ts_val = row.get(ts_field)
            ts_display = format_datetime(ts_val)

            details = ""
            if "update_text" in log_df.columns:
                update_val = row.get("update_text")
                if not (isinstance(update_val, float) and pd.isna(update_val)):
                    details = str(update_val) if update_val is not None else ""

            ts_sort_val = row.get("_parsed_ts")
            ts_sort = ""
            if pd.notna(ts_sort_val):
                try:
                    ts_sort = pd.to_datetime(ts_sort_val).isoformat()
                except Exception:
                    ts_sort = ""
            if not ts_sort:
                ts_sort = str(ts_val) if ts_val is not None else ""

            logs_by_key.setdefault(key_str, []).append(
                {
                    "timestamp": ts_display,
                    "ts_sort": ts_sort,
                    "details": details,
                }
            )

        logger.info(
            "tasks_log_joined,log_rows=%s,parsed_nonnull=%s,tasks_rows=%s,joined_rows=%s,keys_with_logs=%s",
            original_rows,
            int(log_df["_parsed_ts"].notna().sum()),
            len(tasks_df),
            merged[out_field].notna().sum(),
            len(logs_by_key),
        )
        return merged, logs_by_key

    except Exception as exc:
        logger.error("failed_to_join_tasks_log,error=%s", exc)
        return tasks_df, logs_by_key


# ---------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------

def generate_html(
    df: pd.DataFrame,
    cfg: Dict[str, Any],
    logs_by_key: Dict[str, List[Dict[str, str]]] | None = None,
) -> str:
    def clean_cell(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, float) and pd.isna(value):
            return ""
        text = str(value).strip()
        if text.lower() in {"nan", "none", "null"}:
            return ""
        return text

    def truncate_label(value: str, max_len: int = 56) -> str:
        if len(value) <= max_len:
            return value
        return value[: max_len - 5].rstrip() + "....+"

    title = cfg.get("title") or cfg.get("name") or "Tasks"
    subtitle = cfg.get("subtitle")
    display_columns: List[Dict[str, Any]] = cfg.get("display_columns") or []
    status_field: str | None = cfg.get("status_field")
    search_fields: List[str] = cfg.get("search_fields") or []
    id_field: str = cfg.get("id_field", "task_id")
    name_field: str = cfg.get("name_field", "task_label")
    assignee_field: str = cfg.get("assignee_field", "assignee")
    due_field: str = cfg.get("due_field", "due_date")
    priority_field: str = cfg.get("priority_field", "priority")
    phase_field: str = cfg.get("phase_field", "phase")

    logs_by_key = logs_by_key or {}

    stats = compute_status_counts(df, status_field)

    subtitle_html = (
        f'<div class="muted">{escape(subtitle)}</div>' if subtitle else ""
    )

    summary_html = f"""
      <div class="summary-grid">
        <div class="summary-card active" data-filter="all">
          <div class="summary-label">All Tasks</div>
          <div class="summary-value">{stats['total']}</div>
        </div>
        <div class="summary-card" data-filter="backlog">
          <div class="summary-label">Backlog</div>
          <div class="summary-value">{stats['backlog']}</div>
        </div>
        <div class="summary-card" data-filter="in_progress">
          <div class="summary-label">In Progress</div>
          <div class="summary-value">{stats['in_progress']}</div>
        </div>
        <div class="summary-card warning" data-filter="blocked">
          <div class="summary-label">Blocked</div>
          <div class="summary-value">{stats['blocked']}</div>
        </div>
        <div class="summary-card" data-filter="completed">
          <div class="summary-label">Completed</div>
          <div class="summary-value">{stats['completed']}</div>
        </div>
      </div>
    """

    # Override table columns to match requested UX
    display_columns = [
        {"field": id_field, "label": "Task ID", "type": "text"},
        {"field": name_field, "label": "Task Name", "type": "text"},
        {"field": status_field or "status", "label": "Status", "type": "status"},
        {"field": priority_field, "label": "Priority", "type": "text"},
        {"field": phase_field, "label": "Phase", "type": "text"},
    ]

    header_cells: List[str] = []
    for col_cfg in display_columns:
        label = col_cfg.get("label") or col_cfg.get("field") or ""
        col_type = (col_cfg.get("type") or "text").lower()

        if col_type in {"number", "int", "float"}:
            sort_type = "number"
        elif col_type in {"datetime", "date", "timestamp"}:
            sort_type = "date"
        else:
            sort_type = "string"

        header_cells.append(
            f'<th data-type="{escape(sort_type)}">{escape(str(label))}<span class="sort-indicator"></span></th>'
        )

    header_html = "<tr>" + "".join(header_cells) + "</tr>"

    body_rows_html: List[str] = []

    if df.empty or not display_columns:
        body_rows_html.append(
            '<tr><td colspan="{colspan}" class="muted">No tasks found.</td></tr>'.format(
                colspan=len(display_columns) + 1 or 1
            )
        )
    else:
        task_rows: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            search_tokens: List[str] = []
            for field in search_fields:
                if field in row.index:
                    val = row[field]
                    if val is not None and not (isinstance(val, float) and pd.isna(val)):
                        search_tokens.append(str(val))
            search_attr = escape(" ".join(search_tokens).lower())

            task_id_val = row[id_field] if id_field in row.index else None
            task_id_str = "" if task_id_val is None else str(task_id_val)
            entry_logs = logs_by_key.get(task_id_str, [])
            entry_logs_sorted = sorted(
                entry_logs,
                key=lambda x: str(x.get("ts_sort") or x.get("timestamp") or ""),
                reverse=True,
            )

            task_rows.append(
                {
                    "task_id": task_id_str,
                    "task_name": clean_cell(row.get(name_field, "") if name_field in row.index else ""),
                    "status_raw": str(row.get(status_field, "") if status_field in row.index else ""),
                    "status_norm": normalize_status(row.get(status_field, "")),
                    "status_label": status_display_label(row.get(status_field, "")),
                    "priority": clean_cell(row.get(priority_field, "") if priority_field in row.index else ""),
                    "phase": clean_cell(row.get(phase_field, "") if phase_field in row.index else ""),
                    "assignee": clean_cell(row.get(assignee_field, "") if assignee_field in row.index else ""),
                    "due_date": format_datetime(row.get(due_field, "")) if due_field in row.index else "",
                    "updates": entry_logs_sorted,
                }
            )

            cells_html: List[str] = []
            for col_cfg in display_columns:
                field = col_cfg.get("field")
                col_type = (col_cfg.get("type") or "text").lower()
                raw_val = row[field] if field in row.index else ""

                data_value = ""
                display_value = ""

                if col_type in {"datetime", "date", "timestamp"}:
                    display_value = format_datetime(raw_val)
                    try:
                        ts = pd.to_datetime(raw_val, errors="coerce")
                        data_value = ts.isoformat() if not pd.isna(ts) else ""
                    except Exception:
                        data_value = ""
                elif col_type in {"number", "int", "float"}:
                    if raw_val is None or (isinstance(raw_val, float) and pd.isna(raw_val)):
                        display_value = ""
                        data_value = ""
                    else:
                        display_value = str(raw_val)
                        try:
                            data_value = str(float(raw_val))
                        except Exception:
                            data_value = ""
                elif col_type == "status":
                    display_value = status_display_label(raw_val)
                    norm = normalize_status(raw_val)
                    data_value = norm
                    pill_class = f"status-pill status-{norm}"
                    cell_html = (
                        f'<td data-value="{escape(data_value)}">'
                        f'<span class="{pill_class}">{escape(display_value or "")}</span>'
                        "</td>"
                    )
                    cells_html.append(cell_html)
                    continue
                else:
                    display_value = clean_cell(raw_val)
                    data_value = display_value.lower()

                if field == name_field and display_value:
                    full_value = display_value
                    display_value = truncate_label(display_value)
                    cell_html = (
                        f'<td data-value="{escape(str(full_value).lower())}" '
                        f'title="{escape(full_value)}">{escape(display_value)}</td>'
                    )
                    cells_html.append(cell_html)
                    continue

                cells_html.append(
                    f'<td data-value="{escape(str(data_value))}">{escape(display_value)}</td>'
                )

            status_norm = normalize_status(row.get(status_field, ""))
            row_html = (
                f'<tr data-search="{search_attr}" data-task-id="{escape(task_id_str)}" '
                f'data-status="{escape(status_norm)}">'
                + "".join(cells_html)
                + "</tr>"
            )
            body_rows_html.append(row_html)

    body_html = "\n".join(body_rows_html)

    tasks_js = json.dumps(task_rows, ensure_ascii=False)

    template_str = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>$TITLE</title>
  <style>
    :root {
      --bg: #0e1222;
      --bg-elevated: #0e1222;
      --panel: #151a30;
      --panel-soft: #151a30;
      --ink: #eaeef8;
      --ink-soft: #c5d0ff;
      --ink-muted: #9aa3b2;
      --accent: #c5d0ff;
      --accent-soft: rgba(197, 208, 255, 0.12);
      --accent-strong: #c5d0ff;
      --border-soft: rgba(255, 255, 255, 0.07);
      --danger: #f97373;
      --danger-soft: rgba(249, 115, 115, 0.35);
      --warning: #fb923c;
      --warning-soft: rgba(251, 146, 60, 0.12);
      --success: #4ade80;
      --success-soft: rgba(74, 222, 128, 0.25);
      --shadow-soft: 0 6px 24px rgba(0,0,0,0.35);
      --radius-lg: 12px;
      --radius-pill: 999px;
      --font-sans: Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      padding: 24px 28px 32px;
      font-family: var(--font-sans);
      background: #0e1222;
      color: var(--ink);
      min-height: 100vh;
    }

    .page {
      max-width: 1320px;
      margin: 0 auto;
    }

    .page-header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
      margin-bottom: 18px;
    }

    .page-title-block h1 {
      margin: 0;
      font-size: 24px;
      letter-spacing: 0.02em;
      font-weight: 600;
    }

    .page-title-block .muted {
      margin-top: 6px;
      font-size: 13px;
      color: var(--ink-muted);
    }

    .controls-row {
      display: flex;
      align-items: center;
      gap: 10px;
      justify-content: flex-end;
      margin-bottom: 14px;
    }

    .pill-label {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.16em;
      color: var(--ink-muted);
    }

    .search-input {
      min-width: 220px;
      padding: 7px 10px;
      border-radius: 999px;
      border: 1px solid var(--border-soft);
      background: rgba(15, 23, 42, 0.9);
      color: var(--ink);
      font-size: 13px;
      outline: none;
      box-shadow: 0 0 0 1px rgba(15, 23, 42, 0.7);
    }

    .search-input::placeholder {
      color: var(--ink-muted);
    }

    .summary-grid {
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 10px;
      margin-top: 8px;
      margin-bottom: 14px;
    }

    .summary-card {
      position: relative;
      border-radius: 12px;
      padding: 10px 12px;
      background: #151a30;
      border: 1px solid rgba(255,255,255,0.07);
      box-shadow: 0 6px 24px rgba(0,0,0,0.35);
      overflow: hidden;
      cursor: pointer;
    }

    .summary-card::before {
      content: "";
      position: absolute;
      inset: -20%;
      background:
        radial-gradient(circle at top left, rgba(197, 208, 255, 0.35), transparent 52%),
        radial-gradient(circle at bottom right, rgba(197, 208, 255, 0.35), transparent 55%);
      opacity: 0.12;
      filter: blur(6px);
    }

    .summary-card.warning {
      border-color: rgba(249, 115, 115, 0.55);
    }

    .summary-card.warning::before {
      background:
        radial-gradient(circle at top left, rgba(251, 146, 60, 0.7), transparent 52%),
        radial-gradient(circle at bottom right, rgba(248, 113, 113, 0.7), transparent 55%);
    }

    .summary-card > * {
      position: relative;
      z-index: 1;
    }

    .summary-label {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.16em;
      color: var(--ink-muted);
      margin-bottom: 4px;
    }

    .summary-value {
      font-size: 18px;
      font-weight: 600;
    }
    .summary-card.active {
      border-color: rgba(197, 208, 255, 0.9);
      box-shadow: 0 0 0 1px rgba(197, 208, 255, 0.45), 0 0 12px rgba(197, 208, 255, 0.25);
    }

    .tile {
      border-radius: var(--radius-lg);
      padding: 14px 16px 16px;
      background: #151a30;
      border: 1px solid rgba(255,255,255,0.07);
      box-shadow: var(--shadow-soft);
    }

    .tile-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 10px;
    }

    .tile-title {
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.18em;
      color: var(--ink-muted);
    }

    .table-wrapper {
      margin-top: 6px;
      border-radius: 12px;
      border: 1px solid rgba(255,255,255,0.07);
      background: #151a30;
      overflow: hidden;
    }

    table.data {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }

    table.data thead tr {
      background: #151a30;
    }

    table.data th,
    table.data td {
      padding: 6px 9px;
      text-align: left;
      border-bottom: 1px solid rgba(255,255,255,0.07);
      white-space: nowrap;
    }

    table.data th {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.16em;
      color: var(--ink-muted);
      cursor: pointer;
      position: relative;
      user-select: none;
    }

    table.data th .sort-indicator {
      margin-left: 4px;
      font-size: 9px;
      opacity: 0.7;
    }

    table.data th.sorted-asc .sort-indicator::before {
      content: "▲";
    }

    table.data th.sorted-desc .sort-indicator::before {
      content: "▼";
    }

    table.data tbody tr:nth-child(even) {
      background: #151a30;
    }

    table.data tbody tr:nth-child(odd) {
      background: #151a30;
    }

    table.data tbody tr:hover {
      background: rgba(255,255,255,0.04);
    }

    table.data td {
      color: var(--ink-soft);
    }

    table.data td:first-child,
    table.data th:first-child {
      padding-left: 12px;
    }

    table.data td:last-child,
    table.data th:last-child {
      padding-right: 12px;
    }

    .status-pill {
      display: inline-flex;
      align-items: center;
      padding: 3px 8px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 500;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }

    .status-backlog {
      background: rgba(255,255,255,0.12);
      color: #9aa3b2;
      border: 1px solid rgba(255,255,255,0.12);
    }

    .status-in_progress {
      background: rgba(197, 208, 255, 0.2);
      color: #c5d0ff;
      border: 1px solid rgba(197, 208, 255, 0.7);
    }

    .status-completed {
      background: rgba(74, 222, 128, 0.25);
      color: #4ade80;
      border: 1px solid rgba(74, 222, 128, 0.7);
    }

    .status-blocked,
    .status-on_hold {
      background: rgba(249, 115, 115, 0.35);
      color: #f97373;
      border: 1px solid rgba(249, 115, 115, 0.7);
    }

    .status-other,
    .status-unknown {
      background: rgba(15, 23, 42, 0.9);
      color: var(--ink-soft);
      border: 1px solid rgba(75, 85, 99, 0.8);
    }

    .muted {
      color: var(--ink-muted);
    }

    .log-modal.hidden {
      display: none;
    }

    .log-modal {
      position: fixed;
      inset: 0;
      z-index: 40;
      display: flex;
      align-items: center;
      justify-content: center;
    }

    .log-modal-backdrop {
      position: absolute;
      inset: 0;
      background: rgba(15, 23, 42, 0.75);
      backdrop-filter: blur(6px);
    }

    .log-modal-dialog {
      position: relative;
      z-index: 1;
      max-width: 640px;
      width: 100%;
      margin: 0 16px;
      border-radius: 16px;
      background: #151a30;
      border: 1px solid rgba(255,255,255,0.07);
      box-shadow: 0 6px 24px rgba(0,0,0,0.35);
      padding: 14px 16px 16px;
    }

    .log-modal-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 8px;
    }

    .log-modal-title {
      font-size: 14px;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--ink-soft);
    }

    .log-modal-close {
      border: none;
      background: transparent;
      color: var(--ink-soft);
      font-size: 18px;
      cursor: pointer;
    }

    .log-modal-body {
      font-size: 12px;
      max-height: 420px;
      overflow-y: auto;
    }

    .log-modal-task-id {
      font-size: 11px;
      margin-bottom: 8px;
    }

    .log-entry-list {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .log-entry-row {
      display: grid;
      grid-template-columns: 140px 1fr;
      gap: 8px;
      padding: 6px 8px;
      border-radius: 10px;
      background: #151a30;
      border: 1px solid rgba(255,255,255,0.07);
    }

    .log-entry-timestamp {
      font-size: 11px;
      color: var(--ink-muted);
      white-space: nowrap;
    }

    .log-entry-details {
      font-size: 12px;
      color: var(--ink-soft);
      white-space: pre-wrap;
      word-break: break-word;
    }

    .task-row {
      cursor: pointer;
    }

    .task-modal-meta {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }

    .task-meta-block {
      background: #0f1326;
      border: 1px solid rgba(255,255,255,0.07);
      border-radius: 10px;
      padding: 8px 10px;
    }

    .task-meta-label {
      font-size: 10px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--ink-muted);
      margin-bottom: 4px;
    }

    .task-meta-value {
      font-size: 13px;
      color: var(--ink);
    }

    .status-indicator {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }

    .status-indicator.completed {
      background: rgba(74, 222, 128, 0.25);
      color: #4ade80;
    }

    .status-indicator.in_progress {
      background: rgba(197, 208, 255, 0.2);
      color: #c5d0ff;
    }

    .status-indicator.blocked {
      background: rgba(249, 115, 115, 0.35);
      color: #f97373;
    }

    .status-indicator.backlog {
      background: rgba(255,255,255,0.12);
      color: #9aa3b2;
    }

    @media (max-width: 900px) {
      body {
        padding: 16px;
      }

      .tile {
        padding: 12px 12px 14px;
      }

      .search-input {
        min-width: 180px;
      }

      table.data {
        font-size: 11px;
      }

      table.data th,
      table.data td {
        padding: 4px 6px;
      }

      .summary-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }
  </style>
</head>
<body>
  <div id="log-modal" class="log-modal hidden">
    <div class="log-modal-backdrop"></div>
    <div class="log-modal-dialog">
      <div class="log-modal-header">
        <div class="log-modal-title">Task Details</div>
        <button id="log-modal-close" class="log-modal-close">&times;</button>
      </div>
      <div class="log-modal-body">
        <div id="log-modal-task-id" class="log-modal-task-id muted"></div>
        <div id="log-modal-status" class="status-indicator"></div>
        <div class="task-modal-meta" id="log-modal-meta"></div>
        <div class="log-modal-title">Updates</div>
        <div id="log-modal-entries" class="log-modal-entries"></div>
      </div>
    </div>
  </div>

  <div class="page">
    <div class="page-header">
      <div class="page-title-block">
        <h1>$TITLE_ESC</h1>
        $SUBTITLE_HTML
      </div>
      <div class="controls-row">
        <div>
          <div class="pill-label">Search</div>
          <input
            id="filter-input"
            class="search-input"
            type="text"
            placeholder="Search by ID, name, status, priority, assignee..."
          />
        </div>
      </div>
    </div>

    $SUMMARY_HTML

    <div class="tile">
      <div class="tile-header">
        <div class="tile-title">Tasks Table</div>
      </div>
      <div class="table-wrapper">
        <table class="data" id="tasks-table">
          <thead>
            $HEADER_HTML
          </thead>
          <tbody>
            $BODY_HTML
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <script>
    const TASKS = $TASKS_JS;
  </script>

  <script>
    (function() {
      const cleanValue = (val) => {
        if (val === null || val === undefined) {
          return "";
        }
        const text = String(val).trim();
        if (!text) {
          return "";
        }
        const lowered = text.toLowerCase();
        if (["nan", "none", "null"].includes(lowered)) {
          return "";
        }
        return text;
      };
      const filterInput = document.getElementById("filter-input");
      const table = document.getElementById("tasks-table");
      const tbody = table.querySelector("tbody");
      const headers = Array.from(table.querySelectorAll("thead th"));
      const summaryCards = Array.from(document.querySelectorAll(".summary-card"));

      let currentSortIndex = null;
      let currentSortDir = "asc";
      let statusFilter = "all";

      function getCellValue(row, idx) {
        const cell = row.children[idx];
        if (!cell) return "";
        const dataValue = cell.getAttribute("data-value");
        return dataValue !== null ? dataValue : (cell.textContent || "");
      }

      function compareValues(a, b, type, direction) {
        if (type === "number") {
          const na = parseFloat(a);
          const nb = parseFloat(b);
          const A = isNaN(na) ? -Infinity : na;
          const B = isNaN(nb) ? -Infinity : nb;
          return direction === "asc" ? A - B : B - A;
        }
        if (type === "date") {
          const da = Date.parse(a);
          const db = Date.parse(b);
          const A = isNaN(da) ? 0 : da;
          const B = isNaN(db) ? 0 : db;
          return direction === "asc" ? A - B : B - A;
        }

        const A = (a || "").toString().toLowerCase();
        const B = (b || "").toString().toLowerCase();
        if (A < B) return direction === "asc" ? -1 : 1;
        if (A > B) return direction === "asc" ? 1 : -1;
        return 0;
      }

      function sortByColumn(index) {
        const type = headers[index].getAttribute("data-type") || "string";
        const rows = Array.from(tbody.querySelectorAll("tr"));

        if (currentSortIndex === index) {
          currentSortDir = currentSortDir === "asc" ? "desc" : "asc";
        } else {
          currentSortIndex = index;
          currentSortDir = "asc";
        }

        headers.forEach((h, i) => {
          h.classList.remove("sorted-asc", "sorted-desc");
          if (i === currentSortIndex) {
            h.classList.add(currentSortDir === "asc" ? "sorted-asc" : "sorted-desc");
          }
        });

        rows.sort((rowA, rowB) => {
          const a = getCellValue(rowA, index);
          const b = getCellValue(rowB, index);
          return compareValues(a, b, type, currentSortDir);
        });

        rows.forEach(row => tbody.appendChild(row));
      }

      headers.forEach((header, index) => {
        header.addEventListener("click", () => sortByColumn(index));
      });

      function applySearchFilter() {
        const term = (filterInput.value || "").toLowerCase().trim();
        const rows = Array.from(tbody.querySelectorAll("tr"));

        rows.forEach(row => {
          const haystack = row.getAttribute("data-search") || "";
          const rowStatus = row.getAttribute("data-status") || "";
          const matchesSearch = !term || haystack.includes(term);
          const matchesStatus = statusFilter === "all" || rowStatus === statusFilter;
          row.style.display = matchesSearch && matchesStatus ? "" : "none";
        });
      }

      filterInput.addEventListener("input", applySearchFilter);

      const modal = document.getElementById("log-modal");
      const modalBackdrop = modal.querySelector(".log-modal-backdrop");
      const modalClose = document.getElementById("log-modal-close");
      const modalTaskId = document.getElementById("log-modal-task-id");
      const modalStatus = document.getElementById("log-modal-status");
      const modalMeta = document.getElementById("log-modal-meta");
      const modalEntries = document.getElementById("log-modal-entries");

      function openModalForTask(taskId) {
        const task = TASKS.find(t => t.task_id === taskId) || null;
        const entries = task ? (task.updates || []) : [];
        modalTaskId.textContent = "Task ID: " + (taskId || "");
        modalStatus.textContent = task ? (task.status_label || "") : "";
        modalStatus.className = "status-indicator " + (task ? (task.status_norm || "backlog") : "backlog");
        modalMeta.innerHTML = "";

        const metaItems = [
          ["Task Name", cleanValue(task ? task.task_name : "")],
          ["Priority", cleanValue(task ? task.priority : "")],
          ["Phase", cleanValue(task ? task.phase : "")],
          ["Assignee", cleanValue(task ? task.assignee : "")],
          ["Due Date", cleanValue(task ? task.due_date : "")],
        ];

        metaItems.forEach(([label, value]) => {
          const block = document.createElement("div");
          block.className = "task-meta-block";
          block.innerHTML =
            '<div class="task-meta-label">' + label + '</div>' +
            '<div class="task-meta-value">' + (value || "") + '</div>';
          modalMeta.appendChild(block);
        });

        modalEntries.innerHTML = "";

        if (!entries.length) {
          const empty = document.createElement("div");
          empty.className = "muted";
          empty.textContent = "No updates found for this task.";
          modalEntries.appendChild(empty);
        } else {
          const list = document.createElement("div");
          list.className = "log-entry-list";

          entries.forEach((entry) => {
            const row = document.createElement("div");
            row.className = "log-entry-row";

            const ts = document.createElement("div");
            ts.className = "log-entry-timestamp";
            ts.textContent = entry.timestamp || "";

            const details = document.createElement("div");
            details.className = "log-entry-details";
            details.textContent = entry.details || "";

            row.appendChild(ts);
            row.appendChild(details);
            list.appendChild(row);
          });

          modalEntries.appendChild(list);
        }

        modal.classList.remove("hidden");
      }

      function closeModal() {
        modal.classList.add("hidden");
      }

      function wireRowClicks() {
        const rows = table.querySelectorAll("tbody tr");
        rows.forEach((row) => {
          row.classList.add("task-row");
          row.addEventListener("click", function() {
            const taskId = this.getAttribute("data-task-id") || "";
            openModalForTask(taskId);
          });
        });
      }

      wireRowClicks();

      function setStatusFilter(next) {
        statusFilter = next;
        summaryCards.forEach(card => {
          card.classList.toggle("active", card.getAttribute("data-filter") === statusFilter);
        });
        applySearchFilter();
      }

      summaryCards.forEach(card => {
        card.addEventListener("click", () => {
          const next = card.getAttribute("data-filter") || "all";
          if (statusFilter === next) {
            setStatusFilter("all");
          } else {
            setStatusFilter(next);
          }
        });
      });

      modalBackdrop.addEventListener("click", closeModal);
      modalClose.addEventListener("click", closeModal);
      document.addEventListener("keydown", function(evt) {
        if (evt.key === "Escape") {
          closeModal();
        }
      });
    })();
  </script>
</body>
</html>
"""

    tmpl = Template(template_str)
    html_str = tmpl.substitute(
        TITLE=escape(title),
        TITLE_ESC=escape(title),
        SUBTITLE_HTML=subtitle_html,
        SUMMARY_HTML=summary_html,
        HEADER_HTML=header_html,
        BODY_HTML=body_html,
        TASKS_JS=tasks_js,
    )
    return html_str


# ---------------------------------------------------------------------
# Main build logic
# ---------------------------------------------------------------------

def build_dashboard(job: Dict[str, Any]) -> None:
    name = job.get("name", "tasks")

    inputs = job.get("inputs") or []
    outputs = job.get("outputs") or []
    if not isinstance(inputs, list):
        inputs = [inputs]
    if not isinstance(outputs, list):
        outputs = [outputs]

    source_csv = job.get("source_csv") or (inputs[0] if inputs else None)
    output_html = job.get("output_html") or (outputs[0] if outputs else None)

    if not source_csv or not output_html:
        logger.error(
            "dashboard_missing_paths,name=%s,source_csv=%s,output_html=%s",
            name,
            source_csv,
            output_html,
        )
        return

    source_csv = resolve_vtx_path(source_csv)
    output_html = resolve_vtx_path(output_html)

    if not source_csv or not output_html:
        logger.error(
            "dashboard_missing_paths,name=%s,source_csv=%s,output_html=%s",
            name,
            source_csv,
            output_html,
        )
        return

    if not os.path.exists(source_csv):
        logger.error("dashboard_source_not_found,name=%s,path=%s", name, source_csv)
        return

    try:
        df = pd.read_csv(source_csv)
    except Exception as exc:
        logger.error("dashboard_failed_to_read_source,name=%s,path=%s,error=%s", name, source_csv, exc)
        return

    df, logs_by_key = enrich_with_log(df, job)

    html = generate_html(df, job, logs_by_key)

    os.makedirs(os.path.dirname(output_html), exist_ok=True)
    with open(output_html, "w", encoding="utf-8") as f:
        f.write(html)

    logger.info(
        "dashboard_written,name=%s,source_rows=%s,output=%s",
        name,
        len(df),
        output_html,
    )


# ---------------------------------------------------------------------
# CLI / Options
# ---------------------------------------------------------------------

@dataclass
class Options:
    config_path: Path
    job: str = ""


def parse_args(argv: Optional[list[str]] = None) -> Options:
    p = argparse.ArgumentParser(description="VTX visual tasks dashboard (job-oriented)")
    p.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Path to YAML config (default: {DEFAULT_CONFIG_PATH})",
    )
    p.add_argument("--job", default="", help='Run a single job by id (exact match)')
    args = p.parse_args(argv)

    cfg_path = vtx_path(args.config, must_exist=True)
    return Options(config_path=cfg_path, job=str(args.job or "").strip())


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    opt = parse_args(argv)
    logger.info("VTX_ROOT=%s", VTX_ROOT)
    logger.info("Config=%s", opt.config_path)

    cfg = load_yaml(opt.config_path)
    payload = cfg.get("payload") if isinstance(cfg, dict) else {}
    if not isinstance(payload, dict):
        payload = {}

    jobs = payload.get("jobs") or []
    if jobs is None:
        jobs = []
    if not isinstance(jobs, list):
        raise ValueError("config.payload.jobs must be a list")

    if not jobs:
        logger.error("visual_tasks_dashboard_no_jobs_defined,yaml=%s", opt.config_path)
        return 0

    if opt.job:
        target = opt.job
        found = False
        for job in jobs:
            if not isinstance(job, dict):
                continue
            jid = str(job.get("id") or "").strip()
            if jid == target:
                build_dashboard(job)
                found = True
                break
        if not found:
            raise SystemExit(f"Job not found: {target}.")
        return 0

    for job in jobs:
        if not isinstance(job, dict):
            continue
        try:
            build_dashboard(job)
        except Exception as exc:
            name = job.get("name", "tasks")
            logger.error("visual_tasks_dashboard_failed,name=%s,error=%s", name, exc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
