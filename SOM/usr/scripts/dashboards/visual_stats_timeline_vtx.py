#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
visual_stats_timeline_vtx.py
----------------------------
Purpose:
  Generate timeline HTML reports from statistics CSV tables using the VTX
  universal config template (payload.jobs).

Inputs:
  - Config YAML (VTX universal template)
  - Statistics CSV files per job

Outputs:
  - HTML timeline reports in the configured output locations
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

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
      3) infer relative to this file: <VTX_ROOT>/usr/scripts/dashboards/<script>.py
    """
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    here = Path(__file__).resolve()
    inferred = here.parents[3]  # dashboards/<script>.py -> scripts -> usr -> VTX_ROOT
    return inferred


VTX_ROOT = resolve_vtx_root()

DEFAULT_CONFIG_DIR = VTX_ROOT / "usr" / "config" / "run"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "visual_stats_timeline._vtx.yaml"

_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


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

    # If still not absolute, treat as VTX-relative
    if not p.is_absolute():
        p = (VTX_ROOT / p).resolve()

    if must_exist and not p.exists():
        raise FileNotFoundError(f"Path does not exist: {p}")
    return p


def resolve_any_path(path_str: str | Path | None) -> Optional[Path]:
    if path_str is None:
        return None
    if isinstance(path_str, Path):
        if path_str.is_absolute():
            return path_str
        return vtx_path(path_str)

    s = str(path_str).strip()
    if not s:
        return None

    s = os.path.expandvars(s)
    s = os.path.expanduser(s)

    if _WINDOWS_ABS_RE.match(s) or s.startswith("\\\\") or s.startswith("//"):
        return Path(s)
    if s.startswith("/"):
        return Path(s)

    return vtx_path(s)


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


logger = get_logger(component="visual_stats_timeline_vtx")


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
# Helpers
# ---------------------------------------------------------------------

def load_program_attribute_lookup(mapping_yaml_path: str | Path | None) -> Dict[str, str]:
    """
    Loads a YAML that contains a 'program_attribute_mapping' dict like:
        program_attribute_mapping:
          CPU: VMPROCESSORS
          Hostname: SERVERNAME

    Returns an inverted dict:
        { "VMPROCESSORS": "CPU", "SERVERNAME": "Hostname" }
    """
    if not mapping_yaml_path:
        return {}

    mapping_path = resolve_any_path(mapping_yaml_path)
    if not mapping_path or not mapping_path.exists():
        logger.warning("Program attribute mapping YAML not found: %s", mapping_yaml_path)
        return {}

    try:
        mapping_cfg = yaml.safe_load(mapping_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("Failed to read mapping YAML '%s': %s", mapping_path, exc)
        return {}

    pam = mapping_cfg.get("program_attribute_mapping")
    if not isinstance(pam, dict):
        reports = mapping_cfg.get("reports")
        if isinstance(reports, list) and reports:
            for r in reports:
                if isinstance(r, dict) and isinstance(r.get("program_attribute_mapping"), dict):
                    pam = r.get("program_attribute_mapping")
                    break

    if not isinstance(pam, dict):
        return {}

    lookup: Dict[str, str] = {}
    for program_attr, source_field in pam.items():
        if not program_attr or not source_field:
            continue
        lookup[str(source_field).strip()] = str(program_attr).strip()

    return lookup


def safe_mtime(path: str | Path | None) -> float:
    if not path:
        return 0.0
    try:
        p = Path(path)
        return p.stat().st_mtime
    except OSError:
        return 0.0


def build_stats_data(df: pd.DataFrame) -> Dict[str, Any]:
    import math

    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"].astype(str), format="%Y%m%d", errors="coerce")
    df = df.dropna(subset=["Date"])
    df = df.sort_values(["Date", "Field Name"])

    unique_dates = sorted(df["Date"].unique())
    dates = [pd.Timestamp(d) for d in unique_dates]
    date_labels = [d.strftime("%Y-%m-%d") for d in dates]

    pivot_count = df.pivot(index="Date", columns="Field Name", values="Count")

    has_active = bool(
        "Active Count" in df.columns
          and df["Active Count"].fillna(0).sum() > 0
    )
    pivot_active = None
    if has_active:
        pivot_active = df.pivot(index="Date", columns="Field Name", values="Active Count")

    if "ACTIVE" in pivot_count.columns:
        total_rows_series = pivot_count["ACTIVE"].reindex(dates).fillna(0)
    else:
        total_rows_series = pivot_count.reindex(index=dates).max(axis=1).fillna(0)

    if pivot_active is not None and "ACTIVE" in pivot_active.columns:
        total_active_series = pivot_active["ACTIVE"].reindex(dates).fillna(0)
        total_active_list = [float(x) for x in total_active_series]
    else:
        total_active_series = total_rows_series.copy()
        total_active_list = [float(x) for x in total_active_series]
        has_active = False

    fields: Dict[str, Any] = {}
    for field in pivot_count.columns:
        if field == "ACTIVE":
            continue
        if field is None or (isinstance(field, float) and math.isnan(field)):
            continue

        c = pivot_count[field].reindex(dates)
        count_list = [float(x) if pd.notna(x) else None for x in c]

        if pivot_active is not None and field in pivot_active.columns:
            a = pivot_active[field].reindex(dates)
            active_list = [float(x) if pd.notna(x) else None for x in a]
        else:
            active_list = [None] * len(dates)

        fields[str(field)] = {
            "count": count_list,
            "activeCount": active_list,
        }

    stats = {
        "dates": date_labels,
        "fields": fields,
        "totalRows": [float(x) for x in total_rows_series],
        "totalActiveRows": total_active_list,
        "hasActive": has_active,
    }
    return stats



def render_html(title: str, stats_data: Dict[str, Any]) -> str:
    stats_json = json.dumps(stats_data)
    html = f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>{title}</title>
  <style>
    body {{
      font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      margin:0;
      padding:24px;
      background:#0e1222;
      color:#eaeef8;
    }}
    .header {{
      font-size:26px;
      font-weight:800;
      margin-bottom:18px;
    }}
    .grid {{
      display:grid;
      grid-template-columns: minmax(260px, 1fr) minmax(260px, 1fr);
      gap:16px;
      margin-bottom:16px;
      align-items:start;
    }}
    .tile {{
      background:#151a30;
      border-radius:12px;
      padding:16px;
      box-shadow:0 6px 24px rgba(0,0,0,0.35);
    }}
    .wide-tile {{
      margin-top:8px;
    }}
    .label {{
      font-weight:700;
      color:#c5d0ff;
      margin-bottom:6px;
    }}
    .muted {{
      color:#9aa3b2;
      font-size:12px;
    }}
    .controls-row {{
      display:flex;
      flex-direction:column;
      gap:12px;
      margin-top:8px;
    }}
    .field-group,
    .days-group, .overlay-group {{
      display:flex;
      flex-wrap:wrap;
      align-items:center;
      gap:8px;
    }}
    select, input[type=\"number\"] {{
      background:#0e1222;
      border:1px solid rgba(148,163,184,0.4);
      border-radius:999px;
      padding:4px 10px;
      color:#eaeef8;
      font-size:12px;
      outline:none;
      min-width:140px;
    }}
    input[type=\"number\"] {{
      width:90px;
    }}
    .radio-pill {{
      display:inline-flex;
      align-items:center;
      gap:4px;
      padding:4px 10px;
      border-radius:999px;
      border:1px solid rgba(148,163,184,0.4);
      font-size:11px;
      cursor:pointer;
      color:#cbd5f5;
    }}
    .radio-pill input {{
      accent-color:#6366f1;
    }}
    .summary-grid {{
      display:grid;
      grid-template-columns: repeat(3, minmax(0,1fr));
      gap:10px;
      margin-top:12px;
    }}
    .summary-item {{
      background:#111827;
      border-radius:10px;
      padding:10px 12px;
      border:1px solid rgba(148,163,184,0.35);
      min-height:80px;
    }}
    .summary-label {{
      font-size:11px;
      color:#9aa3b2;
      margin-bottom:4px;
    }}
    .summary-value {{
      font-size:28px;
      font-weight:800;
      color:#eaeef8;
      line-height:1.2;
    }}
    .util-value {{
      font-size:34px;
    }}
    .rag-green {{
      color:#22c55e;
    }}
    .rag-amber {{
      color:#facc15;
    }}
    .rag-red {{
      color:#ef4444;
    }}
    #chart {{
      width:100%;
      height:400px;
    }}
  </style>
  <script type=\"text/javascript\">window.PlotlyConfig = {{MathJaxConfig: \"local\"}};</script>
  <script src=\"https://cdn.plot.ly/plotly-3.1.0.min.js\" integrity=\"sha256-Ei4740bWZhaUTQuD6q9yQlgVCMPBz6CZWhevDYPv93A=\" crossorigin=\"anonymous\"></script>
</head>
<body>
  <div class=\"header\">{title}</div>

  <div class=\"grid\">
    <!-- Timeline controls (top-left) -->
    <div class=\"tile\">
      <div class=\"label\">Timeline controls</div>
      <div class=\"muted\">Pick a field, choose how many days to look back, and (if available) compare total vs active rows.</div>

      <div class=\"controls-row\">
        <div class=\"field-group\">
          <span class=\"muted\">Field:</span>
          <select id=\"fieldSelect\"></select>
        </div>

        <!-- NEW: Program Attribute focus toggle -->
        <div class=\"overlay-group\">
          <span class=\"muted\">Field focus:</span>
          <label class=\"radio-pill\">
            <input type=\"radio\" name=\"fieldFocus\" value=\"all\" checked />
            <span>All fields</span>
          </label>
          <label class=\"radio-pill\">
            <input type=\"radio\" name=\"fieldFocus\" value=\"program\" />
            <span>Program attributes only</span>
          </label>
        </div>

        <div class=\"days-group\">
          <span class=\"muted\">Last X days:</span>
          <input id=\"daysInput\" type=\"number\" min=\"1\" step=\"1\" value=\"90\" />
          <span class=\"muted\">(use 9999 for all history)</span>
        </div>

        <div class=\"overlay-group\">
          <span class=\"muted\">Baseline:</span>
          <label class=\"radio-pill\">
            <input type=\"radio\" name=\"overlayMode\" value=\"totalRows\" checked />
            <span>Total rows</span>
          </label>
          <label class=\"radio-pill\">
            <input type=\"radio\" name=\"overlayMode\" value=\"activeRows\" />
            <span>Active rows</span>
          </label>
        </div>
      </div>
    </div>

    <!-- Latest counts (top-right) -->
    <div class=\"tile\">
      <div class=\"label\">Latest counts</div>
      <div class=\"muted\">As of the most recent sample in the selected window.</div>

      <div class=\"summary-grid\">
        <div class=\"summary-item\">
          <div class=\"summary-label\">Non-null values (latest)</div>
          <div id=\"summaryNonNull\" class=\"summary-value\">—</div>
        </div>
        <div class=\"summary-item\">
          <div class=\"summary-label\">Baseline rows (latest)</div>
          <div id=\"summaryBaseline\" class=\"summary-value\">—</div>
        </div>
        <div class=\"summary-item\">
          <div class=\"summary-label\">Utilization (latest)</div>
          <div id=\"summaryUtil\" class=\"summary-value util-value\">—</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Full-width chart underneath -->
  <div class=\"tile wide-tile\">
    <div class=\"label\">Field utilization over time</div>
    <div id=\"chart\"></div>
  </div>

  <script>
    const STATS_DATA = {stats_json};
    const HAS_ACTIVE = !!STATS_DATA.hasActive;

    // NEW: mapping from source field name -> program attribute name
    const PROGRAM_LOOKUP = STATS_DATA.programLookup || {{}};

    function getFieldFocusMode() {{
      const radios = document.querySelectorAll('input[name="fieldFocus"]');
      for (const r of radios) {{
        if (r.checked) return r.value;
      }}
      return "all";
    }}

    function displayFieldLabel(sourceField) {{
      const p = PROGRAM_LOOKUP[sourceField];
      return p ? (p + ": " + sourceField) : sourceField;
    }}

    // UPDATED: field list respects Field focus toggle
    function getFieldList() {{
      const allFields = Object.keys(STATS_DATA.fields || {{}});
      const mode = getFieldFocusMode();

      if (mode === "program") {{
        // Only show fields that are mapped AND exist in this stats dataset
        return allFields
          .filter(f => Object.prototype.hasOwnProperty.call(PROGRAM_LOOKUP, f))
          .sort((a, b) => {{
            const pa = (PROGRAM_LOOKUP[a] || "").toLowerCase();
            const pb = (PROGRAM_LOOKUP[b] || "").toLowerCase();
            if (pa < pb) return -1;
            if (pa > pb) return 1;
            return a.localeCompare(b);
          }});
      }}

      return allFields.sort();
    }}

    function parseDate(str) {{
      // Expect YYYY-MM-DD
      const [y, m, d] = str.split("-").map(Number);
      return new Date(y, m - 1, d);
    }}

    function getFilteredIndexes(daysBack) {{
      const dates = STATS_DATA.dates || [];
      if (!dates.length) return [];
      if (!daysBack || daysBack >= 9999) {{
        return dates.map((_, idx) => idx);
      }}
      const lastDate = parseDate(dates[dates.length - 1]);
      const cutoffMs = lastDate.getTime() - daysBack * 24 * 60 * 60 * 1000;
      const idxs = [];
      for (let i = 0; i < dates.length; i++) {{
        const d = parseDate(dates[i]);
        if (d.getTime() >= cutoffMs) {{
          idxs.push(i);
        }}
      }}
      return idxs;
    }}

    function getOverlayMode() {{
      if (!HAS_ACTIVE) return "totalRows";
      const radios = document.querySelectorAll('input[name="overlayMode"]');
      for (const r of radios) {{
        if (r.checked) return r.value;
      }}
      return "totalRows";
    }}

    function formatNumber(val) {{
      if (val === null || val === undefined || Number.isNaN(val)) return "—";
      if (Math.abs(val) >= 1_000_000) {{
        return (val / 1_000_000).toFixed(1).replace(/\\.0$/, "") + "M";
      }}
      if (Math.abs(val) >= 1_000) {{
        return (val / 1_000).toFixed(1).replace(/\\.0$/, "") + "k";
      }}
      return String(Math.round(val));
    }}

    function formatPercent(val) {{
      if (val === null || val === undefined || Number.isNaN(val)) return "—";
      return val.toFixed(1).replace(/\\.0$/, "") + "%";
    }}

    function updateSummary(fieldName, idxs) {{
      const nonNullEl = document.getElementById("summaryNonNull");
      const baselineEl = document.getElementById("summaryBaseline");
      const utilEl = document.getElementById("summaryUtil");

      // Reset classes on utilization
      utilEl.classList.remove("rag-green", "rag-amber", "rag-red");

      if (!idxs.length) {{
        nonNullEl.textContent = "—";
        baselineEl.textContent = "—";
        utilEl.textContent = "—";
        return;
      }}

      const fieldData = STATS_DATA.fields[fieldName] || {{}};
      const totalSeries = fieldData.count || [];
      const activeSeries = fieldData.activeCount || [];

      const totalRows = STATS_DATA.totalRows || [];
      const activeRows = STATS_DATA.totalActiveRows || [];

      const mode = getOverlayMode();
      const nonNullSeries = (mode === "activeRows" && HAS_ACTIVE) ? activeSeries : totalSeries;
      const baselineSeries = (mode === "activeRows" && HAS_ACTIVE) ? activeRows : totalRows;

      // latest index in filtered window where we have any data
      let latest = idxs[idxs.length - 1];
      for (let i = idxs.length - 1; i >= 0; i--) {{
        const idx = idxs[i];
        if (nonNullSeries[idx] !== null && !Number.isNaN(nonNullSeries[idx])) {{
          latest = idx;
          break;
        }}
      }}

      const nonNull = nonNullSeries[latest] ?? null;
      const baseline = baselineSeries[latest] ?? null;
      const util = (baseline && baseline > 0 && nonNull !== null)
        ? (nonNull / baseline) * 100.0
        : null;

      nonNullEl.textContent = formatNumber(nonNull);
      baselineEl.textContent = formatNumber(baseline);
      utilEl.textContent = formatPercent(util);

      if (util !== null && !Number.isNaN(util)) {{
        if (util >= 95) {{
          utilEl.classList.add("rag-green");
        }} else if (util >= 50) {{
          utilEl.classList.add("rag-amber");
        }} else {{
          utilEl.classList.add("rag-red");
        }}
      }}
    }}

    function updateChart() {{
      const fieldSelect = document.getElementById("fieldSelect");
      const daysInput = document.getElementById("daysInput");
      const chartDiv = document.getElementById("chart");
      if (!fieldSelect || !chartDiv) return;

      const fieldName = fieldSelect.value;
      const daysBack = parseInt(daysInput.value, 10) || 0;

      const idxs = getFilteredIndexes(daysBack);
      const dates = STATS_DATA.dates || [];
      const fieldData = STATS_DATA.fields[fieldName] || {{}};

      const totalSeries = fieldData.count || [];
      const activeSeries = fieldData.activeCount || [];

      const totalRows = STATS_DATA.totalRows || [];
      const activeRows = STATS_DATA.totalActiveRows || [];

      const mode = getOverlayMode();
      const series = (mode === "activeRows" && HAS_ACTIVE) ? activeSeries : totalSeries;

      const x = idxs.map(i => dates[i]);
      const yField = idxs.map(i => series[i]);

      // Optional: make legend label match dropdown label (CPU: VMPROCESSORS)
      const traceLabel = displayFieldLabel(fieldName);

      const traces = [
        {{
          x,
          y: yField,
          type: "scatter",
          mode: "lines+markers",
          name: traceLabel + (mode === "activeRows" && HAS_ACTIVE ? " (active non-null)" : " (non-null rows)"),
          line: {{ shape: "spline" }},
          marker: {{ size: 5 }}
        }}
      ];

      if (mode === "totalRows" || !HAS_ACTIVE) {{
        const yBase = idxs.map(i => totalRows[i]);
        traces.push({{
          x,
          y: yBase,
          type: "scatter",
          mode: "lines",
          name: "Total rows",
          line: {{ dash: "dot", width: 2 }}
        }});
      }} else if (mode === "activeRows" && HAS_ACTIVE) {{
        const yBase = idxs.map(i => activeRows[i]);
        traces.push({{
          x,
          y: yBase,
          type: "scatter",
          mode: "lines",
          name: "Active rows",
          line: {{ dash: "dot", width: 2 }}
        }});
      }}

      const layout = {{
        margin: {{ l: 40, r: 8, t: 10, b: 32 }},
        paper_bgcolor: "rgba(0,0,0,0)",
        plot_bgcolor: "#151a30",
        xaxis: {{
          showgrid: true,
          gridcolor: "rgba(148,163,184,0.2)",
          tickfont: {{ size: 10, color: "#cbd5f5" }},
          showline: false
        }},
        yaxis: {{
          showgrid: true,
          gridcolor: "rgba(148,163,184,0.25)",
          zeroline: false,
          tickfont: {{ size: 10, color: "#cbd5f5" }}
        }},
        legend: {{
          orientation: "h",
          yanchor: "bottom",
          y: 1.02,
          xanchor: "right",
          x: 1,
          font: {{ size: 10, color: "#eaeef8" }}
        }},
        showlegend: true
      }};

      Plotly.newPlot(chartDiv, traces, layout, {{ responsive: true, displaylogo: false }});
      updateSummary(fieldName, idxs);
    }}

    function initControls() {{
      const fieldSelect = document.getElementById("fieldSelect");
      const daysInput = document.getElementById("daysInput");
      const overlayRadios = document.querySelectorAll('input[name="overlayMode"]');
      const focusRadios = document.querySelectorAll('input[name="fieldFocus"]');

      function rebuildFieldOptionsAndUpdate() {{
        const prev = fieldSelect.value;
        const fields = getFieldList();

        fieldSelect.innerHTML = "";
        for (const f of fields) {{
          const opt = document.createElement("option");
          opt.value = f;
          opt.textContent = displayFieldLabel(f);
          fieldSelect.appendChild(opt);
        }}

        if (fields.includes(prev)) {{
          fieldSelect.value = prev;
        }} else if (fields.length) {{
          fieldSelect.value = fields.includes("APP_ID") ? "APP_ID" : fields[0];
        }}

        updateChart();
      }}

      if (!HAS_ACTIVE) {{
        const baselineGroups = document.querySelectorAll('input[name="overlayMode"]');
        if (baselineGroups.length) {{
          const baselineContainer = baselineGroups[0].closest(".overlay-group");
          if (baselineContainer) baselineContainer.style.display = "none";
        }}
      }} else {{
        const activeRadio = document.querySelector('input[name="overlayMode"][value="activeRows"]');
        if (activeRadio) activeRadio.checked = true;
      }}

      fieldSelect.addEventListener("change", updateChart);
      daysInput.addEventListener("change", updateChart);
      overlayRadios.forEach(r => r.addEventListener("change", updateChart));
      focusRadios.forEach(r => r.addEventListener("change", rebuildFieldOptionsAndUpdate));

      rebuildFieldOptionsAndUpdate();
    }}

    document.addEventListener("DOMContentLoaded", initControls);
  </script>
</body>
</html>
"""
    return html


def build_report(job: Dict[str, Any], *, dry_run: bool) -> None:
    job_id = str(job.get("id") or "").strip() or "<unnamed job>"
    enabled = bool(job.get("enabled", True))
    if not enabled:
        logger.info("Skipping disabled job: %s", job_id)
        return

    name = str(job.get("name") or job_id or "stats_timeline").strip()

    sources = job.get("sources") or {}
    input_cfg = None
    mapping_yaml_cfg = None

    if isinstance(sources, dict):
        input_cfg = sources.get("input_table") or sources.get("input_csv") or sources.get("input")
        mapping_yaml_cfg = sources.get("program_attribute_mapping_yaml")
        if not input_cfg:
            paths = sources.get("paths")
            if isinstance(paths, list) and paths:
                input_cfg = paths[0]
                if len(paths) > 1 and not mapping_yaml_cfg:
                    mapping_yaml_cfg = paths[1]
    elif isinstance(sources, list):
        if sources:
            input_cfg = sources[0]
        if len(sources) > 1:
            mapping_yaml_cfg = sources[1]
    elif isinstance(sources, str):
        input_cfg = sources

    if not input_cfg:
        raise ValueError(f"Job '{job_id}' missing sources.input_table")

    output_cfg = job.get("output") or {}
    if not isinstance(output_cfg, dict):
        raise ValueError(f"Job '{job_id}' output must be a dict with dir/file")

    out_dir = str(output_cfg.get("dir") or "").strip()
    out_file = str(output_cfg.get("file") or "").strip()
    if not out_dir or not out_file:
        raise ValueError(f"Job '{job_id}' missing output.dir or output.file")

    input_path = resolve_any_path(input_cfg)
    output_path = vtx_path(Path(out_dir) / out_file)

    if not input_path or not input_path.exists():
        raise FileNotFoundError(f"Input stats table not found for job '{job_id}': {input_cfg}")

    input_mtime = safe_mtime(input_path)
    output_mtime = safe_mtime(output_path)

    if output_mtime >= input_mtime and output_mtime > 0:
        logger.info(
            "Skipping stats timeline report '%s' - output is newer than input (output_mtime=%s, input_mtime=%s)",
            name,
            output_mtime,
            input_mtime,
        )
        return

    logger.info("Building stats timeline report '%s', input=%s, output=%s", name, input_path, output_path)

    df = pd.read_csv(input_path)
    stats_data = build_stats_data(df)

    program_lookup = load_program_attribute_lookup(mapping_yaml_cfg)
    stats_data["programLookup"] = program_lookup

    html = render_html(name, stats_data)

    if dry_run:
        logger.info("[dry-run] Would write: %s", output_path)
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")

    logger.info("Wrote stats timeline HTML for report '%s' to: %s", name, output_path)


# ---------------------------------------------------------------------
# CLI / Options
# ---------------------------------------------------------------------

@dataclass
class Options:
    config_path: Path
    job: str = ""
    dry_run: bool = False


def parse_args(argv: Optional[list[str]] = None) -> Options:
    p = argparse.ArgumentParser(description="VTX visual stats timeline (job-oriented)")
    p.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Path to YAML config (default: {DEFAULT_CONFIG_PATH})",
    )
    p.add_argument("--job", default="", help='Run a single job by id (exact match)')
    p.add_argument("--dry-run", action="store_true", help="Do not write outputs")
    args = p.parse_args(argv)

    cfg_path = vtx_path(args.config, must_exist=True)
    return Options(config_path=cfg_path, job=str(args.job or "").strip(), dry_run=bool(args.dry_run))


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

    if opt.job:
        target = opt.job
        found = False
        for job in jobs:
            if not isinstance(job, dict):
                continue
            jid = str(job.get("id") or "").strip()
            if jid == target:
                build_report(job, dry_run=opt.dry_run)
                found = True
                break
        if not found:
            raise SystemExit(f"Job not found: {target}.")
        return 0

    for job in jobs:
        if not isinstance(job, dict):
            continue
        try:
            build_report(job, dry_run=opt.dry_run)
        except Exception as exc:
            logger.exception("Failed to build stats timeline report: %s", exc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
