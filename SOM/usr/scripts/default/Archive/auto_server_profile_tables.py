#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
auto_server_profile_tables.py
-----------------------------
Profile direct-child CSVs under var/tables and generate only the
server profile YAML used by server_raw_table_builder.py.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from auto_profile_tables_vtx import (
    CANONICAL_SERVER_ATTRIBUTES,
    VTX_ROOT,
    _best_column_for_attribute,
    extract_jobs,
    job_id,
    load_criteria,
    load_yaml,
    logger,
    profile_table,
    resolve_path,
    select_jobs,
    to_vtx_rel,
    iter_direct_csvs,
)

try:
    import yaml
except Exception as e:
    raise SystemExit("Missing dependency: pyyaml. Install it in the VTX venv.") from e


DEFAULT_CONFIG_PATH = VTX_ROOT / "usr" / "config" / "default" / "auto_server_profile_tables.yaml"


class Options(argparse.Namespace):
    config_path: Path
    job: Optional[str]
    dry_run: bool


def parse_args(argv: Optional[Sequence[str]] = None) -> Options:
    ap = argparse.ArgumentParser(description="Auto profile var/tables and generate only the server profile YAML.")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help=f"Config path (default: {DEFAULT_CONFIG_PATH})")
    ap.add_argument("--job", default=None, help="Run only the selected job id")
    ap.add_argument("--dry-run", action="store_true", help="Do not write outputs")
    args = ap.parse_args(argv, namespace=Options())
    args.config_path = resolve_path(args.config, must_exist=True)
    return args


def _dump_yaml(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False).rstrip()


def _write_yaml(path: Path, doc: Dict[str, Any], *, dry_run: bool) -> None:
    rendered = _dump_yaml(doc) + "\n"
    if dry_run:
        logger.info("[dry-run] Would write %s", path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    logger.info("Wrote %s", path)


def _overall_server_rank(profile: Any) -> tuple:
    return (
        -int(profile.matches.get("server", 0)),
        -float(profile.scores.get("server", 0.0)),
        profile.stem.lower(),
    )


def _mapped_attributes(profile: Any, criteria: List[Any]) -> Dict[str, Optional[str]]:
    mapping: Dict[str, Optional[str]] = {}
    for attr in CANONICAL_SERVER_ATTRIBUTES:
        col = _best_server_column_for_attribute(profile, criteria, attr)
        mapping[attr] = col or None
    return mapping


def _norm_col(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip()


SERVER_ATTR_RULES: Dict[str, Dict[str, List[str]]] = {
    "Hostname": {
        "exact": ["servername", "server", "hostname", "host", "dns name"],
        "prefer": ["server name", "servername", "hostname", "dns name", "server", "host"],
        "avoid": [" os", "os ", "typeos", "host os", "server type", "sdk", "cluster", "cpu", "memory", "storage", "ip"],
    },
    "IP Address": {
        "exact": ["ipaddress", "ip address", "primary ip address", "sourceip"],
        "prefer": ["primary ip address", "ip address", "ipaddress", "sourceip"],
        "avoid": ["ilo", "sdk", "version", "bits", "mask", "subnet"],
    },
    "OS": {
        "exact": ["typeos", "os according to the vmware tools", "os according to the configuration file", "os", "operating system"],
        "prefer": ["typeos", "operating system", "vmware tools", "configuration file", "os version", "os"],
        "avoid": ["hostname", "server name", "servername", "server", "dns name", "host os", "ip", "environment", "cpu", "memory", "storage"],
    },
    "Environment": {
        "exact": ["environment", "env"],
        "prefer": ["environment", "env"],
        "avoid": ["server", "host", "ip", "os"],
    },
    "CPU": {
        "exact": ["cpus", "cpu", "cpu count"],
        "prefer": ["cpus", "cpu", "processor", "core"],
        "avoid": ["readiness", "ready", "environment", "memory", "storage", "host", "server"],
    },
    "Memory": {
        "exact": ["memory", "vmmemory", "ram"],
        "prefer": ["memory", "vmmemory", "ram"],
        "avoid": ["active memory", "swap", "storage", "cpu", "host", "server"],
    },
    "Storage": {
        "exact": ["vmstorage", "storage", "disk", "capacity"],
        "prefer": ["vmstorage", "storage", "disk", "capacity"],
        "avoid": ["memory", "cpu", "host", "server"],
    },
}


def _server_name_score(column: str, attr: str) -> float:
    norm = _norm_col(column)
    rules = SERVER_ATTR_RULES.get(attr, {})
    exact = rules.get("exact", [])
    prefer = rules.get("prefer", [])
    avoid = rules.get("avoid", [])
    score = 0.0
    if norm in exact:
        score += 100.0
    for token in prefer:
        if token and token in norm:
            score += 10.0
    for token in avoid:
        token_norm = _norm_col(token)
        if token_norm and token_norm in norm:
            score -= 15.0
    return score


def _best_server_column_for_attribute(profile: Any, criteria: List[Any], attribute: str) -> Optional[str]:
    best_score = float("-inf")
    best_col: Optional[str] = None
    for col in profile.df.columns:
        score = _server_name_score(col, attribute)
        score += float(profile.column_uniqueness.get(col, 0.0))
        if attribute in {"Hostname", "IP Address"}:
            score += float(profile.column_scores.get(col, 0.0)) * 5.0
        else:
            score += float(profile.column_scores.get(col, 0.0)) * 2.0
        if score > best_score:
            best_score = score
            best_col = col

    fallback = _best_column_for_attribute(profile, criteria, attribute)
    if best_col is not None and best_score > 0:
        return best_col
    return fallback


def build_server_profile_doc(server_profiles: List[Any], criteria: List[Any], payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not server_profiles:
        return None

    outputs_cfg = payload.get("outputs") or {}
    raw_output = str(outputs_cfg.get("server_raw_parquet_output") or "var/masterdata/raw_server_table_auto.parquet")
    presence_output = str(outputs_cfg.get("hostname_presence_output") or "var/analysis/presence_hostname_matrix_auto.csv")
    presence_html_output = str(outputs_cfg.get("hostname_presence_html_output") or "var/analysis/presence_hostname_matrix_auto.html")
    rankings_output = str(outputs_cfg.get("inventory_rankings_output") or "var/analysis/server_inventory_field_rankings.csv")

    ranked_profiles = sorted(server_profiles, key=_overall_server_rank)
    sources: List[Dict[str, Any]] = []
    source_mappings: Dict[str, Dict[str, Optional[str]]] = {}
    for idx, prof in enumerate(ranked_profiles, start=1):
        mapping = _mapped_attributes(prof, criteria)
        source_mappings[prof.stem] = mapping
        sources.append(
            {
                "id": prof.stem,
                "enabled": True,
                "priority": idx,
                "path": to_vtx_rel(prof.path),
                "format": "csv",
                "map": mapping,
                "filters": [],
            }
        )

    primary_source = sources[0]["id"] if sources else ""
    trust_ranking: Dict[str, List[str]] = {}
    ordered_source_ids = [str(src["id"]) for src in sources]
    for attr in CANONICAL_SERVER_ATTRIBUTES:
        with_mapping = [sid for sid in ordered_source_ids if source_mappings.get(sid, {}).get(attr)]
        without_mapping = [sid for sid in ordered_source_ids if sid not in with_mapping]
        trust_ranking[attr] = with_mapping + without_mapping

    inventory_id = "Server_Profile_Tables_Autodiscovered"
    job_id = "server_raw_table_auto"
    inventory_fields = list(CANONICAL_SERVER_ATTRIBUTES)
    inventory_table = {
        "path": raw_output,
        "include_presence_flags": False,
        "include_value_provenance": False,
        "include_ranked_values": False,
        "ranked_values_delimiter": ";",
        "fields": list(inventory_fields),
    }

    doc = {
        "_vtx": {
            "schema": "v1",
            "kind": "report",
            "id": "server_profile_tables",
            "title": "Server Profile Tables",
            "description": "Auto-generated server profile YAML for raw server coalescing and downstream matrix analysis.",
            "owner": "default",
            "version": "1.0.0",
            "tags": ["server", "masterdata", "auto"],
            "consumers": [
                "usr/scripts/default/server_raw_table_builder.py",
                "usr/scripts/analysis/server_inventory_matrix_vtx.py",
            ],
        },
        "config": {
            "io": {
                "inputs": [src["path"] for src in sources],
                "outputs": [raw_output],
            },
            "run": {
                "enabled": True,
                "role": "any",
                "log_level": "INFO",
                "dry_run": False,
                "fail_fast": False,
                "cwd": "",
            },
            "payload": {
                "globals": {
                    "vtx_root": "",
                    "default_normalization": {
                        "hostname": {"casefold": True, "fqdn_to_shortname": True, "strip": True},
                        "ip_address": {"strip": True},
                    },
                },
                "inventories": [
                    {
                        "id": inventory_id,
                        "enabled": True,
                        "primary_source": primary_source,
                        "attributes": list(CANONICAL_SERVER_ATTRIBUTES),
                        "match": {
                            "condition": "any",
                            "keys": [
                                {"name": "Hostname", "type": "hostname"},
                                {"name": "IP Address", "type": "ip_address"},
                            ],
                        },
                        "trust_ranking": trust_ranking,
                        "sources": sources,
                    }
                ],
                "jobs": [
                    {
                        "id": job_id,
                        "enabled": True,
                        "inventory_id": inventory_id,
                        "outputs": {
                            "inventory_table": dict(inventory_table),
                            "inventory_rankings": {
                                **dict(inventory_table),
                                "path": rankings_output,
                                "fields": list(inventory_fields),
                            },
                            "presence_matrix_reports": [
                                {
                                    "id": "hostname_presence",
                                    "attribute": "Hostname",
                                    "normalize_as": "hostname",
                                    "source_of_truth": primary_source,
                                    "display_attributes": ["IP Address"],
                                    "path": presence_output,
                                    "html_path": presence_html_output,
                                }
                            ],
                        },
                    }
                ],
            },
        },
    }
    return doc


def run_auto_profile(payload: Dict[str, Any], *, dry_run: bool) -> int:
    dict_cfg = payload.get("dictionary") or {}
    dict_path = resolve_path(dict_cfg.get("path") or "usr/config/default/data_source_analysis.yaml", must_exist=True)
    criteria = load_criteria(dict_path)

    csv_paths = iter_direct_csvs()
    if not csv_paths:
        logger.warning("No direct-child CSVs found under var/tables")
        return 0

    profiles = [profile_table(p, criteria, payload) for p in csv_paths]
    for prof in profiles:
        logger.info(
            "Table classified: %s => %s (matches=%s, score=%s, confidence=%.3f)",
            prof.stem,
            prof.classification,
            prof.matches,
            {k: round(v, 3) for k, v in prof.scores.items()},
            prof.confidence,
        )

    server_profiles = [p for p in profiles if p.classification == "server"]
    outputs_cfg = payload.get("outputs") or {}
    output_path = resolve_path(outputs_cfg.get("server_profile_yaml") or "usr/config/auto_v2/server_profile_tables.yaml")
    doc = build_server_profile_doc(server_profiles, criteria, payload)
    if not doc:
        logger.warning("No server profiles detected; skipping server profile output")
        return 0
    _write_yaml(output_path, doc, dry_run=dry_run)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    opt = parse_args(argv)
    logger.info("VTX_ROOT=%s", VTX_ROOT)
    logger.info("Config=%s", opt.config_path)

    cfg = load_yaml(opt.config_path)
    payload = cfg.get("config", {}).get("payload", {}) if isinstance(cfg.get("config"), dict) else {}
    if not isinstance(payload, dict):
        raise ValueError("config.payload must be a mapping/dict")

    jobs = extract_jobs(cfg)
    if not jobs:
        logger.error("No jobs defined in config.payload.jobs")
        return 1

    selected, err = select_jobs(jobs, opt.job)
    if err:
        logger.error(err)
        print(err)
        return 2

    for job_cfg in selected:
        jid = job_id(job_cfg) or "job"
        try:
            logger.info("Starting job: %s", jid)
            print(f"[auto_server_profile_tables] Running job '{jid}'")
            run_auto_profile(payload, dry_run=opt.dry_run)
        except Exception as exc:
            logger.exception("Job failed: %s (%s)", jid, exc)
            print(f"[auto_server_profile_tables] ERROR in job '{jid}': {exc}")
            return 3

    print("[auto_server_profile_tables] Complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
