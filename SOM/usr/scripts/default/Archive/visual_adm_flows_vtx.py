#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
visual_adm_flows_vtx.py
-----------------------
Purpose:
  Build an interactive HTML report that visualizes ADM traffic flows by stackname
  groups, with hostnames inside each group, and edges between groups based on
  observed connectivity.

Inputs:
  - ADM flow CSVs (var/adm/*.csv)
  - consolidated_server_view.csv for hostname resolution by IP

Outputs:
  - HTML report: var/reporting/adm_flows.html
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd


# ---------------------------------------------------------------------
# VTX path + logging
# ---------------------------------------------------------------------

def resolve_vtx_root(cli_root: Optional[str] = None) -> Path:
    if cli_root:
        return Path(cli_root).expanduser().resolve()
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    here = Path(__file__).resolve()
    return here.parents[3]


def vtx_path(path_str: str | Path, vtx_root: Path, *, must_exist: bool = False) -> Path:
    if isinstance(path_str, Path):
        p = path_str
    else:
        s = str(path_str).strip()
        s = os.path.expandvars(s)
        s = os.path.expanduser(s)
        s = s.replace("VTX_ROOT" + os.sep, str(vtx_root) + os.sep)
        s = s.replace("BTDM_ROOT" + os.sep, str(vtx_root) + os.sep)
        s = s.replace("VTX_ROOT/", str(vtx_root) + "/")
        s = s.replace("BTDM_ROOT/", str(vtx_root) + "/")
        if s.startswith("VTX" + os.sep):
            s = str(vtx_root) + os.sep + s[len("VTX" + os.sep):]
        if s.startswith("VTX/"):
            s = str(vtx_root) + "/" + s[len("VTX/") :]
        p = Path(s)
    if not p.is_absolute():
        p = (vtx_root / p).resolve()
    if must_exist and not p.exists():
        raise FileNotFoundError(f"Path does not exist: {p}")
    return p


def get_logger(component: str, vtx_root: Path) -> logging.Logger:
    lib_dir = vtx_root / "usr" / "lib"
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))
    try:
        import vtx_logging  # type: ignore

        return vtx_logging.get_logger(component=component)
    except Exception:
        logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
        return logging.getLogger(component)


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ADM flow visualization")
    p.add_argument("--vtx-root", default="", help="Optional VTX root override")
    p.add_argument("--input-dir", default="var/adm", help="ADM CSV directory")
    p.add_argument(
        "--server-view",
        default="var/tables/views/consolidated_server_view.csv",
        help="Server lookup CSV",
    )
    p.add_argument(
        "--output",
        default="var/reporting/adm_flows.html",
        help="Output HTML path",
    )
    return p.parse_args(argv)


# ---------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------

def find_col(columns: List[str], candidates: List[str]) -> Optional[str]:
    lower_map = {c.lower(): c for c in columns}
    for cand in candidates:
        key = cand.lower()
        if key in lower_map:
            return lower_map[key]
    return None


def is_unknown(host: str) -> bool:
    return "unknown" in host.lower()


def html_escape(value: str) -> str:
    return html.escape(value or "", quote=True)


def resolve_host(host: str, ip: str, ip_map: Dict[str, str]) -> str:
    host = (host or "").strip()
    ip = (ip or "").strip()
    if host and not is_unknown(host):
        return host
    if ip and ip in ip_map:
        return ip_map[ip]
    if ip:
        return ip
    return ""


def build_ip_map(server_df: pd.DataFrame) -> Dict[str, str]:
    ip_cols = [c for c in ["IPADDRESS", "IPADDRESS2", "IPADDRESS3", "ILOIPADDRESS"] if c in server_df.columns]
    name_col = "SERVERNAME" if "SERVERNAME" in server_df.columns else None
    if not name_col:
        return {}
    ip_map: Dict[str, str] = {}
    for _, row in server_df.iterrows():
        name = str(row.get(name_col) or "").strip()
        if not name:
            continue
        for c in ip_cols:
            ip = str(row.get(c) or "").strip()
            if ip:
                ip_map[ip] = name
    return ip_map


def load_adm_file(path: Path, ip_map: Dict[str, str]) -> Dict[str, Any]:
    df = pd.read_csv(path)
    if df.empty:
        return {}

    cols = df.columns.tolist()
    c_src_stack = find_col(cols, ["source_stackname", "src_stackname", "src_stack", "source_stack", "source_stack_name"])
    c_dst_stack = find_col(cols, ["dest_stackname", "dst_stackname", "dst_stack", "dest_stack", "dest_stack_name"])
    c_src_host = find_col(cols, ["source_hostname", "src_hostname", "src_host", "source_host", "src_host_name"])
    c_dst_host = find_col(cols, ["dest_hostname", "dst_hostname", "dst_host", "dest_host", "dst_host_name"])
    c_src_ip = find_col(cols, ["src_ip", "source_ip", "source_ip_address", "srcip", "src_ip_address"])
    c_dst_ip = find_col(cols, ["dest_ip", "dst_ip", "dest_ip_address", "dstip", "dest_ip_address"])

    if not c_src_stack or not c_dst_stack:
        return {}

    stacks: Dict[str, set] = {}
    edges: Dict[Tuple[str, str], int] = {}

    for _, row in df.iterrows():
        src_stack = str(row.get(c_src_stack) or "").strip()
        dst_stack = str(row.get(c_dst_stack) or "").strip()
        if not src_stack or not dst_stack:
            continue

        src_host = resolve_host(str(row.get(c_src_host) or ""), str(row.get(c_src_ip) or ""), ip_map) if c_src_host or c_src_ip else ""
        dst_host = resolve_host(str(row.get(c_dst_host) or ""), str(row.get(c_dst_ip) or ""), ip_map) if c_dst_host or c_dst_ip else ""

        if src_stack not in stacks:
            stacks[src_stack] = set()
        if dst_stack not in stacks:
            stacks[dst_stack] = set()
        if src_host:
            stacks[src_stack].add(src_host)
        if dst_host:
            stacks[dst_stack].add(dst_host)

        a, b = sorted([src_stack, dst_stack])
        edges[(a, b)] = edges.get((a, b), 0) + 1

    nodes = []
    for stack, hosts in stacks.items():
        nodes.append({"id": stack, "hosts": sorted(hosts)})

    edge_list = [{"source": a, "target": b, "count": cnt} for (a, b), cnt in edges.items()]

    return {"nodes": nodes, "edges": edge_list}


# ---------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------

def build_html(sections: List[Dict[str, Any]], output_path: Path) -> None:
    cards_html_parts: List[str] = []
    for idx, section in enumerate(sections):
        focus = html_escape(section["focused"])
        search_text = html_escape(
            " ".join(
                [section["focused"]]
                + [n["id"] for n in section["nodes"]]
                + [" ".join(n["hosts"]) for n in section["nodes"]]
            )
        )
        cards_html_parts.append(
            "\n".join(
                [
                    f"<details class='card' data-idx='{idx}' data-search='{search_text}'>",
                    "  <summary>",
                    "    <span class='chev'>▶</span>",
                    f"    <span class='name'>{focus}</span>",
                    "  </summary>",
                    "  <div class='body'>",
                    f"    <svg class='graph' id='graph_{idx}'></svg>",
                    "  </div>",
                    "</details>",
                ]
            )
        )

    html = """<!DOCTYPE html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\"/>
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"/>
<title>ADM Flows</title>
<style>
:root{{
  --bg:#0e1222;
  --panel:#151a30;
  --panel-soft:#0f1428;
  --text:#eaeef8;
  --muted:#9aa3b2;
  --accent:#c5d0ff;
  --border:rgba(255,255,255,0.07);
  --shadow:0 6px 24px rgba(0,0,0,0.35);
}}
*{{box-sizing:border-box}}
body{{
  margin:0;
  background:var(--bg);
  color:var(--text);
  font:13px/1.5 Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
  padding:24px;
}}
a{{color:var(--accent);text-decoration:none}}
.wrap{{max-width:1320px;margin:0 auto}}
.hdr h1{{margin:0 0 6px 0;font-size:24px;font-weight:700}}
.muted{{color:var(--muted);font-size:12px}}
.controls{{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0 18px}}
input[type="text"]{{
  background:#0b1022;
  color:var(--text);
  border:1px solid var(--border);
  border-radius:8px;
  padding:8px 10px;
  width:320px;
}}
.grid{{display:grid;grid-template-columns:1fr;gap:16px}}
.card{{
  border:1px solid var(--border);
  border-radius:12px;
  padding:12px 14px;
  background:var(--panel);
  box-shadow:var(--shadow);
}}
summary{{display:flex;align-items:center;gap:10px;cursor:pointer;list-style:none}}
summary::-webkit-details-marker{{display:none}}
.chev{{display:inline-block;transition:transform .15s ease;opacity:.8}}
details[open] .chev{{transform:rotate(90deg)}}
.name{{font-weight:700;font-size:16px;color:var(--accent)}}
.body{{padding:14px;border-top:1px solid var(--border)}}
.graph{{width:100%;height:1320px;border:1px solid var(--border);border-radius:12px;background:var(--panel-soft)}}
.stack-label{{font-weight:700;color:var(--accent)}}
.node-text{{font-size:11px;fill:var(--text)}}
.node-title{{font-weight:700;fill:var(--accent)}}
.edge-label{{font-size:11px;fill:var(--muted)}}
</style>
</head>
<body>
<div class=\"wrap\">
  <div class=\"hdr\">
    <h1>ADM Flows</h1>
    <div class=\"muted\">Stack groups with hostnames and inter-stack connectivity</div>
  </div>
  <div class=\"controls\">
    <input id=\"search\" type=\"text\" placeholder=\"Search stackname...\" />
  </div>
  <div class=\"grid\" id=\"cards\">
__CARDS_HTML__
  </div>
</div>
<script>
const SECTIONS = __SECTIONS_JSON__;
const cards = document.getElementById('cards');
const search = document.getElementById('search');
const cardEls = Array.from(document.querySelectorAll('.card'));

function matches(section, term) {{
  if (!term) return true;
  const t = term.toLowerCase();
  if (section.focused.toLowerCase().includes(t)) return true;
  for (const n of section.nodes) {{
    if (n.id.toLowerCase().includes(t)) return true;
    if (n.hosts.join(' ').toLowerCase().includes(t)) return true;
  }}
  return false;
}}

function render() {{
  const term = search.value.trim();
  cardEls.forEach((card) => {{
    const idx = parseInt(card.dataset.idx || '0', 10);
    const section = SECTIONS[idx];
    if (matches(section, term)) {{
      card.style.display = '';
    }} else {{
      card.style.display = 'none';
    }}
  }});
}}

function drawGraph(svgId, section) {{
  const svg = document.getElementById(svgId);
  const width = svg.clientWidth;
  const height = svg.clientHeight;
  const center = { x: width / 2, y: height / 2 };
  const nodes = section.nodes;
  const focused = section.focused;

  const positions = {{}};
  const others = nodes.filter(n => n.id !== focused);
  positions[focused] = center;
  const radius = Math.min(width, height) * 0.48;
  others.forEach((n, i) => {{
    const angle = (i / Math.max(1, others.length)) * Math.PI * 2;
    positions[n.id] = {
      x: center.x + radius * Math.cos(angle),
      y: center.y + radius * Math.sin(angle)
    };
  }});

  const lines = [];
  section.edges.forEach(e => {{
    const a = positions[e.source];
    const b = positions[e.target];
    if (!a || !b) return;
    lines.push(`<line x1='${a.x}' y1='${a.y}' x2='${b.x}' y2='${b.y}' stroke='rgba(255,255,255,0.2)' stroke-width='${1 + Math.log(1 + e.count)}' />`);
    const mx = (a.x + b.x) / 2;
    const my = (a.y + b.y) / 2;
    lines.push(`<text x='${mx}' y='${my - 4}' class='edge-label'>${e.count}</text>`);
  }});

  const nodeEls = [];
  nodes.forEach(n => {{
    const pos = positions[n.id] || center;
    const hostLines = n.hosts;
    const r = 40 + Math.min(120, hostLines.length * 4);
    nodeEls.push(`<circle cx='${pos.x}' cy='${pos.y}' r='${r}' fill='rgba(21,26,48,0.85)' stroke='rgba(197,208,255,0.3)' />`);
    nodeEls.push(`<text x='${pos.x}' y='${pos.y - r + 16}' text-anchor='middle' class='node-title'>${n.id}</text>`);
    hostLines.forEach((h, i) => {{
      const y = pos.y - r + 30 + i * 12;
      nodeEls.push(`<text x='${pos.x}' y='${y}' text-anchor='middle' class='node-text'>${h}</text>`);
    }});
  }});

  svg.innerHTML = lines.join('') + nodeEls.join('');
}}

cardEls.forEach((card) => {{
  const idx = parseInt(card.dataset.idx || '0', 10);
  const svgId = `graph_${idx}`;
  const section = SECTIONS[idx];
  drawGraph(svgId, section);
}});
search.addEventListener('input', render);
render();
</script>
</body>
</html>
    """
    html = html.replace("__SECTIONS_JSON__", json.dumps(sections))
    html = html.replace("__CARDS_HTML__", "\n".join(cards_html_parts))
    html = html.replace("{{", "{").replace("}}", "}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    vtx_root = resolve_vtx_root(args.vtx_root or None)
    logger = get_logger(component="visual_adm_flows_vtx", vtx_root=vtx_root)

    input_dir = vtx_path(args.input_dir, vtx_root, must_exist=True)
    server_view = vtx_path(args.server_view, vtx_root, must_exist=True)
    output_path = vtx_path(args.output, vtx_root, must_exist=False)

    server_df = pd.read_csv(server_view)
    ip_map = build_ip_map(server_df)

    sections: List[Dict[str, Any]] = []
    for csv_path in sorted(input_dir.glob("*.csv")):
        stem = csv_path.stem
        focused = stem.split("_", 1)[1] if "_" in stem else stem
        data = load_adm_file(csv_path, ip_map)
        if not data:
            continue
        nodes = data["nodes"]
        if focused not in [n["id"] for n in nodes]:
            nodes.append({"id": focused, "hosts": []})
        sections.append(
            {
                "file": csv_path.name,
                "focused": focused,
                "nodes": nodes,
                "edges": data["edges"],
            }
        )

    build_html(sections, output_path)
    logger.info("Wrote HTML: %s", output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
