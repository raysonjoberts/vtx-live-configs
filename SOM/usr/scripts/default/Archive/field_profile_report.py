#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Field Profile Report (finalized version)
- Reads config at VTX_ROOT/usr/config/default/data_source_field_analysis.conf
- For each configured CSV, generates a dark-themed HTML profiling page per column.
- Metrics: Total Rows, Non-Null Count, Usage %, Distinct Count.
- Usage % has RAG (>=80 green, 40–79 amber, <40 red).
- Each field section is collapsible.
- Field names use the accent yellow.
- Pie + legend are spaced side by side.
"""

import os
import sys
import html
import json
from configparser import ConfigParser
from string import Template

import pandas as pd

VTX_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
CONF_PATH = os.path.join(VTX_ROOT, "usr", "config", "default", "data_source_field_analysis.conf")
DEFAULT_OUTDIR = os.path.join(VTX_ROOT, "var", "reporting")

# ------------------------------------------------------------
# Logging (BTDM/VTX style)
# ------------------------------------------------------------
try:
    sys.path.append(os.path.join(VTX_ROOT, "usr", "lib"))
    import btdm_logging  # type: ignore

    logger = btdm_logging.get_logger(component="field_profile_report")
except Exception:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    logger = logging.getLogger("field_profile_report")

# ------------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------------

def _read_conf(conf_path: str) -> ConfigParser:
    cp = ConfigParser()
    cp.read(conf_path)
    return cp


def _iter_sources_from_section(cp: ConfigParser, section: str):
    for key, val in cp.items(section):
        if key.startswith("source") and not key.endswith("filters"):
            src = val.strip()
            flt = cp.get(section, f"{key}_filters", fallback="").strip()
            yield src, (flt if flt else None)


def _safe_read_csv(path: str) -> pd.DataFrame:
    try:
        return pd.read_csv(path)
    except Exception as e:
        sys.stderr.write("[WARN] Could not read CSV: {} ({})\n".format(path, e))
        return pd.DataFrame()


def _null_mask(series: pd.Series) -> pd.Series:
    s = series.astype(object)
    isna = pd.isna(s)
    isblank = s.astype(str).str.strip().eq("")
    return isna | isblank


def _value_counts_with_nulls(series: pd.Series) -> pd.Series:
    mask_null = _null_mask(series)
    labeled = series.astype(object).copy()
    labeled[mask_null] = "(null)"
    return labeled.astype(str).value_counts(dropna=False)


def _top6_plus_others(vc: pd.Series):
    if vc.empty:
        return []
    vc_sorted = vc.sort_values(ascending=False)
    head = vc_sorted.iloc[:6]
    tail_sum = int(vc_sorted.iloc[6:].sum()) if len(vc_sorted) > 6 else 0
    items = [(str(idx), int(cnt)) for idx, cnt in head.items()]
    if tail_sum > 0:
        items.append(("others", tail_sum))
    return items


def _percent(n: float, d: float) -> float:
    return (100.0 * n / d) if d > 0 else 0.0


def _escape(s: str) -> str:
    return html.escape(str(s), quote=True)

# ------------------------------------------------------------------------------------
# Filtering helpers (simple: supports AND with '=' or '!=')
# ------------------------------------------------------------------------------------

def _apply_filters(df: pd.DataFrame, filt: str) -> pd.DataFrame:
    if not filt:
        return df
    parts = [p.strip() for p in filt.split('AND')]
    mask = pd.Series(True, index=df.index)
    for cond in parts:
        if not cond:
            continue
        op = '!=' if '!=' in cond else '='
        left, right = [x.strip() for x in cond.split(op, 1)]
        # strip optional quotes around the value
        if right.startswith('"') and right.endswith('"'):
            right = right[1:-1]
        elif right.startswith("'") and right.endswith("'"):
            right = right[1:-1]
        if left not in df.columns:
            sys.stderr.write("[WARN] Filter column not found: {}\n".format(left))
            continue
        s = df[left]
        if op == '=':
            mask &= (s.astype(str) == right)
        else:
            mask &= (s.astype(str) != right)
    return df[mask]

# ------------------------------------------------------------------------------------
# HTML Templates
# ------------------------------------------------------------------------------------

_HEAD_TPL = Template(
    """<!DOCTYPE html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\"/>
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"/>
<title>$TITLE</title>
<style>
:root{ --bg:#0f1320; --panel:#151a2e; --ink:#e9eefc; --muted:#9aa3c7; --accent:#ffc147; --ok:#3ecf8e; --warn:#ffd166; --bad:#ff4d6d; --line:#232847; }
*{box-sizing:border-box} html,body{height:100%}
body{margin:0;background:radial-gradient(1200px 600px at 20% 90%,#17305511 0,#0000 55%),var(--bg);color:var(--ink);font:400 15px/1.5 system-ui,-apple-system,\"Segoe UI\",Roboto,Inter,Arial}
.topbar{position:sticky;top:0;z-index:5;background:linear-gradient(180deg,#0f1320,#0f1320ee 70%,#0f132000);border-bottom:1px solid var(--line)}
.wrap{max-width:1200px;margin:auto;padding:16px 20px;display:flex;gap:16px;align-items:center}
.title{font-weight:800;letter-spacing:.3px}
.pill{font-size:12px;color:#0f1320;background:var(--accent);border-radius:999px;padding:4px 10px;font-weight:700;margin-left:6px}
.spacer{flex:1}
.search{background:#0b0f20;border:1px solid var(--line);color:var(--ink);border-radius:10px;padding:8px 12px;min-width:260px;outline:none}
.search::placeholder{color:var(--muted)}
.count{font-size:12px;color:var(--muted)}
.grid{max-width:1200px;margin:18px auto;padding:0 20px;display:grid;grid-template-columns:1fr;gap:18px}

/* Collapsible cards */
details.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;overflow:hidden}
details.card[open] .chev{transform:rotate(90deg)}
.card>summary{list-style:none;display:flex;align-items:center;gap:14px;padding:14px 16px;cursor:pointer}
.card>summary::-webkit-details-marker{display:none}
.chev{transition:transform .2s ease}
.name{font-weight:800;color:var(--accent)}
.meta{display:flex;gap:14px;align-items:center;margin-left:auto}
.badge{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:999px;padding:6px 10px;font-size:12px}
.dot{width:14px;height:14px;border-radius:50%;background:var(--bad)}
.dot.ok{background:var(--ok)}
.dot.warn{background:var(--warn)}
.body{display:grid;grid-template-columns:0.58fr 0.42fr;gap:16px;padding:16px;border-top:1px solid var(--line)}
.table{border:1px solid var(--line);border-radius:12px;overflow:hidden}
.table table{width:100%;border-collapse:collapse;table-layout:fixed}
.table thead th{background:#0f1430;color:var(--muted);text-align:left;padding:10px 12px;font-size:13px;border-bottom:1px solid var(--line)}
.table td{padding:10px 12px;border-bottom:1px solid #1e2342;vertical-align:top;white-space:pre-wrap;overflow-wrap:anywhere}
.table tr:last-child td{border-bottom:none}

/* pie + legend layout (pie then legend) */
canvas.pie{width:220px;height:220px;background:#0f143000;display:block}
.chartwrap{display:flex;gap:16px;align-items:center;justify-content:flex-start}
.legend{min-width:220px;max-width:280px;display:flex;flex-direction:column;row-gap:8px;color:var(--ink)}
.legend .item{display:flex;align-items:center;gap:8px;font-size:12px}
.legend .sw{width:14px;height:14px;border-radius:3px}

@media (max-width:900px){ .body{grid-template-columns:1fr} }
</style>
<script>
// Pie renderer; legend built into DOM element on the right
function drawPie(canvasId, data, legendId){
  const el = document.getElementById(canvasId);
  if (!el) return;
  const ctx = el.getContext('2d');
  const w = el.width = el.clientWidth * devicePixelRatio;
  const h = el.height = el.clientHeight * devicePixelRatio;
  const cx = w/2, cy = h/2;
  const r  = Math.min(w, h) * 0.42;

  const total = data.reduce((s,d)=>s+d.value,0) || 1;
  const base  = ['#7aa6ff','#87e3ff','#a8ffcf','#ffe08a','#ff9ab1','#c69cff','#7de0b0','#f2a97f','#b0b6ff','#f7d96e'];
  const colors = data.map((_,i)=> base[i % base.length]);

  let a0 = -Math.PI/2;
  data.forEach(function(d,i){
    var a1 = a0 + 2*Math.PI*(d.value/total);
    ctx.beginPath();
    ctx.moveTo(cx,cy);
    ctx.arc(cx,cy,r,a0,a1);
    ctx.fillStyle = colors[i];
    ctx.fill();
    a0 = a1;
  });

  var legendEl = document.getElementById(legendId);
  if (legendEl){
    var html = '';
    data.slice(0,8).forEach(function(d,i){
      var pct = total ? Math.round(1000*d.value/total)/10 : 0;
      html += '<div class=\"item\"><span class=\"sw\" style=\"background:'+colors[i]+'\"></span>' + d.label + ' ('+pct+'%)</div>';
    });
    legendEl.innerHTML = html;
  }
}

// Field search (filters cards by field name)
function setupFieldSearch(){
  var inp = document.getElementById('fieldSearch');
  if(!inp) return;
  var countEl = document.getElementById('matchCount');
  var cards = Array.from(document.querySelectorAll('details.card'));
  var total = cards.length;
  function apply(){
    var q = (inp.value||'').toLowerCase().trim();
    var shown = 0;
    cards.forEach(function(d){
      var nameEl = d.querySelector('.name');
      var name = nameEl ? nameEl.textContent.toLowerCase() : '';
      var match = !q || name.indexOf(q) !== -1;
      d.style.display = match ? '' : 'none';
      if (match) shown++;
    });
    if (countEl) countEl.textContent = (q ? (shown + ' / ' + total) : total);
  }
  inp.addEventListener('input', apply);
  try{ var params = new URLSearchParams(window.location.search); var preset = params.get('q'); if (preset) inp.value = preset; }catch(e){}
  apply();
}

document.addEventListener('DOMContentLoaded', setupFieldSearch);
</script>
</head>
<body>
<div class=\"topbar\"><div class=\"wrap\">
  <div class=\"title\">Field Profile <span class=\"pill\">Report</span></div>
  <div class=\"spacer\"></div>
  <input id=\"fieldSearch\" class=\"search\" type=\"search\" placeholder=\"Search fields…\"/>
  <span class=\"count\" id=\"matchCount\"></span>
</div></div>
<div class=\"grid\">
"""
)

_TAIL_HTML = """
</div>
</body>
</html>
"""

_CARD_TPL = Template(
    """
<details class="card">
  <summary>
    <span class="chev">▶</span>
    <span class="name">$FIELD_NAME</span>
    <span class="meta"><span class="badge"><span class="dot $RAG"></span>Usage: $USAGE_PCT%</span></span>
  </summary>
  <div class="body">
    <div class="table">
      <table>
        <thead><tr><th width="42%">Metric</th><th>Value</th></tr></thead>
        <tbody>
          <tr><td>Total Rows</td><td>$TOTAL_ROWS</td></tr>
          <tr><td>Non-Null Count</td><td>$NON_NULL_COUNT</td></tr>
          <tr><td>Usage (non-null ÷ rows)</td><td>$USAGE_PCT%</td></tr>
          <tr><td>Distinct Count</td><td>$DISTINCT_COUNT</td></tr>
        </tbody>
      </table>
    </div>
    <div class="chartwrap">
      <canvas class="pie" id="$PIE_ID"></canvas>
      <div class="legend" id="$LEGEND_ID"></div>
    </div>
  </div>
</details>
<script>
  drawPie("$PIE_ID", $PIE_DATA_JSON, "$LEGEND_ID");
</script>
"""
)

# ------------------------------------------------------------------------------------
# Core
# ------------------------------------------------------------------------------------

def _rag_class(pct_used: float) -> str:
    if pct_used >= 80:
        return "ok"
    if pct_used >= 40:
        return "warn"
    return "bad"


def _render_head(title: str) -> str:
    return _HEAD_TPL.safe_substitute(TITLE=_escape(title))


def _render_card(field_name: str, total_rows: int, non_null_count: int, pct_used: float, distinct_count: int, pie_id: str, legend_id: str, pie_pairs):
    rag = _rag_class(pct_used)
    pie_json = json.dumps([{"label": k, "value": v} for (k, v) in pie_pairs])
    return _CARD_TPL.safe_substitute(
        FIELD_NAME=_escape(field_name),
        TOTAL_ROWS=str(total_rows),
        NON_NULL_COUNT=str(non_null_count),
        USAGE_PCT=f"{pct_used:.1f}",
        DISTINCT_COUNT=str(distinct_count),
        RAG=rag,
        PIE_ID=_escape(pie_id),
        LEGEND_ID=_escape(legend_id),
        PIE_DATA_JSON=pie_json,
    )

def profile_one_csv(csv_path: str, outdir: str, filters: str = None) -> str:
    df = _safe_read_csv(csv_path)
    # Apply optional filters before computing metrics
    if filters:
        try:
            df = _apply_filters(df, filters)
        except Exception as e:
            sys.stderr.write("[WARN] Failed to apply filters '{}': {}\n".format(filters, e))
    title = f"Field Profile • {os.path.basename(csv_path)}"

    if df.empty:
        parts = [_render_head(title),
                 '<details class="card" open><summary><span class="chev">▶</span><span class="name">'+_escape(os.path.basename(csv_path))+'</span></summary>'
                 '<div class="body"><div class="table"><table><tbody>'
                 '<tr><td>Notice</td><td>Could not read or CSV was empty.</td></tr>'
                 '</tbody></table></div></div></details>',
                 _TAIL_HTML]
        os.makedirs(outdir, exist_ok=True)
        outfile = os.path.join(outdir, f"{os.path.splitext(os.path.basename(csv_path))[0]}_field_profile.html")
        with open(outfile, "w", encoding="utf-8") as f:
            f.write("".join(parts))
        return outfile

    total_rows = int(len(df))
    parts = [_render_head(title)]

    for col in df.columns:
        s = df[col]
        mask_null = _null_mask(s)
        non_null_count = int((~mask_null).sum())
        pct_used = _percent(non_null_count, total_rows)
        distinct_count = int(s[~mask_null].astype(str).nunique())

        vc = _value_counts_with_nulls(s)
        pie_pairs = _top6_plus_others(vc)
        pie_id = "pie_" + str(abs(hash((col, csv_path))) % (10**10))
        legend_id = "legend_" + str(abs(hash((col, csv_path))) % (10**10))

        parts.append(_render_card(col, total_rows, non_null_count, pct_used, distinct_count, pie_id, legend_id, pie_pairs))

    parts.append(_TAIL_HTML)
    os.makedirs(outdir, exist_ok=True)
    outfile = os.path.join(outdir, f"{os.path.splitext(os.path.basename(csv_path))[0]}_field_profile.html")
    with open(outfile, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    return outfile

def main() -> int:
    cp = _read_conf(CONF_PATH)
    produced = []
    for section in cp.sections():
        if not (section.startswith("field_profile:") or section.startswith("report:")):
            continue
        outdir = cp.get(section, "output_path", fallback=DEFAULT_OUTDIR)
        if outdir.upper().startswith("VTX_ROOT"):
            outdir = outdir.replace("VTX_ROOT", VTX_ROOT)
        for src, filt in _iter_sources_from_section(cp, section):
            src_path = src
            if src_path.upper().startswith("VTX_ROOT"):
                src_path = src_path.replace("VTX_ROOT", VTX_ROOT)
            produced.append(profile_one_csv(src_path, outdir, filt))
    if not produced:
        tables_dir = os.path.join(VTX_ROOT, "var", "tables")
        try:
            entries = [e for e in os.listdir(tables_dir) if e.lower().endswith('.csv')]
        except FileNotFoundError:
            sys.stderr.write("[WARN] No configured sources and {} not found.\n".format(tables_dir))
            return 0
        for e in entries:
            produced.append(profile_one_csv(os.path.join(tables_dir, e), DEFAULT_OUTDIR))
    for p in produced:
        logger.info("Wrote:", p)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
