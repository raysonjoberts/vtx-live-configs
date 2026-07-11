#!/usr/bin/env python3
from __future__ import annotations

import csv
import html
import os
import re
from pathlib import Path
from typing import Any, Dict, List


def resolve_vtx_root() -> Path:
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


VTX_ROOT = resolve_vtx_root()
INPUT_PATH = VTX_ROOT / "var" / "interactive" / "dependencies.csv"
OUTPUT_DIR = VTX_ROOT / "var" / "dependencies"
INDEX_PATH = OUTPUT_DIR / "index.html"

FIELDS = [
    "PRODUCING_APP_ID",
    "Producing App",
    "Consuming App",
    "CONSUMING_APP_ID",
    "Producing Active",
    "Consuming Active",
    "Sharedata Type",
    "Frequency",
    "Mechanism",
    "Classification",
    "DESCRIPTION",
]


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def is_active_flag(value: Any) -> bool:
    return normalize_text(value).upper() == "Y"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", normalize_text(value).lower())
    slug = slug.strip("_")
    return slug or "application"


def safe_filename(app_name: str, used: Dict[str, int]) -> str:
    base = slugify(app_name)
    count = used.get(base, 0)
    used[base] = count + 1
    if count == 0:
        return f"{base}.html"
    return f"{base}_{count + 1}.html"


def build_app_id_map(rows: List[Dict[str, str]]) -> Dict[str, str]:
    app_ids: Dict[str, str] = {}
    for row in rows:
        for name_field, id_field in (
            ("Producing App", "PRODUCING_APP_ID"),
            ("Consuming App", "CONSUMING_APP_ID"),
        ):
            app_name = normalize_text(row.get(name_field))
            app_id = normalize_text(row.get(id_field))
            if not app_name or not app_id:
                continue
            app_ids.setdefault(app_name, app_id)
    return app_ids


def html_page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)}</title>
  <style>
    body {{
      font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      margin: 0;
      padding: 24px;
      background: #0e1222;
      color: #eaeef8;
    }}
    a {{
      color: #9ec5ff;
      text-decoration: none;
    }}
    a:hover {{
      text-decoration: underline;
    }}
    .page {{
      max-width: 1480px;
      margin: 0 auto;
    }}
    .topbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      margin-bottom: 18px;
      flex-wrap: wrap;
    }}
    .title {{
      font-size: 28px;
      font-weight: 800;
      letter-spacing: 0.01em;
    }}
    .subtitle {{
      color: #9aa3b2;
      font-size: 13px;
      margin-top: 6px;
    }}
    .nav-link {{
      background: #151a30;
      border: 1px solid rgba(148, 163, 184, 0.28);
      border-radius: 999px;
      padding: 10px 14px;
      display: inline-block;
      color: #eaeef8;
      font-weight: 600;
    }}
    .search-wrap {{
      margin-top: 16px;
    }}
    .search-input {{
      width: min(560px, 100%);
      background: #111827;
      color: #eaeef8;
      border: 1px solid rgba(148, 163, 184, 0.28);
      border-radius: 12px;
      padding: 12px 14px;
      font-size: 14px;
      outline: none;
      box-sizing: border-box;
    }}
    .search-input::placeholder {{
      color: #74809a;
    }}
    .search-hint {{
      color: #9aa3b2;
      font-size: 12px;
      margin-top: 8px;
    }}
    .hero {{
      background: #151a30;
      border-radius: 16px;
      padding: 22px;
      box-shadow: 0 10px 32px rgba(0, 0, 0, 0.34);
      margin-bottom: 18px;
    }}
    .hero-grid {{
      display: grid;
      grid-template-columns: 1fr 320px;
      gap: 18px;
      align-items: center;
    }}
    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .summary-card {{
      background: #111827;
      border: 1px solid rgba(148, 163, 184, 0.2);
      border-radius: 12px;
      padding: 12px 14px;
    }}
    .summary-label {{
      color: #9aa3b2;
      font-size: 11px;
      margin-bottom: 4px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }}
    .summary-value {{
      font-size: 24px;
      font-weight: 800;
    }}
    .matrix {{
      display: grid;
      grid-template-columns: 1fr 280px 1fr;
      gap: 18px;
      align-items: start;
    }}
    .lane {{
      background: #151a30;
      border-radius: 16px;
      padding: 18px;
      box-shadow: 0 10px 32px rgba(0, 0, 0, 0.34);
      min-height: 320px;
    }}
    .lane-title {{
      color: #c5d0ff;
      font-size: 16px;
      font-weight: 800;
      margin-bottom: 8px;
    }}
    .lane-note {{
      color: #9aa3b2;
      font-size: 12px;
      margin-bottom: 14px;
      line-height: 1.5;
    }}
    .hub {{
      position: sticky;
      top: 24px;
      background: linear-gradient(180deg, #1c2441 0%, #151a30 100%);
      border-radius: 20px;
      padding: 28px 22px;
      box-shadow: 0 10px 32px rgba(0, 0, 0, 0.34);
      border: 1px solid rgba(148, 163, 184, 0.22);
      text-align: center;
    }}
    .hub-label {{
      color: #9aa3b2;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      margin-bottom: 10px;
    }}
    .hub-name {{
      font-size: 24px;
      font-weight: 800;
      line-height: 1.25;
    }}
    .app-identity {{
      display: flex;
      flex-direction: column;
      gap: 4px;
      margin-bottom: 8px;
    }}
    .app-name {{
      font-size: 18px;
      font-weight: 800;
      line-height: 1.35;
    }}
    .app-id {{
      color: #c5d0ff;
      font-size: 14px;
      font-weight: 700;
      letter-spacing: 0.02em;
    }}
    .cards {{
      display: flex;
      flex-direction: column;
      gap: 14px;
    }}
    .dep-card {{
      background: #111827;
      border: 1px solid rgba(148, 163, 184, 0.22);
      border-radius: 14px;
      padding: 14px;
    }}
    .dep-app {{
      font-size: 18px;
      font-weight: 800;
      margin-bottom: 6px;
    }}
    .dep-meta {{
      color: #9aa3b2;
      font-size: 12px;
      margin-bottom: 10px;
    }}
    .detail-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .detail {{
      background: #0e1222;
      border-radius: 10px;
      padding: 10px 12px;
      border: 1px solid rgba(148, 163, 184, 0.16);
    }}
    .detail-label {{
      color: #9aa3b2;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin-bottom: 4px;
    }}
    .detail-value {{
      font-size: 13px;
      line-height: 1.5;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .empty {{
      color: #9aa3b2;
      font-size: 14px;
      padding: 12px 0;
    }}
    .index-list {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 14px;
      margin-top: 18px;
    }}
    .index-card {{
      background: #151a30;
      border-radius: 14px;
      padding: 16px;
      box-shadow: 0 10px 32px rgba(0, 0, 0, 0.34);
    }}
    .index-name {{
      font-size: 18px;
      font-weight: 800;
      margin-bottom: 8px;
    }}
    .index-id {{
      color: #c5d0ff;
      font-size: 13px;
      font-weight: 700;
      margin-bottom: 10px;
    }}
    .index-stats {{
      color: #9aa3b2;
      font-size: 12px;
      line-height: 1.6;
    }}
    @media (max-width: 1100px) {{
      .hero-grid {{
        grid-template-columns: 1fr;
      }}
      .matrix {{
        grid-template-columns: 1fr;
      }}
      .hub {{
        position: static;
        order: -1;
      }}
    }}
    @media (max-width: 680px) {{
      body {{
        padding: 16px;
      }}
      .detail-grid, .summary-grid {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class="page">
    {body}
  </div>
  <script>
    document.querySelectorAll('[data-search-input]').forEach(function(input) {{
      var targetSelector = input.getAttribute('data-search-target');
      var targets = Array.prototype.slice.call(document.querySelectorAll(targetSelector || ''));
      var emptySelector = input.getAttribute('data-empty-target');
      var emptyState = emptySelector ? document.querySelector(emptySelector) : null;

      function applyFilter() {{
        var query = input.value.trim().toLowerCase();
        var visible = 0;
        targets.forEach(function(node) {{
          var haystack = (node.getAttribute('data-search') || '').toLowerCase();
          var show = !query || haystack.indexOf(query) !== -1;
          node.style.display = show ? '' : 'none';
          if (show) visible += 1;
        }});
        if (emptyState) {{
          emptyState.style.display = visible === 0 ? 'block' : 'none';
        }}
      }}

      input.addEventListener('input', applyFilter);
      applyFilter();
    }});
  </script>
</body>
</html>
"""


def load_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Dependency input file not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"Dependency CSV is missing a header row: {path}")
        missing = [field for field in FIELDS if field not in reader.fieldnames]
        if missing:
            raise ValueError(f"Dependency CSV missing required fields: {missing}")
        rows = [{key: normalize_text(value) for key, value in row.items()} for row in reader]
    return rows


def filter_active_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    return [
        row
        for row in rows
        if is_active_flag(row.get("Producing Active")) and is_active_flag(row.get("Consuming Active"))
    ]


def relationship_card(counterparty: str, counterparty_id: str, row: Dict[str, str], direction: str) -> str:
    description = normalize_text(row.get("DESCRIPTION")) or "No description provided."
    frequency = normalize_text(row.get("Frequency")) or "Not specified"
    mechanism = normalize_text(row.get("Mechanism")) or "Not specified"
    classification = normalize_text(row.get("Classification")) or "Not specified"
    sharedata_type = normalize_text(row.get("Sharedata Type")) or "Not specified"
    meta = "Produces data for this application" if direction == "consumer" else "Consumes data from this application"
    search_value = " ".join(part for part in [counterparty, counterparty_id] if part)
    return f"""
      <div class="dep-card" data-search="{html.escape(search_value)}">
        <div class="app-identity">
          <div class="app-name">{html.escape(counterparty)}</div>
          <div class="app-id">{html.escape(counterparty_id or "App ID not available")}</div>
        </div>
        <div class="dep-meta">{html.escape(meta)}</div>
        <div class="detail-grid">
          <div class="detail">
            <div class="detail-label">Sharedata Type</div>
            <div class="detail-value">{html.escape(sharedata_type)}</div>
          </div>
          <div class="detail">
            <div class="detail-label">Frequency</div>
            <div class="detail-value">{html.escape(frequency)}</div>
          </div>
          <div class="detail">
            <div class="detail-label">Mechanism</div>
            <div class="detail-value">{html.escape(mechanism)}</div>
          </div>
          <div class="detail">
            <div class="detail-label">Classification</div>
            <div class="detail-value">{html.escape(classification)}</div>
          </div>
          <div class="detail">
            <div class="detail-label">DESCRIPTION</div>
            <div class="detail-value">{html.escape(description)}</div>
          </div>
        </div>
      </div>
    """


def render_lane(title: str, note: str, cards: List[str]) -> str:
    if not cards:
        inner = '<div class="empty">No active dependency relationships in this direction.</div>'
    else:
        inner = f'<div class="cards">{"".join(cards)}</div>'
    return f"""
      <section class="lane">
        <div class="lane-title">{html.escape(title)}</div>
        <div class="lane-note">{html.escape(note)}</div>
        {inner}
      </section>
    """


def render_app_page(app_name: str, app_id: str, incoming: List[Dict[str, str]], outgoing: List[Dict[str, str]]) -> str:
    consumer_cards = [
        relationship_card(row.get("Producing App", ""), row.get("PRODUCING_APP_ID", ""), row, "consumer")
        for row in incoming
    ]
    producer_cards = [
        relationship_card(row.get("Consuming App", ""), row.get("CONSUMING_APP_ID", ""), row, "producer")
        for row in outgoing
    ]
    body = f"""
      <div class="topbar">
        <div>
          <div class="title">{html.escape(app_name)}</div>
          <div class="subtitle">{html.escape(app_id or "App ID not available")}</div>
        </div>
        <a class="nav-link" href="index.html">Back to Index</a>
      </div>

      <section class="hero">
        <div class="hero-grid">
          <div>
            <div class="title" style="font-size:22px;">Dependency overview</div>
            <div class="subtitle">Left side shows applications that produce data consumed by this application. Right side shows applications that consume data produced by this application.</div>
            <div class="search-wrap">
              <input
                class="search-input"
                type="search"
                placeholder="Search related applications by name or App ID"
                data-search-input
                data-search-target=".dep-card"
                data-empty-target="#dependency-search-empty"
              />
              <div class="search-hint">Search filters both Consumer For and Producer For relationship cards.</div>
            </div>
          </div>
          <div class="summary-grid">
            <div class="summary-card">
              <div class="summary-label">Consumer For</div>
              <div class="summary-value">{len(incoming)}</div>
            </div>
            <div class="summary-card">
              <div class="summary-label">Producer For</div>
              <div class="summary-value">{len(outgoing)}</div>
            </div>
          </div>
        </div>
      </section>

      <div class="matrix">
        {render_lane("Consumer For", "Producing applications that provide content consumed by this application.", consumer_cards)}
        <aside class="hub">
          <div class="hub-label">Centered Application</div>
          <div class="app-identity">
            <div class="hub-name">{html.escape(app_name)}</div>
            <div class="app-id">{html.escape(app_id or "App ID not available")}</div>
          </div>
        </aside>
        {render_lane("Producer For", "Consuming applications that depend on content produced by this application.", producer_cards)}
      </div>
      <div class="empty" id="dependency-search-empty" style="display:none; margin-top:16px;">No relationships match the current search.</div>
    """
    return html_page(f"{app_name} dependency view", body)


def render_index(pages: List[Dict[str, Any]], row_count: int) -> str:
    cards = []
    for page in pages:
        search_value = " ".join(part for part in [page["app"], page["app_id"]] if part)
        cards.append(
            f"""
            <div class="index-card" data-search="{html.escape(search_value)}">
              <div class="index-name"><a href="{html.escape(page['filename'])}">{html.escape(page['app'])}</a></div>
              <div class="index-id">{html.escape(page['app_id'] or "App ID not available")}</div>
              <div class="index-stats">
                Consumer For relationships: {page['incoming_count']}<br>
                Producer For relationships: {page['outgoing_count']}
              </div>
            </div>
            """
        )
    body = f"""
      <div class="topbar">
        <div>
          <div class="title">Application Dependency Index</div>
          <div class="subtitle">Application-centric dependency pages generated from active producing and consuming relationships.</div>
        </div>
      </div>

      <section class="hero">
        <div class="hero-grid">
          <div>
            <div class="title" style="font-size:22px;">Dependency visual reference</div>
            <div class="subtitle">Choose an application to open its hub-and-spoke dependency page.</div>
            <div class="search-wrap">
              <input
                class="search-input"
                type="search"
                placeholder="Search applications by name or App ID"
                data-search-input
                data-search-target=".index-card"
                data-empty-target="#index-search-empty"
              />
              <div class="search-hint">Search matches application names and App IDs.</div>
            </div>
          </div>
          <div class="summary-grid">
            <div class="summary-card">
              <div class="summary-label">Active dependency rows</div>
              <div class="summary-value">{row_count}</div>
            </div>
            <div class="summary-card">
              <div class="summary-label">Application pages</div>
              <div class="summary-value">{len(pages)}</div>
            </div>
          </div>
        </div>
      </section>

      <div class="index-list">
        {"".join(cards)}
      </div>
      <div class="empty" id="index-search-empty" style="display:none; margin-top:16px;">No applications match the current search.</div>
    """
    return html_page("Application Dependency Index", body)


def main() -> int:
    rows = load_rows(INPUT_PATH)
    active_rows = filter_active_rows(rows)
    app_ids = build_app_id_map(active_rows)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    apps = sorted(
        {
            normalize_text(row.get("Producing App"))
            for row in active_rows
            if normalize_text(row.get("Producing App"))
        }
        | {
            normalize_text(row.get("Consuming App"))
            for row in active_rows
            if normalize_text(row.get("Consuming App"))
        },
        key=str.lower,
    )

    used_names: Dict[str, int] = {}
    page_records: List[Dict[str, Any]] = []

    for app in apps:
        incoming = [row for row in active_rows if normalize_text(row.get("Consuming App")) == app]
        outgoing = [row for row in active_rows if normalize_text(row.get("Producing App")) == app]
        filename = safe_filename(app, used_names)
        page_path = OUTPUT_DIR / filename
        page_path.write_text(render_app_page(app, app_ids.get(app, ""), incoming, outgoing), encoding="utf-8")
        page_records.append(
            {
                "app": app,
                "app_id": app_ids.get(app, ""),
                "filename": filename,
                "incoming_count": len(incoming),
                "outgoing_count": len(outgoing),
            }
        )

    INDEX_PATH.write_text(render_index(page_records, len(active_rows)), encoding="utf-8")

    print(f"Input file used: {INPUT_PATH}")
    print(f"Number of active dependency rows processed: {len(active_rows)}")
    print(f"Number of application pages generated: {len(page_records)}")
    print(f"Output directory: {OUTPUT_DIR}")
    print(f"Index file path: {INDEX_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
