#!/usr/bin/env python3
"""
vtx_flow_map.py
---------------
Diagnostic-only orchestration mapper for VTX.

Builds a stage-based flow report with:
- Core boot chain
- Pre-configuration workstreams
- Post-configuration workstreams
- Operations (vital vs non-vital)
- Explicit dependency checks (intended vs actual wiring)

Outputs:
  - HTML (default): VTX/var/analysis/vtx_process_flow_map.html
  - JSON (optional)
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml  # type: ignore
except Exception as exc:  # pragma: no cover
    raise SystemExit("Missing dependency: PyYAML. Run from VTX venv.") from exc


@dataclass
class JobRecord:
    kind: str
    job_id: str
    enabled: bool
    role: str
    rule: str
    script: str
    script_abs: str
    script_exists: bool
    inputs: List[str]
    outputs: List[str]
    watch_dirs: List[str]
    schedule: str


def resolve_vtx_root() -> Path:
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    here = Path(__file__).resolve()
    return here.parents[3]


VTX_ROOT = resolve_vtx_root()


def resolve_path(path_str: str) -> Path:
    s = str(path_str or "").strip()
    if not s:
        return VTX_ROOT

    s = (
        s.replace("{VTX_ROOT}", str(VTX_ROOT))
        .replace("${VTX_ROOT}", str(VTX_ROOT))
        .replace("$VTX_ROOT", str(VTX_ROOT))
    )

    p = Path(s).expanduser()
    if not p.is_absolute():
        p = (VTX_ROOT / p).resolve()
    return p


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(VTX_ROOT)).replace("\\", "/")
    except Exception:
        return str(path).replace("\\", "/")


def load_yaml(path: Path) -> Dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be mapping: {path}")
    return data


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def as_list(v: Any) -> List[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def parse_jobs(cfg: Dict[str, Any]) -> List[JobRecord]:
    out: List[JobRecord] = []

    for kind, key in (("cron", "cron_jobs"), ("backbone", "backbone_jobs")):
        for j in as_list(cfg.get(key)):
            if not isinstance(j, dict):
                continue
            script_rel = str(j.get("script") or "").strip()
            script_abs = resolve_path(script_rel)
            inputs = [str(resolve_path(str(x))) for x in as_list(j.get("inputs"))]
            outputs = [str(resolve_path(str(x))) for x in as_list(j.get("outputs"))]
            watch_dirs = [str(resolve_path(str(x))) for x in as_list(j.get("watch_dirs"))]

            sched = ""
            if kind == "cron":
                schedule = j.get("schedule") or {}
                if isinstance(schedule, dict):
                    if schedule.get("cron"):
                        sched = f"cron:{schedule.get('cron')}"
                    elif schedule.get("daily"):
                        sched = f"daily:{schedule.get('daily')}"
                    elif schedule.get("interval"):
                        sched = f"interval:{schedule.get('interval')}"

            out.append(
                JobRecord(
                    kind=kind,
                    job_id=str(j.get("id") or "").strip(),
                    enabled=bool(j.get("enabled", True)),
                    role=str(j.get("role") or "server"),
                    rule=str(j.get("rule") or ""),
                    script=script_rel,
                    script_abs=str(script_abs),
                    script_exists=script_abs.exists(),
                    inputs=inputs,
                    outputs=outputs,
                    watch_dirs=watch_dirs,
                    schedule=sched,
                )
            )

    return out


def find_job(jobs: List[JobRecord], script_suffix: str, role: Optional[str] = None) -> Optional[JobRecord]:
    suffix = script_suffix.replace("\\", "/")
    for j in jobs:
        s = j.script.replace("\\", "/")
        if s.endswith(suffix):
            if role is None or j.role == role:
                return j
    return None


def status_icon(ok: bool) -> str:
    return "OK" if ok else "GAP"


def file_exists_rel(path_rel: str) -> bool:
    return resolve_path(path_rel).exists()


def dir_exists_rel(path_rel: str) -> bool:
    p = resolve_path(path_rel)
    return p.exists() and p.is_dir()


def check_core(jobs: List[JobRecord]) -> Dict[str, Any]:
    orch_service = (VTX_ROOT.parent / "systemd" / "vtx-orchestrator.service").exists()
    orch_script = (VTX_ROOT / "usr" / "scripts" / "default" / "orchestrator.py").exists()

    run_yaml = VTX_ROOT / "usr" / "config" / "run" / "orchestrator.yaml"
    default_yaml = VTX_ROOT / "usr" / "config" / "default" / "orchestrator.yaml"

    cfg_compiler_job = find_job(jobs, "usr/scripts/default/config_compiler.py")
    intake_job = find_job(jobs, "usr/scripts/default/intake_processor.py")

    return {
        "orchestrator_service_exists": orch_service,
        "orchestrator_script_exists": orch_script,
        "run_orchestrator_yaml_exists": run_yaml.exists(),
        "default_orchestrator_yaml_exists": default_yaml.exists(),
        "default_run_orchestrator_same": run_yaml.exists() and default_yaml.exists() and sha256(run_yaml) == sha256(default_yaml),
        "config_compiler_job_present": cfg_compiler_job is not None,
        "config_compiler_job_enabled": bool(cfg_compiler_job.enabled) if cfg_compiler_job else False,
        "config_compiler_watch_default": bool(cfg_compiler_job and any("/usr/config/default" in p.replace("\\", "/") for p in cfg_compiler_job.watch_dirs)),
        "config_compiler_watch_custom": bool(cfg_compiler_job and any("/usr/config/custom" in p.replace("\\", "/") for p in cfg_compiler_job.watch_dirs)),
        "config_compiler_watch_auto": bool(cfg_compiler_job and any("/usr/config/auto" in p.replace("\\", "/") for p in cfg_compiler_job.watch_dirs)),
        "config_compiler_watch_auto_v2": bool(cfg_compiler_job and any("/usr/config/auto_v2" in p.replace("\\", "/") for p in cfg_compiler_job.watch_dirs)),
        "intake_job_present": intake_job is not None,
        "intake_job_enabled": bool(intake_job.enabled) if intake_job else False,
        "intake_watch_customerdata": bool(intake_job and any("/var/customerdata" in p.replace("\\", "/") for p in intake_job.watch_dirs)),
        "customerdata_dir_exists": dir_exists_rel("var/customerdata"),
        "tables_dir_exists": dir_exists_rel("var/tables"),
    }


def build_streams(jobs: List[JobRecord]) -> Dict[str, Any]:
    # Jobs we care about in operational mental model.
    intake = find_job(jobs, "usr/scripts/default/intake_processor.py")
    auto_profile = find_job(jobs, "usr/scripts/default/auto_profile_tables_vtx.py")
    config_compiler = find_job(jobs, "usr/scripts/default/config_compiler.py")
    server_matrix = find_job(jobs, "usr/scripts/analysis/server_inventory_matrix_vtx.py")
    data_source_analysis = find_job(jobs, "usr/scripts/default/data_source_analysis.py")
    data_source_mapping = find_job(jobs, "usr/scripts/default/data_source_analysis_mapping.py")
    snapshot_stats = find_job(jobs, "usr/scripts/default/snapshot_statistics.py")
    stretch_sync = find_job(jobs, "usr/scripts/default/stretch_sync.py")
    stretch_file_processor = find_job(jobs, "usr/scripts/default/stretch_file_processor.py")

    # Static script-level defaults from code (diagnostic hints).
    auto_profile_outputs = [
        "usr/config/auto_v2/table_aggregator_vtx.yaml",
        "usr/config/auto_v2/auto_server_inventory_matrix_vtx.yaml",
    ]

    server_matrix_expected_cfg = [
        "usr/config/run/server_inventory_matrix_vtx.yaml",
        "usr/config/default/server_inventory_matrix_vtx.yaml",
    ]

    dsa_expected_cfg = [
        "usr/config/run/data_source_analysis_reporting.conf",
        "usr/config/run/data_source_analysis.yaml",
    ]

    post_config_markers = [
        "usr/config/custom/data_source_analysis_mapping.yaml",
        "usr/config/custom/table_aggregator_vtx.yaml",
    ]

    pre_config_tree = [
        {
            "node": "Data arrives in var/customerdata",
            "required": ["var/customerdata"],
            "job": intake.job_id if intake else "<missing>",
            "job_present": intake is not None,
            "job_enabled": bool(intake.enabled) if intake else False,
            "produces": ["var/tables/*.csv"],
        },
        {
            "node": "Auto-profile scans var/tables",
            "required": ["var/tables", "usr/config/default/auto_profile_tables_vtx.yaml"],
            "job": auto_profile.job_id if auto_profile else "<missing>",
            "job_present": auto_profile is not None,
            "job_enabled": bool(auto_profile.enabled) if auto_profile else False,
            "produces": auto_profile_outputs,
        },
        {
            "node": "Config compiler merges auto/default/custom -> run",
            "required": ["usr/config/default", "usr/config/run"],
            "job": config_compiler.job_id if config_compiler else "<missing>",
            "job_present": config_compiler is not None,
            "job_enabled": bool(config_compiler.enabled) if config_compiler else False,
            "produces": ["usr/config/run/*.yaml", "usr/config/run/*.conf", "usr/config/run/*.ini"],
        },
        {
            "node": "Server inventory matrix builds unified inventory",
            "required": server_matrix_expected_cfg,
            "job": server_matrix.job_id if server_matrix else "<missing>",
            "job_present": server_matrix is not None,
            "job_enabled": bool(server_matrix.enabled) if server_matrix else False,
            "produces": ["var/tables/views/unified_server_inventory*.csv", "var/detailedreporting/presence_hostname_matrix*.csv"],
        },
        {
            "node": "Data source analysis builds analysis artifacts",
            "required": dsa_expected_cfg,
            "job": data_source_analysis.job_id if data_source_analysis else "<missing>",
            "job_present": data_source_analysis is not None,
            "job_enabled": bool(data_source_analysis.enabled) if data_source_analysis else False,
            "produces": ["var/analysis/*.csv (from reporting conf)"]
        },
    ]

    post_config_tree = [
        {
            "node": "User updates custom mappings (program attributes)",
            "required": ["usr/config/custom/data_source_analysis_mapping.yaml"],
            "job": data_source_mapping.job_id if data_source_mapping else "<missing>",
            "job_present": data_source_mapping is not None,
            "job_enabled": bool(data_source_mapping.enabled) if data_source_mapping else False,
            "produces": ["mapped analysis outputs (job-config dependent)"],
            "intervention_required": True,
        },
        {
            "node": "User customizes table aggregation",
            "required": ["usr/config/custom/table_aggregator_vtx.yaml"],
            "job": "(downstream script-driven; not directly wired in orchestrator.yaml)",
            "job_present": False,
            "job_enabled": False,
            "produces": ["custom masterdata views/parquet (depends on script chain)"],
            "intervention_required": True,
        },
    ]

    ops_vital = [
        {
            "node": "Snapshot statistics",
            "job": snapshot_stats.job_id if snapshot_stats else "<missing>",
            "job_present": snapshot_stats is not None,
            "job_enabled": bool(snapshot_stats.enabled) if snapshot_stats else False,
            "required": ["var/dailysnapshot", "usr/config/run/snapshot_statistics.yaml"],
            "produces": ["var/statistics/*.csv"],
        },
        {
            "node": "Stretch sync",
            "job": stretch_sync.job_id if stretch_sync else "<missing>",
            "job_present": stretch_sync is not None,
            "job_enabled": bool(stretch_sync.enabled) if stretch_sync else False,
            "required": ["usr/config/run/stretch.yaml"],
            "produces": ["external/internal synchronized files"],
        },
        {
            "node": "Stretch file processor",
            "job": stretch_file_processor.job_id if stretch_file_processor else "<missing>",
            "job_present": stretch_file_processor is not None,
            "job_enabled": bool(stretch_file_processor.enabled) if stretch_file_processor else False,
            "required": ["usr/config/run/stretch_file_processor.yaml"],
            "produces": ["stretch output csv files"],
        },
    ]

    ops_non_vital = [
        {
            "node": "Orchestrator status report",
            "job": find_job(jobs, "usr/scripts/ops/orchestrator_status_vtx.py").job_id if find_job(jobs, "usr/scripts/ops/orchestrator_status_vtx.py") else "<missing>",
            "kind": "monitoring",
        },
        {
            "node": "VTX job checker",
            "job": find_job(jobs, "usr/scripts/ops/vtx_job_checker.py").job_id if find_job(jobs, "usr/scripts/ops/vtx_job_checker.py") else "<missing>",
            "kind": "monitoring",
        },
        {
            "node": "Log archive move",
            "job": "Move zipped logs to log archive (server/client)",
            "kind": "housekeeping",
        },
    ]

    gaps: List[str] = []

    # Gap: auto_v2 output vs config compiler watch list.
    if config_compiler and config_compiler.enabled:
        watches_auto_v2 = any("/usr/config/auto_v2" in p.replace("\\", "/") for p in config_compiler.watch_dirs)
        if not watches_auto_v2:
            gaps.append(
                "Run Config Compiler does not watch usr/config/auto_v2 in orchestrator rule, even though auto_profile_tables_vtx writes there."
            )

    # Gap: server matrix config basename mismatch.
    auto_matrix_exists = file_exists_rel("usr/config/auto_v2/auto_server_inventory_matrix_vtx.yaml")
    run_matrix_exists = file_exists_rel("usr/config/run/server_inventory_matrix_vtx.yaml")
    default_matrix_exists = file_exists_rel("usr/config/default/server_inventory_matrix_vtx.yaml")
    if (auto_matrix_exists or True) and not (run_matrix_exists or default_matrix_exists):
        gaps.append(
            "server_inventory_matrix_vtx.py defaults to server_inventory_matrix_vtx.yaml, but auto_profile_tables_vtx generates auto_server_inventory_matrix_vtx.yaml."
        )

    # Gap: data source analysis runtime conf missing.
    if not file_exists_rel("usr/config/run/data_source_analysis_reporting.conf"):
        gaps.append("data_source_analysis.py expects usr/config/run/data_source_analysis_reporting.conf, but it is missing.")

    # Gap: intake watch directory missing.
    if intake and intake.enabled and not dir_exists_rel("var/customerdata"):
        gaps.append("Run Intake Processor watches var/customerdata, but that directory is missing in this installer tree.")

    # Gap: missing auto_v2 dir entirely.
    if not dir_exists_rel("usr/config/auto_v2"):
        gaps.append("usr/config/auto_v2 directory is missing, which blocks auto-generated config outputs.")

    # Out-of-box vs intervention summaries.
    out_of_box_ready = []
    intervention_required = []

    for item in pre_config_tree:
        req_ok = all(file_exists_rel(r) or dir_exists_rel(r) for r in item["required"])
        job_ok = item["job_present"] and item["job_enabled"]
        if req_ok and job_ok:
            out_of_box_ready.append(item["node"])
        else:
            intervention_required.append(item["node"])

    for item in post_config_tree:
        intervention_required.append(item["node"])

    return {
        "pre_config_tree": pre_config_tree,
        "post_config_tree": post_config_tree,
        "ops_vital": ops_vital,
        "ops_non_vital": ops_non_vital,
        "gaps": gaps,
        "out_of_box_ready": out_of_box_ready,
        "intervention_required": intervention_required,
        "post_config_markers": post_config_markers,
    }


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def tree_to_text(title: str, rows: List[Dict[str, Any]]) -> str:
    lines = [title]
    for i, r in enumerate(rows):
        lead = "- " if i == 0 else "- "
        req = r.get("required") or []
        req_ok = all(file_exists_rel(x) or dir_exists_rel(x) for x in req)
        job_ok = bool(r.get("job_present") and r.get("job_enabled"))
        lines.append(f"{lead}{r['node']} [{status_icon(req_ok and job_ok)}]")
        lines.append(f"  trigger/job: {r.get('job', '<none>')}")
        if req:
            lines.append(f"  requires: {', '.join(req)}")
        prod = r.get("produces") or []
        if prod:
            lines.append(f"  produces: {', '.join(prod)}")
        if r.get("intervention_required"):
            lines.append("  requires human configuration: yes")
    return "\n".join(lines)


def render_html(report: Dict[str, Any]) -> str:
    core = report["core"]
    streams = report["streams"]
    jobs = report["jobs"]

    core_lines = [
        f"- orchestrator service file present: {core['orchestrator_service_exists']}",
        f"- orchestrator script present: {core['orchestrator_script_exists']}",
        f"- run orchestrator.yaml present: {core['run_orchestrator_yaml_exists']}",
        f"- default/run orchestrator yaml equal: {core['default_run_orchestrator_same']}",
        f"- config compiler job present+enabled: {core['config_compiler_job_present']} / {core['config_compiler_job_enabled']}",
        f"- config compiler watches default/custom/auto/auto_v2: {core['config_compiler_watch_default']} / {core['config_compiler_watch_custom']} / {core['config_compiler_watch_auto']} / {core['config_compiler_watch_auto_v2']}",
        f"- intake job present+enabled: {core['intake_job_present']} / {core['intake_job_enabled']}",
        f"- intake watches customerdata and dir exists: {core['intake_watch_customerdata']} / {core['customerdata_dir_exists']}",
    ]

    pre_txt = tree_to_text("Pre-Configuration Stream", streams["pre_config_tree"])
    post_txt = tree_to_text("Post-Configuration Stream", streams["post_config_tree"])

    vital_lines = []
    for r in streams["ops_vital"]:
        req_ok = all(file_exists_rel(x) or dir_exists_rel(x) for x in r.get("required", []))
        job_ok = bool(r.get("job_present") and r.get("job_enabled"))
        vital_lines.append(f"- {r['node']} [{status_icon(req_ok and job_ok)}]")
        vital_lines.append(f"  job: {r['job']}")
        vital_lines.append(f"  requires: {', '.join(r.get('required', []))}")
        vital_lines.append(f"  produces: {', '.join(r.get('produces', []))}")
    vital_txt = "\n".join(vital_lines)

    non_vital_lines = [f"- {r['node']} ({r.get('kind','')}) -> {r['job']}" for r in streams["ops_non_vital"]]
    non_vital_txt = "\n".join(non_vital_lines)

    gaps_html = "".join(f"<li>{esc(g)}</li>" for g in streams["gaps"]) or "<li>No gaps detected.</li>"

    out_of_box = "".join(f"<li>{esc(x)}</li>" for x in streams["out_of_box_ready"]) or "<li>None</li>"
    intervention = "".join(f"<li>{esc(x)}</li>" for x in streams["intervention_required"]) or "<li>None</li>"

    job_rows = []
    for j in jobs:
        rec = j
        status = "enabled" if rec["enabled"] else "disabled"
        if not rec["script_exists"]:
            status = "broken"
        job_rows.append(
            "<tr>"
            f"<td>{esc(rec['kind'])}</td>"
            f"<td>{esc(rec['job_id'])}</td>"
            f"<td>{esc(status)}</td>"
            f"<td>{esc(rec['role'])}</td>"
            f"<td>{esc(rec['rule'] or rec['schedule'] or '-')}</td>"
            f"<td>{esc(rec['script'])}</td>"
            "</tr>"
        )

    core_text = esc("\n".join(core_lines))
    pre_text = esc(pre_txt)
    post_text = esc(post_txt)
    vital_text = esc("Vital\n" + vital_txt)
    non_vital_text = esc("Non-vital\n" + non_vital_txt)

    return f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8' />
  <meta name='viewport' content='width=device-width, initial-scale=1' />
  <title>VTX Process Flow Map</title>
  <style>
    :root {{
      --bg:#0e1222;
      --panel:#151a30;
      --text:#eaeef8;
      --muted:#9aa3b2;
      --accent:#c5d0ff;
      --line:rgba(255,255,255,0.08);
      --red:#f97373;
      --amber:#fb923c;
      --green:#4ade80;
    }}
    body {{ font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin:0; padding:24px; background:var(--bg); color:var(--text); }}
    .header {{ font-size:28px; font-weight:800; margin-bottom:4px; }}
    .muted {{ color:var(--muted); font-size:12px; }}
    .tile {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px; margin-top:12px; }}
    .title {{ color:var(--accent); font-size:13px; font-weight:700; margin-bottom:8px; }}
    pre {{ margin:0; white-space:pre-wrap; background:rgba(255,255,255,0.02); border:1px solid var(--line); border-radius:10px; padding:10px; font-size:12px; line-height:1.4; }}
    ul {{ margin:0; padding-left:18px; }}
    li {{ margin:4px 0; }}
    .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
    .table-wrap {{ border:1px solid var(--line); border-radius:10px; overflow:auto; max-height:60vh; }}
    table {{ width:100%; border-collapse:collapse; font-size:12px; }}
    th, td {{ padding:8px; border-bottom:1px solid var(--line); text-align:left; }}
    th {{ position:sticky; top:0; background:var(--panel); color:var(--accent); }}
    @media (max-width: 900px) {{ .grid {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
  <div class='header'>VTX Process Flow Map</div>
  <div class='muted'>Dependency trees + order-of-operations (diagnostic, no auto-fixes)</div>

  <div class='tile'>
    <div class='title'>Core Boot Chain</div>
    <pre>{core_text}</pre>
  </div>

  <div class='tile'>
    <div class='title'>Order Of Operations Trees</div>
    <div class='grid'>
      <pre>{pre_text}</pre>
      <pre>{post_text}</pre>
    </div>
  </div>

  <div class='tile'>
    <div class='title'>Operations Streams</div>
    <div class='grid'>
      <pre>{vital_text}</pre>
      <pre>{non_vital_text}</pre>
    </div>
  </div>

  <div class='tile'>
    <div class='title'>Out-Of-Box vs Human Intervention</div>
    <div class='grid'>
      <div>
        <div class='muted'>Out-of-box expected to run now</div>
        <ul>{out_of_box}</ul>
      </div>
      <div>
        <div class='muted'>Requires human config/setup</div>
        <ul>{intervention}</ul>
      </div>
    </div>
  </div>

  <div class='tile'>
    <div class='title'>Intended vs Actual Gaps</div>
    <ul>{gaps_html}</ul>
  </div>

  <div class='tile'>
    <div class='title'>Configured Jobs Inventory</div>
    <div class='table-wrap'>
      <table>
        <thead><tr><th>Kind</th><th>Job</th><th>Status</th><th>Role</th><th>Rule/Schedule</th><th>Script</th></tr></thead>
        <tbody>{''.join(job_rows)}</tbody>
      </table>
    </div>
  </div>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="VTX dependency flow mapper.")
    ap.add_argument("--default-yaml", default=str(VTX_ROOT / "usr" / "config" / "default" / "orchestrator.yaml"))
    ap.add_argument("--run-yaml", default=str(VTX_ROOT / "usr" / "config" / "run" / "orchestrator.yaml"))
    ap.add_argument("--html-out", default=str(VTX_ROOT / "var" / "analysis" / "vtx_process_flow_map.html"))
    ap.add_argument("--json-out", default=str(VTX_ROOT / "var" / "analysis" / "vtx_process_flow_map.json"))
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    default_yaml = Path(args.default_yaml).expanduser().resolve()
    run_yaml = Path(args.run_yaml).expanduser().resolve()
    html_out = Path(args.html_out).expanduser().resolve()
    json_out = Path(args.json_out).expanduser().resolve() if args.json_out else None

    if not default_yaml.exists():
        raise SystemExit(f"Missing default YAML: {default_yaml}")
    if not run_yaml.exists():
        raise SystemExit(f"Missing run YAML: {run_yaml}")

    run_cfg = load_yaml(run_yaml)
    jobs = parse_jobs(run_cfg)
    core = check_core(jobs)
    streams = build_streams(jobs)

    payload = {
        "summary": {
            "cron_jobs": len([j for j in jobs if j.kind == "cron"]),
            "backbone_jobs": len([j for j in jobs if j.kind == "backbone"]),
            "enabled_jobs": len([j for j in jobs if j.enabled]),
            "default_run_same": sha256(default_yaml) == sha256(run_yaml),
        },
        "core": core,
        "streams": streams,
        "jobs": [j.__dict__ for j in jobs],
    }

    html_out.parent.mkdir(parents=True, exist_ok=True)
    html_out.write_text(render_html(payload), encoding="utf-8")

    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"[OK] wrote HTML: {html_out}")
    if json_out:
        print(f"[OK] wrote JSON: {json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
