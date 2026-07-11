#!/usr/bin/env python3
"""
Per-Application Report Card — V8

Loads program attribute mappings from the YAML mapping file:
  usr/config/run/data_source_analysis_mapping.yaml

Other behavior preserved:
- Only YAML attributes with type in {application, custom}
- Columns shown: Attribute | Mapped Field | Value | Status
"""

from __future__ import annotations
import os, sys, argparse, csv, json, webbrowser
from typing import Any, Dict, List, Sequence, Optional, Tuple
try:
    import yaml  # type: ignore
except Exception:
    yaml = None

# ---------- Paths / Defaults ----------
BTDM_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

DEFAULT_YAML      = os.path.join(BTDM_ROOT, "usr", "config", "default", "data_source_analysis.yaml")
DEFAULT_MAPPINGS  = os.path.join(BTDM_ROOT, "usr", "config", "run", "data_source_analysis_mapping.yaml")
SOURCE_CSV        = os.path.join(BTDM_ROOT, "var", "tables", "views", "consolidated_application_view_expanded.csv")
OUTPUT_HTML       = os.path.join(BTDM_ROOT, "var", "reporting", "application_report_cards_expanded.html")
OUTPUT_HTML_P1    = os.path.join(BTDM_ROOT, "var", "reporting", "application_report_cards_priority_one_expanded.htm")

#LOGS_DIR = os.path.join(BTDM_ROOT, "var", "logs")

# =============
# BTDM Logging
# =============
try:
    sys.path.append(os.path.join(BTDM_ROOT, "usr", "lib"))
    import btdm_logging  # type: ignore
    logger = btdm_logging.get_logger(component="per_application_report_cards")
except Exception:
    import logging
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    logger = logging.getLogger("per_application_report_cards")

DEFAULTS: Dict[str, Any] = {
    "title": "Application Report Cards",
    "columns": {"id": "ID", "name": "TITLE_NAME", "acronym": "ACRONYM", "department": "DEPARTMENT_NAME"},
    "filters": [{"column": "ACTIVE", "equals": "Y"}],
}

# ---------- Helpers ----------
def html_escape(s: Optional[str]) -> str:
    if s is None: return ""
    return (str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
            .replace('"',"&quot;").replace("'","&#39;"))

def expand_under_root(path: str, root: str = BTDM_ROOT) -> str:
    p = os.path.expandvars(path).replace("BTDM_ROOT", root)
    p = os.path.expanduser(p)
    if not os.path.isabs(p): p = os.path.join(root, p)
    return os.path.normpath(p)

def read_csv_rows(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, newline='', encoding='utf-8-sig') as fp:
        rdr = csv.DictReader(fp)
        for row in rdr:
            rows.append(row)
    return rows

def apply_filters(rows: List[Dict[str, Any]], filters: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for r in rows:
        ok = True
        for f in filters or []:
            col = f.get("column"); eq = f.get("equals")
            if col and eq is not None:
                if str(r.get(col, "")).strip() != str(eq).strip():
                    ok = False; break
        if ok: out.append(r)
    return out

def non_empty(val: Any) -> bool:
    s = str(val).strip()
    return not (s == "" or s.lower() in {"n/a","none","null","nan"})

def clean_mapped_field(name: Optional[str]) -> str:
    """Display-friendly mapped field name; strip dataset/table/prefixes if present."""
    s = str(name or "").strip()
    for sep in ["|", ":", "/", "\\"]:
        if sep in s:
            s = s.split(sep)[-1]
    if "." in s:
        s = s.split(".")[-1]
    return s

def get_row_value(row: Dict[str, Any], candidates: Sequence[str]) -> str:
    for cand in candidates:
        cand_l = cand.strip().lower()
        for col in row.keys():
            if col.strip().lower() == cand_l:
                return str(row.get(col) or "").strip()
    return ""

def parse_mapped_servers(value: Any) -> List[str]:
    if value is None:
        return []
    raw = str(value)
    if not raw.strip():
        return []
    parts = [p.strip() for p in raw.split(";")]
    seen = set()
    out: List[str] = []
    for p in parts:
        if not p:
            continue
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out

def resolve_server_key_column(server_rows: List[Dict[str, Any]]) -> str:
    candidates = ["Server", "Server Name", "Hostname", "Host", "FQDN"]
    if not server_rows:
        raise RuntimeError("Server table has no rows; cannot resolve server key column.")
    columns = list(server_rows[0].keys())
    for name in candidates:
        for col in columns:
            if col.strip().lower() == name.lower():
                return col
    raise RuntimeError(
        "Server key column not found. Looked for "
        f"{candidates}. Available columns: {columns}"
    )

def build_server_index(server_rows: List[Dict[str, Any]], key_col: str) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for row in server_rows:
        key_val = row.get(key_col)
        if key_val is None:
            continue
        key = str(key_val).strip().lower()
        if not key:
            continue
        index[key] = row
    return index

def server_attr_coverage_status(
    mapped_servers: List[str],
    server_index: Dict[str, Dict[str, Any]],
    server_columns: List[str],
    server_attr_col: Optional[str],
) -> Tuple[str, str]:
    if not mapped_servers:
        return ("red", "No servers mapped")
    if not server_attr_col:
        return ("red", "Server attribute not mapped")
    if server_attr_col not in server_columns:
        return ("red", "Server column missing")
    total = len(mapped_servers)
    with_value = 0
    for server in mapped_servers:
        row = server_index.get(server.strip().lower())
        if not row:
            continue
        if non_empty(row.get(server_attr_col)):
            with_value += 1
    if with_value == 0:
        return ("red", f"Servers with value: 0/{total}")
    if with_value == total:
        return ("green", f"Servers with value: {with_value}/{total}")
    return ("amber", f"Servers with value: {with_value}/{total}")

# ---------- YAML & INI ----------
NAME_KEYS = ("attribute","program_attribute","name","label","display_name","title")

def _collect_yaml_attrs(node: Any, out: List[Dict[str, Any]]) -> None:
    """Collect YAML attributes recursively (type in {application,custom,server}, has priority_level, has a name)."""
    if isinstance(node, dict):
        typ = str(node.get("type","")).strip().lower()
        if typ in {"application","custom","server"} and "priority_level" in node:
            # find attribute name
            name_val = None
            for k in NAME_KEYS:
                if k in node:
                    name_val = node.get(k)
                    break
            name = str(name_val or "").strip() if name_val is not None else ""
            if name:
                # normalize priority
                try: lvl = int(node.get("priority_level", 3))
                except Exception: lvl = 3
                lvl = 1 if lvl<=1 else (2 if lvl==2 else 3)
                out.append({
                    "attribute": name,
                    "priority_level": lvl,
                    "type": typ,
                    "blocks": str(node.get("blocks","Unspecified")).strip(),
                })
        for v in node.values():
            _collect_yaml_attrs(v, out)
    elif isinstance(node, list):
        for item in node:
            _collect_yaml_attrs(item, out)

def load_yaml_attrs(yaml_path: str) -> List[Dict[str, Any]]:
    if yaml is None:
        raise RuntimeError("PyYAML is required. Please install pyyaml.")
    with open(yaml_path, "r", encoding="utf-8") as fp:
        data = yaml.safe_load(fp)

    attrs: List[Dict[str, Any]] = []
    _collect_yaml_attrs(data, attrs)

    attrs.sort(key=lambda x: (x["priority_level"], x["attribute"].lower()))
    p1 = sum(1 for x in attrs if x["priority_level"]==1)
    p2 = sum(1 for x in attrs if x["priority_level"]==2)
    p3 = sum(1 for x in attrs if x["priority_level"]==3)
    print(f"[report-card] Loaded YAML attributes (application/custom only): total={len(attrs)} (P1={p1}, P2={p2}, P3={p3}) from {yaml_path}")
    return attrs

def load_mapping_dict(yaml_path: str, mapping_section: Optional[str], csv_path: str) -> Dict[str, str]:
    """Return {Program Attribute -> mapped source column} filtered to the CSV in use."""
    if yaml is None:
        raise RuntimeError("PyYAML is required. Please install pyyaml.")
    if not os.path.exists(yaml_path):
        print(f"[report-card] Mappings YAML not found: {yaml_path}")
        return {}

    with open(yaml_path, "r", encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}

    csv_base = os.path.basename(csv_path).strip().lower()
    reports = data.get("reports") or []

    selected: Optional[Dict[str, Any]] = None
    if mapping_section:
        for report in reports:
            name = str(report.get("name", "")).strip().lower()
            if name == str(mapping_section).strip().lower():
                selected = report
                break

    if selected is None:
        for report in reports:
            input_csv = str(report.get("input_csv1", "")).strip()
            if input_csv and os.path.basename(input_csv).strip().lower() == csv_base:
                selected = report
                break

    if selected is None and len(reports) == 1:
        selected = reports[0]

    if selected is None:
        print(f"[report-card] No mapping report resolved in {yaml_path}; proceeding with empty mapping.")
        return {}

    mapping_block = selected.get("program_attribute_mapping") or {}
    if not isinstance(mapping_block, dict):
        print(f"[report-card] program_attribute_mapping missing or invalid in {yaml_path}; proceeding empty.")
        return {}

    mapping: Dict[str, str] = {}
    total = 0
    for attr, col in mapping_block.items():
        total += 1
        if col is None:
            continue
        col_str = str(col).strip()
        if not col_str or col_str.lower() in {"null", "none", "nan"}:
            continue
        mapping[str(attr).strip()] = col_str
    print(f"[report-card] Loaded {len(mapping)}/{total} YAML mappings for CSV '{csv_base}' from {yaml_path}")
    return mapping

# ---------- Readiness & Payload ----------
def readiness_for_priority(attrs: List[Dict[str, Any]], mapping: Dict[str,str], row: Dict[str, Any], priority: int) -> Tuple[str, float]:
    subset = [a for a in attrs if a["priority_level"] == priority]
    if not subset:
        return ("green", 100.0)
    total = len(subset)
    good = 0
    for a in subset:
        col = mapping.get(a["attribute"])  # Program Attribute -> CSV column
        if not col:
            continue
        val = row.get(col)
        if non_empty(val):
            good += 1
    pct = (good / total) * 100.0
    status = "green" if pct >= 100.0 - 1e-9 else ("amber" if pct > 50.0 else "red")
    return (status, round(pct, 0))


# --- Milestone readiness (Priority 1 application/custom attrs filtered by blocks) ---
from typing import Set, Tuple

MP_BLOCKS: Set[str] = {"Discover","Program Strategy","Rationalize","Migration Planning"}
ME_BLOCKS: Set[str] = {"Discover","Program Strategy","Rationalize","Migration Planning","Migration Execution"}

def readiness_for_milestone(attrs: List[Dict[str, Any]], mapping: Dict[str,str], row: Dict[str, Any], blocks: Set[str]) -> Tuple[str, float]:
    subset = [a for a in attrs 
              if a.get("priority_level")==1 
              and str(a.get("type","")).lower() in {"application","custom"}
              and str(a.get("blocks","")).strip() in blocks]
    if not subset:
        return ("green", 100.0)
    total = len(subset)
    good = 0
    for a in subset:
        col = mapping.get(a["attribute"])
        if not col:
            continue
        val = row.get(col)
        if non_empty(val):
            good += 1
    pct = (good / total) * 100.0
    status = "green" if pct >= 100.0 - 1e-9 else ("amber" if pct > 50.0 else "red")
    return (status, round(pct, 0))

def build_payload(
    rows: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    attrs: List[Dict[str, Any]],
    mapping: Dict[str, str],
    server_index: Dict[str, Dict[str, Any]],
    server_columns: List[str],
) -> List[Dict[str, Any]]:
    cols = cfg.get("columns", {})
    id_col = cols.get("id", "ID")
    name_col = cols.get("name", "TITLE_NAME")
    acr_col = cols.get("acronym", "ACRONYM")
    dept_col = cols.get("department", "DEPARTMENT_NAME")
    server_map_col = mapping.get("Application to Server Mapping") or "Application to Server Mapping"
    env_candidates = ["ENVIRONMENT", "Environment", "Env"]
    dept_acr_candidates = ["DEPARTMENT_ACRONYM", "Department_Acronym", "Dept_Acronym", "Dept Acronym"]
    bureau_acr_candidates = ["BUREAU_ACRONYM", "Bureau_Acronym", "Agency_Acronym", "Agency Acronym"]

    out: List[Dict[str, Any]] = []
    for row in rows:
        app_id = str(row.get(id_col) or "").strip()
        if not app_id:
            continue
        env = get_row_value(row, env_candidates)
        app_id_display = f"{app_id}.{env}" if env else app_id
        name = str(row.get(name_col) or "").strip()
        acr  = str(row.get(acr_col) or "").strip()
        dept = str(row.get(dept_col) or "").strip()
        dept_acr = get_row_value(row, dept_acr_candidates)
        bureau_acr = get_row_value(row, bureau_acr_candidates)

        role_rows: List[Dict[str, Any]] = []
        if server_map_col not in row:
            if "SERVERNAME" in row:
                server_map_col = "SERVERNAME"
            elif "ServerName" in row:
                server_map_col = "ServerName"
            elif "Server" in row:
                server_map_col = "Server"
        mapped_servers = parse_mapped_servers(row.get(server_map_col))
        for a in attrs:
            label = a["attribute"]
            col   = mapping.get(label)  # mapping is Program Attribute -> CSV column name
            val   = row.get(col) if col else ""
            typ = str(a.get("type", "")).strip().lower()
            if label == "Application to Server Mapping":
                ok = non_empty(row.get(server_map_col))
                status_state = "green" if ok else "red"
                status_detail = ""
                value_str = str(row.get(server_map_col) or "")
            elif typ == "server":
                status_state, status_detail = server_attr_coverage_status(
                    mapped_servers,
                    server_index,
                    server_columns,
                    col,
                )
                ok = status_state == "green"
                value_str = ""
            else:
                ok = non_empty(val) if col else False
                status_state = "green" if ok else "red"
                status_detail = ""
                value_str = str(val or "")
            role_rows.append({
                "role": label,
                "mapped": clean_mapped_field(col),
                "value": value_str,
                "status": bool(ok),
                "status_state": status_state,
                "status_detail": status_detail,
                "priority": a["priority_level"],
                "block": a.get("blocks","Unspecified"),
            })

        mp_status, mp_pct = readiness_for_milestone(attrs, mapping, row, MP_BLOCKS)
        me_status, me_pct = readiness_for_milestone(attrs, mapping, row, ME_BLOCKS)

        out.append({
            "app_id": app_id,
            "app_id_display": app_id_display,
            "environment": env,
            "name": name,
            "acronym": acr,
            "department": dept,
            "department_acronym": dept_acr,
            "bureau_acronym": bureau_acr,
            "mp": mp_status, 'mp_pct': mp_pct, 'me': me_status, 'me_pct': me_pct,
            "roles": role_rows,
        })
    return out

def filter_payload_priority(payload: List[Dict[str, Any]], priority: int) -> List[Dict[str, Any]]:
    filtered: List[Dict[str, Any]] = []
    for entry in payload:
        roles = [r for r in entry.get("roles", []) if int(r.get("priority") or 0) == priority]
        new_entry = entry.copy()
        new_entry["roles"] = roles
        filtered.append(new_entry)
    return filtered

# ---------- HTML (token replacement; no str.format) ----------
HTML_TMPL = """
<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>__TITLE__</title>
<style>
:root{
  --bg:#0e1222;
  --panel:#151a30;
  --panel-soft:#0f1428;
  --text:#eaeef8;
  --muted:#9aa3b2;
  --accent:#c5d0ff;
  --border:rgba(255,255,255,0.07);
  --shadow:0 6px 24px rgba(0,0,0,0.35);
  --ok:#4ade80;
  --warn:#fbbf24;
  --bad:#f97373;
}
*{box-sizing:border-box}
body{
  margin:0;
  background:var(--bg);
  color:var(--text);
  font:13px/1.5 Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
  padding:24px;
}
a{color:var(--accent);text-decoration:none}
.wrap{max-width:1320px;margin:0 auto}
.hdr h1{margin:0 0 6px 0;font-size:24px;font-weight:700}
.muted{color:var(--muted);font-size:12px}
.controls{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0 18px}
input[type="text"],select{
  background:#0b1022;
  color:var(--text);
  border:1px solid var(--border);
  border-radius:8px;
  padding:8px 10px;
}
.grid{display:grid;grid-template-columns:1fr;gap:16px}
.card{
  border:1px solid var(--border);
  border-radius:12px;
  padding:12px 14px;
  background:var(--panel);
  box-shadow:var(--shadow);
}
summary{display:flex;align-items:center;gap:10px;cursor:pointer;list-style:none}
summary::-webkit-details-marker{display:none}
.chev{display:inline-block;transition:transform .15s ease;opacity:.8}
details[open] .chev{transform:rotate(90deg)}
.id{font-weight:700;color:var(--accent);font-variant-numeric:tabular-nums}
.name{font-weight:700;font-size:16px}
.acr{color:var(--muted);font-weight:600}
.tags{margin-left:auto;display:flex;gap:8px;flex-wrap:wrap}
.tag{font-size:12px;padding:3px 8px;border-radius:999px;border:1px solid var(--border);color:var(--muted);display:inline-flex;gap:6px;align-items:center}
.body{padding:14px;border-top:1px solid var(--border)}
.summary{margin:10px 0 16px}
.sum-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}
.sum-card{border:1px solid var(--border);border-radius:10px;padding:10px;background:var(--panel);box-shadow:var(--shadow)}
.sum-title{font-weight:700;margin-bottom:6px;color:var(--accent)}
.sum-rows{display:flex;gap:14px;align-items:center}
.sum-rows .pair{display:flex;align-items:center;gap:6px}
.rag-icon{
  display:inline-flex;
  align-items:center;
  justify-content:center;
  width:18px;
  height:18px;
  border-radius:999px;
  font-size:12px;
  font-weight:700;
}
.rag-icon.ok{background:rgba(74,222,128,0.2);color:var(--ok);box-shadow:0 0 0 1px rgba(74,222,128,0.35)}
.rag-icon.warn{background:rgba(251,191,36,0.2);color:var(--warn);box-shadow:0 0 0 1px rgba(251,191,36,0.35)}
.rag-icon.bad{background:rgba(249,115,115,0.2);color:var(--bad);box-shadow:0 0 0 1px rgba(249,115,115,0.45)}

.phase-grid{
  display:grid;
  grid-template-columns:repeat(3, minmax(240px, 1fr));
  gap:12px;
}
@media (max-width:1100px){
  .phase-grid{grid-template-columns:repeat(2, minmax(220px, 1fr));}
}
@media (max-width:720px){
  .phase-grid{grid-template-columns:1fr;}
}
.phase-card{
  border:1px solid var(--border);
  border-radius:10px;
  background:var(--panel-soft);
  padding:10px;
}
.phase-title{
  font-weight:700;
  color:var(--accent);
  margin-bottom:6px;
  font-size:13px;
}
.phase-content{
  border-left:2px solid rgba(197,208,255,0.2);
  padding-left:10px;
}
.tbl{width:100%;border-collapse:collapse}
.tbl th,.tbl td{padding:6px 8px;border-bottom:1px solid var(--border);vertical-align:top;font-size:12px}
.tbl th{color:var(--accent);text-align:left;background:var(--panel);position:relative}
.tbl td{color:var(--text)}
.status{text-align:center;font-size:14px}
.small{font-size:12px;color:var(--muted)}
</style>
</head>
<body>
<div class="wrap">
  <div class="hdr">
    <h1>__TITLE__</h1>
    <div class="muted">Per-application view. Source CSV: <code>__CSV__</code></div>
  </div>

  <div id="summary"></div>

  <div class="controls">
    <input id="search" type="text" placeholder="Search by name, acronym, id..."/>
    <select id="dept"><option value="">All Departments</option></select>
  </div>

  <div class="grid" id="cards">__INITIAL_CARDS__</div>
</div>

<script>
const DATA = __DATA__;
function esc(s){return (s??'').toString().replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;').replace(/'/g,'&#39;')}
function ragIcon(status){
  if(status==='green') return "<span class='rag-icon ok'>✓</span>";
  if(status==='amber') return "<span class='rag-icon warn'>●</span>";
  return "<span class='rag-icon bad'>✕</span>";
}


function summarize(data){
  const counts = { mp:{green:0,amber:0,red:0}, me:{green:0,amber:0,red:0} };
  for(const d of data){
    const mp = (d.mp||'').toLowerCase();
    const me = (d.me||'').toLowerCase();
    if(mp==='green') counts.mp.green++; else if(mp==='amber') counts.mp.amber++; else counts.mp.red++;
    if(me==='green') counts.me.green++; else if(me==='amber') counts.me.amber++; else counts.me.red++;
  }
  function block(title, obj){
    return `<div class="sum-card">
      <div class="sum-title">${title}</div>
      <div class="sum-rows">
        <span class="pair">${ragIcon('green')}<b>${obj.green}</b></span>
        <span class="pair">${ragIcon('amber')}<b>${obj.amber}</b></span>
        <span class="pair">${ragIcon('red')}<b>${obj.red}</b></span>
      </div>
    </div>`;
  }
  return `<div class="summary"><div class="sum-grid">${block('Migration Planning Ready', counts.mp)}${block('Migration Execution Ready', counts.me)}</div></div>`;
}
document.getElementById('summary').innerHTML = summarize(DATA);

function rowHTML(r){
  const statusState = (r.status_state || (r.status ? "green" : "red")).toLowerCase();
  const statusDetail = r.status_detail || "";
  const statusIcon = statusState === "green"
    ? "<span class='rag-icon ok'" + (statusDetail ? (" title='" + esc(statusDetail) + "'") : "") + ">✓</span>"
    : statusState === "amber"
      ? "<span class='rag-icon warn'" + (statusDetail ? (" title='" + esc(statusDetail) + "'") : "") + ">●</span>"
      : "<span class='rag-icon bad'" + (statusDetail ? (" title='" + esc(statusDetail) + "'") : "") + ">✕</span>";
  return "<tr>"
    + "<td>"+esc(r.role)+"</td>"
    + "<td class='muted'>"+esc(r.mapped||'')+"</td>"
    + "<td class='muted'>"+esc((r.value||'').replace(/;/g,'; '))+"</td>"
    + "<td class='status'>"+statusIcon+"</td>"
    + "</tr>";
}

function phaseHTML(title, rows){
  const body = rows.map(rowHTML).join("\\n") || "<tr><td colspan='4' class='muted'>No attributes found in this block.</td></tr>";
  return `
    <div class="phase-card">
      <div class="phase-title">${esc(title)}</div>
      <div class="phase-content">
        <table class="tbl" cellspacing="0" cellpadding="0">
          <thead><tr><th width="40%">Attribute</th><th width="25%">Mapped Field</th><th>Value</th><th width="70">Status</th></tr></thead>
          <tbody>${body}</tbody>
        </table>
      </div>
    </div>`;
}

function cardHTML(d){
  const all = (d.roles||[]);
  const ORDER = ["Discover","Program Strategy","Rationalize","Migration Planning","Migration Execution"];
  const sections = ORDER.map(b=>phaseHTML(b, all.filter(x => (x.block||"").toLowerCase()===b.toLowerCase()))).join("");
  const deptAcr = d.department_acronym ? `<span class="tag">Dept: <span class="small">${esc(d.department_acronym)}</span></span>` : "";
  const bureauAcr = d.bureau_acronym ? `<span class="tag">Agency: <span class="small">${esc(d.bureau_acronym)}</span></span>` : "";
  const appIdDisplay = d.app_id_display || d.app_id || '';
  return `
<details class="card" id="app-${esc(appIdDisplay)}" data-dept="${esc(d.department || '')}" data-id-raw="${esc(d.app_id || '')}">
  <summary>
    <span class="chev">▶</span>
    <span class="id">${esc(appIdDisplay)}</span>
    <span class="name">${esc(d.name)}</span>
    <span class="acr">${esc(d.acronym? '('+d.acronym+')':'')}</span>
    <span class="tags">
      ${deptAcr}${bureauAcr}
      <span class="tag">MP Ready: ${ragIcon(d.mp)} <span class="small">${d.mp_pct}%</span></span>
      <span class="tag">ME Ready: ${ragIcon(d.me)} <span class="small">${d.me_pct}%</span></span></span>
    </span>
  </summary>
  <div class="body">
    <div class="phase-grid">
      ${sections}
    </div>
  </div>
</details>`;
}

function render(list){ return list.map(cardHTML).join("\\n"); }

function init(){
  const grid = document.getElementById('cards');
  grid.innerHTML = render(DATA);

  const sel = document.getElementById('dept');
  const depts = Array.from(new Set(DATA.map(d => d.department).filter(Boolean))).sort();
  for(const d of depts){ const opt=document.createElement('option'); opt.value=d; opt.textContent=d; sel.appendChild(opt); }

  function applyFilters(){
    const q = (document.getElementById('search').value||'').toLowerCase();
    const dept = sel.value || '';
    const details = Array.from(document.querySelectorAll('#cards details.card'));
    for(const el of details){
      const name = (el.querySelector('.name')?.textContent||'').toLowerCase();
      const acr  = (el.querySelector('.acr')?.textContent||'').toLowerCase();
      const id   = (el.querySelector('.id')?.textContent||'').toLowerCase();
      const idRaw = (el.getAttribute('data-id-raw') || '').toLowerCase();
      const deptVal = (el.getAttribute('data-dept') || '').toLowerCase();
      const hit = (!q || name.includes(q) || acr.includes(q) || id.includes(q) || idRaw.includes(q))
        && (!dept || deptVal===dept.toLowerCase());
      el.style.display = hit ? '' : 'none';
    }
  }
  document.getElementById('search').addEventListener('input', applyFilters);
  sel.addEventListener('change', applyFilters);
}
document.addEventListener('DOMContentLoaded', init);
</script>
</body></html>
"""

def render_html(data: List[Dict[str, Any]], cfg: Dict[str, Any], csv_path: str) -> str:
    title = cfg.get("title") or DEFAULTS["title"]
    html = HTML_TMPL
    html = html.replace("__TITLE__", html_escape(title))
    html = html.replace("__CSV__", html_escape(csv_path))
    html = html.replace("__DATA__", json.dumps(data, separators=(",",":")))
    html = html.replace("__INITIAL_CARDS__", render(data))
    return html

def render(list_: List[Dict[str, Any]]) -> str:
    # Server-side prerender to match JS
    def rag_icon(status: str) -> str:
        if status == "green":
            return "<span class='rag-icon ok'>✓</span>"
        if status == "amber":
            return "<span class='rag-icon warn'>●</span>"
        return "<span class='rag-icon bad'>✕</span>"

    def status_icon(state: str, detail: str) -> str:
        title = f" title='{html_escape(detail)}'" if detail else ""
        if state == "green":
            return f"<span class='rag-icon ok'{title}>✓</span>"
        if state == "amber":
            return f"<span class='rag-icon warn'{title}>●</span>"
        return f"<span class='rag-icon bad'{title}>✕</span>"

    def row(r: Dict[str, Any]) -> str:
        state = str(r.get("status_state") or ("green" if r.get("status") else "red")).lower()
        detail = str(r.get("status_detail") or "")
        return ("<tr>"
                f"<td>{html_escape(r.get('role') or '')}</td>"
                f"<td class='muted'>{html_escape(r.get('mapped') or '')}</td>"
                f"<td class='muted'>{html_escape((r.get('value') or '').replace(';','; '))}</td>"
                f"<td class='status'>{status_icon(state, detail)}</td>"
                "</tr>")
    def phase_block(title: str, rows: List[Dict[str, Any]]) -> str:
        body = "\n".join(row(r) for r in rows) or "<tr><td colspan='4' class='muted'>No attributes found in this block.</td></tr>"
        return (f"<div class='phase-card'><div class='phase-title'>{html_escape(title)}</div>"
                "<div class='phase-content'>"
                "<table class='tbl' cellspacing='0' cellpadding='0'>"
                "<thead><tr><th width='40%'>Attribute</th><th width='25%'>Mapped Field</th><th>Value</th><th width='70'>Status</th></tr></thead>"
                f"<tbody>{body}</tbody></table></div></div>")
    out = []
    for d in list_:
        order = ["Discover","Program Strategy","Rationalize","Migration Planning","Migration Execution"]
        sections = ""
        for b in order:
            rows_b = [r for r in d.get("roles", []) if str(r.get("block","")).strip().lower()==b.lower()]
            sections += phase_block(b, rows_b)
        dept_acr = html_escape(d.get("department_acronym") or "")
        bureau_acr = html_escape(d.get("bureau_acronym") or "")
        dept_tag = f"<span class='tag'>Dept: <span class='small'>{dept_acr}</span></span>" if dept_acr else ""
        bureau_tag = f"<span class='tag'>Agency: <span class='small'>{bureau_acr}</span></span>" if bureau_acr else ""
        app_id_display = html_escape(d.get("app_id_display") or d.get("app_id") or "")
        card = f"""
<details class="card" id="app-{app_id_display}" data-dept="{html_escape(d.get('department') or '')}" data-id-raw="{html_escape(d.get('app_id') or '')}">
  <summary>
    <span class="chev">▶</span>
    <span class="id">{app_id_display}</span>
    <span class="name">{html_escape(d.get('name') or '')}</span>
    <span class="acr">{html_escape('(' + d.get('acronym') + ')' if d.get('acronym') else '')}</span>
    <span class="tags">
      {dept_tag}{bureau_tag}
      <span class="tag">MP Ready: {rag_icon(str(d.get('mp') or ''))} <span class="small">{int(d.get('mp_pct') or 0)}%</span></span>
      <span class="tag">ME Ready: {rag_icon(str(d.get('me') or ''))} <span class="small">{int(d.get('me_pct') or 0)}%</span></span></span>
    </span>
  </summary>
  <div class="body">
    <div class="phase-grid">
      {sections}
    </div>
  </div>
</details>
"""
        out.append(card)
    return "\n".join(out)

# ---------- Main ----------
def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Per-Application Report Cards (V8)")
    ap.add_argument("--yaml", default=DEFAULT_YAML, help="data_source_analysis.yaml path")
    ap.add_argument("--mappings", default=DEFAULT_MAPPINGS, help="data_source_analysis_mapping.yaml path")
    ap.add_argument("--mapping-section", default=None, help="Mapping report name to select from YAML")
    ap.add_argument("--csv", default=SOURCE_CSV, help="consolidated_application_view.csv path")
    ap.add_argument("--out", default=OUTPUT_HTML, help="Output HTML path")
    ap.add_argument("--title", default=DEFAULTS["title"])
    ap.add_argument("--open", action="store_true", help="Open the HTML in a browser")
    args = ap.parse_args(argv)

    cfg = DEFAULTS.copy()
    if args.title:
        cfg["title"] = args.title

    # Resolve paths under BTDM_ROOT
    csv_path = expand_under_root(args.csv)
    out_path = expand_under_root(args.out)
    out_p1_path = expand_under_root(OUTPUT_HTML_P1)
    yaml_path = args.yaml
    mappings_path = args.mappings

    logger.info(
        "Report-card run requested csv=%s out=%s yaml=%s mappings=%s",
        csv_path,
        out_path,
        yaml_path,
        mappings_path,
    )

    if not os.path.exists(csv_path):
        logger.error("CSV not found: %s", csv_path)
        raise SystemExit(f"CSV not found: {csv_path}")

    # ------------------------------------------------------------------
    # Timing check:
    # If output HTML is newer than (or same age as) the source CSV,
    # skip doing any work so this can run every minute efficiently.
    # ------------------------------------------------------------------
    if os.path.exists(out_path):
        try:
            source_mtime = os.path.getmtime(csv_path)
            output_mtime = os.path.getmtime(out_path)
        except OSError as e:
            logger.warning(
                "Failed to stat csv/output; proceeding with generation. csv=%s out=%s err=%s",
                csv_path,
                out_path,
                e,
            )
        else:
            p1_ready = os.path.exists(out_p1_path)
            p1_mtime = os.path.getmtime(out_p1_path) if p1_ready else 0
            if output_mtime >= source_mtime and p1_ready and p1_mtime >= source_mtime:
                logger.info(
                    "Skipping report generation; output is up-to-date. "
                    "output_mtime=%s priority_mtime=%s source_mtime=%s",
                    output_mtime,
                    p1_mtime,
                    source_mtime,
                )
                return 0

    # Proceed with normal pipeline
    rows = read_csv_rows(csv_path)
    logger.info("Loaded %d rows from %s", len(rows), csv_path)

    rows = apply_filters(rows, cfg.get("filters", []))
    logger.info("Rows after filters: %d", len(rows))

    attrs = load_yaml_attrs(yaml_path)
    logger.info("Loaded %d attributes from %s", len(attrs), yaml_path)

    mapping = load_mapping_dict(mappings_path, args.mapping_section, csv_path)

    server_csv = None
    try:
        with open(yaml_path, "r", encoding="utf-8") as fp:
            yaml_data = yaml.safe_load(fp) or {}
        server_csv = yaml_data.get("server_csv") or yaml_data.get("server_table")
    except Exception:
        server_csv = None

    if not server_csv:
        server_csv = os.path.join(BTDM_ROOT, "var", "tables", "views", "consolidated_server_view.csv")

    server_csv_path = expand_under_root(str(server_csv))
    server_rows: List[Dict[str, Any]] = []
    server_index: Dict[str, Dict[str, Any]] = {}
    server_columns: List[str] = []
    if os.path.exists(server_csv_path):
        try:
            server_rows = read_csv_rows(server_csv_path)
            if server_rows:
                server_columns = list(server_rows[0].keys())
                server_key_col = resolve_server_key_column(server_rows)
                server_index = build_server_index(server_rows, server_key_col)
                logger.info(
                    "Loaded server table rows=%s key_col=%s path=%s",
                    len(server_rows),
                    server_key_col,
                    server_csv_path,
                )
            else:
                logger.warning("Server table empty: %s", server_csv_path)
        except Exception as exc:
            logger.error("Failed to load server table %s: %s", server_csv_path, exc)
    else:
        logger.warning("Server table not found: %s", server_csv_path)

    payload = build_payload(rows, cfg, attrs, mapping, server_index, server_columns)

    html = render_html(payload, cfg, csv_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fp:
        fp.write(html)
    logger.info("Wrote report to %s", out_path)
    print(f"Wrote: {out_path}")

    payload_p1 = filter_payload_priority(payload, 1)
    html_p1 = render_html(payload_p1, cfg, csv_path)
    os.makedirs(os.path.dirname(out_p1_path), exist_ok=True)
    with open(out_p1_path, "w", encoding="utf-8") as fp:
        fp.write(html_p1)
    logger.info("Wrote priority-one report to %s", out_p1_path)
    print(f"Wrote: {out_p1_path}")
    if args.open:
        try:
            webbrowser.open('file://' + out_path)
        except Exception as exc:
            logger.warning("Failed to open browser for %s: %s", out_path, exc)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
