#!/usr/bin/env python3
"""
File: usr/scripts/default/migration_program_raci_marketing_yamlonly.py

YAML-only text, slick marketing-style UI.
- No invented headings, taglines, subtitles, or extra explanations.
- All visible text comes from YAML fields, except:
  - Phase names + component names used inside the SVG visuals (per user direction).
- Hero visual is loaded from an external SVG and inlined into the HTML:
    VTX_ROOT/usr/lib/content/program_overview.svg
  (falls back to the older generated SVG if the file is missing.)
"""

from __future__ import annotations

import os
import sys
import re
import html
import argparse
import logging
from typing import Any, Dict, List, Optional

import yaml

# ------------------------------------------------------------
# Globals / Paths (VTX style)
# ------------------------------------------------------------
VTX_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

DEFAULT_INPUT_YAML = os.path.join(
    VTX_ROOT, "usr", "config", "run", "migration_program_dictionary.yaml"
)
DEFAULT_OUTPUT_HTML = os.path.join(
    VTX_ROOT, "var", "dictionaries", "migration_program_dictionary.html"
)

# New: external SVG asset (inline into HTML)
PROGRAM_OVERVIEW_SVG = os.path.join(VTX_ROOT, "usr", "lib", "content", "program_overview.svg")

# ------------------------------------------------------------
# Logging (BTDM / VTX style)
# ------------------------------------------------------------
try:
    sys.path.append(os.path.join(VTX_ROOT, "usr", "lib"))
    import btdm_logging  # type: ignore

    logger = btdm_logging.get_logger(component="migration_program_dictionary_report")
except Exception:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    logger = logging.getLogger("migration_program_dictionary_report")


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def esc(x: Any) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def ensure_parent_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def read_yaml(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"YAML not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError("Top-level YAML must be a mapping/dict.")
    return data


def load_svg_inline(svg_path: str) -> str:
    """
    Load an SVG file and return its contents for inlining into HTML.
    If missing/unreadable, return empty string.
    """
    try:
        if not svg_path or not os.path.exists(svg_path):
            return ""
        with open(svg_path, "r", encoding="utf-8") as f:
            txt = f.read().strip()

        # Strip XML declaration if present (optional; keeps HTML cleaner)
        txt = re.sub(r"^\s*<\?xml[^>]*\?>\s*", "", txt, flags=re.IGNORECASE)
        return txt
    except Exception as e:
        logger.warning(f"Failed to load SVG: {svg_path}: {e}")
        return ""


def coerce_migration_program(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Supports:
      migration_program: [ {introduction: ...}, {phase: ...}, ... ]
    or:
      migration_program:
        introduction: ...
        phases: [ ... ]
    Returns:
      { "introduction": str, "phases": [dict...] }
    """
    mp = data.get("migration_program")
    if mp is None:
        raise ValueError("YAML must contain 'migration_program'.")

    if isinstance(mp, dict):
        intro = mp.get("introduction", "") or ""
        phases = mp.get("phases", []) or []
        if not isinstance(phases, list):
            raise ValueError("'migration_program.phases' must be a list.")
        return {"introduction": str(intro), "phases": [p for p in phases if isinstance(p, dict)]}

    if isinstance(mp, list):
        intro = ""
        phases: List[Dict[str, Any]] = []
        for item in mp:
            if not isinstance(item, dict):
                continue
            if "introduction" in item:
                part = item.get("introduction") or ""
                if part:
                    intro = (intro + "\n\n" + str(part)).strip() if intro else str(part)
            elif "phase" in item:
                phases.append(item)
        return {"introduction": intro, "phases": phases}

    raise ValueError("'migration_program' must be either a list or a dict.")


def prose_html(text: str) -> str:
    """
    YAML-only rendering, readable:
    - paragraphs split on blank lines
    - bullet lines ("- x") converted to <ul>
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    lines = [ln.rstrip() for ln in raw.splitlines()]
    blocks: List[List[str]] = []
    cur: List[str] = []
    for ln in lines:
        if not ln.strip():
            if cur:
                blocks.append(cur)
                cur = []
            continue
        cur.append(ln)
    if cur:
        blocks.append(cur)

    out: List[str] = []
    for blk in blocks:
        is_list = any(re.match(r"^\s*-\s+", ln) for ln in blk)
        if is_list:
            items: List[str] = []
            for ln in blk:
                m = re.match(r"^\s*-\s+(.*)$", ln)
                if m:
                    items.append(f"<li>{esc(m.group(1).strip())}</li>")
                else:
                    if items:
                        items[-1] = items[-1].replace("</li>", f" {esc(ln.strip())}</li>")
                    else:
                        items.append(f"<li>{esc(ln.strip())}</li>")
            out.append(f"<ul class='proseList'>{''.join(items)}</ul>")
        else:
            para = " ".join([ln.strip() for ln in blk])
            out.append(f"<p>{esc(para)}</p>")
    return "\n".join(out)


# ------------------------------------------------------------
# RACI parsing (legacy freeform)
# ------------------------------------------------------------
def parse_raci_freeform(raci_text: str) -> List[Dict[str, str]]:
    if not raci_text:
        return []
    lines = [ln.rstrip() for ln in raci_text.replace("\t", "  ").splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    rows: List[Dict[str, str]] = []
    current: Optional[Dict[str, str]] = None

    def flush() -> None:
        nonlocal current
        if current and (current.get("responsibility") or "").strip():
            for k in ("responsible", "accountable", "consulted", "informed"):
                current.setdefault(k, "")
            rows.append(current)
        current = None

    for raw in lines:
        ln = raw.strip()
        if not ln:
            continue

        m_resp = re.match(r"^Responsibility\s*:\s*(.*)$", ln, re.IGNORECASE)
        if m_resp:
            flush()
            current = {"responsibility": m_resp.group(1).strip()}
            continue

        m_key = re.match(r"^(Responsible|Accountable|Consulted|Informed)\s*:\s*(.*)$", ln, re.IGNORECASE)
        if m_key:
            if current is None:
                current = {"responsibility": "(Unlabeled responsibility)"}
            current[m_key.group(1).lower()] = m_key.group(2).strip()
            continue

        if current is not None:
            for field in ("informed", "consulted", "accountable", "responsible"):
                if current.get(field):
                    current[field] = (current[field] + " " + ln).strip()
                    break

    flush()
    return rows


def pill_list(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return '<span class="dash">—</span>'
    parts = [p.strip() for p in v.split(",") if p.strip()]
    if not parts:
        return '<span class="dash">—</span>'
    return " ".join([f'<span class="pill">{esc(p)}</span>' for p in parts])


# ------------------------------------------------------------
# SVG visuals (fallback / on-the-fly)
# ------------------------------------------------------------
def svg_program_overview(phases: List[str]) -> str:
    """
    Fallback only (used if external SVG missing).
    """
    W, H = 1200, 260
    left, right, y = 92, W - 92, 138
    n = max(1, len(phases))
    xs = [left + int(i * (right - left) / (n - 1)) for i in range(n)] if n > 1 else [W // 2]

    arrows = []
    nodes = []
    labels = []

    for i, (x, lab) in enumerate(zip(xs, phases)):
        nodes.append(
            f"""
          <g>
            <circle cx="{x}" cy="{y}" r="18" class="node"/>
            <circle cx="{x}" cy="{y}" r="36" class="nodeGlow"/>
          </g>
        """
        )
        labels.append(f"""<text x="{x}" y="{y+60}" text-anchor="middle" class="lbl">{esc(lab)}</text>""")
        if i < n - 1:
            x2 = xs[i + 1]
            arrows.append(
                f"""
              <path d="M {x+26} {y} L {x2-26} {y}" class="arrow"/>
              <path d="M {x2-32} {y-8} L {x2-26} {y} L {x2-32} {y+8}" class="arrowHead"/>
            """
            )

    loop = f"""
      <path d="M {xs[-1]} {y-44} C {xs[-1]} 28, {xs[0]} 28, {xs[0]} {y-44}" class="loop"/>
      <path d="M {xs[0]-10} {y-48} L {xs[0]} {y-44} L {xs[0]+10} {y-48}" class="loopHead"/>
    """

    return f"""
    <svg viewBox="0 0 {W} {H}" class="heroSvg" role="img" aria-label="Program phases overview">
      <defs>
        <linearGradient id="gradLine" x1="0" x2="1">
          <stop offset="0%" stop-color="rgba(132, 99, 255, .85)"/>
          <stop offset="45%" stop-color="rgba(70, 230, 167, .65)"/>
          <stop offset="100%" stop-color="rgba(255, 214, 107, .70)"/>
        </linearGradient>
        <filter id="softGlow" x="-25%" y="-25%" width="150%" height="150%">
          <feGaussianBlur stdDeviation="10" result="b"/>
          <feColorMatrix in="b" type="matrix"
            values="1 0 0 0 0
                    0 1 0 0 0
                    0 0 1 0 0
                    0 0 0 .35 0"/>
          <feMerge>
            <feMergeNode/>
            <feMergeNode in="SourceGraphic"/>
          </feMerge>
        </filter>
      </defs>

      <rect x="12" y="12" width="{W-24}" height="{H-24}" rx="24" class="svgPanel"/>

      <path d="M {xs[0]} {y} L {xs[-1]} {y}" class="base"/>
      {''.join(arrows)}
      {loop}
      {''.join(nodes)}
      {''.join(labels)}
    </svg>
    """


def svg_discover_zoom(components: List[str]) -> str:
    W, H = 1200, 260
    left, right, y = 210, W - 210, 140
    n = max(1, len(components))
    xs = [left + int(i * (right - left) / (n - 1)) for i in range(n)] if n > 1 else [W // 2]

    cards = []
    arrows = []
    for i, (x, lab) in enumerate(zip(xs, components)):
        cards.append(
            f"""
          <g>
            <rect x="{x-180}" y="{y-48}" width="360" height="106" rx="20" class="zCard"/>
            <text x="{x}" y="{y-6}" text-anchor="middle" class="zTitle">{esc(lab)}</text>
          </g>
        """
        )
        if i < n - 1:
            x2 = xs[i + 1]
            arrows.append(
                f"""
              <path d="M {x+190} {y+10} L {x2-190} {y+10}" class="zArrow"/>
              <path d="M {x2-198} {y+2} L {x2-190} {y+10} L {x2-198} {y+18}" class="zArrowHead"/>
            """
            )

    loop = f"""
      <path d="M {xs[-1]+140} {y-58}
               C {xs[-1]+240} 34, {xs[0]-240} 34, {xs[0]-140} {y-58}" class="zLoop"/>
      <path d="M {xs[0]-154} {y-62} L {xs[0]-140} {y-58} L {xs[0]-126} {y-62}" class="zLoopHead"/>
    """

    return f"""
    <svg viewBox="0 0 {W} {H}" class="zoomSvg" role="img" aria-label="Discover components overview">
      <defs>
        <linearGradient id="gradCard" x1="0" x2="1">
          <stop offset="0%" stop-color="rgba(132, 99, 255, .18)"/>
          <stop offset="50%" stop-color="rgba(70, 230, 167, .12)"/>
          <stop offset="100%" stop-color="rgba(255, 214, 107, .14)"/>
        </linearGradient>
      </defs>

      <rect x="12" y="12" width="{W-24}" height="{H-24}" rx="24" class="svgPanel2"/>
      {''.join(arrows)}
      {loop}
      {''.join(cards)}
    </svg>
    """


# ------------------------------------------------------------
# Rendering: YAML-only visible text
# ------------------------------------------------------------
def render_components(phase: Dict[str, Any]) -> str:
    comps = phase.get("components")
    if not comps:
        return ""

    if not isinstance(comps, list):
        return ""

    rows: List[str] = []
    for item in comps:
        if isinstance(item, dict):
            name = str(item.get("name", "") or "").strip()
            desc = str(item.get("description", "") or "").strip()
            if not name and not desc:
                continue
            rows.append(
                f"""
              <div class="comp">
                <div class="compName">{esc(name)}</div>
                <div class="prose">{prose_html(desc) if desc else ""}</div>
              </div>
            """
            )

    if not rows:
        return ""

    return f"""<div class="components">{''.join(rows)}</div>"""


def extract_raci(phase: Dict[str, Any]) -> Dict[str, str]:
    raci = phase.get("raci")
    if isinstance(raci, dict):
        return {
            "description": str(raci.get("description", "") or ""),
            "table": str(raci.get("table", "") or ""),
        }
    return {
        "description": str(phase.get("raci_description", "") or ""),
        "table": str(phase.get("raci_table", "") or ""),
    }


def build_html(introduction: str, phases: List[Dict[str, Any]]) -> str:
    intro_html = prose_html(introduction) if (introduction or "").strip() else ""

    # Hero visual: prefer external SVG asset, fallback to generated SVG
    fallback_hero_labels = ["Discover", "Program Strategy", "Rationalize", "Migration Planning", "Migration Execution"]
    hero_svg_inline = load_svg_inline(PROGRAM_OVERVIEW_SVG)
    hero_visual_html = hero_svg_inline if hero_svg_inline else svg_program_overview(fallback_hero_labels)

    cards: List[str] = []
    for i, ph in enumerate(phases, start=1):
        phase_name = str(ph.get("phase", f"Phase {i}")).strip()
        desc = str(ph.get("description", "") or "").strip()

        # Discover zoom visual uses components list if present; else fallback defaults
        zoom_svg = ""
        if phase_name.lower() == "discover":
            comp_labels: List[str] = []
            comps = ph.get("components")
            if isinstance(comps, list):
                for it in comps:
                    if isinstance(it, dict):
                        nm = str(it.get("name", "") or "").strip()
                        if nm:
                            comp_labels.append(nm)
            if not comp_labels:
                comp_labels = ["Inventory Identification", "Program Attribute Mapping", "Inventory Analysis and Reporting"]
            zoom_svg = f"<div class='visual'>{svg_discover_zoom(comp_labels[:3])}</div>"

        comp_html = render_components(ph)

        raci = extract_raci(ph)
        raci_desc_html = prose_html(raci.get("description", "")) if raci.get("description", "").strip() else ""
        raci_rows = parse_raci_freeform(raci.get("table", ""))

        raci_table_html = ""
        if raci_rows:
            body = "\n".join(
                f"""
                <tr>
                  <td class="respCell"><div class="respTitle">{esc(r.get("responsibility",""))}</div></td>
                  <td>{pill_list(r.get("responsible",""))}</td>
                  <td>{pill_list(r.get("accountable",""))}</td>
                  <td>{pill_list(r.get("consulted",""))}</td>
                  <td>{pill_list(r.get("informed",""))}</td>
                </tr>
                """.strip()
                for r in raci_rows
            )
            raci_table_html = f"""
            <div class="tableWrap">
              <table class="raci">
                <thead>
                  <tr>
                    <th class="thResp">Responsibility</th>
                    <th>Responsible</th>
                    <th>Accountable</th>
                    <th>Consulted</th>
                    <th>Informed</th>
                  </tr>
                </thead>
                <tbody>{body}</tbody>
              </table>
            </div>
            """

        raci_block = ""
        if raci_desc_html or raci_table_html:
            raci_block = f"""
            <div class="sectionBlock">
              <div class="prose">{raci_desc_html}</div>
              {raci_table_html}
            </div>
            """

        cards.append(
            f"""
        <details class="card" {"open" if i == 1 else ""}>
          <summary class="cardSum">
            <div class="left">
              <span class="badge">{esc(phase_name)}</span>
            </div>
            <div class="right">
              <span class="chev" aria-hidden="true"></span>
            </div>
          </summary>

          <div class="cardBody">
            {zoom_svg}

            <div class="sectionBlock">
              <div class="prose">{prose_html(desc) if desc else ""}</div>
            </div>

            {f"<div class='sectionBlock'>{comp_html}</div>" if comp_html else ""}

            {raci_block}
          </div>
        </details>
        """.strip()
        )

    cards_html = "\n".join(cards) if cards else ""

    css = r"""
    :root{
      --bg0:#070815;
      --bg1:#0A0F24;

      --ink:#EEF2FF;
      --muted:#B2BCD9;

      /* Accent palette */
      --p:#8463FF;
      --g:#46E6A7;
      --y:#FFD66B;
      --c:#6DE3FF;

      --line: rgba(255,255,255,.12);
      --line2: rgba(255,255,255,.18);

      --shadow: 0 22px 80px rgba(0,0,0,.52);
      --r: 22px;
      --r2: 28px;
    }

    *{ box-sizing:border-box; }
    html,body{ height:100%; }
    body{
      margin:0;
      color:var(--ink);
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
      background:
        radial-gradient(1000px 600px at 15% 0%, rgba(132,99,255,.22), transparent 60%),
        radial-gradient(900px 600px at 90% 10%, rgba(109,227,255,.14), transparent 55%),
        radial-gradient(900px 650px at 55% 100%, rgba(70,230,167,.12), transparent 62%),
        linear-gradient(180deg, var(--bg0), var(--bg1));
    }

    /* Wider utilization */
    .wrap{
      width: min(1680px, calc(100vw - 44px));
      margin: 0 auto;
      padding: 26px 0 78px;
    }

    .top{
      display:flex;
      justify-content:space-between;
      align-items:center;
      gap: 14px;
      padding: 0 22px;
      margin-bottom: 14px;
    }

    .brand{
      display:flex; gap:12px; align-items:center;
    }
    .mark{
      width:40px; height:40px; border-radius: 16px;
      background: linear-gradient(135deg, rgba(132,99,255,.95), rgba(109,227,255,.55));
      box-shadow: 0 16px 50px rgba(0,0,0,.42);
    }

    .hero{
      margin: 0 22px 14px;
      border: 1px solid var(--line);
      border-radius: var(--r2);
      box-shadow: var(--shadow);
      overflow:hidden;
      background: linear-gradient(180deg, rgba(255,255,255,.07), rgba(255,255,255,.03));
    }

    .heroIn{
      display:grid;
      grid-template-columns: 1fr;
      gap: 0;
    }

    .heroLeft{ padding: 22px 22px 10px; }
    .heroRight{ padding: 12px 18px 18px; }

    .prose{
      color: rgba(238,242,255,.88);
      font-size: 14.2px;
      line-height: 1.68;
    }
    .prose p{ margin: 0 0 12px; }
    .prose p:last-child{ margin-bottom: 0; }
    .proseList{ margin: 0 0 12px 18px; padding: 0; }
    .proseList li{ margin: 6px 0; }

    /* Cards */
    details.card{
      margin: 12px 22px 0;
      border: 1px solid var(--line);
      border-radius: var(--r);
      background: linear-gradient(180deg, rgba(255,255,255,.05), rgba(255,255,255,.02));
      box-shadow: var(--shadow);
      overflow:hidden;
    }
    summary.cardSum{
      cursor:pointer;
      user-select:none;
      list-style:none;
      display:flex;
      justify-content:space-between;
      align-items:center;
      gap:12px;
      padding: 14px 16px;
      background:
        radial-gradient(700px 180px at 18% 0%, rgba(132,99,255,.12), transparent 60%),
        radial-gradient(640px 220px at 95% 90%, rgba(70,230,167,.10), transparent 60%),
        rgba(0,0,0,.10);
      border-bottom: 1px solid rgba(255,255,255,.08);
    }
    summary::-webkit-details-marker{ display:none; }

    .badge{
      font-size: 16px;
      font-weight: 950;
      letter-spacing: .15px;
    }

    .chev{
      width: 10px; height: 10px;
      border-right: 2px solid rgba(238,242,255,.70);
      border-bottom: 2px solid rgba(238,242,255,.70);
      transform: rotate(45deg);
      opacity: .9;
      transition: transform .18s ease;
    }
    details[open] .chev{ transform: rotate(225deg); }

    .cardBody{
      padding: 14px 16px 16px;
      background: rgba(0,0,0,.10);
    }

    .sectionBlock{
      border: 1px solid rgba(255,255,255,.10);
      border-radius: 18px;
      background: linear-gradient(180deg, rgba(255,255,255,.045), rgba(255,255,255,.02));
      padding: 12px 12px;
      margin-top: 12px;
    }

    .visual{ margin-top: 6px; }

    /* Components */
    .components{ display:flex; flex-direction:column; gap: 12px; }
    .comp{
      border: 1px solid rgba(255,255,255,.10);
      border-radius: 16px;
      padding: 12px 12px;
      background: rgba(0,0,0,.12);
    }
    .compName{
      font-weight: 950;
      font-size: 14px;
      letter-spacing: .15px;
      margin-bottom: 8px;
      color: rgba(238,242,255,.94);
    }

    /* Table */
    .tableWrap{
      margin-top: 12px;
      border-radius: 16px;
      border: 1px solid rgba(255,255,255,.12);
      background: rgba(0,0,0,.18);
      overflow:auto;
    }
    table.raci{ width:100%; min-width: 980px; border-collapse: separate; border-spacing: 0; }
    table.raci thead th{
      position: sticky; top: 0; z-index: 2;
      text-align:left;
      font-size: 12px;
      letter-spacing: .30px;
      text-transform: uppercase;
      color: rgba(238,242,255,.80);
      padding: 12px 12px;
      background:
        radial-gradient(900px 200px at 20% 0%, rgba(132,99,255,.14), transparent 55%),
        rgba(10,14,30,.92);
      border-bottom: 1px solid rgba(255,255,255,.14);
      backdrop-filter: blur(10px);
    }
    table.raci tbody td{
      padding: 12px 12px;
      vertical-align: top;
      border-bottom: 1px solid rgba(255,255,255,.07);
      font-size: 13.5px;
      color: rgba(238,242,255,.90);
      background: rgba(255,255,255,.02);
    }
    table.raci tbody tr:nth-child(even) td{ background: rgba(255,255,255,.035); }
    .thResp{ width: 38%; }
    .respTitle{ font-weight: 900; line-height: 1.25; }

    .pill{
      display:inline-block;
      padding: 7px 10px;
      border-radius: 999px;
      border: 1px solid rgba(255,214,107,.25);
      background: rgba(255,214,107,.10);
      color: rgba(238,242,255,.92);
      font-size: 12.2px;
      margin: 0 8px 8px 0;
      white-space: nowrap;
    }
    .dash{ color: var(--muted); }

    /* SVG */
    .heroSvg, .zoomSvg{ width:100%; height:auto; display:block; }
    /* New: external inlined SVG in heroRight (no class) */
    .heroRight svg{ width:100%; height:auto; display:block; }

    .svgPanel{ fill: rgba(0,0,0,.12); stroke: rgba(255,255,255,.12); }
    .svgPanel2{ fill: rgba(0,0,0,.14); stroke: rgba(255,255,255,.12); }

    .base{ stroke: url(#gradLine); stroke-width: 3.2; opacity: .9; }
    .arrow{ stroke: rgba(238,242,255,.45); stroke-width: 2.2; }
    .arrowHead{ stroke: rgba(238,242,255,.45); stroke-width: 2.2; fill: none; stroke-linecap: round; stroke-linejoin: round; }
    .loop{ stroke: rgba(109,227,255,.55); stroke-width: 2.2; fill: none; stroke-dasharray: 6 7; opacity: .9; }
    .loopHead{ stroke: rgba(109,227,255,.55); stroke-width: 2.2; fill: none; stroke-linecap: round; stroke-linejoin: round; }
    .node{ fill: rgba(238,242,255,.92); stroke: rgba(255,214,107,.55); stroke-width: 2.4; filter: url(#softGlow); }
    .nodeGlow{ fill: rgba(255,214,107,.12); stroke: none; }
    .lbl{ font-size: 12.5px; fill: rgba(238,242,255,.86); font-weight: 800; }

    .zCard{ fill: url(#gradCard); stroke: rgba(255,255,255,.16); }
    .zTitle{ font-size: 13.5px; fill: rgba(238,242,255,.95); font-weight: 950; }
    .zArrow{ stroke: rgba(238,242,255,.50); stroke-width: 2.2; }
    .zArrowHead{ stroke: rgba(238,242,255,.50); stroke-width: 2.2; fill: none; stroke-linecap: round; stroke-linejoin: round; }
    .zLoop{ stroke: rgba(70,230,167,.55); stroke-width: 2.2; fill:none; stroke-dasharray: 6 7; opacity: .9; }
    .zLoopHead{ stroke: rgba(70,230,167,.55); stroke-width: 2.2; fill:none; stroke-linecap: round; stroke-linejoin: round; }

    @media (max-width: 980px){
      .wrap{ width: calc(100vw - 22px); }
      .top{ padding: 0 12px; }
      .hero{ margin: 0 12px 12px; }
      details.card{ margin: 12px 12px 0; }
    }
    """

    js = r"""
    (function(){
      // rotate chevron already handled by CSS; no added text.
    })();
    """

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Migration Program</title>
  <style>{css}</style>
</head>
<body>
  <div class="wrap">

    <div class="top">
      <div class="brand">
        <div class="mark" aria-hidden="true"></div>
      </div>
      <div></div>
    </div>

    <section class="hero">
      <div class="heroIn">
        <div class="heroLeft">
          <div class="prose">{intro_html}</div>
        </div>
        <div class="heroRight">
          {hero_visual_html}
        </div>
      </div>
    </section>

    {cards_html}

  </div>

  <script>{js}</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate Migration Program Dictionary + RACI HTML (YAML-only text)."
    )
    ap.add_argument("--in", dest="in_path", default=DEFAULT_INPUT_YAML, help="Input YAML path")
    ap.add_argument("--out", dest="out_path", default=DEFAULT_OUTPUT_HTML, help="Output HTML path")
    ap.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = ap.parse_args()

    if args.debug:
        try:
            logger.setLevel(logging.DEBUG)
        except Exception:
            pass

    logger.info(f"VTX_ROOT = {VTX_ROOT}")
    logger.info(f"Reading YAML: {args.in_path}")
    logger.info(f"Program overview SVG: {PROGRAM_OVERVIEW_SVG}")

    data = read_yaml(args.in_path)
    mp = coerce_migration_program(data)

    introduction = mp.get("introduction", "") or ""
    phases = mp.get("phases", []) or []

    logger.info(f"Parsed: phases={len(phases)}")

    out_html = build_html(str(introduction), phases)

    ensure_parent_dir(args.out_path)
    with open(args.out_path, "w", encoding="utf-8") as f:
        f.write(out_html)

    logger.info(f"Wrote HTML: {args.out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())