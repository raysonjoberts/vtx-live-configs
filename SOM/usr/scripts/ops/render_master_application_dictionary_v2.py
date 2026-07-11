#!/usr/bin/env python3
from __future__ import annotations

import html
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml


def resolve_vtx_root() -> Path:
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    here = Path(__file__).resolve()
    return here.parents[3]


VTX_ROOT = resolve_vtx_root()
DEFAULT_INPUT = VTX_ROOT / "usr" / "config" / "run" / "master_data_dictionary_v2.yaml"
DEFAULT_OUTPUT_DIR = VTX_ROOT / "var" / "dictionaries" / "framework_v2"

INDEX_SECTIONS = [
    ("Operations", ["ITSM Operations", "BCDR Continuity", "Engineering DevOps", "Monitoring Observability"]),
    ("Finance", ["Finance FinOps", "Vendor Contract Management"]),
    ("EA", ["Portfolio Management", "Enterprise Architecture", "Migration Transformation"]),
    ("Security", ["Security GRC", "Data Governance Privacy", "Audit Compliance"]),
]

SECTION_ACCENTS = {
    "Operations": "accent-green",
    "Finance": "accent-blue",
    "EA": "accent-orange",
    "Security": "accent-purple",
    "Additional Domains": "accent-blue",
}

DOMAIN_VISUAL = {
    "ITSM Operations": ("⚙", "ITSM\nOperations"),
    "BCDR Continuity": ("🛡", "BCDR\nContinuity"),
    "Engineering DevOps": ("▣", "Engineering\nDevOps"),
    "Monitoring Observability": ("◉", "Monitoring\nObservability"),
    "Finance FinOps": ("$", "Finance\nFinOps"),
    "Vendor Contract Management": ("⌁", "Vendor Contract\nManagement"),
    "Portfolio Management": ("📊", "Portfolio\nManagement"),
    "Enterprise Architecture": ("◈", "Enterprise\nArchitecture"),
    "Migration Transformation": ("⇄", "Migration\nTransformation"),
    "Security GRC": ("🔐", "Security\nGRC"),
    "Data Governance Privacy": ("◍", "Data Governance\nPrivacy"),
    "Audit Compliance": ("✓", "Audit\nCompliance"),
}


def load_model(path: Path) -> Dict[str, Any]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        raise ValueError("Dictionary root must be a mapping")
    model = doc.get("enterprise_application_metadata_model")
    if not isinstance(model, dict):
        raise ValueError("enterprise_application_metadata_model not found")
    return model


def prettify(name: str) -> str:
    return str(name).replace("_", " ").strip()


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower())
    return slug.strip("_") or "item"


def normalize_program_keys(raw: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for key, value in (raw or {}).items():
        if isinstance(value, dict):
            out[str(key)] = value
    return out


def domain_visual(name: str) -> Tuple[str, str]:
    return DOMAIN_VISUAL.get(name, ("•", name))


def collect_program_pages(model: Dict[str, Any]) -> List[Dict[str, Any]]:
    programs = [str(x) for x in (model.get("programs_and_domains") or []) if str(x).strip()]
    domains = [d for d in (model.get("domains") or []) if isinstance(d, dict)]
    pages: List[Dict[str, Any]] = []
    for program in programs:
        groups: List[Dict[str, Any]] = []
        total = 0
        for domain in domains:
            domain_name = prettify(str(domain.get("domain") or "Uncategorized"))
            attrs = [a for a in (domain.get("attributes") or []) if isinstance(a, dict)]
            rows: List[Dict[str, str]] = []
            for attr in attrs:
                usage_map = normalize_program_keys(attr.get("program_usage") or {})
                usage = usage_map.get(program)
                if not usage:
                    continue
                auth = attr.get("authoritative_source") or {}
                typical = auth.get("typical_systems") or []
                if isinstance(typical, list):
                    typical_text = ", ".join(str(x) for x in typical if str(x).strip())
                else:
                    typical_text = str(typical)
                rows.append(
                    {
                        "attribute": prettify(str(attr.get("attribute") or "Unnamed Attribute")),
                        "definition": str(attr.get("definition") or "").strip(),
                        "how_used": str((usage or {}).get("how_used") or "").strip(),
                        "metadata_domain": domain_name,
                        "authoritative_type": prettify(str(auth.get("type") or "")),
                        "typical_systems": typical_text,
                    }
                )
            if rows:
                total += len(rows)
                groups.append({"domain": domain_name, "rows": rows})
        pages.append(
            {
                "program_key": program,
                "program": prettify(program),
                "slug": f"{slugify(program)}.html",
                "groups": groups,
                "count": total,
            }
        )
    return pages


def collect_category_maps(model: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    categories_raw = model.get("program_domain_categories") or {}
    category_map: Dict[str, Dict[str, Any]] = {}
    program_to_category: Dict[str, str] = {}
    if not isinstance(categories_raw, dict):
        return category_map, program_to_category
    for category_name, payload in categories_raw.items():
        if not isinstance(payload, dict):
            continue
        description = str(payload.get("description") or "").strip()
        domains = [str(x) for x in (payload.get("domains") or []) if str(x).strip()]
        category_map[str(category_name)] = {
            "name": str(category_name),
            "description": description,
            "domains": domains,
        }
        for dom in domains:
            program_to_category[dom] = str(category_name)
    return category_map, program_to_category


def collect_raci_pages(model: Dict[str, Any], category_map: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    raci_root = (((model.get("raci_models") or {}).get("category_level")) or {})
    pages: List[Dict[str, Any]] = []
    if not isinstance(raci_root, dict):
        return pages
    for category_name, payload in raci_root.items():
        if not isinstance(payload, dict):
            continue
        category_info = category_map.get(str(category_name), {"description": "", "domains": []})
        activities: List[Dict[str, Any]] = []
        for key in (
            "gather_application_information",
            "maintain_application_information",
            "verify_application_information",
            "use_application_information",
        ):
            section = payload.get(key) or {}
            if not isinstance(section, dict):
                section = {}
            activities.append(
                {
                    "id": key,
                    "label": prettify(key).title(),
                    "responsible": [prettify(str(x)) for x in (section.get("responsible") or [])],
                    "accountable": [prettify(str(x)) for x in (section.get("accountable") or [])],
                    "consulted": [prettify(str(x)) for x in (section.get("consulted") or [])],
                    "informed": [prettify(str(x)) for x in (section.get("informed") or [])],
                }
            )
        pages.append(
            {
                "category": str(category_name),
                "category_pretty": prettify(str(category_name)),
                "slug": f"raci_{slugify(str(category_name))}.html",
                "purpose": str(payload.get("purpose") or "").strip(),
                "focus_areas": [prettify(str(x)) for x in (payload.get("focus_areas") or [])],
                "typical_attributes": [prettify(str(x)) for x in (payload.get("typical_attributes") or [])],
                "domains": [prettify(x) for x in category_info.get("domains", [])],
                "category_description": str(category_info.get("description") or "").strip(),
                "activities": activities,
            }
        )
    pages.sort(key=lambda x: x["category_pretty"])
    return pages


def collect_attribute_summary(program_pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_attr: Dict[str, Dict[str, Any]] = {}
    for page in program_pages:
        program = page["program"]
        for group in page["groups"]:
            for row in group["rows"]:
                entry = by_attr.setdefault(
                    row["attribute"],
                    {"attribute": row["attribute"], "definition": row["definition"], "domains": []},
                )
                entry["domains"].append(
                    {
                        "program": program,
                        "how_used": row["how_used"],
                        "metadata_domain": row["metadata_domain"],
                        "authoritative_type": row["authoritative_type"],
                        "typical_systems": row["typical_systems"],
                    }
                )
    items: List[Dict[str, Any]] = []
    for name in sorted(by_attr):
        data = by_attr[name]
        data["domains"] = sorted(data["domains"], key=lambda x: x["program"])
        items.append(data)
    return items


def collect_authoritative_summary(program_pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_source: Dict[str, Dict[str, Any]] = {}
    for page in program_pages:
        program = page["program"]
        for group in page["groups"]:
            for row in group["rows"]:
                source = row["authoritative_type"] or "Unspecified"
                src = by_source.setdefault(source, {"source": source, "attributes": {}})
                attr_entry = src["attributes"].setdefault(
                    row["attribute"],
                    {
                        "attribute": row["attribute"],
                        "definition": row["definition"],
                        "metadata_domain": row["metadata_domain"],
                        "typical_systems": row["typical_systems"],
                        "programs": set(),
                    },
                )
                attr_entry["programs"].add(program)
    items: List[Dict[str, Any]] = []
    for source in sorted(by_source):
        attrs: List[Dict[str, Any]] = []
        for attr_name in sorted(by_source[source]["attributes"]):
            data = by_source[source]["attributes"][attr_name]
            attrs.append(
                {
                    "attribute": data["attribute"],
                    "definition": data["definition"],
                    "metadata_domain": data["metadata_domain"],
                    "typical_systems": data["typical_systems"],
                    "programs": sorted(data["programs"]),
                }
            )
        items.append({"source": source, "attributes": attrs})
    return items


def css_block() -> str:
    return """
@import url(\"https://fonts.googleapis.com/css?family=Oleo+Script:400|Open+Sans:300,300italic,600,600italic,800\");
*{box-sizing:border-box}
body{
  margin:0;
  background-color:#f7f7f7;
  background-image:linear-gradient(to top, rgba(0,0,0,0.04), rgba(0,0,0,0)), radial-gradient(circle at top left, rgba(0,144,197,0.06), transparent 35%), radial-gradient(circle at top right, rgba(84,201,150,0.06), transparent 35%);
  font-family:'Open Sans',sans-serif;
  font-size:13pt;
  color:#696969;
  font-weight:300;
  line-height:2.15em;
}
a{color:#0090c5;text-decoration:underline}
a:hover{text-decoration:none}
h1,h2,h3,h4,h5,h6{color:#444;font-weight:800;margin:0}
h2{font-size:2.15em;line-height:1.15}
h3{font-size:1.2em;line-height:1.3}
p{margin:0}
#page-wrapper{min-height:100vh}
.container{margin:0 auto;max-width:100%;width:1240px}
#header-wrapper{background:#ffffff;box-shadow:inset 0 -1px 0 #d8d8d8}
#header{display:flex;justify-content:space-between;align-items:center;gap:2rem;padding:2.25rem 0}
#logo h1{display:inline-block;font-family:'Oleo Script',serif;font-size:3.1em;line-height:1;margin-right:.3em}
#logo h1 a{color:#444;text-decoration:none}
#logo span{font-size:.85em;letter-spacing:.12rem;text-transform:uppercase;color:#999}
#nav ul{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:.5rem;align-items:center}
#nav ul li a{display:block;padding:.75rem 1.15rem;border-radius:6px;text-decoration:none;color:#444;font-weight:700;font-size:.9em;letter-spacing:.06rem}
#nav ul li a:hover{background:#f1f1f1}
#nav ul li.current a{background:#444;color:#fff}
#banner-wrapper{padding:2rem 0 0}
#banner{background:#fff;border-radius:10px;box-shadow:0 2px 0 0 #e3e3e3;padding:3rem 3rem}
#banner .row{display:flex;flex-wrap:wrap;gap:2rem;align-items:center}
#banner .left{flex:1 1 34rem}
#banner .right{flex:0 1 22rem}
#banner h2{font-size:2.7em;line-height:1.05;margin-bottom:.35em}
#banner p{font-size:1.2em;line-height:1.8em;color:#666}
#banner .actions{display:flex;flex-direction:column;gap:1rem}
.button{display:inline-block;padding:.95rem 1.5rem;border-radius:8px;background:#0090c5;color:#fff!important;text-decoration:none;font-weight:700;line-height:1.2;box-shadow:inset 0 -2px 0 rgba(0,0,0,0.15);border:0;cursor:pointer}
.button.alt,.button.secondary{background:#f1f1f1;color:#444!important;box-shadow:inset 0 -2px 0 rgba(0,0,0,0.08)}
.button.small{padding:.75rem 1rem;font-size:.9em}
#features-wrapper{padding:2rem 0 0}
.feature-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:1.5rem}
.box{background:#fff;border-radius:10px;box-shadow:0 2px 0 0 #e3e3e3}
.box.feature{padding:0}
.box.feature .inner{padding:1.6rem 1.6rem 1.8rem}
.box.feature header{margin-bottom:1.25rem}
.box.feature header h3{margin-bottom:.35rem}
.box.feature header p{color:#999;line-height:1.7}
.box.feature p{line-height:1.8;color:#666}
#main-wrapper{padding:2rem 0 0}
#main-wrapper .row{display:flex;flex-wrap:wrap;gap:2rem;align-items:flex-start}
.main-col{flex:1 1 100%}
.box.panel{padding:2rem 2.2rem}
.box.panel h2{margin-bottom:1rem}
.box.panel p.lead{margin-bottom:1.6rem;color:#666;line-height:1.85}
.toolbar{display:flex;gap:1rem;align-items:center;flex-wrap:wrap;margin-bottom:1.5rem}
.input{appearance:none;border:solid 1px #ddd;border-radius:8px;background:#fff;color:#555;height:3rem;padding:0 1rem;min-width:18rem;font:inherit}
.input:focus{outline:none;border-color:#0090c5;box-shadow:0 0 0 3px rgba(0,144,197,0.08)}
.stats{margin-left:auto;font-size:.85em;letter-spacing:.08rem;text-transform:uppercase;color:#999;line-height:1.4}
.article-stack{display:flex;flex-direction:column;gap:1.5rem}
.section-block{padding-top:.5rem;border-top:solid 1px #eee}
.section-block:first-child{padding-top:0;border-top:0}
.section-block h3{margin-bottom:.9rem}
.link-list{display:flex;flex-direction:column;gap:.7rem;padding-left:.85rem}
.link-card{display:block;padding:.85rem 0;border-bottom:solid 1px #f0f0f0;text-decoration:none}
.link-card:last-child{border-bottom:0}
.link-title{font-size:.94em;font-weight:700;color:#444;line-height:1.35}
.link-meta{font-size:.86em;color:#888;line-height:1.6}
.detail-list{display:flex;flex-direction:column;gap:1.5rem}
.flow-row{display:grid;grid-template-columns:minmax(18rem, 24rem) 1fr;gap:2rem;align-items:flex-start;padding-bottom:1.25rem;border-bottom:solid 1px #f0f0f0}
.flow-row:last-child{padding-bottom:0;border-bottom:0}
.flow-row.flow-row-emphasis{grid-template-columns:minmax(15rem, 18rem) 1fr;gap:3.25rem}
.attr-block h3{margin-bottom:.45rem}
.attr-block p{color:#666;line-height:1.85}
.detail-stack{display:grid;grid-template-columns:repeat(auto-fit,minmax(12rem,1fr));gap:1rem 1.25rem}
.detail-line{display:flex;flex-direction:column;gap:.2rem}
.detail-label{font-size:.72em;letter-spacing:.12rem;text-transform:uppercase;color:#000;line-height:1.4;font-weight:800}
.detail-value{color:#555;line-height:1.8}
.domain-cluster{display:flex;flex-wrap:wrap;gap:1rem 1.5rem;align-items:flex-start}
.domain-item{display:flex;align-items:flex-start;gap:.65rem;min-width:9rem;max-width:13rem;cursor:default}
.domain-icon{display:block;min-width:1.2rem;font-size:1.05rem;line-height:1.2;color:#0090c5}
.domain-text{display:flex;flex-direction:column;font-size:.92em;line-height:1.35;font-weight:300;color:#555}
.domain-text div{font-weight:300}
.flow-row.flow-row-emphasis .domain-cluster{padding-left:1rem}
.flow-row.flow-row-emphasis .domain-item{min-width:11rem;max-width:16rem;gap:.85rem}
.flow-row.flow-row-emphasis .domain-icon{min-width:1.55rem;font-size:1.35rem}
.flow-row.flow-row-emphasis .domain-text{font-size:1.02em;line-height:1.45}
.signal-grid-4{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1rem 1.5rem;align-items:start}
.signal-grid-4 .domain-item{min-width:0;max-width:none}
.tooltip{position:absolute;z-index:20;display:none;max-width:34rem;background:#ffffff;border:solid 1px #ddd;border-radius:8px;padding:1rem 1.1rem;box-shadow:0 6px 18px rgba(0,0,0,0.08)}
.tooltip .t-h{font-weight:800;color:#444;margin-bottom:.4rem;line-height:1.35}
.tooltip .t-l{color:#666;line-height:1.75;margin-top:.25rem}
.tooltip .t-l strong{color:#444;font-weight:700}
.raci-table{width:100%;border-collapse:collapse;table-layout:fixed}
.raci-table th,.raci-table td{border:1px solid #e5e5e5;padding:.7rem .75rem;vertical-align:top;line-height:1.55}
.raci-table th{background:#f7f7f7;color:#444;font-weight:800;font-size:.78em;letter-spacing:.08rem;text-transform:uppercase}
.raci-table td.item{font-weight:700;color:#444;width:22%}
.raci-table td{word-break:break-word}
#footer-wrapper{padding:2rem 0 3rem}
#footer{color:#999;font-size:.82em}
#copyright{text-align:center;padding-top:1rem}
#copyright .menu{list-style:none;margin:0;padding:0;display:flex;justify-content:center;gap:1rem;flex-wrap:wrap}
#copyright .menu li{line-height:1.6}
@media screen and (max-width: 980px){
  .container{width:90%}
  #header{flex-direction:column;align-items:flex-start}
  #banner .row{flex-direction:column}
  #banner .right{width:100%;flex:1 1 auto}
  .flow-row,.flow-row.flow-row-emphasis{grid-template-columns:1fr;gap:1rem}
  .flow-row.flow-row-emphasis .domain-cluster{padding-left:0}
  .signal-grid-4{grid-template-columns:repeat(2,minmax(0,1fr))}
  .stats{margin-left:0}
}
@media screen and (max-width: 736px){
  body{font-size:11pt}
  .container{width:100%;padding:0 1rem}
  #header{padding:1.5rem 0}
  #logo h1{font-size:2.2em}
  #nav ul{width:100%;justify-content:flex-start}
  #nav ul li a{padding:.65rem .9rem}
  #banner{padding:2rem 1.5rem}
  .toolbar{flex-direction:column;align-items:stretch}
  .input{min-width:0;width:100%}
  .signal-grid-4{grid-template-columns:1fr}
}
"""


def shell_head(title: str, subtitle: str, nav_links: List[Tuple[str, str]], banner_actions: List[Tuple[str, str, str]] | None = None) -> str:
    nav_parts: List[str] = []
    for i, (label, href) in enumerate(nav_links):
        cls = 'current' if i == 0 else ''
        nav_parts.append(f'<li class="{cls}"><a href="{html.escape(href)}">{html.escape(label)}</a></li>')
    nav_items = ''.join(nav_parts)
    action_html = ''
    if banner_actions:
        action_parts: List[str] = []
        for label, href, kind in banner_actions:
            cls = 'button alt' if kind == 'alt' else 'button'
            action_parts.append(f'<a href="{html.escape(href)}" class="{cls}">{html.escape(label)}</a>')
        action_html = '<div class="right"><div class="actions">' + ''.join(action_parts) + '</div></div>'
    return f"""<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\" />
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, user-scalable=no\" />
<title>{html.escape(title)}</title>
<style>
{css_block()}
</style>
</head>
<body>
<div id=\"page-wrapper\">
  <div id=\"header-wrapper\">
    <header id=\"header\" class=\"container\">
      <div id=\"logo\">
        <h1><a href=\"index.html\">Application Inventory</a></h1>
        <span>Framework Views</span>
      </div>
      <nav id=\"nav\"><ul>{nav_items}</ul></nav>
    </header>
  </div>
  <div id=\"banner-wrapper\">
    <div id=\"banner\" class=\"box container\">
      <div class=\"row\">
        <div class=\"left\">
          <h2>{html.escape(title)}</h2>
          <p>{html.escape(subtitle)}</p>
        </div>
        {action_html}
      </div>
    </div>
  </div>
"""


def footer_block(label: str) -> str:
    return f"""
  <div id=\"footer-wrapper\">
    <footer id=\"footer\" class=\"container\">
      <div id=\"copyright\">
        <ul class=\"menu\">
          <li>{html.escape(label)}</li>
        </ul>
      </div>
    </footer>
  </div>
</div>
</body>
</html>
"""


def build_domain_card(page: Dict[str, Any]) -> str:
    meta = page['count']
    meta_text = f"{meta} mapped attributes" if isinstance(meta, int) else str(meta)
    return (
        f'<a class="link-card" href="{html.escape(page["slug"])}">'
        f'<div class="link-title">{html.escape(page["program"])}</div>'
        f'<div class="link-meta">{html.escape(meta_text)}</div>'
        '</a>'
    )


def build_signal_item(name: str, attrs: Dict[str, str]) -> str:
    icon, label = domain_visual(name)
    lines = ''.join(f'<div>{html.escape(part)}</div>' for part in label.split('\n'))
    attr_str = ' '.join(f'{k}="{html.escape(v)}"' for k, v in attrs.items())
    return (
        f'<span class="domain-item" {attr_str}>'
        f'<span class="domain-icon">{html.escape(icon)}</span>'
        f'<span class="domain-text">{lines}</span>'
        '</span>'
    )


def render_index(program_pages: List[Dict[str, Any]], raci_pages: List[Dict[str, Any]]) -> str:
    by_program = {page['program']: page for page in program_pages}
    raci_by_category = {r["category"]: r for r in raci_pages}
    used: set[str] = set()
    boxes: List[str] = []
    for heading, names in INDEX_SECTIONS:
        links: List[str] = []
        for name in names:
            page = by_program.get(name)
            if not page:
                continue
            used.add(name)
            links.append(build_domain_card(page))
        if links:
            raci_link = ""
            raci_page = raci_by_category.get(heading)
            if raci_page:
                raci_link = (
                    '<div style="margin-top:1rem;">'
                    f'<a href="{html.escape(raci_page["slug"])}" class="button alt">View {html.escape(raci_page["category_pretty"])} RACI</a>'
                    '</div>'
                )
            boxes.append(
                f'<section class="box feature {SECTION_ACCENTS.get(heading, "")}">' 
                '<div class="inner">'
                f'<header><h3>{html.escape(heading)}</h3></header>'
                f'<div class="link-list">{"".join(links)}</div>{raci_link}'
                '</div></section>'
            )
    remaining = [by_program[name] for name in sorted(by_program) if name not in used]
    if remaining:
        boxes.append(
            f'<section class="box feature {SECTION_ACCENTS["Additional Domains"]}"><div class="inner"><header><h3>Additional Domains</h3></header><div class="link-list">{"".join(build_domain_card(p) for p in remaining)}</div></div></section>'
        )
    return shell_head(
        'Master Application Dictionary',
        'Select a focused domain view or use the summary pages to review the framework from the inverse relationships.',
        [("Home", "index.html")],
        [
            ("Attribute Summary", "attribute_summary.html", "primary"),
            ("Sources Summary", "authoritative_source_summary.html", "alt"),
        ],
    ) + (
        '<div id="features-wrapper"><div class="container"><div class="feature-grid">' + ''.join(boxes) + '</div></div></div>' +
        footer_block('Metadata Model - Enterprise Visibility Layer')
    )


def script_block(item_selector: str, stats_suffix: str | None, hover_selector: str | None = None) -> str:
    hover = ''
    if hover_selector:
        hover = (
            f"  document.querySelectorAll('{hover_selector}').forEach((el) => {{\n"
            "    el.addEventListener('mousemove', (e) => {\n"
            "      tip.innerHTML = '<div class=\"t-h\">' + (el.dataset.title || '') + '</div>' +\n"
            "        '<div class=\"t-l\"><strong>Metadata Domain:</strong> ' + (el.dataset.domain || '—') + '</div>' +\n"
            "        '<div class=\"t-l\"><strong>Definition:</strong> ' + (el.dataset.definition || '—') + '</div>' +\n"
            "        '<div class=\"t-l\"><strong>How Used / Programs:</strong> ' + (el.dataset.detail || '—') + '</div>' +\n"
            "        '<div class=\"t-l\"><strong>Authoritative Source:</strong> ' + (el.dataset.authtype || '—') + '</div>' +\n"
            "        ((el.dataset.typical || '') ? '<div class=\"t-l\"><strong>Typical Systems:</strong> ' + el.dataset.typical + '</div>' : '');\n"
            "      tip.style.display = 'block';\n"
            "      const pad = 12; let x = e.pageX + pad; let y = e.pageY + pad;\n"
            "      const rect = tip.getBoundingClientRect(); const vw = window.innerWidth; const vh = window.innerHeight;\n"
            "      if (x + rect.width > vw - 10) x = vw - rect.width - 10;\n"
            "      if (y + rect.height > vh - 10) y = vh - rect.height - 10;\n"
            "      tip.style.left = x + 'px'; tip.style.top = y + 'px';\n"
            "    });\n"
            "    el.addEventListener('mouseleave', () => { tip.style.display = 'none'; });\n"
            "  });\n"
        )
    stats_line = "    stats.textContent = '';\n"
    if stats_suffix:
        stats_line = f"    stats.textContent = `${{visible}} / ${{items.length}} {stats_suffix}`;\n"
    return (
        '<script>\n'
        '(function(){\n'
        f"  const items = Array.from(document.querySelectorAll('{item_selector}'));\n"
        "  const q = document.getElementById('q');\n"
        "  const stats = document.getElementById('stats');\n"
        "  const tip = document.getElementById('tooltip');\n"
        "  function apply(){\n"
        "    const needle = (q.value || '').trim().toLowerCase();\n"
        "    let visible = 0;\n"
        "    items.forEach((item) => {\n"
        "      const show = !needle || (item.dataset.search || '').includes(needle);\n"
        "      item.style.display = show ? '' : 'none';\n"
        "      if (show) visible += 1;\n"
        "    });\n"
        f"{stats_line}"
        "  }\n"
        "  q.addEventListener('input', apply);\n"
        "  document.getElementById('clear').addEventListener('click', () => { q.value = ''; apply(); });\n"
        f"{hover}"
        "  apply();\n"
        '})();\n'
        '</script>\n'
    )


def render_domain_page(page: Dict[str, Any], raci_link: str | None) -> str:
    sections: List[str] = [
        '<div id="main-wrapper"><div class="container"><div class="row"><div class="main-col">',
        '<div class="box panel">'
        '<p class="lead">This view keeps the full dictionary visible for a single program context. The content is unchanged; the layout is simplified so the attribute signal reads first and the operational detail reads second.</p>'
        '<div class="toolbar" id="search">'
        '<input id="q" class="input" type="search" placeholder="Search attributes or text..." />'
        '<button id="clear" class="button secondary small" type="button">Clear</button>'
        '<div class="stats" id="stats"></div>'
        '</div></div>'
    ]
    for group in page['groups']:
        rows: List[str] = []
        for row in group['rows']:
            search_bits = ' '.join([row['attribute'], row['definition'], row['how_used'], row['metadata_domain'], row['authoritative_type'], row['typical_systems']]).lower()
            rows.append(
                '<div class="flow-row detail-item" data-search="{}">'.format(html.escape(search_bits)) +
                '<div class="attr-block">'
                f'<h3>{html.escape(row["attribute"])}</h3>'
                f'<p>{html.escape(row["definition"])}</p>'
                '</div>'
                '<div class="detail-stack">'
                f'<div class="detail-line"><div class="detail-label">How Used</div><div class="detail-value">{html.escape(row["how_used"])}</div></div>'
                f'<div class="detail-line"><div class="detail-label">Metadata Domain</div><div class="detail-value">{html.escape(row["metadata_domain"])}</div></div>'
                f'<div class="detail-line"><div class="detail-label">Authoritative Source</div><div class="detail-value">{html.escape(row["authoritative_type"] or "—")}</div></div>'
                f'<div class="detail-line"><div class="detail-label">Typical Systems</div><div class="detail-value">{html.escape(row["typical_systems"] or "—")}</div></div>'
                '</div></div>'
            )
        sections.append(
            '<div class="box panel">'
            f'<div class="detail-list">{"".join(rows)}</div>'
            '</div>'
        )
    if not page['groups']:
        sections.append('<div class="box panel"><p>No attributes mapped to this domain.</p></div>')
    sections.append('</div></div></div></div>')
    subtitle = f'Attributes used by {page["program"]}. Search the full dictionary text below.'
    if raci_link:
        subtitle = subtitle + ' This domain is mapped to a category RACI model.'
    body = ''.join(sections)
    return shell_head(
        page['program'],
        subtitle,
        [("Search", "#search"), ("Index", "index.html")],
        [("RACI Model", raci_link, "alt")] if raci_link else None,
    ) + body + script_block('.detail-item', 'attributes') + footer_block('Metadata Model - Focused Domain View')


def _raci_people(values: List[str]) -> str:
    if not values:
        return "—"
    return ", ".join(html.escape(v) for v in values)


def render_raci_page(page: Dict[str, Any]) -> str:
    activity_rows: List[str] = []
    for activity in page["activities"]:
        activity_rows.append(
            "<tr>"
            f"<td class='item'>{html.escape(activity['label'])}</td>"
            f"<td>{_raci_people(activity['responsible'])}</td>"
            f"<td>{_raci_people(activity['accountable'])}</td>"
            f"<td>{_raci_people(activity['consulted'])}</td>"
            f"<td>{_raci_people(activity['informed'])}</td>"
            "</tr>"
        )
    body = (
        '<div id="main-wrapper"><div class="container"><div class="row"><div class="main-col">'
        '<div class="box panel">'
        f'<table class="raci-table"><thead><tr><th>Item</th><th>Responsible</th><th>Accountable</th><th>Consulted</th><th>Informed</th></tr></thead><tbody>{"".join(activity_rows)}</tbody></table>'
        '</div>'
        '</div></div></div></div>'
    )
    return shell_head(
        f'{page["category_pretty"]} Category RACI',
        "Role matrix for gathering, maintaining, verifying, and using application metadata.",
        [("Index", "index.html")],
    ) + body + footer_block("Metadata Model - Category RACI Detail")


def render_attribute_summary(items: List[Dict[str, Any]]) -> str:
    rows: List[str] = []
    for item in items:
        search_bits = [item['attribute'], item['definition']]
        signals: List[str] = []
        for dom in item['domains']:
            search_bits.extend([dom['program'], dom['how_used'], dom['metadata_domain'], dom['authoritative_type'], dom['typical_systems']])
            signals.append(
                build_signal_item(
                    dom['program'],
                    {
                        'data-title': dom['program'],
                        'data-detail': dom['how_used'],
                        'data-definition': item['definition'],
                        'data-domain': dom['metadata_domain'],
                        'data-authtype': dom['authoritative_type'],
                        'data-typical': dom['typical_systems'],
                    },
                )
            )
        rows.append(
            '<div class="flow-row flow-row-emphasis flow-item" data-search="{}">'.format(html.escape(' '.join(search_bits).lower())) +
            '<div class="attr-block">'
            f'<h3>{html.escape(item["attribute"])}</h3>'
            f'<p>{html.escape(item["definition"])}</p>'
            '</div>'
            f'<div class="domain-cluster">{"".join(signals)}</div>'
            '</div>'
        )
    body = (
        '<div id="main-wrapper"><div class="container"><div class="row"><div class="main-col">'
        '<div class="box panel">'
        '<p class="lead">This inverse view keeps each attribute as the anchor and shows every domain that consumes it. This is a relationship view, not a workflow diagram.</p>'
        '<div class="toolbar" id="search">'
        '<input id="q" class="input" type="search" placeholder="Search attributes or domains..." />'
        '<button id="clear" class="button secondary small" type="button">Clear</button>'
        '<div class="stats" id="stats"></div>'
        '</div>'
        f'<div class="detail-list">{"".join(rows)}</div>'
        '<div id="tooltip" class="tooltip"></div>'
        '</div></div></div></div></div>'
    )
    return shell_head(
        'Attribute Summary',
        'Each attribute is shown once with the domains that use it.',
        [("Search", "#search"), ("Index", "index.html")],
    ) + body + script_block('.flow-item', None, '.domain-item') + footer_block('Metadata Model - Relational Attribute View')


def render_authoritative_source_summary(items: List[Dict[str, Any]]) -> str:
    rows: List[str] = []
    for item in items:
        search_bits = [item['source']]
        signals: List[str] = []
        for attr in item['attributes']:
            programs = ', '.join(attr['programs'])
            search_bits.extend([attr['attribute'], attr['definition'], attr['metadata_domain'], attr['typical_systems'], programs])
            signals.append(
                build_signal_item(
                    attr['attribute'],
                    {
                        'data-title': attr['attribute'],
                        'data-detail': programs,
                        'data-definition': attr['definition'],
                        'data-domain': attr['metadata_domain'],
                        'data-authtype': item['source'],
                        'data-typical': attr['typical_systems'],
                    },
                )
            )
        rows.append(
            '<div class="flow-row source-item" data-search="{}">'.format(html.escape(' '.join(search_bits).lower())) +
            '<div class="attr-block">'
            f'<h3>{html.escape(item["source"])}</h3>'
            '</div>'
            f'<div class="signal-grid-4">{"".join(signals)}</div>'
            '</div>'
        )
    body = (
        '<div id="main-wrapper"><div class="container"><div class="row"><div class="main-col">'
        '<div class="box panel">'
        '<p class="lead">Each authoritative source type is shown once with the attributes anchored to it. Hover any attribute signal for the contextual details.</p>'
        '<div class="toolbar" id="search">'
        '<input id="q" class="input" type="search" placeholder="Search sources or attributes..." />'
        '<button id="clear" class="button secondary small" type="button">Clear</button>'
        '<div class="stats" id="stats"></div>'
        '</div>'
        f'<div class="detail-list">{"".join(rows)}</div>'
        '<div id="tooltip" class="tooltip"></div>'
        '</div></div></div></div></div>'
    )
    return shell_head(
        'Authoritative Sources Summary',
        'Each authoritative source type is shown once with the attributes anchored to it.',
        [("Search", "#search"), ("Index", "index.html")],
    ) + body + script_block('.source-item', 'sources', '.domain-item') + footer_block('Metadata Model - Source-of-Truth View')


def write_site(
    output_dir: Path,
    program_pages: List[Dict[str, Any]],
    attribute_items: List[Dict[str, Any]],
    source_items: List[Dict[str, Any]],
    raci_pages: List[Dict[str, Any]],
    program_to_category: Dict[str, str],
) -> List[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    pages = {
        'index.html': render_index(program_pages, raci_pages),
        'attribute_summary.html': render_attribute_summary(attribute_items),
        'authoritative_source_summary.html': render_authoritative_source_summary(source_items),
    }
    raci_by_category = {x["category"]: x for x in raci_pages}
    for page in program_pages:
        category_name = program_to_category.get(page["program_key"])
        raci_link = None
        if category_name and category_name in raci_by_category:
            raci_link = raci_by_category[category_name]["slug"]
        pages[page['slug']] = render_domain_page(page, raci_link)
    for page in raci_pages:
        pages[page["slug"]] = render_raci_page(page)
    written: List[Path] = []
    for name, content in pages.items():
        path = output_dir / name
        path.write_text(content, encoding='utf-8')
        written.append(path)
    return written


def main() -> int:
    model = load_model(DEFAULT_INPUT)
    program_pages = collect_program_pages(model)
    category_map, program_to_category = collect_category_maps(model)
    raci_pages = collect_raci_pages(model, category_map)
    attribute_items = collect_attribute_summary(program_pages)
    source_items = collect_authoritative_summary(program_pages)
    written = write_site(
        DEFAULT_OUTPUT_DIR,
        program_pages,
        attribute_items,
        source_items,
        raci_pages,
        program_to_category,
    )
    print(f'wrote {len(written)} files')
    for path in written:
        print(path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
