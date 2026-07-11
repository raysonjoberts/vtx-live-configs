#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
visual_program_attribute_mapping_vtx.py
--------------------------------------
Purpose:
  Render program-attribute mapping dashboards from prepared CSV inputs
  using the VTX universal config template (payload.jobs).

Inputs:
  - Config YAML (VTX universal template)
  - One CSV per job listed under inputs

Outputs:
  - One HTML file per job listed under outputs
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml  # PyYAML
except Exception as e:
    raise SystemExit("Missing dependency: pyyaml. Install it in the VTX venv.") from e

try:
    import pandas as pd  # type: ignore
except Exception as e:
    raise SystemExit("Missing dependency: pandas. Install it in the VTX venv.") from e


# ---------------------------------------------------------------------
# Globals / Paths (VTX style)
# ---------------------------------------------------------------------

def resolve_vtx_root() -> Path:
    """
    Determine VTX_ROOT in a cross-platform way.
    Priority:
      1) env VTX_ROOT
      2) env BTDM_ROOT (legacy)
      3) infer relative to this file: <VTX_ROOT>/usr/scripts/dashboards/<script>.py
    """
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    here = Path(__file__).resolve()
    inferred = here.parents[3]  # dashboards/<script>.py -> scripts -> usr -> VTX_ROOT
    return inferred


VTX_ROOT = resolve_vtx_root()

DEFAULT_CONFIG_DIR = VTX_ROOT / "usr" / "config" / "default"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "visual_program_attribute_mapping_vtx.yaml"


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
        s = os.path.expandvars(s)
        s = os.path.expanduser(s)

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
    """Prefer vtx_logging if present; fall back to stdlib."""
    lib_dir = VTX_ROOT / "usr" / "lib"
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))

    for mod_name in ("vtx_logging", "btdm_logging"):
        try:
            mod = __import__(mod_name)  # type: ignore
            if hasattr(mod, "get_logger"):
                return mod.get_logger(component=component)  # type: ignore
        except Exception:
            pass

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    return logging.getLogger(component)


logger = get_logger(component="visual_program_attribute_mapping_vtx")


# ---------------------------------------------------------------------
# Config loading / job selection
# ---------------------------------------------------------------------

def load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        msg = f"visual_program_attribute_mapping_config_not_found,path={path}"
        logger.error(msg)
        print(msg)
        raise FileNotFoundError(msg)

    logger.info(f"visual_program_attribute_mapping_config_found,path={path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def coerce_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def extract_jobs(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Preferred (VTX template):
      cfg["config"]["payload"]["jobs"] -> list[dict]
    Legacy support:
      - top-level cfg["reports"] -> list[dict]
      - cfg["config"]["payload"]["reports"] -> list[dict]
    """
    try:
        jobs = cfg.get("config", {}).get("payload", {}).get("jobs", None)
        if jobs is not None:
            return [j for j in coerce_list(jobs) if isinstance(j, dict)]
    except Exception:
        pass

    try:
        reps = cfg.get("config", {}).get("payload", {}).get("reports", None)
        if reps is not None:
            return [r for r in coerce_list(reps) if isinstance(r, dict)]
    except Exception:
        pass

    reps = cfg.get("reports", None)
    if reps is not None:
        return [r for r in coerce_list(reps) if isinstance(r, dict)]

    return []


def job_id(job: Dict[str, Any]) -> str:
    return str(job.get("id") or job.get("name") or "").strip()


def select_jobs(all_jobs: List[Dict[str, Any]], wanted: Optional[str]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    if not wanted:
        return all_jobs, None

    wanted_norm = wanted.strip()
    matches = [j for j in all_jobs if job_id(j) == wanted_norm]
    if matches:
        return matches, None

    ci_matches = [j for j in all_jobs if job_id(j).lower() == wanted_norm.lower()]
    if ci_matches:
        return ci_matches, None

    return [], f"No job found matching '{wanted_norm}'."


# ---------------------------------------------------------------------
# Data + HTML
# ---------------------------------------------------------------------

BLOCK_ORDER: Dict[str, int] = {
    "Discover": 1,
    "Program Strategy": 2,
    "Rationalize": 3,
    "Migration Planning": 4,
    "Migration Execution": 5,
}


def normalize_flags(flag_value) -> set[str]:
    if flag_value is None or (isinstance(flag_value, float) and math.isnan(flag_value)):
        return set()
    if not isinstance(flag_value, str):
        flag_value = str(flag_value)
    parts = [p.strip() for p in flag_value.split(",")]
    return {p for p in parts if p}


FLAG_DEFINITIONS: Dict[str, Dict[str, str]] = {
    "Low Utilization": {
        "label": "Low Utilization",
        "description": (
            "This field is utilized 20% of the time or less in the data. "
            "Low utilization is an indicator that this field is not currently "
            "used in any existing processes and as a result, there are no "
            "checks in place to ensure this data remains accurate. Not only "
            "is there missing data but the accuracy of the data that is there "
            "should be considered questionable."
        ),
    },
    "High Generic Values": {
        "label": "High Generic Values",
        "description": (
            "At least 50% of the values entered for this field are either "
            "Unknown, Custom, Other or NA. This likely indicates that either "
            "there is a default, nondescript value in place for this field "
            "which is always changed or the list of choices the user has to "
            "choose from are not defined well enough for them to define a match. "
            "When a large amount of values are Generic Values, the accuracy of "
            "the data is questionable."
        ),
    },
    "High Unique Values Warning": {
        "label": "High Unique Values Warning",
        "description": (
            "At least 33% of the values in this field are unique. A high "
            "percentage of Unique Values indicates this field is likely a "
            "freeform text field, allowing users to enter subjective "
            "information which may be difficult to use to make objective "
            "measurements, making the accuracy of this data questionable."
        ),
    },
    "Variance Warning": {
        "label": "Variance Warning",
        "description": (
            "The values of the data in this field varies in length "
            "considerably, possibly indicating a freeform text field where "
            "users are typing narrative statements rather than choosing from "
            "predefined values or using standard nomenclature. Narrative "
            "statements are subjective and that may make it difficult to make "
            "objective measurements, making the accuracy of this field "
            "questionable."
        ),
    },
}


def safe_get(row, col, default=""):
    return row[col] if col in row and pd.notna(row[col]) else default


def generate_html(df: pd.DataFrame, title: str, subtitle: str | None = None) -> str:
    required_cols = [
        "Program Attribute",
        "Field Name",
        "Priority",
        "Total Rows",
        "Used %",
        "Unique Count",
        "Value Match Ratio",
        "Flags",
    ]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(f"Missing expected column(s) in mapping DataFrame: {missing}")

    body_rows_html: List[str] = []

    for _, r in df.iterrows():
        program_attr = safe_get(r, "Program Attribute", "")
        field_name = safe_get(r, "Field Name", "")
        priority = safe_get(r, "Priority", "")

        if priority is not None and priority != "":
            try:
                num = float(priority)
                if num.is_integer():
                    priority = str(int(num))
                else:
                    priority = str(priority)
            except Exception:
                priority = str(priority)

        total_rows = safe_get(r, "Total Rows", "")
        used_pct = safe_get(r, "Used %", "")
        unique_count = safe_get(r, "Unique Count", "")
        value_match_ratio = safe_get(r, "Value Match Ratio", "")
        flags = normalize_flags(safe_get(r, "Flags", ""))

        def fmt_int(val):
            try:
                return f"{int(val):,}"
            except Exception:
                return escape(str(val))

        def fmt_pct_from_ratio(val):
            try:
                num = float(val)
                return f"{num * 100:0.1f}%"
            except Exception:
                return escape(str(val))

        def fmt_ratio(val):
            try:
                num = float(val)
                return f"{num * 100:0.1f}%"
            except Exception:
                return escape(str(val))

        total_rows_disp = fmt_int(total_rows)
        used_pct_disp = fmt_pct_from_ratio(used_pct)
        unique_count_disp = fmt_int(unique_count)
        value_match_disp = fmt_ratio(value_match_ratio)

        flag_cells_html: List[str] = []
        for key in [
            "Low Utilization",
            "High Generic Values",
            "High Unique Values Warning",
            "Variance Warning",
        ]:
            has_flag = key in flags
            flag_def = FLAG_DEFINITIONS[key]
            if has_flag:
                cell_html = (
                    f'<td class="flag-cell flag-bad">'
                    f'<span class="flag-emoji" title="{escape(flag_def["description"])}">✖</span>'
                    f"</td>"
                )
            else:
                cell_html = (
                    '<td class="flag-cell flag-good">'
                    '<span class="flag-emoji">✔</span>'
                    "</td>"
                )
            flag_cells_html.append(cell_html)

        row_html = (
            "<tr>"
            f'<td>{escape(str(program_attr))}</td>'
            f'<td>{escape(str(field_name))}</td>'
            f'<td data-value="{escape(str(priority))}" class="numeric">{escape(str(priority)) if priority != "" else ""}</td>'
            f'<td data-value="{escape(str(total_rows))}" class="numeric">{total_rows_disp}</td>'
            f'<td data-value="{escape(str(used_pct))}" class="numeric">{used_pct_disp}</td>'
            f'<td data-value="{escape(str(unique_count))}" class="numeric">{unique_count_disp}</td>'
            f'<td data-value="{escape(str(value_match_ratio))}" class="numeric">{value_match_disp}</td>'
            + "".join(flag_cells_html) +
            "</tr>"
        )
        body_rows_html.append(row_html)

    subtitle_html = (
        f'<div class="muted">{escape(subtitle)}</div>' if subtitle else ""
    )

    html_str = f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>{escape(title)}</title>
  <style>
    :root {{
      --bg:#050816;
      --panel:#0b1021;
      --ink:#e4ecff;
      --ink-muted:#a8b3d9;
      --border-soft:rgba(148,163,184,0.35);
      --border-strong:rgba(148,163,184,0.7);
      --accent:#ffd86b;
      --accent-soft:rgba(250,204,21,0.2);
      --accent-amber:#fbbf24;
      --danger-soft:#3b0a18;
      --danger:#fb7185;
    }}

    * {{
      box-sizing:border-box;
    }}

    body {{
      margin:0;
      padding:24px 32px 40px;
      font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
      background:radial-gradient(circle at top left,#1e293b 0,#020617 55%);
      color:var(--ink);
    }}

    .header {{
      font-size:24px;
      font-weight:600;
      margin-bottom:4px;
      letter-spacing:0.02em;
    }}

    .muted {{
      font-size:13px;
      color:var(--ink-muted);
      margin-bottom:16px;
    }}

    .tile {{
      background:radial-gradient(circle at 0 0,rgba(129,140,248,0.35),transparent 55%),
                 radial-gradient(circle at 100% 0,rgba(14,165,233,0.35),transparent 55%),
                 #020617;
      border-radius:18px;
      border:1px solid rgba(148,163,184,0.45);
      padding:16px 18px 18px;
      box-shadow:0 18px 45px rgba(15,23,42,0.9);
    }}

    .label {{
      display:inline-flex;
      align-items:center;
      gap:6px;
      font-size:11px;
      text-transform:uppercase;
      letter-spacing:0.2em;
      color:var(--ink-muted);
      margin-bottom:10px;
    }}

    .badge-dot {{
      width:8px;
      height:8px;
      border-radius:999px;
      background:radial-gradient(circle at 30% 30%,#bef264,#22c55e);
      box-shadow:0 0 12px rgba(74,222,128,0.9);
    }}

    .summary {{
      display:flex;
      flex-wrap:wrap;
      gap:16px;
      margin-bottom:12px;
    }}

    .summary-item {{
      padding:8px 10px;
      border-radius:12px;
      border:1px solid var(--border-soft);
      background:rgba(15,23,42,0.85);
      min-width:180px;
    }}

    .summary-label {{
      font-size:11px;
      color:var(--ink-muted);
      text-transform:uppercase;
      letter-spacing:0.12em;
      margin-bottom:4px;
    }}

    .summary-value {{
      font-size:18px;
      font-weight:600;
      color:var(--accent-amber);
    }}

    .controls {{
      display:flex;
      flex-wrap:wrap;
      gap:10px;
      margin-bottom:12px;
      align-items:center;
      justify-content:space-between;
    }}

    .controls-left,
    .controls-right {{
      display:flex;
      flex-wrap:wrap;
      gap:8px;
      align-items:center;
    }}

    .search-input {{
      min-width:240px;
      padding:6px 10px;
      border-radius:999px;
      border:1px solid var(--border-soft);
      background:rgba(15,23,42,0.85);
      color:var(--ink);
      font-size:12px;
      outline:none;
    }}

    .search-input::placeholder {{
      color:var(--ink-muted);
    }}

    .pill-checkbox {{
      display:inline-flex;
      align-items:center;
      gap:6px;
      padding:4px 9px;
      border-radius:999px;
      border:1px solid var(--border-soft);
      background:rgba(15,23,42,0.9);
      color:var(--ink-muted);
      font-size:11px;
    }}

    .pill-checkbox input {{
      width:14px;
      height:14px;
    }}

    .pill-checkbox span {{
      font-size:11px;
    }}

    .table-wrapper {{
      margin-top:10px;
      border-radius:14px;
      overflow:hidden;
      border:1px solid rgba(148,163,184,0.4);
      box-shadow:0 22px 45px rgba(15,23,42,0.9);
    }}

    table.data {{
      width:100%;
      border-collapse:collapse;
      font-size:12px;
      background:linear-gradient(180deg,rgba(15,23,42,0.96),#020617);
      table-layout:fixed;
    }}

    table.data thead tr:first-child th {{
      background:linear-gradient(90deg,#020617,#1e293b,#020617);
      padding:8px 8px 6px;
      font-size:11px;
      text-transform:uppercase;
      letter-spacing:0.22em;
      color:#c4d3ff;
      border-bottom:1px solid rgba(148,163,184,0.6);
    }}

    table.data thead tr:nth-child(2) th {{
      font-size:11px;
      text-transform:uppercase;
      letter-spacing:0.14em;
      padding:6px 8px;
      border-bottom:1px solid rgba(148,163,184,0.45);
      background:radial-gradient(circle at top,#020617,#020617 45%,#020617);
    }}

    table.data th,
    table.data td {{
      padding:6px 8px;
      border-bottom:1px solid rgba(30,41,59,0.9);
      white-space:nowrap;
      overflow:hidden;
      text-overflow:ellipsis;
    }}

    table.data tbody tr:last-child td {{
      border-bottom:none;
    }}

    table.data tbody tr:hover {{
      background:rgba(148,163,184,0.08);
    }}

    table.data th {{
      font-weight:600;
      color:#c5d0ff;
      position:sticky;
      top:0;
      background:#151a30;
      cursor:pointer;
    }}

    table.data th[data-type=\"number\"],
    table.data td.numeric {{
      text-align:center !important;
      font-variant-numeric:tabular-nums;
    }}

    table.data th[data-type=\"flag\"],
    table.data td.flag-cell {{
      text-align:center !important;
    }}

    table.data th.sort-asc::after {{
      content:\"▲\";
      font-size:9px;
      margin-left:3px;
    }}

    table.data th.sort-desc::after {{
      content:\"▼\";
      font-size:9px;
      margin-left:3px;
    }}

    .flag-group-header {{
      background:var(--danger-soft);
      color:var(--danger);
      font-weight:600;
      text-align:center;
      border-top:1px solid rgba(248,113,113,0.55);
      border-bottom:2px solid rgba(248,113,113,0.75);
      letter-spacing:0.16em;
      text-transform:uppercase;
      font-size:11px !important;
    }}

    .flag-cell {{
      width:46px;
    }}

    .flag-emoji {{
      display:inline-flex;
      align-items:center;
      justify-content:center;
      width:24px;
      height:24px;
      border-radius:999px;
      font-size:14px;
      font-weight:700;
    }}

    .flag-good .flag-emoji {{
      background:#102920;
      color:#4ade80;
      box-shadow:0 0 0 1px rgba(74,222,128,0.35),
                 0 0 10px rgba(34,197,94,0.95);
    }}

    .flag-bad .flag-emoji {{
      background:#2b1114;
      color:#f97373;
      box-shadow:0 0 0 1px rgba(249,115,115,0.55),
                 0 0 12px rgba(249,115,115,0.45);
    }}

    .circle-red {{
      position:relative;
      display:inline-block;
      padding:0 3px;
    }}

    .circle-red::after {{
      content:\"\";
      position:absolute;
      inset:-3px -4px;
      border-radius:999px;
      border:2px solid rgba(248,113,113,0.92);
      box-shadow:0 0 18px rgba(248,113,113,0.85);
      pointer-events:none;
    }}

    .circle-orange {{
      position:relative;
      display:inline-block;
      padding:0 3px;
    }}

    .circle-orange::after {{
      content:\"\";
      position:absolute;
      inset:-3px -4px;
      border-radius:999px;
      border:2px solid rgba(251,146,60,0.95);
      box-shadow:0 0 18px rgba(251,146,60,0.9);
      pointer-events:none;
    }}

    .not-mapped {{
      font-style:italic;
      color:var(--danger);
    }}

    @media (max-width:900px) {{
      body {{
        padding:16px;
      }}
      .tile {{
        padding:12px 12px 14px;
      }}
      .search-input {{
        min-width:180px;
      }}
      table.data {{
        font-size:11px;
      }}
      table.data th,
      table.data td {{
        padding:4px 6px;
      }}
    }}

    .pill-checkbox input {{
      accent-color:#ffd86b;
    }}
  </style>
</head>
<body>
  <div class=\"header\">{escape(title)}</div>
  {subtitle_html}

  <div class=\"tile\">
    <div class=\"label\">
      <span class=\"badge-dot\"></span>
      <span>Field quality overview (mapped attributes)</span>
    </div>

    <div class=\"summary\">
      <div class=\"summary-item\">
        <div class=\"summary-label\">Total Program Attributes</div>
        <div id=\"stat-total-attrs\" class=\"summary-value\">0</div>
      </div>
      <div class=\"summary-item\">
        <div class=\"summary-label\">Not Mapped</div>
        <div id=\"stat-not-mapped\" class=\"summary-value\">0</div>
      </div>
      <div class=\"summary-item\">
        <div class=\"summary-label\">Mapped with Flags</div>
        <div id=\"stat-mapped-with-flags\" class=\"summary-value\">0</div>
      </div>
    </div>

    <div class=\"controls\">
      <div class=\"controls-left\">
        <input id=\"filter-input\" class=\"search-input\" type=\"text\"
               placeholder=\"Filter by Program Attribute or Field Name...\" />

        <label class=\"pill-checkbox\">
          <input type=\"checkbox\" id=\"flagged-only\" />
          <span>Show only fields with issues</span>
        </label>

        <label class=\"pill-checkbox\">
          <input type=\"checkbox\" id=\"clean-only\" />
          <span>Show only fields without issues</span>
        </label>
      </div>

      <div class=\"controls-right\">
        <label class=\"pill-checkbox\">
          <input type=\"checkbox\" id=\"hide-general\" />
          <span>Hide general attributes</span>
        </label>

        <label class=\"pill-checkbox\">
          <input type=\"checkbox\" id=\"priority-one-only\" />
          <span>Show Priority 1 only</span>
        </label>
      </div>

    <div class=\"table-wrapper\">
      <table class=\"data\" id=\"quality-table\">
        <thead>
          <tr>
            <th colspan=\"6\"></th>
            <th colspan=\"4\" class=\"flag-group-header\">Validity Flags</th>
          </tr>
          <tr id=\"real-header-row\">
            <th data-type=\"string\">Program Attribute</th>
            <th data-type=\"string\">Field Name</th>
            <th data-type=\"number\">Priority</th>
            <th data-type=\"number\">Total Rows</th>
            <th data-type=\"number\">Used %</th>
            <th data-type=\"number\">Unique Count</th>
            <th data-type=\"number\">Value Match Ratio</th>
            <th data-type=\"flag\">Low Utilization</th>
            <th data-type=\"flag\">High Generic Values</th>
            <th data-type=\"flag\">High Unique Values Warning</th>
            <th data-type=\"flag\">Variance Warning</th>
          </tr>
        </thead>
        <tbody>
          {"".join(body_rows_html)}
        </tbody>
      </table>
    </div>
  </div>

  <script>
    (function() {{
      const table = document.getElementById("quality-table");
      const tbody = table.tBodies[0];
      const realHeader = document.getElementById("real-header-row").children;

      const filterInput = document.getElementById("filter-input");
      const flaggedOnlyCheckbox = document.getElementById("flagged-only");
      const cleanOnlyCheckbox = document.getElementById("clean-only");
      const hideGeneralCheckbox = document.getElementById("hide-general");
      const priorityOneCheckbox = document.getElementById("priority-one-only");

      const statTotal = document.getElementById("stat-total-attrs");
      const statNotMapped = document.getElementById("stat-not-mapped");
      const statMappedWithFlags = document.getElementById("stat-mapped-with-flags");

      let currentSortIndex = null;
      let currentSortDir = "asc";

      function getCellValue(row, idx) {{
        const cell = row.children[idx];
        if (!cell) return "";
        const dataValue = cell.getAttribute("data-value");
        return dataValue !== null ? dataValue : cell.textContent || "";
      }}

      function compareValues(a, b, type, direction) {{
        if (type === "number") {{
          const na = parseFloat(a);
          const nb = parseFloat(b);
          const A = isNaN(na) ? -Infinity : na;
          const B = isNaN(nb) ? -Infinity : nb;
          return direction === "asc" ? A - B : B - A;
        }}

        if (type === "flag") {{
          const score = v => v.includes("✖") ? 1 : 0;
          return direction === "asc" ? score(a) - score(b) : score(b) - score(a);
        }}

        return direction === "asc"
          ? a.localeCompare(b)
          : b.localeCompare(a);
      }}

      function sortByColumn(index, type) {{
        const rows = Array.from(tbody.querySelectorAll("tr"));

        const direction = (
          currentSortIndex === index && currentSortDir === "asc"
        ) ? "desc" : "asc";

        currentSortIndex = index;
        currentSortDir = direction;

        rows
          .sort((rowA, rowB) => {{
            const a = getCellValue(rowA, index);
            const b = getCellValue(rowB, index);
            return compareValues(a, b, type, direction);
          }})
          .forEach(row => tbody.appendChild(row));

        Array.from(realHeader).forEach(th =>
          th.classList.remove("sort-asc", "sort-desc")
        );

        realHeader[index].classList.add(
          direction === "asc" ? "sort-asc" : "sort-desc"
        );
      }}

      function wrapWithClass(cell, className) {{
        if (!cell) return;
        if (cell.querySelector("." + className)) return;
        const text = cell.textContent;
        cell.innerHTML = '<span class="' + className + '">' + text + '</span>';
      }}

      function applyHighlighting() {{
        const rows = Array.from(tbody.querySelectorAll("tr"));

        rows.forEach(row => {{
          const cells = row.children;

          const attributeCell = cells[0];
          const fieldNameCell = cells[1];
          const priorityCell = cells[2];
          const usedPctCell = cells[4];
          const uniqueCountCell = cells[5];
          const valueMatchCell = cells[6];

          const attributeRaw = attributeCell ? attributeCell.textContent.trim() : "";
          const fieldNameRaw = fieldNameCell ? fieldNameCell.textContent.trim() : "";

          const usedPctRaw = usedPctCell ? usedPctCell.getAttribute("data-value") : null;
          const uniqueRaw = uniqueCountCell ? uniqueCountCell.getAttribute("data-value") : null;
          const valueMatchRaw = valueMatchCell ? valueMatchCell.getAttribute("data-value") : null;

          const usedPct = usedPctRaw !== null ? parseFloat(usedPctRaw) : NaN;
          const uniqueCount = uniqueRaw !== null ? parseFloat(uniqueRaw) : NaN;
          const valueMatchRatio = valueMatchRaw !== null ? parseFloat(valueMatchRaw) : NaN;

          const lowUtilCell = cells[7];
          const highGenericCell = cells[8];
          const nonUniqueCell = cells[9];
          const varianceCell = cells[10];

          const isNotMapped = fieldNameRaw.toLowerCase() === "not mapped";

          if (isNotMapped && fieldNameCell) {{
            fieldNameCell.classList.add("not-mapped");
            [lowUtilCell, highGenericCell, nonUniqueCell, varianceCell].forEach(cell => {{
              if (cell) cell.textContent = "";
            }});
          }}

          const hasLowUtil = lowUtilCell && lowUtilCell.textContent.includes("✖");
          const hasHighGeneric = highGenericCell && highGenericCell.textContent.includes("✖");
          const hasNonUnique = nonUniqueCell && nonUniqueCell.textContent.includes("✖");

          if (hasLowUtil) {{
            wrapWithClass(usedPctCell, "circle-red");
          }}

          if (hasHighGeneric && attributeRaw === "General Unknown/Custom/Other fields") {{
            wrapWithClass(attributeCell, "circle-red");
          }}

          if (hasNonUnique) {{
            wrapWithClass(uniqueCountCell, "circle-red");
          }}

          const isNoMatch = attributeRaw === "No Match";
          const isGeneralAttr = attributeRaw.startsWith("General ");
          if (!isNoMatch && !isGeneralAttr && !isNaN(valueMatchRatio) && valueMatchRatio <= 0.80) {{
            wrapWithClass(valueMatchCell, "circle-orange");
          }}
        }});
      }}

      function applyFilters() {{
        const query = (filterInput.value || "").toLowerCase();
        const flaggedOnly = flaggedOnlyCheckbox.checked;
        const cleanOnly = cleanOnlyCheckbox.checked;
        const hideGeneral = hideGeneralCheckbox.checked;
        const priorityOnly = priorityOneCheckbox && priorityOneCheckbox.checked;

        const rows = Array.from(tbody.querySelectorAll("tr"));

        rows.forEach(row => {{
          const cells = row.children;

          const programAttr = cells[0] ? cells[0].textContent.toLowerCase() : "";
          const fieldName = cells[1] ? cells[1].textContent.toLowerCase() : "";
          const priorityText = cells[2] ? cells[2].textContent.trim() : "";

          const flagsText = Array.from(cells)
            .slice(7)
            .map(c => c.textContent)
            .join(" ")
            .toLowerCase();

          let matchesText = true;
          if (query) {{
            matchesText =
              programAttr.includes(query) ||
              fieldName.includes(query) ||
              flagsText.includes(query);
          }}

          let hasIssue = flagsText.includes("✖");

          let visible = matchesText;
          if (flaggedOnly) visible = visible && hasIssue;
          if (cleanOnly) visible = visible && !hasIssue;
          if (priorityOnly) visible = visible && priorityText === "1";

          if (hideGeneral && programAttr.startsWith("general ")) {{
            visible = false;
          }}

          row.style.display = visible ? "" : "none";
        }});
      }}

      function updateStats() {{
        const rows = Array.from(tbody.querySelectorAll("tr"));

        let totalAttrs = 0;
        let notMappedCount = 0;
        let mappedWithFlags = 0;

        rows.forEach(row => {{
          totalAttrs += 1;
          const cells = row.children;
          const fieldName = cells[1] ? cells[1].textContent.trim().toLowerCase() : "";
          const isNotMapped = fieldName === "not mapped";

          if (isNotMapped) {{
            notMappedCount += 1;
          }}

          const flagsText = Array.from(cells)
            .slice(7)
            .map(c => c.textContent)
            .join(" ");

          const hasFlag = flagsText.includes("✖");

          if (!isNotMapped && hasFlag) {{
            mappedWithFlags += 1;
          }}
        }});

        if (statTotal) statTotal.textContent = totalAttrs;
        if (statNotMapped) statNotMapped.textContent = notMappedCount;
        if (statMappedWithFlags) statMappedWithFlags.textContent = mappedWithFlags;
      }}

      filterInput.addEventListener("input", applyFilters);

      flaggedOnlyCheckbox.addEventListener("change", () => {{
        if (flaggedOnlyCheckbox.checked) cleanOnlyCheckbox.checked = false;
        applyFilters();
      }});

      cleanOnlyCheckbox.addEventListener("change", () => {{
        if (cleanOnlyCheckbox.checked) flaggedOnlyCheckbox.checked = false;
        applyFilters();
      }});

      hideGeneralCheckbox.addEventListener("change", applyFilters);

      if (priorityOneCheckbox) {{
        priorityOneCheckbox.addEventListener("change", applyFilters);
      }}

      Array.from(realHeader).forEach((th, index) => {{
        th.addEventListener("click", () => {{
          sortByColumn(index, th.getAttribute("data-type"));
        }});
      }});

      applyHighlighting();
      applyFilters();
      updateStats();
    }})();
  </script>
</body>
</html>"""
    return html_str


# ---------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------

def process_job(job: Dict[str, Any], *, dry_run: bool = False) -> None:
    name = job_id(job) or "report"

    inputs = coerce_list(job.get("inputs"))
    if not inputs:
        fallback = job.get("input_csv") or job.get("source_csv")
        inputs = coerce_list(fallback)

    outputs = coerce_list(job.get("outputs"))
    if not outputs:
        fallback = job.get("output_html") or job.get("html_output")
        outputs = coerce_list(fallback)

    if not inputs:
        raise FileNotFoundError(f"Input CSV not configured for report '{name}'")
    if not outputs:
        raise FileNotFoundError(f"Output HTML not configured for report '{name}'")

    input_csv_cfg = inputs[0]
    output_html_cfg = outputs[0]

    title = job.get("title") or "Program Attribute Mapping — Visual Overview"
    subtitle = job.get("subtitle")

    input_csv = vtx_path(input_csv_cfg, must_exist=True)
    output_html = vtx_path(output_html_cfg)

    if output_html.exists():
        input_mtime = input_csv.stat().st_mtime
        output_mtime = output_html.stat().st_mtime
        if output_mtime >= input_mtime:
            logger.info(
                "visual_program_attribute_mapping_skipped_up_to_date,"
                f"report={name},input_csv={input_csv_cfg},output_html={output_html_cfg}"
            )
            print(
                f"[visual_program_attribute_mapping] Skipping '{name}' because output is newer than input."
            )
            return

    logger.info(
        f"visual_program_attribute_mapping_load_csv_start,report={name},input_csv={input_csv}"
    )
    df = pd.read_csv(input_csv)
    logger.info(
        f"visual_program_attribute_mapping_load_csv_complete,report={name},rows={len(df)}"
    )

    html_str = generate_html(df, title=title, subtitle=subtitle)

    if dry_run:
        logger.info("visual_program_attribute_mapping_dry_run,report=%s", name)
        return

    output_html.parent.mkdir(parents=True, exist_ok=True)
    output_html.write_text(html_str, encoding="utf-8")

    logger.info(
        f"visual_program_attribute_mapping_complete,report={name},rows={len(df)}"
    )


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

@dataclass
class Options:
    config_path: Path
    job: Optional[str]
    dry_run: bool = False


def parse_args(argv: Optional[List[str]] = None) -> Options:
    p = argparse.ArgumentParser(description="Render program attribute mapping dashboards")
    p.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Path to YAML config (default: {DEFAULT_CONFIG_PATH})",
    )
    p.add_argument("--job", default=None, help="Run only the job with this id")
    p.add_argument("--dry-run", action="store_true", help="Do not write outputs")
    args = p.parse_args(argv)

    cfg_path = vtx_path(args.config, must_exist=True)
    return Options(config_path=cfg_path, job=args.job, dry_run=bool(args.dry_run))


def main(argv: Optional[List[str]] = None) -> int:
    opt = parse_args(argv)
    logger.info("VTX_ROOT=%s", VTX_ROOT)
    logger.info("Config=%s", opt.config_path)

    cfg = load_yaml(opt.config_path)
    jobs = extract_jobs(cfg)
    if not jobs:
        msg = f"visual_program_attribute_mapping_no_reports_defined,yaml={opt.config_path}"
        logger.error(msg)
        print(msg)
        return 1

    selected, err = select_jobs(jobs, opt.job)
    if err:
        logger.error(err)
        print(err)
        return 2

    logger.info("visual_program_attribute_mapping_batch_start,jobs=%d", len(selected))
    print(f"[visual_program_attribute_mapping] Using YAML_PATH={opt.config_path}")

    for report_cfg in selected:
        name = job_id(report_cfg) or "report"
        try:
            logger.info("visual_program_attribute_mapping_report_start,name=%s", name)
            print(f"[visual_program_attribute_mapping] Processing report '{name}'")
            process_job(report_cfg, dry_run=opt.dry_run)
            logger.info("visual_program_attribute_mapping_report_complete,name=%s", name)
        except Exception as e:
            logger.exception(
                "visual_program_attribute_mapping_report_failed,name=%s,error=%s",
                name,
                e,
            )
            print(f"[visual_program_attribute_mapping] ERROR processing '{name}': {e}")
            continue

    logger.info("visual_program_attribute_mapping_batch_complete")
    print("[visual_program_attribute_mapping] Batch complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
