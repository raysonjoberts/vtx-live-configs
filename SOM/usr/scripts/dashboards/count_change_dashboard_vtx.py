#!/usr/bin/env python3
#first try at new script template
"""
count_change_dashboard.py — Generate HTML dashboards showing row count changes over time.

Adds:
- 30-day change history (per-day added/removed values)
- Optional detail fields: show additional columns for each added/removed value
  (both for latest change and for history), configured via YAML as `detail_fields`.

Usage examples:
    python count_change_dashboard.py --job app_inventory_changes
    python count_change_dashboard.py --all
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import yaml
from dateutil.relativedelta import relativedelta
from jinja2 import Template
import plotly.graph_objects as go
from plotly.io import to_html

# ---------------------------------------------------------------------
# Globals / Paths (VTX style)
# ---------------------------------------------------------------------
def resolve_vtx_root() -> Path:
    """
    Determine VTX_ROOT in a cross-platform way.
    Priority:
      1) env VTX_ROOT
      2) env VTX_ROOT (legacy)
      3) infer relative to this file: <VTX_ROOT>/usr/scripts/default/<script>.py
    """
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("VTX_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    here = Path(__file__).resolve()
    inferred = here.parents[3]  # default/<script>.py -> scripts -> usr -> VTX_ROOT
    return inferred


VTX_ROOT = resolve_vtx_root()

DEFAULT_CONFIG_DIR = VTX_ROOT / "usr" / "config" / "run"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "count_change_dashboards_vtx.yaml"


#os.makedirs(LOGS_DIR, exist_ok=True)
#logging.basicConfig(
#    filename=os.path.join(LOGS_DIR, "count_change_dashboard.log"),
#    level=logging.INFO,
#    format="%(asctime)s,INFO,count_change_dashboard,%(message)s",
#)
#logger = logging.getLogger("count_change_dashboard")


# ------------------------------------------------------------
# Logging (BTDM/VTX style)
# ------------------------------------------------------------
try:
    sys.path.append(str(VTX_ROOT / "usr" / "lib"))
    import btdm_logging  # type: ignore

    logger = btdm_logging.get_logger(component="count_change_dashboard")
except Exception:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    logger = logging.getLogger("count_change_dashboard")


# -------------------------
# Utilities / config types
# -------------------------

def vtx_path(path_str: Any, *, must_exist: bool = False) -> Path:
    """
    Resolve a path that may be:
      - VTX-relative: "var/tables/x.csv"
      - absolute: "/opt/vtx/..." or "C:\VTX\..."
    Supports env var substitution and legacy placeholders:
      - ${VTX_ROOT}, $VTX_ROOT
      - ${BTDM_ROOT}, $BTDM_ROOT
    """
    s = str(path_str)
    s = s.replace("${VTX_ROOT}", str(VTX_ROOT)).replace("$VTX_ROOT", str(VTX_ROOT))
    s = s.replace("${BTDM_ROOT}", str(VTX_ROOT)).replace("$BTDM_ROOT", str(VTX_ROOT))
    s = os.path.expandvars(os.path.expanduser(s))
    p = Path(s)
    if not p.is_absolute():
        p = VTX_ROOT / p
    p = p.resolve()
    if must_exist and not p.exists():
        raise FileNotFoundError(f"Path does not exist: {p}")
    return p



@dataclass
class JobConfig:
    id: str
    title: str
    output_dir: Path
    output_file: str
    snapshots_dir: Path
    snapshots_pattern: str
    lookback_days: int
    filters: List[Dict[str, Any]]
    compare_field: str
    column_aliases: Dict[str, str]
    theme: Dict[str, Any]
    detail_fields: List[str]   # NEW: extra fields to show with added/removed apps

# ---------------
# YAML loader
# ---------------

def _load_yaml(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fp:
        return yaml.safe_load(fp) or {}

def _extract_jobs(cfg: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Support both legacy YAML (top-level 'jobs') and VTX universal config template."""
    if isinstance(cfg.get("config"), dict):
        payload = cfg["config"].get("payload") or {}
        if isinstance(payload, dict):
            jobs = payload.get("jobs")
            if jobs is not None:
                return jobs or []
        # fallback: some configs may put jobs directly under config
        jobs = cfg["config"].get("jobs")
        if jobs is not None:
            return jobs or []
    jobs = cfg.get("jobs")
    return jobs or []


def _job_from_dict(job_dict: Dict[str, Any]) -> JobConfig:
    out = job_dict.get("output", {})
    src = job_dict.get("sources", {})

    default_output_dir = Path(VTX_ROOT) / "var" / "reporting"
    default_snap_dir = Path(VTX_ROOT) / "var" / "dailysnapshot"

    jc = JobConfig(
        id=job_dict["id"],
        title=job_dict.get("title", job_dict["id"]),
        output_dir=vtx_path(out.get("dir", str(default_output_dir))),
        output_file=out.get("file", f"{job_dict['id']}.html"),
        snapshots_dir=vtx_path(src.get("snapshots_dir", str(default_snap_dir))),
        snapshots_pattern=src.get("snapshots_pattern", "snapshot_%Y%m%d.csv"),
        lookback_days=int(src.get("lookback_days", 30)),
        filters=job_dict.get("filters", []) or [],
        compare_field=job_dict.get("compare_field", "ID"),
        column_aliases=job_dict.get("column_aliases", {}) or {},
        theme=job_dict.get("theme", {}) or {},
        detail_fields=job_dict.get("detail_fields", []) or [],
    )
    logger.info(
        "job_paths_resolved,job=%s,output_dir=%s,snapshots_dir=%s",
        jc.id, jc.output_dir, jc.snapshots_dir,
    )
    return jc


def _select_job(cfg: Dict[str, Any], job_id: str) -> JobConfig:
    jobs = _extract_jobs(cfg)
    job = next((j for j in jobs if j.get("id") == job_id), None)
    if not job:
        raise SystemExit(f"Job id '{job_id}' not found in YAML.")

    return _job_from_dict(job)

# -----------------
# Data operations
# -----------------

def _resolve_col(df: pd.DataFrame, name: str) -> Optional[str]:
    """Resolve a column name in a case-insensitive way."""
    if name in df.columns:
        return name
    lowmap = {c.lower(): c for c in df.columns}
    return lowmap.get(name.lower())


def _try_cast(v: Any) -> Any:
    if pd.isna(v):
        return v
    for caster in (int, float):
        try:
            return caster(v)
        except Exception:
            continue
    return v


def _eval_op(series: pd.Series, op: str, value: Any) -> pd.Series:
    op = op.lower()
    if op == "is_null":
        return series.isna() | (series == "")
    if op == "is_not_null":
        return ~(series.isna() | (series == ""))

    if op in {"contains", "not_contains"}:
        s = series.astype(str).str.contains(str(value), na=False, case=False)
        return s if op == "contains" else ~s

    if op in {"in", "not_in"}:
        vals = value if isinstance(value, (list, tuple, set)) else [value]
        s = series.isin(vals)
        return s if op == "in" else ~s

    left = series.map(_try_cast)
    right = _try_cast(value)

    if op == "=":
        return left == right
    if op == "!=":
        return left != right
    if op == ">":
        return left > right
    if op == "<":
        return left < right
    if op == ">=":
        return left >= right
    if op == "<=":
        return left <= right

    raise ValueError(f"Unsupported operator: {op}")


def _apply_filters(df: pd.DataFrame, filters: List[Dict[str, Any]]) -> pd.DataFrame:
    if not filters or df.empty:
        return df
    mask = pd.Series(True, index=df.index)
    for f in filters:
        field_req = f["field"]
        field = _resolve_col(df, field_req) or field_req
        op = f["op"]
        value = f.get("value")
        if field not in df.columns:
            tmp = pd.Series([pd.NA] * len(df), index=df.index)
        else:
            tmp = df[field]
        mask &= _eval_op(tmp, op, value)
    return df[mask]


def _read_csv_safe(path: Path) -> Optional[pd.DataFrame]:
    if not path.exists():
        logger.info("missing_snapshot,%s", path)
        return None
    try:
        return pd.read_csv(path)
    except Exception as e:
        logger.info("read_error,%s,%s", path, e)
        return None


def _load_snapshots(job: JobConfig) -> List[Tuple[dt.date, pd.DataFrame]]:
    """Return list of (date, df) for all days in lookback window that exist."""
    today = dt.date.today()
    rows: List[Tuple[dt.date, pd.DataFrame]] = []
    for i in range(job.lookback_days):
        day = today - relativedelta(days=i)
        fname = day.strftime(job.snapshots_pattern)
        fpath = job.snapshots_dir / fname
        df = _read_csv_safe(fpath)
        if df is not None:
            rows.append((day, df))
    rows.sort(key=lambda x: x[0])
    return rows


def _extract_detail_row(df: pd.DataFrame, id_col: str, id_value: Any, detail_fields: List[str]) -> Dict[str, Any]:
    """Given a DF, the compare-field column name, and a value, grab the first matching row
    and extract the configured detail fields."""
    if df is None or df.empty:
        return {}
    try:
        subset = df[df[id_col].astype(str) == str(id_value)]
        if subset.empty:
            return {}
        row = subset.iloc[0]
    except Exception:
        return {}

    details: Dict[str, Any] = {}
    for f in detail_fields:
        col = _resolve_col(df, f) or f
        if col in df.columns:
            val = row.get(col, "")
        else:
            val = ""
        details[f] = val
    return details

# -----------------
# Metrics
# -----------------

def compute_counts(job: JobConfig) -> Dict[str, Any]:
    snaps_raw = _load_snapshots(job)
    if not snaps_raw:
        return {
            "latest_date": None,
            "prev_date": None,
            "latest_count": 0,
            "prev_count": 0,
            "change": 0,
            "pct_change": 0.0,
            "timeline": pd.DataFrame(columns=["date", "count"]),
            "added": [],
            "removed": [],
            "history_changes": [],
        }

    # Timeline (after filters) + store filtered snapshots for change history
    tl_rows = []
    filtered_snaps: List[Tuple[dt.date, pd.DataFrame]] = []
    for day, df in snaps_raw:
        filt_df = _apply_filters(df, job.filters)
        tl_rows.append({"date": day, "count": len(filt_df)})
        filtered_snaps.append((day, filt_df))
    tl = pd.DataFrame(tl_rows).sort_values("date")

    # Latest and previous snapshots (filtered)
    latest_date, latest_df = filtered_snaps[-1]
    if len(filtered_snaps) >= 2:
        prev_date, prev_df = filtered_snaps[-2]
    else:
        prev_date, prev_df = (None, pd.DataFrame(columns=latest_df.columns))

    latest_count = int(len(latest_df))
    prev_count = int(len(prev_df)) if prev_df is not None else 0
    change = latest_count - prev_count
    pct_change = (change / prev_count) * 100.0 if prev_count else 0.0

    # Added / removed values for the most recent change, with detail fields
    added_rows: List[Dict[str, Any]] = []
    removed_rows: List[Dict[str, Any]] = []
    if prev_df is not None and not prev_df.empty:
        cf = job.compare_field
        latest_col = _resolve_col(latest_df, cf)
        prev_col = _resolve_col(prev_df, cf)
        if latest_col and prev_col:
            latest_vals = set(latest_df[latest_col].dropna().astype(str))
            prev_vals = set(prev_df[prev_col].dropna().astype(str))
            added_vals = sorted(latest_vals - prev_vals)
            removed_vals = sorted(prev_vals - latest_vals)

            for v in added_vals:
                fields = _extract_detail_row(latest_df, latest_col, v, job.detail_fields)
                added_rows.append({"value": v, "fields": fields})
            for v in removed_vals:
                fields = _extract_detail_row(prev_df, prev_col, v, job.detail_fields)
                removed_rows.append({"value": v, "fields": fields})

    # Change history over the lookback period:
    # for each day (except the first), compare to previous day's snapshot
    history_changes: List[Dict[str, Any]] = []
    if len(filtered_snaps) >= 2:
        cf = job.compare_field
        for i in range(1, len(filtered_snaps)):
            day, cur_df = filtered_snaps[i]
            _, prev_df_hist = filtered_snaps[i - 1]

            cur_col = _resolve_col(cur_df, cf)
            prev_col_hist = _resolve_col(prev_df_hist, cf)

            if not cur_col or not prev_col_hist:
                continue

            cur_vals = set(cur_df[cur_col].dropna().astype(str))
            prev_vals_hist = set(prev_df_hist[prev_col_hist].dropna().astype(str))

            added_vals = sorted(cur_vals - prev_vals_hist)
            removed_vals = sorted(prev_vals_hist - cur_vals)

            for v in added_vals:
                fields = _extract_detail_row(cur_df, cur_col, v, job.detail_fields)
                history_changes.append(
                    {"date": day, "direction": "added", "value": v, "fields": fields}
                )
            for v in removed_vals:
                fields = _extract_detail_row(prev_df_hist, prev_col_hist, v, job.detail_fields)
                history_changes.append(
                    {"date": day, "direction": "removed", "value": v, "fields": fields}
                )

    return {
        "latest_date": latest_date,
        "prev_date": prev_date,
        "latest_count": latest_count,
        "prev_count": prev_count,
        "change": change,
        "pct_change": pct_change,
        "timeline": tl,
        "added": added_rows,
        "removed": removed_rows,
        "history_changes": history_changes,
    }

# ------------------
# Plot builders
# ------------------

def timeline_figure(df: pd.DataFrame, label: str) -> go.Figure:
    x = df["date"]
    y = df["count"]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=x, y=y, mode="lines+markers", name=label))
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=10, r=10, t=30, b=10),
        height=260,
        xaxis_title=None,
        yaxis_title="Row Count",
        title=go.layout.Title(text=f"{label} — Row Count over Time"),
    )
    return fig

# ------------------
# HTML template
# ------------------

HTML_TEMPLATE = Template(r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{{ title }}</title>
  <style>
    body { font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin:0; padding:24px; background:#0e1222; color:#eaeef8; }
    .header { font-size: 26px; font-weight:800; margin-bottom:18px; }
    .grid { display:grid; grid-template-columns: minmax(260px, 1.2fr) minmax(260px, 1.2fr); gap:16px; margin-bottom:24px; align-items:start; }
    .tile { background:#151a30; border-radius:12px; padding:16px; box-shadow:0 6px 24px rgba(0,0,0,0.35); }
    .label { font-weight:700; color:#c5d0ff; margin-bottom:6px; }
    .muted { color:#9aa3b2; font-size:12px; }
    .count { font-size:24px; font-weight:800; color:#ffd86b; }
    .delta-up { color:#4ade80; font-weight:700; }
    .delta-down { color:#f97373; font-weight:700; }
    .badge { display:inline-flex; align-items:center; gap:6px; padding:4px 8px; border-radius:999px; background:#11172b; font-size:11px; color:#c5d0ff; }

    .chart { background:#0f1326; border-radius:12px; padding:8px; }

    .history-table-wrapper { margin-top:12px; max-height:260px; overflow:auto; }
    table.data { width:100%; border-collapse:collapse; font-size:12px; }
    table.data th, table.data td { padding:4px 6px; border-bottom:1px solid rgba(255,255,255,0.07); text-align:left; }
    table.data th { font-weight:600; color:#c5d0ff; position:sticky; top:0; background:#151a30; }
    .history-added { color:#4ade80; font-weight:600; }
    .history-removed { color:#f97373; font-weight:600; }

    .two-tables { display:grid; grid-template-columns: 1fr 1fr; gap:12px; }
  </style>
</head>
<body>
  <div class="header">{{ title }}</div>

  <div class="grid">
    <div class="tile">
      <div class="label">Filtered row count (latest vs previous)</div>
      <div style="display:flex; gap:18px; align-items:flex-end; margin-top:8px;">
        <div>
          <div class="muted">Latest snapshot{% if latest_date %} — {{ latest_date.strftime('%Y-%m-%d') }}{% endif %}</div>
          <div class="count">{{ latest_count }}</div>
        </div>
        <div>
          <div class="muted">Previous snapshot{% if prev_date %} — {{ prev_date.strftime('%Y-%m-%d') }}{% else %} (none){% endif %}</div>
          <div class="count">{{ prev_count }}</div>
        </div>
      </div>
      <div style="margin-top:10px;">
        {% if change > 0 %}
          <div class="delta-up">▲ +{{ change }} ({{ pct_change|round(1) }}%)</div>
        {% elif change < 0 %}
          <div class="delta-down">▼ {{ change }} ({{ pct_change|round(1) }}%)</div>
        {% else %}
          <div class="muted">No change in filtered row count.</div>
        {% endif %}
      </div>
      {% if filters_text %}
        <div style="margin-top:10px;">
          <span class="badge">Filters applied: {{ filters_text }}</span>
        </div>
      {% endif %}
    </div>

    <div class="tile">
      <div class="label">Values of {{ compare_field_label }} added / removed (latest vs previous)</div>
      {% if change != 0 and (added or removed) %}
        <div class="two-tables" style="margin-top:8px;">
          <div>
            <div class="muted">Added in latest (not in previous)</div>
            {% if added %}
              <div class="history-table-wrapper">
                <table class="data">
                  <thead>
                    <tr>
                      <th>{{ compare_field_label }}</th>
                      {% for lbl in detail_field_labels %}
                        <th>{{ lbl }}</th>
                      {% endfor %}
                    </tr>
                  </thead>
                  <tbody>
                    {% for row in added %}
                      <tr>
                        <td>{{ row.value }}</td>
                        {% for key in detail_field_keys %}
                          <td>{{ row.fields.get(key, "") }}</td>
                        {% endfor %}
                      </tr>
                    {% endfor %}
                  </tbody>
                </table>
              </div>
            {% else %}
              <div class="muted">None</div>
            {% endif %}
          </div>
          <div>
            <div class="muted">Removed (in previous, not in latest)</div>
            {% if removed %}
              <div class="history-table-wrapper">
                <table class="data">
                  <thead>
                    <tr>
                      <th>{{ compare_field_label }}</th>
                      {% for lbl in detail_field_labels %}
                        <th>{{ lbl }}</th>
                      {% endfor %}
                    </tr>
                  </thead>
                  <tbody>
                    {% for row in removed %}
                      <tr>
                        <td>{{ row.value }}</td>
                        {% for key in detail_field_keys %}
                          <td>{{ row.fields.get(key, "") }}</td>
                        {% endfor %}
                      </tr>
                    {% endfor %}
                  </tbody>
                </table>
              </div>
            {% else %}
              <div class="muted">None</div>
            {% endif %}
          </div>
        </div>
      {% else %}
        <div class="muted" style="margin-top:8px;">
          No change in row count or no previous snapshot available, so there are no added/removed values to list.
        </div>
      {% endif %}
    </div>
  </div>

  <div class="tile">
    <div class="label">30-day row count timeline</div>
    <div class="chart">
      {{ timeline_html | safe }}
    </div>
  </div>

  <div class="tile" style="margin-top:16px;">
    <div class="label">Change history (last {{ lookback_days }} days)</div>
    {% if history_changes %}
      <div class="history-table-wrapper">
        <table class="data">
          <thead>
            <tr>
              <th>Date</th>
              <th>Change</th>
              <th>{{ compare_field_label }}</th>
              {% for lbl in detail_field_labels %}
                <th>{{ lbl }}</th>
              {% endfor %}
            </tr>
          </thead>
          <tbody>
            {% for c in history_changes %}
              <tr>
                <td>{{ c.date.strftime('%Y-%m-%d') }}</td>
                <td>
                  {% if c.direction == 'added' %}
                    <span class="history-added">+ added</span>
                  {% else %}
                    <span class="history-removed">− removed</span>
                  {% endif %}
                </td>
                <td>{{ c.value }}</td>
                {% for key in detail_field_keys %}
                  <td>{{ c.fields.get(key, "") }}</td>
                {% endfor %}
              </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    {% else %}
      <div class="muted" style="margin-top:8px;">
        No adds or removes detected between daily snapshots in the lookback window.
      </div>
    {% endif %}
  </div>

  <div class="muted" style="margin-top:18px;">Generated: {{ generated_at }}</div>
</body>
</html>
""")

# ------------------
# Render pipeline
# ------------------

def _filters_to_text(filters: List[Dict[str, Any]]) -> str:
    parts = []
    for f in filters:
        field = f.get("field")
        op = f.get("op")
        value = f.get("value")
        if op in ("in", "not_in") and isinstance(value, (list, tuple, set)):
            val_str = "[" + ", ".join(map(str, value)) + "]"
        else:
            val_str = str(value) if value is not None else ""
        parts.append(f"{field} {op} {val_str}".strip())
    return "; ".join(parts)


def render_job(job: JobConfig) -> str:
    stats = compute_counts(job)
    tl_df: pd.DataFrame = stats["timeline"]

    if tl_df.empty:
        fig = go.Figure()
        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            margin=dict(l=10, r=10, t=30, b=10),
            height=260,
            xaxis_title=None,
            yaxis_title="Row Count",
            title=go.layout.Title(text="No snapshots found in lookback window"),
        )
    else:
        fig = timeline_figure(tl_df, job.title)

    timeline_html = to_html(fig, include_plotlyjs="cdn", full_html=False)

    cf = job.compare_field
    compare_field_label = job.column_aliases.get(cf, cf)

    detail_field_keys = job.detail_fields
    detail_field_labels = [job.column_aliases.get(k, k) for k in detail_field_keys]

    html = HTML_TEMPLATE.render(
        title=job.title,
        latest_date=stats["latest_date"],
        prev_date=stats["prev_date"],
        latest_count=stats["latest_count"],
        prev_count=stats["prev_count"],
        change=stats["change"],
        pct_change=stats["pct_change"],
        compare_field_label=compare_field_label,
        added=stats["added"],
        removed=stats["removed"],
        filters_text=_filters_to_text(job.filters),
        timeline_html=timeline_html,
        generated_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        history_changes=stats["history_changes"],
        lookback_days=job.lookback_days,
        detail_field_keys=detail_field_keys,
        detail_field_labels=detail_field_labels,
    )
    return html


def write_html(job: JobConfig, html: str) -> Path:
    job.output_dir.mkdir(parents=True, exist_ok=True)
    out_path = job.output_dir / job.output_file
    out_path.write_text(html, encoding="utf-8")
    return out_path

# ------------------
# CLI
# ------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to YAML file with jobs list (absolute or relative to VTX_ROOT)",
    )
    mx = ap.add_mutually_exclusive_group()
    mx.add_argument("--job", help="Job id to render (runs a single job)")
    mx.add_argument("--all", action="store_true", help="Render ALL jobs in the YAML")
    args = ap.parse_args()

    cfg_path = vtx_path(args.config)
    cfg = _load_yaml(cfg_path)

    run_all = args.all or (args.job is None)

    if run_all:
        jobs_cfg = _extract_jobs(cfg)
        if not jobs_cfg:
            raise SystemExit("No jobs found in YAML.")
        for j in jobs_cfg:
            job = _job_from_dict(j)
            logger.info("start_all,config=%s,job=%s", cfg_path, job.id)
            html = render_job(job)
            out = write_html(job, html)
            logger.info("wrote,%s", out)
            print(f"Wrote: {out}")
    else:
        job = _select_job(cfg, args.job)
        logger.info("start,config=%s,job=%s", cfg_path, job.id)
        html = render_job(job)
        out = write_html(job, html)
        logger.info("wrote,%s", out)
        print(f"Wrote: {out}")


if __name__ == "__main__":
    main()
