#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
program_timeline.py — robust discovery of lane memberships + priority & category UI
- Uses common globals for paths & logging.
- Detects attribute membership if:
    * mapping has `blocks` as list OR single string (supports comma/semicolon/pipe separated),
    * OR any other string/list field contains lane names.
- Attribute name inference: name|title|attribute|id|parent-key
- Description fields: description|desc
- How Used fields: how_used|"how used"|usage
- Optional fields per attribute stanza
    * priority_level: 1..3 (rendered as p1/p2/p3; sorted 1→3 within lane; tooltip shows meaning)
    * category: free text; appended to tooltip; if equals "custom" (case-insensitive), title is colored yellow
    * recommended_source: free text; shown in tooltip as "Recommended Source"
- Fixed lanes: Discover, Program Strategy, Rationalize, Migration Planning, Migration Execution

Updates (2025-09-18):
1) Custom attributes now use yellow font color (no ring).
2) Add per-lane controls to collapse p2 and p3 ("Focus p1").
3) Replace circular badges with italicized priority text (p1=red, p2=yellow, p3=green) right-aligned in each card.
4) Tooltip includes Recommended Source if present.
"""

import os
import sys
import re
import html
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple, Set, Optional

# ---- Common globals ----
BTDM_ROOT   = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
OUTPUT_DIR  = os.path.join(BTDM_ROOT, "var", "reporting")
CONFIG_PATH = os.path.join(BTDM_ROOT, "usr", "config", "default", "data_source_analysis.yaml")

# ------------------------------------------------------------
# Logging (BTDM/VTX style)
# ------------------------------------------------------------
try:
    sys.path.append(os.path.join(VTX_ROOT, "usr", "lib"))
    import btdm_logging  # type: ignore

    logger = btdm_logging.get_logger(component="program_timeline")
except Exception:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    logger = logging.getLogger("program_timeline")

# ---- External deps ----
try:
    import yaml
except Exception as e:
    logger.error("PyYAML not installed. Please `pip install pyyaml`. %s", e)
    raise

# ---- Constants ----
LANES: List[str] = [
    "Discover",
    "Program Strategy",
    "Rationalize",
    "Migration Planning",
    "Migration Execution",
]
LANE_SET: Set[str] = set(LANES)
_LANE_CANON = {ln.lower(): ln for ln in LANES}

PRIORITY_TO_LABEL = {1: "p1", 2: "p2", 3: "p3"}
PRIORITY_TO_TEXT  = {1: "Must Have", 2: "Should Have", 3: "Nice to have"}

# ---- Helpers ----
def _coalesce(*vals):
    for v in vals:
        if v is not None:
            return v
    return None

def _split_lanes_string(s: str) -> List[str]:
    # allow comma/semicolon/pipe separated lane values
    parts = re.split(r'[;,|]', s)
    out: List[str] = []
    seen: Set[str] = set()
    for p in parts:
        k = p.strip().lower()
        if k in _LANE_CANON:
            canon = _LANE_CANON[k]
            if canon not in seen:
                seen.add(canon)
                out.append(canon)
    # preserve global order
    return [lane for lane in LANES if lane in out]

def _list_intersects_lanes(v) -> Optional[List[str]]:
    """If v is a list of strings/numbers and intersects LANES (case-insensitive), return list in canonical order."""
    if isinstance(v, list):
        present: Set[str] = set()
        for x in v:
            if isinstance(x, (str, int, float)):
                k = str(x).strip().lower()
                if k in _LANE_CANON:
                    present.add(_LANE_CANON[k])
        if present:
            return [lane for lane in LANES if lane in present]
    return None

def _normalize_blocks_from_mapping(mapping: Dict[str, Any]) -> List[str]:
    """Extract lane names from a mapping using multiple strategies."""
    # 1) Explicit "blocks": support list OR single string
    if "blocks" in mapping:
        val = mapping["blocks"]
        if isinstance(val, str):
            lanes = _split_lanes_string(val)
            if lanes:
                return lanes
        elif isinstance(val, list):
            present: Set[str] = set()
            for item in val:
                if isinstance(item, str):
                    present.update(_split_lanes_string(item))
                elif isinstance(item, dict):
                    nm = _coalesce(item.get("name"), item.get("title"), item.get("block"), item.get("id"))
                    if isinstance(nm, str):
                        present.update(_split_lanes_string(nm))
            if present:
                return [lane for lane in LANES if lane in present]

    # 2) Heuristic: any string-valued key with lane names inside
    for _, v in mapping.items():
        if isinstance(v, str):
            lanes = _split_lanes_string(v)
            if lanes:
                return lanes

    # 3) Heuristic: any list-valued key that intersects lanes
    for _, v in mapping.items():
        lanes = _list_intersects_lanes(v)
        if lanes:
            return lanes

    return []

def _extract_attribute_mappings(obj, parent_keys: List[str] = None) -> List[Tuple[str, Dict[str, Any]]]:
    """Return list of (inferred_name, mapping) for mappings that appear to be attributes with lane membership."""
    parent_keys = parent_keys or []
    out: List[Tuple[str, Dict[str, Any]]] = []

    if isinstance(obj, dict):
        lanes_here = _normalize_blocks_from_mapping(obj)
        if lanes_here:
            name = _coalesce(obj.get("name"), obj.get("title"), obj.get("attribute"), obj.get("id"))
            if name is None and parent_keys:
                name = parent_keys[-1]
            if name is None:
                name = "Unnamed Attribute"
            mp = dict(obj)
            mp["_normalized_lanes_"] = lanes_here
            out.append((str(name), mp))
        for k, v in obj.items():
            out.extend(_extract_attribute_mappings(v, parent_keys + [str(k)]))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_extract_attribute_mappings(v, parent_keys + [f"[{i}]"]))
    return out


def _collect_by_lane(yaml_root: Any) -> Dict[str, List[Dict[str, Any]]]:
    candidates = _extract_attribute_mappings(yaml_root)
    lanes: Dict[str, List[Dict[str, Any]]] = {lane: [] for lane in LANES}
    for name, mapping in candidates:
        desc = _coalesce(mapping.get("description"), mapping.get("desc"))
        how_used = _coalesce(mapping.get("how_used"), mapping.get("how used"), mapping.get("usage"))
        # Optional fields
        pri_raw = mapping.get("priority_level")
        try:
            priority_level = int(pri_raw) if pri_raw is not None else None
        except Exception:
            priority_level = None
        category = mapping.get("category")
        rec_source = mapping.get("recommended_source")

        lane_list = mapping.get("_normalized_lanes_", [])
        for lane in lane_list:
            lanes[lane].append({
                "name": name,
                "description": desc,
                "how_used": how_used,
                "priority_level": priority_level,
                "category": category,
                "recommended_source": rec_source,
            })

    # Sort within each lane by priority (1->3), then by name
    for lane, items in lanes.items():
        def sort_key(it: Dict[str, Any]):
            pri = it.get("priority_level")
            pri_sort = pri if isinstance(pri, int) else 999  # unspecified sink to bottom
            return (pri_sort, str(it.get("name") or ""))
        items.sort(key=sort_key)

    return lanes

# ---- HTML/CSS/JS ----

def _inline_css() -> str:
    return r"""
:root{
  --bg:#0b1220; --panel:#121a2b; --grid:#202c46; --txt:#e9eefc; --muted:#a7b3d1;
  --accent:#7aa2ff; --chip:#1b2540; --step:22px; --label:#FFC147;
  --p1:#ff5a5f; --p2:#FFC147; --p3:#4CAF50;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial}
.wrapper{padding:24px;max-width:1400px;margin:0 auto}
h1{margin:0 0 8px 0;font-size:28px}
.subtitle{color:var(--muted);margin-bottom:16px}
.controls{display:flex;gap:10px;flex-wrap:wrap;margin:8px 0 16px}
.controls .hint{color:var(--muted);font-size:12px}
.board{display:grid;grid-template-columns:repeat(5,1fr);gap:16px}
.lane{background:var(--panel);border:1px solid var(--grid);border-radius:14px;overflow:hidden;min-height:120px;position:relative}
.lane-head{padding:10px 12px;border-bottom:1px solid var(--grid);font-weight:600;letter-spacing:.2px;display:flex;justify-content:space-between;align-items:center}
.lane-body{padding:12px 12px 18px 12px}
.card{background:var(--chip);border:1px solid var(--grid);border-radius:12px;padding:8px 10px;margin-bottom:8px;position:relative;cursor:default}
.card:hover{border-color:var(--accent)}
.card-title{font-size:14px;font-weight:600;line-height:1.25;padding-right:44px}
/* priority indicator (italic, right-aligned) */
.pri{position:absolute;right:10px;top:8px;font-style:italic;font-size:12px;opacity:.95}
.pri[data-pri="p1"]{color:var(--p1)}
.pri[data-pri="p2"]{color:var(--p2)}
.pri[data-pri="p3"]{color:var(--p3)}
/* custom category: yellow title text */
.custom-title{color:var(--label)}
/* lane-level p1 focus toggles */
.lane-body[data-hide-p2="1"] .card[data-priority="p2"],
.lane-body[data-hide-p3="1"] .card[data-priority="p3"]{display:none}
/* tooltip */
.tooltip{position:absolute;z-index:10;display:none;max-width:460px;background:#0e1a33;border:1px solid #2a3b63;border-radius:10px;padding:10px 12px;color:var(--txt);box-shadow:0 10px 30px rgba(0,0,0,.4)}
.tooltip .t-h{font-weight:700;margin-bottom:6px}
.tooltip .t-l{color:var(--muted);font-size:12px;line-height:1.35}
.tooltip .t-l strong{color:var(--label);}
/* buttons */
.btn{background:#1b2540;border:1px solid var(--grid);color:var(--txt);border-radius:10px;font-size:12px;padding:6px 10px;cursor:pointer}
.btn:hover{border-color:var(--accent)}
"""


def _inline_js() -> str:
    return r"""
document.addEventListener('DOMContentLoaded', () => {
  const tip = document.getElementById('tooltip');
  function priExplain(pri){
    if(pri==='p1') return 'Must Have';
    if(pri==='p2') return 'Should Have';
    if(pri==='p3') return 'Nice to have';
    return '—';
  }
  function showTip(e, name, desc, used, cat, pri, recsrc){
    tip.innerHTML = '<div class="t-h">'+name+'</div>' +
      '<div class="t-l"><strong>Description:</strong> '+(desc || '—')+'</div>' +
      '<div class="t-l"><strong>How Used:</strong> '+(used || '—')+'</div>' +
      '<div class="t-l"><strong>Category:</strong> '+(cat || '—')+'</div>' +
      (recsrc ? '<div class="t-l"><strong>Recommended Source:</strong> '+recsrc+'</div>' : '') +
      '<div class="t-l"><strong>Priority:</strong> '+(pri || '—')+' · '+priExplain(pri)+'</div>';
    tip.style.display = 'block';
    const pad = 12;
    let x = e.pageX + pad, y = e.pageY + pad;
    const rect = tip.getBoundingClientRect();
    const vw = window.innerWidth, vh = window.innerHeight;
    if (x + rect.width > vw - 10) x = vw - rect.width - 10;
    if (y + rect.height > vh - 10) y = vh - rect.height - 10;
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  }
  function hideTip(){ tip.style.display='none'; }

  // Card tooltips
  document.querySelectorAll('.card').forEach(el => {
    el.addEventListener('mousemove', e => showTip(
      e,
      el.dataset.name,
      el.dataset.description,
      el.dataset.howused,
      el.dataset.category,
      el.dataset.priority,
      el.dataset.recsource
    ));
    el.addEventListener('mouseleave', hideTip);
  });

  // Per-lane focus toggles
  document.querySelectorAll('.lane').forEach(lane => {
    const body = lane.querySelector('.lane-body');
    const btnFocus = lane.querySelector('.btn-focus');
    const btnAll = lane.querySelector('.btn-all');
    if(btnFocus){
      btnFocus.addEventListener('click', () => {
        body.setAttribute('data-hide-p2','1');
        body.setAttribute('data-hide-p3','1');
      });
    }
    if(btnAll){
      btnAll.addEventListener('click', () => {
        body.removeAttribute('data-hide-p2');
        body.removeAttribute('data-hide-p3');
      });
    }
  });
});
"""


def _render_html(lanes_dict: Dict[str, List[Dict[str, Any]]]) -> str:
    lane_names = LANES
    #counts = [len(lanes_dict.get(l, [])) for l in lane_names]
    #offsets: List[int] = []
    #cum = 0
    #for c in counts:
    #    offsets.append(cum)
    #    cum += c

    cols: List[str] = []
    for idx, lane in enumerate(lane_names):
        items = lanes_dict.get(lane, [])
        #top_offset = offsets[idx]
        cards: List[str] = []
        for it in items:
            nm_raw = str(it.get("name") or "")
            nm_esc = html.escape(nm_raw)
            desc = html.escape(str(it.get("description"))) if it.get("description") is not None else ""
            used = html.escape(str(it.get("how_used"))) if it.get("how_used") is not None else ""
            recsrc_raw = it.get("recommended_source")
            recsrc = html.escape(str(recsrc_raw)) if recsrc_raw is not None else ""

            # Priority rendering
            pri_num = it.get("priority_level")
            pri_lbl = PRIORITY_TO_LABEL.get(pri_num, "—")
            pri_data = pri_lbl if pri_lbl in ("p1","p2","p3") else ""
            pri_el = f'<div class="pri" data-pri="{pri_data}">{pri_data}</div>' if pri_data else ""

            # Category & custom title color
            cat_raw = it.get("category")
            cat = html.escape(str(cat_raw)) if cat_raw is not None else ""
            is_custom = str(cat_raw).strip().lower() == "custom" if cat_raw is not None else False
            title_inner = f'<span class="custom-title">{nm_esc}</span>' if is_custom else nm_esc

            cards.append(
                '<div class="card" '
                f' data-name="{nm_esc}"'
                f' data-description="{desc}"'
                f' data-howused="{used}"'
                f' data-category="{cat}"'
                f' data-recsource="{recsrc}"'
                f' data-priority="{pri_data}">' \
                f'<div class="card-title">{title_inner}</div>' \
                f'{pri_el}' \
                '</div>'
            )
        if not cards:
            cards.append('<div class="t-l" style="color:var(--muted)">No attributes</div>')

        # Lane controls (Focus p1 / Show all)
        controls = (
            '<div class="lane-tools">'
            '<button class="btn btn-focus" title="Hide p2 & p3 in this lane">Focus p1</button>'
            '<button class="btn btn-all" title="Show all priorities in this lane">Show all</button>'
            '</div>'
        )

        cols.append(
            '<div class="lane">'
            f'<div class="lane-head"><span>{html.escape(lane)}</span>{controls}</div>'
            f'<div class="lane-body">'
            + ''.join(cards) +
            '</div></div>'
        )

    return (
        '<!doctype html><html lang="en"><head>'
        '<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Program Timeline</title>'
        f'<style>{_inline_css()}</style>'
        '</head><body>'
        '<div class="wrapper">'
        '<h1>Program Timeline</h1>'
        '<div class="subtitle">Attributes grouped into program periods. Hover any attribute to see details.</div>'
        '<div class="controls hint">Tip: use the lane-level “Focus p1” to collapse p2/p3, and “Show all” to restore.</div>'
        '<div class="board">'
        + ''.join(cols) +
        '</div></div>'
        '<div id="tooltip" class="tooltip"></div>'
        f'<script>{_inline_js()}</script>'
        '</body></html>'
    )


def main():
    cfg_path = Path(CONFIG_PATH)
    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_html = out_dir / "program_timeline.html"

    if not cfg_path.exists():
        logger.error("YAML not found: %s", cfg_path)
        sys.exit(2)

    try:
        with cfg_path.open("r", encoding="utf-8") as fp:
            data = yaml.safe_load(fp)
    except Exception as e:
        logger.error("Failed to parse YAML: %s", e)
        raise

    lanes_dict = _collect_by_lane(data)
    # Log lane counts
    for lane, items in lanes_dict.items():
        logger.info("Lane '%s': %d attribute(s)", lane, len(items))

    html_out = _render_html(lanes_dict)
    with out_html.open("w", encoding="utf-8") as fp:
        fp.write(html_out)
    logger.info("Wrote: %s", out_html)

if __name__ == "__main__":
    main()
