from __future__ import annotations

import os
import sys
import json
import argparse
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import yaml

# ------------------------------------------------------------
# Globals / Paths (VTX style)
# ------------------------------------------------------------
VTX_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
DEFAULT_CONFIG_YAML = os.path.join(VTX_ROOT, "usr", "config", "run", "publish_dictionaries.yaml")

# ------------------------------------------------------------
# Logging (BTDM / VTX style)
# ------------------------------------------------------------
try:
    sys.path.append(os.path.join(VTX_ROOT, "usr", "lib"))
    import btdm_logging  # type: ignore

    logger = btdm_logging.get_logger(component="publish_dictionary")
except Exception:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    logger = logging.getLogger("publish_dictionary")


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def resolve_vtx_path(path: str) -> str:
    """Resolve path that may be absolute, or relative to VTX_ROOT."""
    if not path:
        return path
    # Expand env vars + user (~)
    path = os.path.expandvars(os.path.expanduser(path))
    # Absolute (Windows drive, UNC, POSIX root) => keep
    if os.path.isabs(path):
        return path
    return os.path.join(VTX_ROOT, path)


def safe_read_yaml(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def ensure_parent_dir(file_path: str) -> None:
    parent = os.path.dirname(file_path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def file_mtime(path: str) -> Optional[float]:
    try:
        return os.path.getmtime(path)
    except Exception:
        return None


def newest_mtime(paths: List[str]) -> Optional[float]:
    mtimes = [file_mtime(p) for p in paths]
    mtimes = [m for m in mtimes if m is not None]
    return max(mtimes) if mtimes else None


def is_output_fresh(output_path: str, input_paths: List[str]) -> bool:
    out_m = file_mtime(output_path)
    if out_m is None:
        return False
    in_m = newest_mtime(input_paths)
    if in_m is None:
        return False
    return out_m >= in_m


def normalize_priority_level(v: Any) -> Optional[int]:
    if v is None:
        return None
    # allow strings like "1", "p1"
    if isinstance(v, int):
        return v
    try:
        s = str(v).strip().lower()
        if s.startswith("p") and s[1:].isdigit():
            return int(s[1:])
        if s.isdigit():
            return int(s)
    except Exception:
        return None
    return None


def html_escape(s: Any) -> str:
    if s is None:
        return ""
    text = str(s)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def get_key_any(d: Dict[str, Any], keys: List[str], default: Any = None) -> Any:
    """Try multiple key spellings."""
    for k in keys:
        if k in d:
            return d.get(k)
    return default


def coerce_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]


def stable_sort(items: List[Dict[str, Any]], sort_spec: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Stable multi-sort.
    Supports optional per-key custom ordering:

    sort:
      - key: blocks
        order: asc
        custom_order: [Discover, Program Strategy, ...]
      - key: attribute
        order: asc
    """
    out = items[:]

    for spec in reversed(sort_spec or []):
        if not isinstance(spec, dict):
            continue

        key = str(spec.get("key", "") or "")
        order = (str(spec.get("order", "asc") or "asc")).lower()
        reverse = order == "desc"

        custom_order = spec.get("custom_order")
        custom_rank: Dict[str, int] = {}
        if isinstance(custom_order, list):
            # rank by the explicit list order (case-sensitive match by default)
            custom_rank = {str(v): i for i, v in enumerate(custom_order)}

        def sort_val(it: Dict[str, Any]) -> Any:
            v = it.get(key)

            # Special handling for numeric priority_level
            if key == "priority_level":
                pv = normalize_priority_level(v)
                return pv if pv is not None else 999

            # Custom ordering for strings (ex: blocks/phase)
            if custom_rank:
                sv = "" if v is None else str(v)
                # items not in list fall to bottom
                return (custom_rank.get(sv, 10**9), sv.lower())

            # Default: case-insensitive string sort, None-safe
            if v is None:
                return ""
            return str(v).lower()

        out.sort(key=sort_val, reverse=reverse)

    return out


# ------------------------------------------------------------
# HTML Builder (palette inspired by your Program Timeline sample)
# ------------------------------------------------------------
def build_html(
    title: str,
    subtitle: str,
    rows: List[Dict[str, Any]],
    fields: List[Dict[str, str]],
    generated_at: str,
) -> str:
    # Prepare distinct filters
    phases = sorted({str(r.get("blocks") or "").strip() for r in rows if str(r.get("blocks") or "").strip()})
    prios = sorted({normalize_priority_level(r.get("priority_level")) for r in rows if normalize_priority_level(r.get("priority_level")) is not None})
    prios = [p for p in prios if p is not None]

    # Embed data for client-side filtering
    # Keep payload small: only publish what we need
    payload = []
    for r in rows:
        item = {
            "attribute": r.get("attribute", ""),
            "blocks": r.get("blocks", ""),
            "priority_level": normalize_priority_level(r.get("priority_level")),
            "data": {},
        }
        for f in fields:
            k = f.get("key", "")
            item["data"][k] = r.get(k, "")
        payload.append(item)

    data_json = json.dumps(payload, ensure_ascii=False)

    # CSS variables aligned to the uploaded sample
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_escape(title)}</title>
<style>
:root{{
  --bg:#0b1220; --panel:#121a2b; --grid:#202c46; --txt:#e9eefc; --muted:#a7b3d1;
  --accent:#7aa2ff; --chip:#1b2540; --label:#FFC147;
  --p1:#ff5a5f; --p2:#FFC147; --p3:#4CAF50;
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--txt);font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial}}
.wrapper{{padding:24px;max-width:1400px;margin:0 auto}}
h1{{margin:0 0 6px 0;font-size:28px;letter-spacing:.2px}}
.subtitle{{color:var(--muted);margin-bottom:14px}}
.meta{{color:var(--muted);font-size:12px;margin-bottom:14px}}
.toolbar{{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:10px 0 16px}}
.input{{background:var(--chip);border:1px solid var(--grid);color:var(--txt);border-radius:10px;padding:8px 10px;font-size:13px;outline:none}}
.input:focus{{border-color:var(--accent)}}
.btn{{background:var(--chip);border:1px solid var(--grid);color:var(--txt);border-radius:10px;font-size:12px;padding:7px 10px;cursor:pointer}}
.btn:hover{{border-color:var(--accent)}}
.pills{{display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
.pill{{display:inline-flex;gap:6px;align-items:center;background:var(--chip);border:1px solid var(--grid);border-radius:999px;padding:6px 10px;font-size:12px;color:var(--txt);cursor:pointer;user-select:none}}
.pill input{{accent-color:var(--accent)}}
.pill .tag{{color:var(--muted)}}
.stats{{margin-left:auto;color:var(--muted);font-size:12px}}
.grid{{display:grid;grid-template-columns:repeat(2,minmax(280px,1fr));gap:14px}}
@media (max-width: 980px){{ .grid{{grid-template-columns:1fr}} .stats{{margin-left:0}} }}
.card{{background:var(--panel);border:1px solid var(--grid);border-radius:14px;overflow:hidden}}
.card-head{{padding:12px 12px;border-bottom:1px solid var(--grid);display:flex;gap:10px;align-items:flex-start}}
.card-title{{font-size:15px;font-weight:700;line-height:1.2;flex:1}}
.chips{{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}}
.chip{{background:var(--chip);border:1px solid var(--grid);border-radius:999px;padding:4px 8px;font-size:11px;color:var(--muted)}}
.chip strong{{color:var(--txt);font-weight:700}}
.pri{{font-style:italic}}
.pri.p1{{color:var(--p1)}}
.pri.p2{{color:var(--p2)}}
.pri.p3{{color:var(--p3)}}
.card-body{{padding:12px 12px 14px 12px}}
.row{{margin-bottom:10px}}
.k{{color:var(--label);font-weight:700;font-size:12px;margin-bottom:4px}}
.v{{color:var(--txt);font-size:13px;line-height:1.35;white-space:pre-wrap}}
.muted{{color:var(--muted)}}
.footer{{margin-top:18px;color:var(--muted);font-size:12px}}
</style>
</head>
<body>
<div class="wrapper">
  <h1>{html_escape(title)}</h1>
  <div class="subtitle">{html_escape(subtitle)}</div>
  <div class="meta">Generated: {html_escape(generated_at)}</div>

  <div class="toolbar">
    <input id="q" class="input" type="search" placeholder="Search attributes or text…" />

    <button id="clear" class="btn" title="Clear search + filters">Clear</button>

    <div class="stats" id="stats"></div>
  </div>

  <div class="toolbar" style="margin-top:-6px;">
    <div class="pills" id="phasePills" title="Filter by Phase"></div>
    <div class="pills" id="prioPills" title="Filter by Priority"></div>
  </div>

  <div class="grid" id="grid"></div>

  <div class="footer">
    Tip: combine Phase + Priority filters, then use search for fast narrowing.
  </div>
</div>

<script>
const DATA = {data_json};

const FIELDS = {json.dumps(fields, ensure_ascii=False)};

const phases = {json.dumps(phases, ensure_ascii=False)};
const prios = {json.dumps(prios, ensure_ascii=False)};

const state = {{
  q: "",
  phases: new Set(),
  prios: new Set(),
}};

function priClass(p) {{
  if (p === 1) return "p1";
  if (p === 2) return "p2";
  if (p === 3) return "p3";
  return "";
}}

function labelPri(p) {{
  if (p === 1) return "p1";
  if (p === 2) return "p2";
  if (p === 3) return "p3";
  return "—";
}}

function renderPills(container, items, kind) {{
  container.innerHTML = "";
  items.forEach(val => {{
    const id = kind + "_" + String(val).replace(/\\s+/g,"_").replace(/[^a-zA-Z0-9_]/g,"");
    const pill = document.createElement("label");
    pill.className = "pill";
    pill.innerHTML = `
      <input type="checkbox" id="${{id}}">
      <span>${{val}}</span>
      <span class="tag">(${{kind}})</span>
    `;
    pill.querySelector("input").addEventListener("change", (e) => {{
      const set = (kind === "phase") ? state.phases : state.prios;
      if (e.target.checked) set.add(String(val));
      else set.delete(String(val));
      apply();
    }});
    container.appendChild(pill);
  }});
}}

function matchesFilters(item) {{
  // Phase
  if (state.phases.size > 0) {{
    const b = String(item.blocks || "");
    if (!state.phases.has(b)) return false;
  }}
  // Priority
  if (state.prios.size > 0) {{
    const p = item.priority_level == null ? "" : String(item.priority_level);
    if (!state.prios.has(p)) return false;
  }}
  // Search
  if (state.q) {{
    const hay = (JSON.stringify(item).toLowerCase());
    if (!hay.includes(state.q)) return false;
  }}
  return true;
}}

function cardHtml(item) {{
  const attr = item.attribute || "";
  const phase = item.blocks || "—";
  const pr = item.priority_level;
  const prLabel = labelPri(pr);
  const prCls = priClass(pr);

  let body = "";
  FIELDS.forEach(f => {{
    const key = f.key;
    const label = f.label || key;
    const val = (item.data && item.data[key] != null) ? String(item.data[key]) : "";
    body += `
      <div class="row">
        <div class="k">${{label}}</div>
        <div class="v">${{val ? escapeHtml(val) : '<span class="muted">—</span>'}}</div>
      </div>
    `;
  }});

  return `
    <div class="card">
      <div class="card-head">
        <div class="card-title">${{escapeHtml(attr)}}</div>
        <div class="chips">
          <div class="chip"><strong>Phase:</strong> ${{escapeHtml(phase)}}</div>
          <div class="chip"><strong>Priority:</strong> <span class="pri ${{prCls}}">${{escapeHtml(prLabel)}}</span></div>
        </div>
      </div>
      <div class="card-body">${{body}}</div>
    </div>
  `;
}}

function escapeHtml(s) {{
  return String(s)
    .replaceAll("&","&amp;")
    .replaceAll("<","&lt;")
    .replaceAll(">","&gt;")
    .replaceAll('"',"&quot;")
    .replaceAll("'","&#x27;");
}}

function apply() {{
  const grid = document.getElementById("grid");
  const filtered = DATA.filter(matchesFilters);
  grid.innerHTML = filtered.map(cardHtml).join("");
  document.getElementById("stats").textContent =
    `${{filtered.length}} / ${{DATA.length}} attributes`;
}}

document.addEventListener("DOMContentLoaded", () => {{
  renderPills(document.getElementById("phasePills"), phases, "phase");
  renderPills(document.getElementById("prioPills"), prios.map(p => String(p)), "prio");

  const q = document.getElementById("q");
  q.addEventListener("input", () => {{
    state.q = q.value.trim().toLowerCase();
    apply();
  }});

  document.getElementById("clear").addEventListener("click", () => {{
    state.q = "";
    state.phases.clear();
    state.prios.clear();
    q.value = "";
    // uncheck all boxes
    document.querySelectorAll('.pill input[type="checkbox"]').forEach(cb => cb.checked = false);
    apply();
  }});

  apply();
}});
</script>
</body>
</html>
"""


# ------------------------------------------------------------
# Job Runner
# ------------------------------------------------------------
def load_publish_config(config_path: str) -> Dict[str, Any]:
    cfg = safe_read_yaml(config_path)
    if not isinstance(cfg, dict):
        raise ValueError(f"Config YAML is not a dict: {config_path}")
    return cfg


def collect_rows_from_sources(
    source_paths: List[str],
    list_key: str,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for p in source_paths:
        doc = safe_read_yaml(p)
        if not isinstance(doc, dict):
            logger.warning("Skipping source (not a dict): %s", p)
            continue

        items = doc.get(list_key)
        if items is None:
            logger.warning("Source missing list_key '%s': %s", list_key, p)
            continue
        if not isinstance(items, list):
            logger.warning("list_key '%s' not a list in %s", list_key, p)
            continue

        for it in items:
            if isinstance(it, dict):
                rows.append(it)
            else:
                # non-dict entry - ignore
                pass
    return rows


def apply_includes(rows: List[Dict[str, Any]], include_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    phases = set(str(x) for x in coerce_list(include_cfg.get("phases")) if str(x).strip())
    prios = set(str(x) for x in coerce_list(include_cfg.get("priorities")) if str(x).strip())
    types = set(str(x).lower() for x in coerce_list(include_cfg.get("types")) if str(x).strip())

    if not phases and not prios and not types:
        return rows

    out: List[Dict[str, Any]] = []
    for r in rows:
        if phases:
            b = str(get_key_any(r, ["blocks", "block", "phase"], "") or "")
            if b not in phases:
                continue
        if prios:
            p = normalize_priority_level(get_key_any(r, ["priority_level", "priority"], None))
            if p is None or str(p) not in prios:
                continue
        if types:
            t = str(get_key_any(r, ["type"], "") or "").lower()
            if t not in types:
                continue
        out.append(r)
    return out

def apply_excludes(rows: List[Dict[str, Any]], exclude_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Exclude rows matching simple criteria.
    Currently supports:
      exclude:
        types: ["general", "custom"]
    """
    types = set(str(x).lower() for x in coerce_list(exclude_cfg.get("types")) if str(x).strip())

    if not types:
        return rows

    out: List[Dict[str, Any]] = []
    for r in rows:
        t = str(get_key_any(r, ["type"], "") or "").lower().strip()
        if t and t in types:
            continue
        out.append(r)
    return out

def run_job(job: Dict[str, Any], config_path: str) -> Tuple[bool, str]:
    job_id = job.get("id", "unknown")
    if not job.get("enabled", True):
        return False, f"[{job_id}] disabled"

    title = str(job.get("title") or job_id)
    subtitle = str(job.get("subtitle") or "")

    sources_raw = job.get("sources") or []
    if not isinstance(sources_raw, list) or not sources_raw:
        return False, f"[{job_id}] no sources configured"

    sources = [resolve_vtx_path(str(p)) for p in sources_raw]
    list_key = str(job.get("list_key") or "criteria")

    output_html = resolve_vtx_path(str(job.get("output_html") or ""))
    if not output_html:
        return False, f"[{job_id}] output_html missing"

    fields = job.get("fields") or []
    if not isinstance(fields, list) or not fields:
        return False, f"[{job_id}] fields missing"


    # Load rows
    raw_rows = collect_rows_from_sources(sources, list_key)

    # Basic normalize: ensure attribute present
    norm_rows: List[Dict[str, Any]] = []
    for r in raw_rows:
        # Skip anything without attribute anchor
        attr = get_key_any(r, ["attribute", "name", "anchor"], None)
        if not attr:
            continue

        nr = dict(r)
        nr["attribute"] = attr

        # Normalize common typos (optional)
        # Some of your sample entries have "bloocks" - treat as blocks if blocks missing
        if "blocks" not in nr and "bloocks" in nr:
            nr["blocks"] = nr.get("bloocks")

        norm_rows.append(nr)

    # Apply include filters from config
    include_cfg = job.get("include") or {}
    if not isinstance(include_cfg, dict):
        include_cfg = {}
    norm_rows = apply_includes(norm_rows, include_cfg)

    # Apply exclude filters from config
    exclude_cfg = job.get("exclude") or {}
    if not isinstance(exclude_cfg, dict):
        exclude_cfg = {}
    norm_rows = apply_excludes(norm_rows, exclude_cfg)

    # Sorting
    sort_spec = job.get("sort") or []
    if not isinstance(sort_spec, list):
        sort_spec = []
    norm_rows = stable_sort(norm_rows, sort_spec)

    # Build generated timestamp
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")

    # Build HTML rows contain only requested field keys + a few base keys
    publish_rows: List[Dict[str, Any]] = []
    for r in norm_rows:
        pr = dict(r)
        # Ensure published keys exist
        for f in fields:
            k = f.get("key", "")
            if k and k not in pr:
                pr[k] = pr.get(k, "")
        publish_rows.append(pr)

    html = build_html(
        title=title,
        subtitle=subtitle,
        rows=publish_rows,
        fields=fields,
        generated_at=generated_at,
    )

    ensure_parent_dir(output_html)
    with open(output_html, "w", encoding="utf-8") as f:
        f.write(html)

    return True, f"[{job_id}] wrote {output_html}"


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Publish data dictionaries (HTML) from VTX comprehensive attribute YAMLs.")
    ap.add_argument("--config", default=DEFAULT_CONFIG_YAML, help="Path to publish_dictionaries.yaml")
    ap.add_argument("--job", default="", help="Run only a single job id")
    ap.add_argument("--all", action="store_true", help="Run all enabled jobs (default behavior if --job not set)")
    args = ap.parse_args()

    config_path = resolve_vtx_path(args.config)

    if not os.path.exists(config_path):
        logger.error("Config not found: %s", config_path)
        return 2

    cfg = load_publish_config(config_path)
    jobs = cfg.get("jobs") or []
    if not isinstance(jobs, list) or not jobs:
        logger.error("No jobs found in config: %s", config_path)
        return 2

    target_job = (args.job or "").strip()

    ran_any = False
    wrote_any = False

    for job in jobs:
        if not isinstance(job, dict):
            continue

        job_id = str(job.get("id", "")).strip()
        if target_job and job_id != target_job:
            continue

        ran_any = True
        ok, msg = run_job(job, config_path=config_path)
        if ok:
            wrote_any = True
            logger.info(msg)
        else:
            logger.info(msg)

    if not ran_any:
        if target_job:
            logger.error("No matching job id found: %s", target_job)
        else:
            logger.error("No runnable jobs found.")
        return 2

    return 0 if wrote_any or args.force else 0


if __name__ == "__main__":
    raise SystemExit(main())