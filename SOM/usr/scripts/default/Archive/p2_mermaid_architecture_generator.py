#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate Mermaid architecture diagrams from the VTX architectural drawing view.
"""

from __future__ import annotations

import argparse
import csv
import ipaddress
import logging
import os
import re
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


def resolve_vtx_root() -> Path:
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


VTX_ROOT = resolve_vtx_root()

try:
    import yaml  # type: ignore
except Exception as exc:  # pragma: no cover
    venv_python = VTX_ROOT / "venv" / "bin" / "python"
    if venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
        os.execv(str(venv_python), [str(venv_python), __file__, *sys.argv[1:]])
    raise SystemExit("Missing dependency: pyyaml. Install it in the VTX venv.") from exc


DEFAULT_CONFIG_PATH = VTX_ROOT / "usr" / "config" / "run" / "p2_mermaid_architecture_generator.yaml"


def vtx_path(path_str: str | Path, *, must_exist: bool = False) -> Path:
    if isinstance(path_str, Path):
        path = path_str
    else:
        text = os.path.expandvars(os.path.expanduser(str(path_str).strip()))
        text = text.replace("VTX_ROOT/", str(VTX_ROOT) + "/")
        text = text.replace("BTDM_ROOT/", str(VTX_ROOT) + "/")
        path = Path(text)
    if not path.is_absolute():
        path = (VTX_ROOT / path).resolve()
    if must_exist and not path.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")
    return path


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


LOG = get_logger("mermaid_architecture_generator")


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


def text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def split_multi(value: Any, delimiter: str) -> List[str]:
    raw = text(value)
    if not raw:
        return []
    return [part.strip() for part in raw.split(delimiter)]


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


def mermaid_id(value: str, prefix: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_]+", "_", text(value))
    clean = re.sub(r"_+", "_", clean).strip("_")
    if not clean:
        clean = "item"
    if clean[0].isdigit():
        clean = f"{prefix}_{clean}"
    return f"{prefix}_{clean}"


def escape_label(value: str) -> str:
    escaped = text(value)
    line_break_token = "__MERMAID_LINE_BREAK__"
    bold_open_token = "__MERMAID_BOLD_OPEN__"
    bold_close_token = "__MERMAID_BOLD_CLOSE__"
    div_tokens: Dict[str, str] = {}

    def preserve_div(match: re.Match[str]) -> str:
        token = f"__MERMAID_DIV_{len(div_tokens)}__"
        div_tokens[token] = match.group(0)
        return token

    escaped = re.sub(r"</?div[^>]*>", preserve_div, escaped)
    escaped = escaped.replace("<br/>", line_break_token)
    escaped = escaped.replace("<b>", bold_open_token)
    escaped = escaped.replace("</b>", bold_close_token)
    escaped = escaped.replace("&", "&amp;")
    escaped = escaped.replace("<", "&lt;")
    escaped = escaped.replace(">", "&gt;")
    escaped = escaped.replace('"', "&quot;")
    escaped = escaped.replace("[", "&#91;").replace("]", "&#93;")
    escaped = escaped.replace("|", "&#124;")
    escaped = escaped.replace("\n", line_break_token)
    escaped = escaped.replace(line_break_token, "<br/>")
    escaped = escaped.replace(bold_open_token, "<b>")
    escaped = escaped.replace(bold_close_token, "</b>")
    for token, div_tag in div_tokens.items():
        escaped = escaped.replace(token, div_tag)
    return escaped


def mermaid_label(value: str) -> str:
    return escape_label(value).replace("`", "'")


def classify_ip(ip_value: str) -> Tuple[str, str]:
    cleaned = text(ip_value)
    if not cleaned:
        return "unknown", "missing IP address"
    try:
        parsed = ipaddress.ip_address(cleaned)
    except ValueError:
        return "unknown", f"invalid IP address: {cleaned}"
    return ("dmz", "") if parsed.is_global else ("internal", "")


def classify_layer(hostname: str, configured_layer: str, patterns: Sequence[str], database_label: str) -> str:
    layer = text(configured_layer)
    if layer:
        return layer
    hostname_lower = hostname.lower()
    for pattern in patterns:
        if text(pattern).lower() in hostname_lower:
            return database_label
    return ""


def read_rows(path: Path) -> List[Dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def group_rows(rows: Sequence[Dict[str, str]], app_id_col: str, env_col: str) -> OrderedDict[Tuple[str, str], List[Dict[str, str]]]:
    grouped: OrderedDict[Tuple[str, str], List[Dict[str, str]]] = OrderedDict()
    for row_number, row in enumerate(rows, start=2):
        app_id = text(row.get(app_id_col))
        environment = text(row.get(env_col))
        if not app_id or not environment:
            LOG.warning(
                "Skipping row %s because Application_ID or Environment is blank: Application_ID=%s Environment=%s",
                row_number,
                app_id,
                environment,
            )
            continue
        grouped.setdefault((app_id, environment), []).append(row)
    return grouped


def first_value(rows: Sequence[Dict[str, str]], column: str) -> str:
    for row in rows:
        value = text(row.get(column))
        if value:
            return value
    return ""


def expand_servers(
    *,
    rows: Sequence[Dict[str, str]],
    app_id: str,
    environment: str,
    columns: Dict[str, str],
    delimiter: str,
    database_patterns: Sequence[str],
    database_label: str,
) -> List[Dict[str, str]]:
    hostname_col = columns.get("hostname", "Hostname")
    ip_col = columns.get("ip_address", "IP_Address")
    layer_col = columns.get("application_layer", "Application_Layer")
    label_col = columns.get("display_label", "Display_Label")
    notes_col = columns.get("notes", "Notes")

    servers: List[Dict[str, str]] = []
    seen: OrderedDict[Tuple[str, str], None] = OrderedDict()
    for row in rows:
        hostnames = split_multi(row.get(hostname_col), delimiter)
        ips = split_multi(row.get(ip_col), delimiter)
        if len(hostnames) != len(ips):
            LOG.warning(
                "Hostname/IP count mismatch for Application_ID=%s Environment=%s; hostnames=%s ips=%s",
                app_id,
                environment,
                len(hostnames),
                len(ips),
            )
        max_count = max(len(hostnames), len(ips))
        layers = split_multi(row.get(layer_col), delimiter)
        display_labels = split_multi(row.get(label_col), delimiter)
        notes = split_multi(row.get(notes_col), delimiter)
        for index in range(max_count):
            hostname = hostnames[index] if index < len(hostnames) else ""
            ip_addr = ips[index] if index < len(ips) else ""
            if not hostname and not ip_addr:
                continue
            layer = layers[index] if index < len(layers) else text(row.get(layer_col))
            display_label = display_labels[index] if index < len(display_labels) else text(row.get(label_col))
            note = notes[index] if index < len(notes) else text(row.get(notes_col))
            region, warning = classify_ip(ip_addr)
            if warning:
                LOG.warning(
                    "%s for Application_ID=%s Environment=%s Hostname=%s",
                    warning,
                    app_id,
                    environment,
                    hostname,
                )
            server = {
                "hostname": hostname,
                "ip_address": ip_addr,
                "display_label": display_label,
                "application_layer": classify_layer(hostname, layer, database_patterns, database_label),
                "notes": note,
                "region": region,
            }
            key = (server["hostname"].lower(), server["ip_address"])
            if key not in seen:
                seen[key] = None
                servers.append(server)
    return servers


def append_node(lines: List[str], node_id: str, label: str, class_name: str) -> None:
    lines.append(f'    {node_id}["{escape_label(label)}"]:::{class_name}')


def render_node(node_id: str, label: str, class_name: str, shape_type: str, indent: str = "      ") -> str:
    escaped = mermaid_label(label)
    if shape_type == "database":
        return f'{indent}{node_id}[("{escaped}")]:::{class_name}'
    return f'{indent}{node_id}["{escaped}"]:::{class_name}'


def append_subgraph(
    lines: List[str],
    graph_id: str,
    title: str,
    nodes: Sequence[Tuple[str, str, str, str]],
    nodes_per_row: int = 3,
    indent: str = "      ",
) -> bool:
    if not nodes:
        return False
    row_size = max(1, nodes_per_row)
    lines.append(f'{indent}subgraph {graph_id}["{mermaid_label(title)}"]')
    lines.append(f"{indent}  direction TB")
    for row_index, start in enumerate(range(0, len(nodes), row_size), start=1):
        row_nodes = nodes[start:start + row_size]
        row_id = f"{graph_id}_row_{row_index}"
        lines.append(f'{indent}  subgraph {row_id}[" "]')
        lines.append(f"{indent}    direction LR")
        for node_id, label, class_name, shape_type in row_nodes:
            lines.append(render_node(node_id, label, class_name, shape_type, indent=f"{indent}    "))
        lines.append(f"{indent}  end")
        lines.append(f"{indent}  style {row_id} fill:transparent,stroke:transparent,color:transparent;")
    lines.append(f"{indent}end")
    return True


def server_label(server: Dict[str, str], icon: str = "") -> str:
    name = server["display_label"] or server["hostname"] or "Unnamed server"
    if icon:
        name = f"{icon} {name}"
    parts = [name]
    if server["hostname"] and server["display_label"] and server["display_label"] != server["hostname"]:
        parts.append(server["hostname"])
    if server["ip_address"]:
        parts.append(server["ip_address"])
    if server["application_layer"]:
        parts.append(server["application_layer"])
    return "<br/>".join(parts)


def is_database_server(server: Dict[str, str], database_label: str) -> bool:
    layer = server.get("application_layer", "").strip().lower()
    label = database_label.strip().lower()
    return bool(layer and (layer == label or "database" in layer))


def add_layer_connectors(lines: List[str], graph_ids: Sequence[str], indent: str = "    ") -> None:
    for left, right in zip(graph_ids, graph_ids[1:]):
        lines.append(f"{indent}{left} --> {right}")


def build_mermaid(instance: Dict[str, Any], job: Dict[str, Any]) -> str:
    labels = job.get("labels") or {}
    colors = job.get("colors") or {}
    layout = job.get("layout") or {}
    direction = text(job.get("mermaid_direction")) or "TB"
    database_label = text(labels.get("database_server")) or "Database Server"
    lines: List[str] = [f"flowchart {direction}"]

    style_defs = {
        "warning": ("#FDECEC", "#C62828", "#8A1F1F"),
        "server": (colors.get("server_fill", "#E8F2FF"), colors.get("server_stroke", "#2F6FAD"), colors.get("server_text", "#1F2937")),
        "databaseServer": (colors.get("database_server_fill", "#FFF3E6"), colors.get("database_server_stroke", "#E07A1F"), colors.get("database_server_text", "#1F2937")),
        "database": (colors.get("database_fill", "#F6F3FF"), colors.get("database_stroke", "#6D5BD0"), colors.get("database_text", "#1F2937")),
    }
    for name, (fill, stroke, color) in style_defs.items():
        lines.append(f"    classDef {name} fill:{fill},stroke:{stroke},color:{color};")

    architecture_columns = int(layout.get("architecture_columns") or 3)

    warnings = instance["migration_exclusions"]
    if warnings:
        warning_title = f"<b>{labels.get('warnings', 'Migration Planning Exclusions')}</b>"
        lines.append(f'    subgraph warnings["{mermaid_label(warning_title)}"]')
        for index, warning in enumerate(warnings, start=1):
            warning_label = f"{labels.get('warning_prefix', '❌')} {warning}"
            lines.append(f'      warning_{index}["{mermaid_label(warning_label)}"]:::warning')
        lines.append("    end")

    present_graphs: List[str] = []
    non_database_servers = [
        server for server in instance["servers"] if not is_database_server(server, database_label)
    ]
    database_servers = [
        server for server in instance["servers"] if is_database_server(server, database_label)
    ]

    region_layers = [
        ("dmz", f"<b>{labels.get('dmz', 'DMZ')}</b>"),
        ("internal", f"<b>{labels.get('internal_networks', 'Internal Networks')}</b>"),
        ("unknown", f"<b>{labels.get('unknown', 'Unknown')}</b>"),
    ]
    for region, title in region_layers:
        nodes: List[Tuple[str, str, str, str]] = []
        region_servers = [server for server in non_database_servers if server["region"] == region]
        for index, server in enumerate(region_servers, start=1):
            node_id = mermaid_id(f"{region}_{index}_{server['hostname']}_{server['ip_address']}", "srv")
            icon = str(labels.get("web_icon", "🌐")) if region == "dmz" else ""
            nodes.append((node_id, server_label(server, icon), "server", "rect"))
        graph_id = f"{region}_region"
        if append_subgraph(lines, graph_id, str(title), nodes, architecture_columns, indent="    "):
            present_graphs.append(graph_id)

    database_server_nodes = []
    for index, server in enumerate(database_servers, start=1):
        node_id = mermaid_id(f"database_server_{index}_{server['hostname']}_{server['ip_address']}", "dbsrv")
        database_server_nodes.append(
            (node_id, server_label(server, str(labels.get("sql_icon", "🗄"))), "databaseServer", "rect")
        )
    if append_subgraph(
        lines,
        "database_servers",
        f"<b>{labels.get('database_servers', 'Database Servers')}</b>",
        database_server_nodes,
        architecture_columns,
        indent="    ",
    ):
        present_graphs.append("database_servers")

    db_nodes = []
    for index, database in enumerate(instance["databases"], start=1):
        db_nodes.append((mermaid_id(f"db_{index}_{database}", "db"), database, "database", "database"))
    if append_subgraph(
        lines,
        "databases",
        f"<b>{labels.get('databases', 'Databases')}</b>",
        db_nodes,
        architecture_columns,
        indent="    ",
    ):
        present_graphs.append("databases")

    add_layer_connectors(lines, present_graphs)

    return "\n".join(lines) + "\n"


def build_application_information_markdown(instance: Dict[str, Any], job: Dict[str, Any]) -> str:
    labels = job.get("labels") or {}
    heading = text(labels.get("information")) or "Application Information"
    info_items = [
        ("Application ID", instance["application_id"]),
        ("Application Name", instance["application_name"]),
        ("Application Acronym", instance["application_acronym"]),
        ("Department", instance["department"]),
        ("Environment", instance["environment"]),
    ]
    lines = [f"## {heading}", ""]
    for label, value in info_items:
        clean_value = text(value)
        if not clean_value:
            continue
        lines.extend([f"**{label}:**", clean_value, ""])
    return "\n".join(lines).rstrip() + "\n\n"


def build_markdown(instance: Dict[str, Any], job: Dict[str, Any]) -> str:
    title = (
        f"{instance['application_id']} "
        f"{instance['application_acronym']} "
        f"{instance['environment']} Architecture"
    ).strip()
    return (
        f"# {title}\n\n"
        f"{build_application_information_markdown(instance, job)}"
        "```mermaid\n"
        f"{build_mermaid(instance, job)}"
        "```\n"
    )


def build_instance(app_id: str, environment: str, rows: Sequence[Dict[str, str]], job: Dict[str, Any]) -> Dict[str, Any]:
    columns = job.get("columns") or {}
    delimiter = text(job.get("delimiter")) or ";"
    application_col = columns.get("application", "Application_Name")
    acronym_col = columns.get("application_acronym", "Application_Acronym")
    department_col = columns.get("department", "Department_Name")
    database_col = columns.get("database_name", "Database_Name")
    exclusions_col = columns.get("migration_exclusions", "tag_migration_panning_exclusions")
    database_patterns = [text(p) for p in job.get("database_hostname_patterns", []) if text(p)]
    database_label = text((job.get("labels") or {}).get("database_server")) or "Database Server"

    return {
        "application_id": app_id,
        "application_name": first_value(rows, application_col),
        "application_acronym": first_value(rows, acronym_col),
        "department": first_value(rows, department_col),
        "environment": environment,
        "databases": unique(db for row in rows for db in split_multi(row.get(database_col), delimiter)),
        "migration_exclusions": unique(item for row in rows for item in split_multi(row.get(exclusions_col), delimiter)),
        "servers": expand_servers(
            rows=rows,
            app_id=app_id,
            environment=environment,
            columns=columns,
            delimiter=delimiter,
            database_patterns=database_patterns,
            database_label=database_label,
        ),
    }


def output_filename(instance: Dict[str, Any]) -> str:
    return (
        f"{filename_part(instance['application_id'])}_"
        f"{filename_part(instance['application_acronym'])}_"
        f"{filename_part(instance['environment'])}.md"
    )


def validate_columns(rows: Sequence[Dict[str, str]], columns: Dict[str, str]) -> None:
    if not rows:
        raise ValueError("Input CSV contains no data rows.")
    header = set(rows[0].keys())
    required = {
        "application_id": columns.get("application_id", "Application_ID"),
        "environment": columns.get("environment", "Environment"),
    }
    missing = [name for name in required.values() if name not in header]
    if missing:
        raise ValueError(f"Required input columns are missing: {', '.join(missing)}")


def clean_output_dir(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(output_dir.glob("*.md")):
        path.unlink()


def run_job(job: Dict[str, Any]) -> List[Path]:
    input_path = vtx_path(str(job.get("input") or ""), must_exist=True)
    output_dir = vtx_path(str(job.get("output_dir") or ""))
    rows = read_rows(input_path)
    columns = job.get("columns") or {}
    validate_columns(rows, columns)
    app_id_col = columns.get("application_id", "Application_ID")
    env_col = columns.get("environment", "Environment")

    grouped = group_rows(rows, app_id_col, env_col)
    if job.get("clean_output_dir", True) is not False:
        clean_output_dir(output_dir)
    else:
        output_dir.mkdir(parents=True, exist_ok=True)

    written: List[Path] = []
    seen_names: Dict[str, int] = {}
    for app_id, environment in grouped:
        instance = build_instance(app_id, environment, grouped[(app_id, environment)], job)
        name = output_filename(instance)
        if name in seen_names:
            seen_names[name] += 1
            stem = Path(name).stem
            name = f"{stem}_{seen_names[name]}.md"
        else:
            seen_names[name] = 1
        output_path = output_dir / name
        output_path.write_text(build_markdown(instance, job), encoding="utf-8")
        written.append(output_path)

    LOG.info("Generated %s Mermaid Markdown file(s) in %s", len(written), output_dir)
    return written


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate Mermaid architecture Markdown diagrams.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to YAML config. Defaults to usr/config/run.")
    parser.add_argument("--job", help="Optional job id to run. Runs all enabled jobs when omitted.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    doc = load_yaml(vtx_path(args.config, must_exist=True))
    jobs = config_jobs(doc)
    if not jobs:
        raise ValueError("No jobs found in Mermaid architecture generator config.")

    selected = []
    for job in jobs:
        if job.get("enabled", True) is False:
            continue
        if args.job and text(job.get("id")) != args.job:
            continue
        selected.append(job)
    if args.job and not selected:
        raise ValueError(f"Job not found or disabled: {args.job}")

    for job in selected:
        run_job(job)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        LOG.exception("mermaid_architecture_generator failed: %s", exc)
        raise SystemExit(1)
