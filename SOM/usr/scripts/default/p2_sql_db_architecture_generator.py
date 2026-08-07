#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate Mermaid Markdown database dependency diagrams from VTX SQL database data.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


def resolve_vtx_root() -> Path:
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


VTX_ROOT = resolve_vtx_root()
DEFAULT_CONFIG_PATH = VTX_ROOT / "usr" / "config" / "run" / "p2_sql_db_architecture_generator.yaml"

try:
    import yaml  # type: ignore
except Exception as exc:  # pragma: no cover
    venv_python = VTX_ROOT / "venv" / "bin" / "python"
    if venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
        os.execv(str(venv_python), [str(venv_python), __file__, *sys.argv[1:]])
    raise SystemExit("Missing dependency: pyyaml. Install it in the VTX venv.") from exc


def get_logger(component: str) -> logging.Logger:
    lib_dir = VTX_ROOT / "usr" / "lib"
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))
    try:
        import vtx_logging  # type: ignore

        return vtx_logging.get_logger(component=component)  # type: ignore
    except Exception:
        logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
        return logging.getLogger(component)


LOG = get_logger("sql_db_architecture_generator")


@dataclass(frozen=True)
class Relationship:
    hostname: str
    instance: str
    database: str
    department: str
    application: str


def vtx_path(path_value: str | Path, *, must_exist: bool = False) -> Path:
    if isinstance(path_value, Path):
        path = path_value
    else:
        cleaned = os.path.expandvars(os.path.expanduser(str(path_value).strip()))
        cleaned = cleaned.replace("\\", os.sep)
        cleaned = cleaned.replace("VTX_ROOT/", str(VTX_ROOT) + "/")
        cleaned = cleaned.replace("BTDM_ROOT/", str(VTX_ROOT) + "/")
        path = Path(cleaned)
    if not path.is_absolute():
        path = (VTX_ROOT / path).resolve()
    if must_exist and not path.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")
    return path


def text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def split_multi(value: Any, delimiter: str) -> List[str]:
    raw = text(value)
    if not raw:
        return []
    return [part.strip() for part in raw.split(delimiter) if part.strip()]


def unique(values: Iterable[str]) -> List[str]:
    seen: OrderedDict[str, None] = OrderedDict()
    for value in values:
        cleaned = text(value)
        if cleaned and cleaned not in seen:
            seen[cleaned] = None
    return list(seen.keys())


def filename_part(value: str, fallback: str = "UNKNOWN") -> str:
    clean = text(value) or fallback
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1F]+', "_", clean)
    clean = re.sub(r"\s+", "_", clean)
    clean = re.sub(r"_+", "_", clean).strip("._ ")
    return clean or fallback


def mermaid_id(*parts: str) -> str:
    raw = "_".join(text(part) for part in parts if text(part))
    clean = re.sub(r"[^A-Za-z0-9_]+", "_", raw)
    clean = re.sub(r"_+", "_", clean).strip("_")
    if not clean:
        clean = "node"
    if clean[0].isdigit():
        clean = f"n_{clean}"
    return clean


def mermaid_label(value: str) -> str:
    escaped = text(value)
    line_break_token = "__MERMAID_LINE_BREAK__"
    escaped = escaped.replace("<br/>", line_break_token)
    escaped = escaped.replace("&", "&amp;")
    escaped = escaped.replace("<", "&lt;")
    escaped = escaped.replace(">", "&gt;")
    escaped = escaped.replace('"', "&quot;")
    escaped = escaped.replace("[", "&#91;").replace("]", "&#93;")
    escaped = escaped.replace("|", "&#124;")
    escaped = escaped.replace("\n", line_break_token)
    escaped = escaped.replace(line_break_token, "<br/>")
    escaped = escaped.replace("`", "'")
    return escaped


def load_yaml(path: Path) -> Dict[str, Any]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"Config root must be a mapping: {path}")
    return doc


def config_jobs(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    jobs = (((doc.get("config") or {}).get("payload") or {}).get("jobs"))
    if not isinstance(jobs, list):
        return []
    return [job for job in jobs if isinstance(job, dict)]


def read_rows(input_path: Path, columns: Dict[str, str]) -> List[Dict[str, str]]:
    with input_path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = set(reader.fieldnames or [])
        required_keys = ["hostname", "instance", "database", "department", "application"]
        missing_mappings = [key for key in required_keys if not text(columns.get(key))]
        if missing_mappings:
            raise ValueError(f"Missing required column mappings: {missing_mappings}")
        missing_columns = sorted(columns[key] for key in required_keys if columns[key] not in header)
        if missing_columns:
            raise ValueError(f"Input file is missing required columns: {missing_columns}")
        return [dict(row) for row in reader]


def aligned_department_app_pairs(departments: Sequence[str], applications: Sequence[str]) -> List[Tuple[str, str]]:
    if not departments and not applications:
        return [("", "")]
    if departments and not applications:
        return [(department, "") for department in departments]
    if applications and not departments:
        return [("Unknown Department", application) for application in applications]
    if len(departments) == len(applications):
        return list(zip(departments, applications))
    if len(departments) == 1:
        return [(departments[0], application) for application in applications]
    if len(applications) == 1:
        return [(department, applications[0]) for department in departments]

    pairs: List[Tuple[str, str]] = []
    pair_count = max(len(departments), len(applications))
    for index in range(pair_count):
        department = departments[index] if index < len(departments) else "Unknown Department"
        application = applications[index] if index < len(applications) else ""
        pairs.append((department, application))
    return pairs


def build_relationships(rows: Sequence[Dict[str, str]], columns: Dict[str, str], delimiter: str) -> OrderedDict[str, List[Relationship]]:
    relationships: OrderedDict[str, List[Relationship]] = OrderedDict()
    seen: set[Relationship] = set()
    skipped_rows = 0

    hostname_col = columns["hostname"]
    instance_col = columns["instance"]
    database_col = columns["database"]
    department_col = columns["department"]
    application_col = columns["application"]

    for row in rows:
        hostnames = unique(split_multi(row.get(hostname_col), delimiter))
        if not hostnames:
            skipped_rows += 1
            continue

        instances = unique(split_multi(row.get(instance_col), delimiter)) or ["Unknown Instance"]
        databases = unique(split_multi(row.get(database_col), delimiter)) or ["Unknown Database"]
        department_app_pairs = aligned_department_app_pairs(
            unique(split_multi(row.get(department_col), delimiter)),
            unique(split_multi(row.get(application_col), delimiter)),
        )

        for hostname in hostnames:
            host_relationships = relationships.setdefault(hostname, [])
            for instance in instances:
                for database in databases:
                    for department, application in department_app_pairs:
                        rel = Relationship(
                            hostname=hostname,
                            instance=instance,
                            database=database,
                            department=text(department) or "Unknown Department",
                            application=text(application),
                        )
                        if rel in seen:
                            continue
                        seen.add(rel)
                        host_relationships.append(rel)

    if skipped_rows:
        LOG.info("Skipped %s source rows without Hostname", skipped_rows)
    return relationships


def summary_counts(relationships: Sequence[Relationship]) -> Dict[str, int]:
    return {
        "instances": len({rel.instance for rel in relationships if rel.instance and rel.instance != "Unknown Instance"}),
        "databases": len({rel.database for rel in relationships if rel.database and rel.database != "Unknown Database"}),
        "departments": len({rel.department for rel in relationships if rel.department and rel.department != "Unknown Department"}),
    }


def class_def(name: str, style: Dict[str, Any], fallback: Dict[str, str]) -> str:
    fill = text(style.get("fill")) or fallback["fill"]
    stroke = text(style.get("stroke")) or fallback["stroke"]
    color = text(style.get("text")) or fallback["text"]
    stroke_width = text(style.get("stroke_width")) or fallback.get("stroke_width", "1.5px")
    return f"classDef {name} fill:{fill},stroke:{stroke},stroke-width:{stroke_width},color:{color};"


def add_node(lines: List[str], node_id: str, label: str, class_name: str, shape: str = "rect") -> None:
    safe_label = mermaid_label(label)
    if shape == "database":
        lines.append(f'    {node_id}[("{safe_label}")]:::{class_name}')
    else:
        lines.append(f'    {node_id}["{safe_label}"]:::{class_name}')


def build_mermaid(hostname: str, relationships: Sequence[Relationship], job: Dict[str, Any]) -> str:
    mermaid = job.get("mermaid") or {}
    labels = job.get("labels") or {}
    styles = mermaid.get("styles") or {}
    direction = text(mermaid.get("direction")) or "LR"
    node_spacing = int(mermaid.get("node_spacing") or 45)
    rank_spacing = int(mermaid.get("rank_spacing") or 55)

    lines: List[str] = [
        "```mermaid",
        "%%{init:{",
        '  "themeVariables":{',
        f'    "fontSize":"{text(mermaid.get("font_size")) or "12px"}"',
        "  },",
        '  "flowchart":{',
        f'    "nodeSpacing":{node_spacing},',
        f'    "rankSpacing":{rank_spacing},',
        '    "diagramPadding":10,',
        '    "htmlLabels":true',
        "  }",
        "}}%%",
        "",
        f"flowchart {direction}",
        "",
        class_def("hostname", styles.get("hostname") or {}, {"fill": "#FFF7D6", "stroke": "#A66A00", "text": "#222", "stroke_width": "2px"}),
        class_def("instance", styles.get("instance") or {}, {"fill": "#E8F2FF", "stroke": "#2F6FAD", "text": "#1F2937"}),
        class_def("database", styles.get("database") or {}, {"fill": "#F6F3FF", "stroke": "#6D5BD0", "text": "#1F2937"}),
        class_def("application", styles.get("application") or {}, {"fill": "#FFFFFF", "stroke": "#666", "text": "#222", "stroke_width": "1.2px"}),
        class_def("department", styles.get("department") or {}, {"fill": "#F4F4F5", "stroke": "#71717A", "text": "#27272A"}),
        "",
    ]

    host_id = mermaid_id("host", hostname)
    add_node(lines, host_id, f"🖥️ {hostname}", "hostname")

    emitted_nodes = {host_id}
    emitted_edges: OrderedDict[Tuple[str, str], None] = OrderedDict()
    application_departments: Dict[str, List[str]] = {}
    for rel in relationships:
        if not rel.application:
            continue
        departments = application_departments.setdefault(rel.application, [])
        if rel.department and rel.department not in departments:
            departments.append(rel.department)

    for rel in relationships:
        instance_id = mermaid_id("instance", rel.database, rel.instance)
        if instance_id not in emitted_nodes:
            add_node(lines, instance_id, f"🗄️ {labels.get('instance', 'Instance')}: {rel.instance}", "instance", shape="database")
            emitted_nodes.add(instance_id)
        emitted_edges.setdefault((host_id, instance_id), None)

        database_id = mermaid_id("database", rel.database)
        if database_id not in emitted_nodes:
            add_node(lines, database_id, f"💾 {labels.get('database', 'Database')}: {rel.database}", "database", shape="database")
            emitted_nodes.add(database_id)
        emitted_edges.setdefault((instance_id, database_id), None)

        if rel.application:
            application_id = mermaid_id("application", rel.application)
            if application_id not in emitted_nodes:
                departments = application_departments.get(rel.application, [])
                department_label = "; ".join(departments) if departments else "Unknown Department"
                application_label = f"🏛️ {department_label}<br/>📦 {rel.application}"
                add_node(lines, application_id, application_label, "application")
                emitted_nodes.add(application_id)
            emitted_edges.setdefault((database_id, application_id), None)

    lines.append("")
    for left, right in emitted_edges.keys():
        lines.append(f"    {left} --> {right}")
    lines.append("```")
    return "\n".join(lines) + "\n"


def build_markdown(hostname: str, relationships: Sequence[Relationship], job: Dict[str, Any]) -> str:
    labels = job.get("labels") or {}
    counts = summary_counts(relationships)
    page_title = text(labels.get("page_title")) or "Database Dependency Architecture"
    summary_title = text(labels.get("summary")) or "Summary"
    topology_title = text(labels.get("topology")) or "Database Topology"

    return "\n".join([
        f"# {mermaid_label(hostname)} {mermaid_label(page_title)}",
        "",
        f"## {mermaid_label(summary_title)}",
        f"**{mermaid_label(labels.get('database_server', 'Database Server Name'))}:** {mermaid_label(hostname)}  ",
        f"**{mermaid_label(labels.get('instance_count', 'Instance Count'))}:** {counts['instances']}  ",
        f"**{mermaid_label(labels.get('database_count', 'Database Count'))}:** {counts['databases']}  ",
        f"**{mermaid_label(labels.get('department_count', 'Department Count'))}:** {counts['departments']}",
        "",
        "---",
        "",
        f"## {mermaid_label(topology_title)}",
        "",
        build_mermaid(hostname, relationships, job).rstrip(),
        "",
    ])


def clean_output_dir(output_dir: Path, enabled: bool) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    if not enabled:
        return
    for path in output_dir.glob("*.md"):
        path.unlink()


def write_diagrams(relationships: OrderedDict[str, List[Relationship]], output_dir: Path, job: Dict[str, Any]) -> int:
    clean_output_dir(output_dir, bool(job.get("clean_output_dir", True)))
    generated = 0
    for hostname in sorted(relationships.keys(), key=str.casefold):
        host_relationships = relationships[hostname]
        if not host_relationships:
            continue
        output_path = output_dir / f"{filename_part(hostname)}.md"
        output_path.write_text(build_markdown(hostname, host_relationships, job), encoding="utf-8")
        generated += 1
    return generated


def run_job(job: Dict[str, Any]) -> int:
    job_id = text(job.get("id")) or "sql_db_architecture"
    input_path = vtx_path(job.get("input") or job.get("inputs", [""])[0], must_exist=True)
    output_dir = vtx_path(job.get("output_dir") or job.get("outputs", [""])[0])
    delimiter = text(job.get("delimiter")) or ";"
    columns = job.get("columns") if isinstance(job.get("columns"), dict) else {}

    LOG.info("Running job=%s", job_id)
    LOG.info("Input=%s", input_path)
    LOG.info("Output directory=%s", output_dir)
    rows = read_rows(input_path, columns)
    relationships = build_relationships(rows, columns, delimiter)
    generated = write_diagrams(relationships, output_dir, job)
    LOG.info("Generated %s Markdown files in %s", generated, output_dir)
    print(f"Generated {generated} Markdown files in {output_dir}")
    return generated


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate SQL DB architecture Mermaid Markdown files.")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Path to YAML config (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument("--job", default=None, help="Run a specific job id")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        LOG.info("VTX_ROOT=%s", VTX_ROOT)
        config_path = vtx_path(args.config, must_exist=True)
        LOG.info("Config=%s", config_path)
        doc = load_yaml(config_path)
        jobs = config_jobs(doc)
        if args.job:
            jobs = [job for job in jobs if job.get("id") == args.job]
            if not jobs:
                raise ValueError(f"Job id not found: {args.job}")
        enabled_jobs = [job for job in jobs if job.get("enabled", True)]
        if not enabled_jobs:
            raise ValueError("No enabled jobs found")
        for job in enabled_jobs:
            run_job(job)
        return 0
    except Exception:
        LOG.exception("SQL DB architecture generation failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
