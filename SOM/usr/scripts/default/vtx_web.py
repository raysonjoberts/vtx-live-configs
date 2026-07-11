#!/usr/bin/env python3
"""
VTX Web Frontend
----------------
- Simple login with local config-backed credentials
- Forces password change on first login (must_change flag)
- Basic landing page
- /reports to list existing HTML reports
"""

import os
import sys
import hashlib
import configparser
import csv
import copy
import io
import json
import re
import fnmatch
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
import yaml
import pandas as pd

# Detect VTX_ROOT
VTX_ROOT = os.environ.get("VTX_ROOT")
if not VTX_ROOT:
    VTX_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

VTX_ROOT_PATH = Path(VTX_ROOT)

VAR_DIR = VTX_ROOT_PATH / "var"
REPORT_DIR = VAR_DIR / "reporting"
ANALYSIS_DIR = VAR_DIR / "analysis"
STATIC_DIR = VTX_ROOT_PATH / "usr" / "web" / "static"
AUTH_CONF = VTX_ROOT_PATH / "usr" / "config" / "run" / "web_auth.conf"
CONFIG_AUTO_V2_DIR = VTX_ROOT_PATH / "usr" / "config" / "auto_v2"
CONFIG_DEFAULT_DIR = VTX_ROOT_PATH / "usr" / "config" / "default"
CONFIG_CUSTOM_DIR = VTX_ROOT_PATH / "usr" / "config" / "custom"
CONFIG_RUN_DIR = VTX_ROOT_PATH / "usr" / "config" / "run"
AWS_CONTEXT_PRICING_DIR = VAR_DIR / "masterdata" / "aws_customer_contextural_pricing"

AWS_EXPORT_EXCLUDE_COLUMNS = {
    "service_code",
    "record_type",
    "usage_type",
    "operation",
    "unit",
    "currency",
    "rate_code",
    "retrieved_at_utc",
    "source_file",
    "source_url",
    "usage_family",
    "usage_family_raw",
    "fit_cpu_delta",
    "fit_mem_delta",
    "fit_score",
    "family_general_preference_rank",
    "generation_rank",
    "candidates_total_before_shortlist",
    "reserved_purchase_assumption",
    "reserved_lease_assumption",
}

# Default creds (used only if config file missing)
DEFAULT_USERNAME = os.environ.get("VTX_WEB_USER", "vtxadmin")
DEFAULT_PASSWORD = os.environ.get("VTX_WEB_PASS", "changeme")
SESSION_SECRET_KEY = os.environ.get("VTX_WEB_SECRET_KEY", "change_this_in_prod")

# Optional logging via btdm_logging if available
try:
    sys.path.insert(0, str(VTX_ROOT_PATH / "usr" / "lib"))
    from btdm_logging import get_logger  # type: ignore
    LOGGER = get_logger("vtx_web")
except Exception:
    LOGGER = None


def log(level: str, msg: str) -> None:
    if LOGGER:
        getattr(LOGGER, level.lower(), LOGGER.info)(msg)
    else:
        print(f"[VTX_WEB] {level.upper()}: {msg}")


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def ensure_auth_config() -> None:
    """Create web_auth.conf with default creds if it doesn't exist."""
    if AUTH_CONF.exists():
        return

    log("info", f"Auth config not found, creating default at {AUTH_CONF}")
    AUTH_CONF.parent.mkdir(parents=True, exist_ok=True)

    config = configparser.ConfigParser()
    config["user"] = {
        "username": DEFAULT_USERNAME,
        "password_hash": hash_password(DEFAULT_PASSWORD),
        "must_change": "true",  # force password change on first login
    }

    tmp = AUTH_CONF.with_suffix(".tmp")
    with tmp.open("w") as f:
        config.write(f)
    tmp.replace(AUTH_CONF)


def load_auth_config() -> dict:
    """Load username, password_hash, must_change from config."""
    ensure_auth_config()

    config = configparser.ConfigParser()
    config.read(AUTH_CONF)

    if "user" not in config:
        raise RuntimeError("web_auth.conf missing [user] section")

    username = config.get("user", "username", fallback=DEFAULT_USERNAME)
    password_hash = config.get(
        "user",
        "password_hash",
        fallback=hash_password(DEFAULT_PASSWORD),
    )
    must_change = config.getboolean("user", "must_change", fallback=True)

    return {
        "username": username,
        "password_hash": password_hash,
        "must_change": must_change,
    }


def save_auth_config(username: str, password_hash: str, must_change: bool) -> None:
    """Write updated credentials to config."""
    config = configparser.ConfigParser()
    config["user"] = {
        "username": username,
        "password_hash": password_hash,
        "must_change": "true" if must_change else "false",
    }

    AUTH_CONF.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_CONF.with_suffix(".tmp")
    with tmp.open("w") as f:
        config.write(f)
    tmp.replace(AUTH_CONF)
    log("info", f"Updated auth config at {AUTH_CONF}")


# Ensure config exists at import time
ensure_auth_config()

app = FastAPI()

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET_KEY,
    session_cookie="vtx_session",
)


def get_current_user(request: Request) -> Optional[str]:
    return request.session.get("user")


def require_login(request: Request) -> Optional[RedirectResponse]:
    user = get_current_user(request)
    if not user:
        request.session["next_url"] = str(request.url.path)
        return RedirectResponse(url="/login", status_code=303)
    return None


def get_server_inventory_source_yaml() -> Path:
    candidates = [
        CONFIG_AUTO_V2_DIR / "server_inventory_matrix_vtx.yaml",
        CONFIG_AUTO_V2_DIR / "server_inventory_matrix.yaml",
        CONFIG_AUTO_V2_DIR / "auto_server_inventory_matrix_vtx.yaml",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def get_server_inventory_custom_yaml() -> Path:
    return CONFIG_CUSTOM_DIR / "server_inventory_matrix_vtx.yaml"


def get_table_aggregator_source_yaml() -> Path:
    candidate = CONFIG_AUTO_V2_DIR / "table_aggregator_vtx.yaml"
    return candidate


def get_table_aggregator_custom_yaml() -> Path:
    return CONFIG_CUSTOM_DIR / "table_aggregator_vtx.yaml"


def load_yaml_dict(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def save_yaml_dict(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(
            data,
            handle,
            sort_keys=False,
            allow_unicode=False,
            default_flow_style=False,
        )
    tmp.replace(path)


CONFIG_TITLE_OVERRIDES = {
    "tag_engine": "Tagging Engine",
}

TITLE_WORD_OVERRIDES = {
    "aws": "AWS",
    "vtx": "VTX",
    "api": "API",
    "ui": "UI",
}

TAG_ENGINE_SCRIPT_PATH = VTX_ROOT_PATH / "usr" / "scripts" / "default" / "tag_engine.py"
MASTERDATA_DIR = VAR_DIR / "masterdata"
METADATA_DIR = VAR_DIR / "metadata"
CONFIG_PAGE_BLACKLIST_NAME = "config_page_blacklist.yaml"


def load_yaml_document(path: Path, allow_commented_fallback: bool = False) -> Any:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError:
        loaded = None
    if loaded is None and allow_commented_fallback:
        loaded = load_commented_yaml_document(text)
    return loaded if loaded is not None else {}


def load_commented_yaml_document(text: str) -> Any:
    uncommented_lines: List[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith("#"):
            continue
        indent = line[: len(line) - len(stripped)]
        body = stripped[1:]
        if body.startswith(" "):
            body = body[1:]
        uncommented_lines.append(f"{indent}{body}")
    if not uncommented_lines:
        return {}
    try:
        return yaml.safe_load("\n".join(uncommented_lines)) or {}
    except yaml.YAMLError:
        return {}


def load_config_page_blacklist() -> Dict[str, List[str]]:
    candidate_paths = [
        CONFIG_RUN_DIR / CONFIG_PAGE_BLACKLIST_NAME,
        CONFIG_DEFAULT_DIR / CONFIG_PAGE_BLACKLIST_NAME,
    ]
    doc: Any = {}
    for path in candidate_paths:
        if path.exists():
            doc = load_yaml_document(path, allow_commented_fallback=True)
            break
    rules = _get_at_path(doc, ("config", "page_blacklist"))
    if not isinstance(rules, dict):
        return {"filenames": [], "patterns": []}

    filenames = [str(value).strip() for value in (rules.get("filenames") or []) if str(value).strip()]
    patterns = [str(value).strip() for value in (rules.get("patterns") or []) if str(value).strip()]
    return {"filenames": filenames, "patterns": patterns}


def is_config_page_blacklisted(filename: str) -> bool:
    blacklist = load_config_page_blacklist()
    if filename in blacklist["filenames"]:
        return True
    return any(fnmatch.fnmatch(filename, pattern) for pattern in blacklist["patterns"])


def normalize_config_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", (value or "").strip().lower().replace("-", "_")).strip("_")


def prettify_config_title(stem: str, doc: Any) -> str:
    if isinstance(doc, dict):
        metadata = doc.get("_vtx")
        if isinstance(metadata, dict):
            title = str(metadata.get("title") or "").strip()
            if title:
                return title
    if stem in CONFIG_TITLE_OVERRIDES:
        return CONFIG_TITLE_OVERRIDES[stem]
    cleaned = stem.replace(".", "_")
    parts = [part for part in re.split(r"[_\-]+", cleaned) if part]
    pretty_parts = [TITLE_WORD_OVERRIDES.get(part.lower(), part.capitalize()) for part in parts]
    return " ".join(pretty_parts) or "Configuration"


def _list_is_job_like(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    return not value or isinstance(value[0], dict)


def _find_job_path(data: Any, path: tuple[str, ...] = ()) -> Optional[tuple[str, ...]]:
    if isinstance(data, dict):
        for preferred_key in ("jobs", "cron_jobs", "inventories", "tasks", "pipelines"):
            value = data.get(preferred_key)
            if _list_is_job_like(value):
                return path + (preferred_key,)
        for key, value in data.items():
            found = _find_job_path(value, path + (key,))
            if found:
                return found
    return None


def _get_at_path(data: Any, path: tuple[str, ...]) -> Any:
    current = data
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _ensure_path(data: Dict[str, Any], path: tuple[str, ...]) -> List[Dict[str, Any]]:
    current: Dict[str, Any] = data
    for part in path[:-1]:
        next_value = current.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            current[part] = next_value
        current = next_value
    terminal = current.get(path[-1])
    if not isinstance(terminal, list):
        terminal = []
        current[path[-1]] = terminal
    return terminal


def _blank_template_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _blank_template_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return []
    if isinstance(value, bool):
        return False
    if value is None:
        return ""
    return ""


def _extract_template_job(default_doc: Any, jobs_path: tuple[str, ...]) -> Dict[str, Any]:
    jobs = _get_at_path(default_doc, jobs_path)
    if isinstance(jobs, list):
        for job in jobs:
            if isinstance(job, dict):
                return _blank_template_value(job)
    return {"id": ""}


def _infer_required_fields(template_job: Dict[str, Any]) -> List[str]:
    required_fields: List[str] = []
    if "id" in template_job:
        required_fields.append("id")
    return required_fields


def _extract_jobs(doc: Any, jobs_path: tuple[str, ...]) -> List[Dict[str, Any]]:
    jobs = _get_at_path(doc, jobs_path)
    if not isinstance(jobs, list):
        return []
    return [copy.deepcopy(job) for job in jobs if isinstance(job, dict)]


def _skeleton_for_path(default_doc: Any, jobs_path: tuple[str, ...]) -> Dict[str, Any]:
    base: Dict[str, Any] = {}
    if isinstance(default_doc, dict):
        metadata = default_doc.get("_vtx")
        if isinstance(metadata, dict):
            base["_vtx"] = copy.deepcopy(metadata)
    _ensure_path(base, jobs_path)
    return base


def get_config_catalog_items() -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not CONFIG_DEFAULT_DIR.exists():
        return items
    for path in sorted(CONFIG_DEFAULT_DIR.glob("*.yaml")):
        if is_config_page_blacklisted(path.name):
            continue
        default_doc = load_yaml_document(path, allow_commented_fallback=True)
        title = prettify_config_title(path.stem, default_doc)
        description = ""
        if isinstance(default_doc, dict):
            metadata = default_doc.get("_vtx")
            if isinstance(metadata, dict):
                description = str(metadata.get("description") or "").strip()
        if not description:
            description = f"Create and edit jobs for {title}. Changes are saved to usr/config/custom."
        items.append(
            {
                "slug": normalize_config_slug(path.stem),
                "filename": path.name,
                "title": title,
                "description": description,
                "href": f"/config/{normalize_config_slug(path.stem)}",
                "custom_exists": (CONFIG_CUSTOM_DIR / path.name).exists(),
            }
        )
    return items


def get_config_spec(config_slug: str) -> Dict[str, Any]:
    normalized_slug = normalize_config_slug(config_slug)
    for item in get_config_catalog_items():
        if item["slug"] == normalized_slug:
            default_path = CONFIG_DEFAULT_DIR / item["filename"]
            custom_path = CONFIG_CUSTOM_DIR / item["filename"]
            default_doc = load_yaml_document(default_path, allow_commented_fallback=True)
            custom_doc = load_yaml_document(custom_path) if custom_path.exists() else {}
            jobs_path = _find_job_path(custom_doc) or _find_job_path(default_doc) or ("config", "payload", "jobs")
            template_job = _extract_template_job(default_doc, jobs_path)
            jobs = _extract_jobs(custom_doc, jobs_path) if custom_path.exists() else []
            return {
                "slug": item["slug"],
                "filename": item["filename"],
                "title": item["title"],
                "description": item["description"],
                "default_path": default_path,
                "custom_path": custom_path,
                "default_doc": default_doc,
                "custom_doc": custom_doc,
                "jobs_path": jobs_path,
                "template_job": template_job,
                "required_fields": _infer_required_fields(template_job),
                "jobs": jobs,
                "loaded_from": custom_path if custom_path.exists() else default_path,
            }
    raise HTTPException(status_code=404, detail="Configuration not found")


def list_masterdata_tables() -> List[str]:
    if not MASTERDATA_DIR.exists():
        return []
    items: List[str] = []
    for path in sorted(MASTERDATA_DIR.iterdir()):
        if path.is_file():
            items.append(f"var/masterdata/{path.name}")
    return items


def list_metadata_csv_tables() -> List[str]:
    if not METADATA_DIR.exists():
        return []
    items: List[str] = []
    for path in sorted(METADATA_DIR.glob("*.csv")):
        if path.is_file():
            items.append(f"var/metadata/{path.name}")
    return items


def read_table_headers_for_ui(path_value: str) -> List[str]:
    resolved = resolve_vtx_path(path_value)
    if not resolved.exists() or not resolved.is_file():
        return []
    try:
        if resolved.suffix.lower() == ".parquet":
            return [str(col) for col in pd.read_parquet(resolved).columns]
        with resolved.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            return next(reader, [])
    except Exception as exc:
        log("warning", f"Failed reading headers from {resolved}: {exc}")
        return []


def get_tag_engine_operator_choices() -> List[str]:
    operators = {
        "==",
        "!=",
        ">",
        ">=",
        "<",
        "<=",
        "contains",
        "notcontains",
        "in_range",
        "isnull",
        "isnotnull",
        "is_null",
        "is_not_null",
        "=",
        "eq",
        "ne",
    }
    if TAG_ENGINE_SCRIPT_PATH.exists():
        text = TAG_ENGINE_SCRIPT_PATH.read_text(encoding="utf-8")
        for match in re.findall(r'operator in \{([^}]+)\}', text):
            operators.update(re.findall(r'"([^"]+)"', match))
        for match in re.findall(r'op in \{([^}]+)\}', text):
            operators.update(re.findall(r'"([^"]+)"', match))
        operators.update(re.findall(r'operator == "([^"]+)"', text))
        operators.update(re.findall(r'op == "([^"]+)"', text))
    preferred = [
        "==",
        "!=",
        ">=",
        "<=",
        ">",
        "<",
        "contains",
        "notcontains",
        "isnull",
        "isnotnull",
        "in_range",
        "=",
        "eq",
        "ne",
        "is_null",
        "is_not_null",
    ]
    return [item for item in preferred if item in operators]


def get_tag_engine_aggregate_choices() -> List[str]:
    aggregates = {"count", "unique_count", "sum", "avg", "min", "max"}
    if TAG_ENGINE_SCRIPT_PATH.exists():
        text = TAG_ENGINE_SCRIPT_PATH.read_text(encoding="utf-8")
        aggregates.update(re.findall(r'agg == "([^"]+)"', text))
    preferred = ["count", "unique_count", "sum", "avg", "min", "max"]
    return [item for item in preferred if item in aggregates]


def get_tag_engine_entity_types() -> List[str]:
    values = set()
    for path in [
        CONFIG_DEFAULT_DIR / "tag_engine.yaml",
        CONFIG_CUSTOM_DIR / "tag_engine.yaml",
        CONFIG_AUTO_V2_DIR / "tag_engine.yaml",
        VTX_ROOT_PATH / "usr" / "config" / "run" / "tag_engine.yaml",
    ]:
        if not path.exists():
            continue
        doc = load_yaml_document(path, allow_commented_fallback=True)
        jobs_path = _find_job_path(doc) or ("config", "payload", "jobs")
        jobs = _extract_jobs(doc, jobs_path)
        for job in jobs:
            entity_type = str(job.get("entity_type") or "").strip()
            if entity_type:
                values.add(entity_type)
    return sorted(values)


def build_tag_engine_template_job(base_template: Dict[str, Any]) -> Dict[str, Any]:
    template = copy.deepcopy(base_template) if isinstance(base_template, dict) else {}
    template.setdefault("id", "")
    template["enabled"] = False
    template.setdefault("inputs", [])
    template.setdefault("outputs", [])
    template.setdefault("anchor_field", "")
    template.setdefault("entity_type", "")
    template.setdefault("key_delimiter", ";")
    template.setdefault("tags", [])
    template.setdefault("derived_tags", [])
    return template


def default_tag_engine_tag() -> Dict[str, Any]:
    return {
        "id": "",
        "tag_key": "",
        "source": "rule",
        "key_fields": [],
        "condition_logic": "all",
        "first_match_only": False,
        "rules": [],
    }


def default_tag_engine_rule() -> Dict[str, Any]:
    return {
        "id": "",
        "tag_value": "",
        "conditions": [],
    }


def default_tag_engine_condition() -> Dict[str, Any]:
    return {
        "field": "",
        "operator": "==",
        "value": "",
        "agg": "",
    }


def normalize_metadata_output_path(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("var/metadata/"):
        name = raw[len("var/metadata/") :]
    else:
        name = Path(raw).name
    stem = Path(name).stem or "output"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "output"
    return f"var/metadata/{safe}.csv"


def sanitize_tag_engine_jobs(jobs: Any) -> List[Dict[str, Any]]:
    if not isinstance(jobs, list):
        raise HTTPException(status_code=400, detail="Payload must include a jobs list")

    valid_inputs = set(list_masterdata_tables())
    job_ids = set()
    sanitized_jobs: List[Dict[str, Any]] = []

    for job_index, raw_job in enumerate(jobs):
        if not isinstance(raw_job, dict):
            raise HTTPException(status_code=400, detail=f"Job at index {job_index} must be an object")
        job = copy.deepcopy(raw_job)
        job_id = str(job.get("id") or "").strip()
        if not job_id:
            raise HTTPException(status_code=400, detail="Each job requires an id")
        if job_id in job_ids:
            raise HTTPException(status_code=400, detail=f"Duplicate job id: {job_id}")
        job_ids.add(job_id)

        inputs = job.get("inputs") or []
        if not isinstance(inputs, list) or len(inputs) != 1:
            raise HTTPException(status_code=400, detail=f"Job '{job_id}' must contain exactly one input")
        input_path = str(inputs[0] or "").strip()
        if input_path not in valid_inputs:
            raise HTTPException(status_code=400, detail=f"Job '{job_id}' input must be a table in var/masterdata")

        outputs = job.get("outputs") or []
        if not isinstance(outputs, list) or len(outputs) != 1:
            raise HTTPException(status_code=400, detail=f"Job '{job_id}' must contain exactly one output")
        output_path = normalize_metadata_output_path(outputs[0])
        if not output_path.startswith("var/metadata/") or not output_path.endswith(".csv"):
            raise HTTPException(status_code=400, detail=f"Job '{job_id}' output must resolve under var/metadata as a .csv")

        anchor_field = str(job.get("anchor_field") or "").strip()
        if not anchor_field:
            raise HTTPException(status_code=400, detail=f"Job '{job_id}' missing anchor_field")

        entity_type = str(job.get("entity_type") or "").strip()
        if not entity_type:
            raise HTTPException(status_code=400, detail=f"Job '{job_id}' missing entity_type")

        key_delimiter = str(job.get("key_delimiter") or ";")
        tags = job.get("tags") or []
        if not isinstance(tags, list):
            raise HTTPException(status_code=400, detail=f"Job '{job_id}' tags must be a list")

        tag_ids = set()
        sanitized_tags: List[Dict[str, Any]] = []
        for tag_index, raw_tag in enumerate(tags):
            if not isinstance(raw_tag, dict):
                raise HTTPException(status_code=400, detail=f"Tag {tag_index + 1} in job '{job_id}' must be an object")
            tag = copy.deepcopy(raw_tag)
            tag_id = str(tag.get("id") or "").strip()
            if not tag_id:
                raise HTTPException(status_code=400, detail=f"Each tag in job '{job_id}' requires an id")
            if tag_id in tag_ids:
                raise HTTPException(status_code=400, detail=f"Duplicate tag id in job '{job_id}': {tag_id}")
            tag_ids.add(tag_id)

            tag_key = str(tag.get("tag_key") or "").strip()
            if not tag_key:
                raise HTTPException(status_code=400, detail=f"Tag '{tag_id}' requires tag_key")

            key_fields = tag.get("key_fields") or []
            if not isinstance(key_fields, list):
                raise HTTPException(status_code=400, detail=f"Tag '{tag_id}' key_fields must be a list")

            rules = tag.get("rules") or []
            if not isinstance(rules, list) or not rules:
                raise HTTPException(status_code=400, detail=f"Tag '{tag_id}' must contain at least one rule")

            rule_ids = set()
            sanitized_rules: List[Dict[str, Any]] = []
            for rule_index, raw_rule in enumerate(rules):
                if not isinstance(raw_rule, dict):
                    raise HTTPException(status_code=400, detail=f"Rule {rule_index + 1} in tag '{tag_id}' must be an object")
                rule = copy.deepcopy(raw_rule)
                rule_id = str(rule.get("id") or "").strip()
                if not rule_id:
                    raise HTTPException(status_code=400, detail=f"Each rule in tag '{tag_id}' requires an id")
                if rule_id in rule_ids:
                    raise HTTPException(status_code=400, detail=f"Duplicate rule id in tag '{tag_id}': {rule_id}")
                rule_ids.add(rule_id)

                tag_value = str(rule.get("tag_value") or "").strip()
                if not tag_value:
                    raise HTTPException(status_code=400, detail=f"Rule '{rule_id}' requires tag_value")

                conditions = rule.get("conditions") or []
                if not isinstance(conditions, list) or not conditions:
                    raise HTTPException(status_code=400, detail=f"Rule '{rule_id}' must contain at least one condition")

                sanitized_conditions: List[Dict[str, Any]] = []
                for condition_index, raw_condition in enumerate(conditions):
                    if not isinstance(raw_condition, dict):
                        raise HTTPException(status_code=400, detail=f"Condition {condition_index + 1} in rule '{rule_id}' must be an object")
                    condition = copy.deepcopy(raw_condition)
                    field = str(condition.get("field") or "").strip()
                    operator = str(condition.get("operator") or "").strip()
                    value = condition.get("value")
                    agg = str(condition.get("agg") or condition.get("aggregate") or "").strip()
                    if not field:
                        raise HTTPException(status_code=400, detail=f"Condition {condition_index + 1} in rule '{rule_id}' requires field")
                    if not operator:
                        raise HTTPException(status_code=400, detail=f"Condition {condition_index + 1} in rule '{rule_id}' requires operator")
                    if operator not in set(get_tag_engine_operator_choices()):
                        raise HTTPException(status_code=400, detail=f"Unsupported operator in rule '{rule_id}': {operator}")
                    if agg and agg not in set(get_tag_engine_aggregate_choices()):
                        raise HTTPException(status_code=400, detail=f"Unsupported aggregation in rule '{rule_id}': {agg}")
                    sanitized_condition = {
                        "field": field,
                        "operator": operator,
                        "value": value if value is not None else "",
                    }
                    if agg:
                        sanitized_condition["agg"] = agg
                    sanitized_conditions.append(sanitized_condition)

                sanitized_rule = {
                    "id": rule_id,
                    "tag_value": tag_value,
                    "conditions": sanitized_conditions,
                }
                rule_logic = str(rule.get("condition_logic") or "").strip()
                if rule_logic:
                    sanitized_rule["condition_logic"] = "any" if rule_logic.lower() == "any" else "all"
                sanitized_rules.append(sanitized_rule)

            sanitized_tag = {
                "id": tag_id,
                "tag_key": tag_key,
                "source": "rule",
                "key_fields": [str(item).strip() for item in key_fields if str(item).strip()],
                "condition_logic": "any" if str(tag.get("condition_logic") or "").strip().lower() == "any" else "all",
                "first_match_only": str(tag.get("first_match_only", False)).strip().lower() in {"true", "1", "yes"},
                "rules": sanitized_rules,
            }
            sanitized_tags.append(sanitized_tag)

        sanitized_job = copy.deepcopy(job)
        sanitized_job["id"] = job_id
        sanitized_job["enabled"] = str(job.get("enabled", False)).strip().lower() in {"true", "1", "yes"}
        sanitized_job["inputs"] = [input_path]
        sanitized_job["outputs"] = [output_path]
        sanitized_job["anchor_field"] = anchor_field
        sanitized_job["entity_type"] = entity_type
        sanitized_job["key_delimiter"] = key_delimiter
        sanitized_job["tags"] = sanitized_tags
        sanitized_job["derived_tags"] = job.get("derived_tags") if isinstance(job.get("derived_tags"), list) else []
        sanitized_jobs.append(sanitized_job)

    return sanitized_jobs


def build_tag_engine_response(spec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "slug": spec["slug"],
        "title": spec["title"],
        "description": spec["description"],
        "jobs": spec["jobs"],
        "template_job": build_tag_engine_template_job(spec["template_job"]),
        "default_tag": default_tag_engine_tag(),
        "default_rule": default_tag_engine_rule(),
        "default_condition": default_tag_engine_condition(),
        "required_fields": spec["required_fields"],
        "loaded_from": str(spec["loaded_from"]),
        "save_path": str(spec["custom_path"]),
        "available_inputs": list_masterdata_tables(),
        "available_outputs": list_metadata_csv_tables(),
        "entity_type_options": get_tag_engine_entity_types(),
        "operator_options": get_tag_engine_operator_choices(),
        "aggregate_options": get_tag_engine_aggregate_choices(),
    }


def resolve_vtx_path(path_value: str) -> Path:
    raw = (path_value or "").strip()
    if not raw:
        return VTX_ROOT_PATH
    if raw.startswith("/"):
        return Path(raw)
    return VTX_ROOT_PATH / raw


def _server_inventory_default_job_outputs() -> Dict[str, Any]:
    return {
        "inventory_table": {
            "path": "var/masterdata/unified_server_inventory.csv",
            "include_presence_flags": False,
            "include_value_provenance": False,
            "include_ranked_values": False,
            "ranked_values_delimiter": ";",
            "fields": ["Hostname", "IP Address", "OS", "Environment", "CPU", "Memory", "Storage"],
        },
        "presence_matrix_reports": [
            {
                "id": "hostname_presence",
                "attribute": "Hostname",
                "normalize_as": "hostname",
                "source_of_truth": "consolidated_server_view",
                "display_attributes": ["IP Address"],
                "path": "var/analysis/presence_hostname_matrix.csv",
                "html_path": "var/analysis/presence_hostname_matrix.html",
            }
        ],
    }


def normalize_server_inventory_doc(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize legacy auto_v2 format (top-level inventories/globals/version) into
    the editor format (config.payload.*) without discarding existing data.
    """
    if not isinstance(data, dict):
        return {
            "_vtx": {"schema": "v1", "kind": "report", "id": "server_inventory_matrix_vtx"},
            "config": {"payload": {"inventories": []}},
        }

    normalized = dict(data)
    config = normalized.get("config")
    if not isinstance(config, dict):
        config = {}
        normalized["config"] = config
    payload = config.get("payload")
    if not isinstance(payload, dict):
        payload = {}
        config["payload"] = payload

    # Lift legacy keys into payload only when missing there.
    for legacy_key in ("version", "globals", "inventories", "outputs", "jobs"):
        if legacy_key in normalized and legacy_key not in payload:
            payload[legacy_key] = normalized[legacy_key]

    if "inventories" not in payload or not isinstance(payload.get("inventories"), list):
        payload["inventories"] = []
    if "jobs" not in payload or not isinstance(payload.get("jobs"), list):
        payload["jobs"] = []

    # Back-compat: if legacy payload.outputs exists, lift it into a default job outputs block.
    legacy_outputs = payload.get("outputs")
    if isinstance(legacy_outputs, dict):
        jobs = payload.get("jobs") or []
        if jobs and isinstance(jobs[0], dict):
            jobs[0].setdefault("outputs", legacy_outputs)
        elif payload.get("inventories"):
            inv_id = str((payload["inventories"][0] or {}).get("id") or "Server_Inventory_Matrix")
            payload["jobs"] = [
                {
                    "id": "server_inventory_matrix",
                    "enabled": True,
                    "inventory_id": inv_id,
                    "outputs": legacy_outputs,
                }
            ]
        payload.pop("outputs", None)

    inventories = [i for i in (payload.get("inventories") or []) if isinstance(i, dict)]
    payload["inventories"] = inventories
    jobs = [j for j in (payload.get("jobs") or []) if isinstance(j, dict)]

    # Normalize into a strict 1:1 inventory->job model with required inventory_id.
    by_inv: Dict[str, Dict[str, Any]] = {}
    for job in jobs:
        inv_ref = str(job.get("inventory_id") or "").strip()
        if inv_ref and inv_ref not in by_inv:
            by_inv[inv_ref] = dict(job)

    normalized_jobs: List[Dict[str, Any]] = []
    for idx, inv in enumerate(inventories):
        inv_id = str(inv.get("id") or "").strip()
        if not inv_id:
            continue
        job = dict(by_inv.get(inv_id) or {})
        if not str(job.get("id") or "").strip():
            job["id"] = "server_inventory_matrix" if len(inventories) == 1 else f"server_inventory_matrix_{idx + 1}"
        job["enabled"] = bool(job.get("enabled", True))
        job["inventory_id"] = inv_id
        if not isinstance(job.get("outputs"), dict):
            job["outputs"] = _server_inventory_default_job_outputs()
        normalized_jobs.append(job)

    payload["jobs"] = normalized_jobs

    if "_vtx" not in normalized:
        normalized["_vtx"] = {
            "schema": "v1",
            "kind": "report",
            "id": "server_inventory_matrix_vtx",
            "title": "Server Inventory Matrix (VTX)",
        }

    return normalized


def normalize_table_aggregator_doc(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize table aggregator YAML into editor shape with config.payload.jobs.
    """
    if not isinstance(data, dict):
        return {
            "_vtx": {"schema": "v1", "kind": "transform", "id": "table_aggregator_vtx"},
            "config": {"payload": {"jobs": []}},
        }

    normalized = dict(data)
    config = normalized.get("config")
    if not isinstance(config, dict):
        config = {}
        normalized["config"] = config
    payload = config.get("payload")
    if not isinstance(payload, dict):
        payload = {}
        config["payload"] = payload

    if "jobs" in normalized and "jobs" not in payload:
        payload["jobs"] = normalized["jobs"]
    if "jobs" not in payload or not isinstance(payload.get("jobs"), list):
        payload["jobs"] = []

    if "_vtx" not in normalized:
        normalized["_vtx"] = {
            "schema": "v1",
            "kind": "transform",
            "id": "table_aggregator_vtx",
            "title": "Table Aggregator (VTX)",
        }

    return normalized


def get_aws_shortlist_regions() -> List[str]:
    if not AWS_CONTEXT_PRICING_DIR.exists():
        return []
    regions: List[str] = []
    for path in sorted(AWS_CONTEXT_PRICING_DIR.glob("server_pricing_shortlist_*.parquet")):
        name = path.stem
        prefix = "server_pricing_shortlist_"
        if name.startswith(prefix):
            regions.append(name[len(prefix):])
    return regions


def load_aws_shortlist(region: str) -> pd.DataFrame:
    path = AWS_CONTEXT_PRICING_DIR / f"server_pricing_shortlist_{region}.parquet"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Region shortlist not found: {region}")
    df = pd.read_parquet(path).fillna("")
    if "price_usd" in df.columns:
        df["price_usd"] = pd.to_numeric(df["price_usd"], errors="coerce")
        df = df[df["price_usd"].notna()]
        df = df[df["price_usd"] > 0]
    return df


def _str(value: Any) -> str:
    return "" if value is None else str(value)


def _is_prod_environment(value: str) -> bool:
    v = _str(value).strip().upper()
    return v in {"PROD", "PRODUCTION"}


def _is_out_of_scope(value: str) -> bool:
    v = _str(value).strip().lower()
    return v in {"out-of-scope", "out of scope"}


def _split_csvish(value: str) -> List[str]:
    return [x.strip() for x in _str(value).split(",") if x.strip()]


def _evaluate_condition(actual: Any, op: str, expected: Any) -> bool:
    op = _str(op).strip().lower()
    lhs = _str(actual).strip()
    rhs = _str(expected).strip()
    if op in {"=", "=="}:
        return lhs.lower() == rhs.lower()
    if op == "!=":
        return lhs.lower() != rhs.lower()
    if op == "contains":
        return rhs.lower() in lhs.lower()
    if op == "regex":
        try:
            return bool(re.search(rhs, lhs, flags=re.IGNORECASE))
        except re.error:
            return False
    if op == "in":
        return lhs.lower() in [x.lower() for x in _split_csvish(rhs)]
    if op == "notin":
        return lhs.lower() not in [x.lower() for x in _split_csvish(rhs)]
    return False


def _normalize_rules(raw_rules: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_rules, list):
        return []
    out: List[Dict[str, Any]] = []
    for rule in raw_rules:
        if not isinstance(rule, dict):
            continue
        conds: List[Dict[str, str]] = []
        for c in rule.get("conditions") or []:
            if not isinstance(c, dict):
                continue
            field = _str(c.get("field")).strip()
            op = _str(c.get("op")).strip()
            value = _str(c.get("value")).strip()
            if field and op:
                conds.append({"field": field, "op": op, "value": value})
        out.append(
            {
                "name": _str(rule.get("name")).strip() or "rule",
                "match_mode": "any" if _str(rule.get("match_mode")).strip().lower() == "any" else "all",
                "conditions": conds,
                "set": {
                    "tenancy": _str((rule.get("set") or {}).get("tenancy")).strip(),
                    "term_type": _str((rule.get("set") or {}).get("term_type")).strip(),
                    "lease_length": _str((rule.get("set") or {}).get("lease_length")).strip(),
                    "license_model": _str((rule.get("set") or {}).get("license_model")).strip(),
                    "cpu_vendor": _str((rule.get("set") or {}).get("cpu_vendor")).strip(),
                    "network_performance": _str((rule.get("set") or {}).get("network_performance")).strip(),
                    "uptime_pct": _str((rule.get("set") or {}).get("uptime_pct")).strip(),
                    "exclude": bool((rule.get("set") or {}).get("exclude", False)),
                },
            }
        )
    return out


def _apply_network_preference(df: pd.DataFrame, preferred: str) -> pd.DataFrame:
    if df.empty:
        return df
    pref = _str(preferred).strip()
    if not pref or "network_performance" not in df.columns:
        return df

    group_cols = [c for c in ["instance_type", "tenancy", "term_type", "lease_length", "license_model", "cpu_vendor"] if c in df.columns]
    if not group_cols:
        group_cols = ["instance_type"] if "instance_type" in df.columns else []
    if not group_cols:
        return df

    def _network_mbps(value: Any) -> float:
        s = _str(value).strip().lower()
        if not s:
            return float("inf")
        # Extract first numeric token and normalize to Mbps when value mentions Gbps.
        m = re.search(r"(\d+(?:\.\d+)?)", s)
        if not m:
            return float("inf")
        n = float(m.group(1))
        if "gb" in s or "gbit" in s:
            return n * 1000.0
        return n

    kept = []
    for _, group in df.groupby(group_cols, dropna=False):
        pref_rows = group[group["network_performance"].astype(str).str.contains(pref, case=False, na=False)]
        if not pref_rows.empty:
            kept.append(pref_rows)
            continue
        # Fallback to smallest network profile in group; tie-break on lowest price.
        g2 = group.copy()
        g2["_network_mbps"] = g2["network_performance"].apply(_network_mbps)
        min_net = g2["_network_mbps"].min()
        net_rows = g2[g2["_network_mbps"] == min_net]
        min_price = net_rows["price_usd"].min()
        kept.append(net_rows[net_rows["price_usd"] == min_price].head(1).drop(columns=["_network_mbps"], errors="ignore"))
    return pd.concat(kept, ignore_index=True) if kept else df.head(0)


def build_aws_cost_model(payload: Dict[str, Any]) -> Dict[str, Any]:
    region = _str(payload.get("region")).strip() or "us-east-1"
    defaults = payload.get("defaults") if isinstance(payload.get("defaults"), dict) else {}
    rules = _normalize_rules(payload.get("rules"))
    exclude_out_of_scope = bool(defaults.get("exclude_out_of_scope", True))

    df = load_aws_shortlist(region)
    if df.empty:
        return {
            "summary": {"region": region, "server_total": 0, "excluded_total": 0, "matched_total": 0, "unmatched_total": 0},
            "preview_rows": [],
            "preview_columns": [],
            "delta_reasons": [],
            "table_total_rows": 0,
        }

    # Build per-server profile for policy decisions.
    profile_cols = [c for c in ["server_id", "Environment", "Final Environment", "Server_Scope", "Server_Disposition", "Final Server Disposition"] if c in df.columns]
    profiles = (
        df.sort_values(by=[c for c in ["server_id", "shortlist_rank"] if c in df.columns])
        .groupby("server_id", as_index=False)[profile_cols[1:]]
        .first()
    )

    rows_out: List[Dict[str, Any]] = []
    per_server_costs: List[Dict[str, Any]] = []
    delta_reasons: Dict[str, int] = {}

    # Pre-group candidates for speed.
    grouped = {sid: g.copy() for sid, g in df.groupby("server_id", sort=False)}

    for _, profile in profiles.iterrows():
        server_id = _str(profile.get("server_id")).strip()
        if not server_id:
            continue
        candidates = grouped.get(server_id, pd.DataFrame())

        env = _str(profile.get("Final Environment") or profile.get("Environment")).strip()
        scope = _str(profile.get("Server_Scope")).strip()
        env_class = "prod" if _is_prod_environment(env) else "non_prod"
        decision = {
            "tenancy": _str(defaults.get("tenancy_prod", "Dedicated") if _is_prod_environment(env) else defaults.get("tenancy_nonprod", "Shared")).strip(),
            "term_type": _str(defaults.get("term_type", "Reserved")).strip(),
            "lease_length": _str(defaults.get("lease_length", "1yr")).strip(),
            "license_model": _str(defaults.get("license_model", "No License required")).strip(),
            "cpu_vendor": _str(defaults.get("cpu_vendor", "amd")).strip(),
            "network_performance": _str(defaults.get("network_performance", "5000")).strip(),
            "uptime_pct": float(defaults.get("uptime_pct", 100) or 100),
            "exclude": bool(exclude_out_of_scope and _is_out_of_scope(scope)),
        }

        # Apply custom rules in order.
        for rule in rules:
            cond_results: List[bool] = []
            for cond in rule.get("conditions") or []:
                field = _str(cond.get("field")).strip()
                cond_results.append(_evaluate_condition(profile.get(field, ""), cond.get("op", ""), cond.get("value", "")))
            if not cond_results:
                continue
            matched = any(cond_results) if rule.get("match_mode") == "any" else all(cond_results)
            if not matched:
                continue
            updates = rule.get("set") or {}
            for key in ("tenancy", "term_type", "lease_length", "license_model", "cpu_vendor", "network_performance"):
                if _str(updates.get(key)).strip():
                    decision[key] = _str(updates.get(key)).strip()
            if _str(updates.get("uptime_pct")).strip():
                try:
                    decision["uptime_pct"] = float(_str(updates.get("uptime_pct")).strip())
                except Exception:
                    pass
            if updates.get("exclude") is True:
                decision["exclude"] = True

        if decision["exclude"]:
            per_server_costs.append({
                "server_id": server_id,
                "env_class": env_class,
                "selected_tenancy": decision["tenancy"],
                "low": 0.0,
                "high": 0.0,
                "low_runtime": 0.0,
                "high_runtime": 0.0,
                "excluded": True,
                "matched": False,
            })
            continue

        if candidates.empty:
            per_server_costs.append({
                "server_id": server_id,
                "env_class": env_class,
                "selected_tenancy": decision["tenancy"],
                "low": 0.0,
                "high": 0.0,
                "low_runtime": 0.0,
                "high_runtime": 0.0,
                "excluded": False,
                "matched": False,
            })
            continue

        # Policy filters
        for k in ("tenancy", "term_type", "lease_length", "license_model", "cpu_vendor"):
            if k in candidates.columns and _str(decision.get(k)).strip():
                candidates = candidates[candidates[k].astype(str).str.lower() == _str(decision.get(k)).strip().lower()]

        candidates = _apply_network_preference(candidates, _str(decision.get("network_performance")))
        if candidates.empty:
            per_server_costs.append({
                "server_id": server_id,
                "env_class": env_class,
                "selected_tenancy": decision["tenancy"],
                "low": 0.0,
                "high": 0.0,
                "low_runtime": 0.0,
                "high_runtime": 0.0,
                "excluded": False,
                "matched": False,
            })
            continue

        # Compact non-material duplicates.
        material = [c for c in ["instance_type", "tenancy", "term_type", "lease_length", "license_model", "cpu_vendor", "network_performance", "price_usd"] if c in candidates.columns]
        sort_cols = [c for c in ["price_usd", "shortlist_rank", "instance_type"] if c in candidates.columns]
        candidates = candidates.sort_values(by=sort_cols).drop_duplicates(subset=material, keep="first")
        if candidates.empty:
            per_server_costs.append({
                "server_id": server_id,
                "env_class": env_class,
                "selected_tenancy": decision["tenancy"],
                "low": 0.0,
                "high": 0.0,
                "low_runtime": 0.0,
                "high_runtime": 0.0,
                "excluded": False,
                "matched": False,
            })
            continue

        low = float(candidates["price_usd"].min())
        high = float(candidates["price_usd"].max())
        runtime = max(0.0, min(100.0, float(decision.get("uptime_pct", 100.0)))) / 100.0
        low_runtime = low * runtime
        high_runtime = high * runtime
        per_server_costs.append({
            "server_id": server_id,
            "env_class": env_class,
            "selected_tenancy": decision["tenancy"],
            "low": low,
            "high": high,
            "low_runtime": low_runtime,
            "high_runtime": high_runtime,
            "excluded": False,
            "matched": True,
        })

        varying_fields = []
        for f in ["instance_type", "tenancy", "term_type", "lease_length", "license_model", "cpu_vendor", "network_performance"]:
            if f in candidates.columns and candidates[f].astype(str).nunique(dropna=False) > 1:
                varying_fields.append(f)
        for f in varying_fields:
            delta_reasons[f] = delta_reasons.get(f, 0) + 1

        candidates = candidates.sort_values(by=[c for c in ["price_usd", "shortlist_rank"] if c in candidates.columns]).copy()
        for row_idx, (_, r) in enumerate(candidates.iterrows()):
            row_obj = {k: (_str(v) if k != "price_usd" else float(v)) for k, v in r.items()}
            row_obj["model_selected_tenancy"] = decision["tenancy"]
            row_obj["model_selected_term_type"] = decision["term_type"]
            row_obj["model_selected_lease_length"] = decision["lease_length"]
            row_obj["model_selected_license_model"] = decision["license_model"]
            row_obj["model_selected_cpu_vendor"] = decision["cpu_vendor"]
            row_obj["model_selected_network_preference"] = decision["network_performance"]
            row_obj["model_uptime_pct"] = float(decision["uptime_pct"])
            row_obj["server_low_watermark"] = low if row_idx == 0 else ""
            row_obj["server_high_watermark"] = high if row_idx == 0 else ""
            row_obj["server_low_watermark_with_runtime"] = low_runtime if row_idx == 0 else ""
            row_obj["server_high_watermark_with_runtime"] = high_runtime if row_idx == 0 else ""
            rows_out.append(row_obj)

    out_df = pd.DataFrame(rows_out)
    server_cost_df = pd.DataFrame(per_server_costs)

    if server_cost_df.empty:
        summary = {
            "region": region,
            "server_total": 0,
            "excluded_total": 0,
            "matched_total": 0,
            "unmatched_total": 0,
            "low_total_hourly": 0.0,
            "high_total_hourly": 0.0,
            "low_total_monthly": 0.0,
            "high_total_monthly": 0.0,
            "low_total_monthly_runtime": 0.0,
            "high_total_monthly_runtime": 0.0,
        }
    else:
        total = len(server_cost_df)
        excluded = int(server_cost_df["excluded"].sum())
        matched = int(server_cost_df["matched"].sum())
        unmatched = total - excluded - matched
        low_hour = float(server_cost_df["low"].sum())
        high_hour = float(server_cost_df["high"].sum())
        low_hour_rt = float(server_cost_df["low_runtime"].sum())
        high_hour_rt = float(server_cost_df["high_runtime"].sum())
        summary = {
            "region": region,
            "server_total": total,
            "excluded_total": excluded,
            "matched_total": matched,
            "unmatched_total": unmatched,
            "excluded_pct": round((excluded / total) * 100.0, 2) if total else 0.0,
            "low_total_hourly": round(low_hour, 4),
            "high_total_hourly": round(high_hour, 4),
            "low_total_monthly": round(low_hour * 730.0, 2),
            "high_total_monthly": round(high_hour * 730.0, 2),
            "low_total_monthly_runtime": round(low_hour_rt * 730.0, 2),
            "high_total_monthly_runtime": round(high_hour_rt * 730.0, 2),
        }

    preview_df = out_df.head(500).copy() if not out_df.empty else pd.DataFrame()
    preview_rows = preview_df.to_dict(orient="records") if not preview_df.empty else []
    preview_columns = list(preview_df.columns) if not preview_df.empty else []
    reasons = [{"field": k, "affected_servers": v} for k, v in sorted(delta_reasons.items(), key=lambda x: x[1], reverse=True)]

    def _branch(dfb: pd.DataFrame) -> Dict[str, Any]:
        if dfb.empty:
            return {
                "servers": 0,
                "low_monthly": 0.0,
                "high_monthly": 0.0,
                "low_monthly_runtime": 0.0,
                "high_monthly_runtime": 0.0,
            }
        return {
            "servers": int(len(dfb)),
            "low_monthly": round(float(dfb["low"].sum()) * 730.0, 2),
            "high_monthly": round(float(dfb["high"].sum()) * 730.0, 2),
            "low_monthly_runtime": round(float(dfb["low_runtime"].sum()) * 730.0, 2),
            "high_monthly_runtime": round(float(dfb["high_runtime"].sum()) * 730.0, 2),
        }

    breakdown = {}
    if not server_cost_df.empty:
        active = server_cost_df[(server_cost_df["matched"] == True) & (server_cost_df["excluded"] == False)]
        prod = active[active["env_class"] == "prod"]
        non_prod = active[active["env_class"] != "prod"]
        prod_dedicated = prod[prod["selected_tenancy"].astype(str).str.lower() == "dedicated"]
        non_prod_shared = non_prod[non_prod["selected_tenancy"].astype(str).str.lower() == "shared"]
        breakdown = {
            "total_servers": int(len(server_cost_df)),
            "lacking_match_criteria": int((server_cost_df["matched"] == False).sum()),
            "out_of_scope": int((server_cost_df["excluded"] == True).sum()),
            "prod": _branch(prod),
            "prod_dedicated": _branch(prod_dedicated),
            "non_prod": _branch(non_prod),
            "non_prod_shared": _branch(non_prod_shared),
            "total": _branch(active),
        }
    else:
        breakdown = {
            "total_servers": 0,
            "lacking_match_criteria": 0,
            "out_of_scope": 0,
            "prod": _branch(pd.DataFrame()),
            "prod_dedicated": _branch(pd.DataFrame()),
            "non_prod": _branch(pd.DataFrame()),
            "non_prod_shared": _branch(pd.DataFrame()),
            "total": _branch(pd.DataFrame()),
        }

    # Build export-friendly frame.
    export_df = out_df.copy()
    if not export_df.empty:
        keep_cols = [c for c in export_df.columns if c not in AWS_EXPORT_EXCLUDE_COLUMNS]
        export_df = export_df[keep_cols]

    return {
        "summary": summary,
        "breakdown": breakdown,
        "preview_rows": preview_rows,
        "preview_columns": preview_columns,
        "delta_reasons": reasons,
        "table_total_rows": int(len(out_df)),
        "export_df": export_df,
        "raw_df": df,
    }


@app.get("/config/aws-cost-model", response_class=HTMLResponse)
async def aws_cost_model_page(request: Request) -> HTMLResponse:
    redirect = require_login(request)
    if redirect:
        return redirect

    html = """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>AWS Cost Model Builder</title>
      <style>
        :root { --ink:#e6eefc; --line:rgba(121,184,255,.25); --bg:#0b1320; --surface:#122238; --muted:#a9bdd7; --accent:#f0b24a; }
        * { box-sizing:border-box; }
        body { margin:0; font:14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color:var(--ink); background:radial-gradient(circle at top right, rgba(121,184,255,.14), transparent 32%), radial-gradient(circle at bottom left, rgba(240,178,74,.1), transparent 36%), linear-gradient(180deg,#0b1320,#101d2f); }
        .top { position:sticky; top:0; z-index:100; backdrop-filter: blur(8px); background:rgba(8,16,28,.86); border-bottom:1px solid var(--line); padding:10px 16px; display:flex; gap:10px; align-items:center; }
        .btn { border:1px solid rgba(67,100,135,.7); background:rgba(19,34,56,.92); color:var(--ink); border-radius:999px; padding:8px 12px; cursor:pointer; text-decoration:none; font-weight:600; }
        .btn:hover { background:rgba(27,48,77,.98); border-color:rgba(121,184,255,.7); }
        .wrap { width:min(1480px, calc(100% - 28px)); margin:16px auto 28px; display:grid; gap:12px; }
        .panel { background:linear-gradient(180deg, rgba(19,35,57,.96), rgba(15,28,45,.96)); border:1px solid var(--line); border-radius:14px; padding:14px; box-shadow:0 18px 48px rgba(0,0,0,.28); }
        .grid { display:grid; grid-template-columns: 420px 1fr; gap:12px; }
        .row { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:8px; }
        .row3 { display:grid; grid-template-columns:1fr 1fr 1fr; gap:8px; margin-bottom:8px; }
        input, select, textarea { width:100%; border:1px solid rgba(67,100,135,.65); border-radius:8px; padding:8px; background:rgba(14,24,40,.92); color:var(--ink); }
        textarea { min-height:68px; resize:vertical; }
        .summary { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; }
        .card { border:1px solid var(--line); border-radius:12px; padding:10px; background:rgba(12,22,36,.76); }
        .label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.06em; }
        .value { font-size:18px; font-weight:700; margin-top:4px; }
        .muted { color:var(--muted); }
        .rule { border:1px solid var(--line); border-radius:12px; padding:10px; margin-bottom:8px; background:rgba(12,22,36,.76); }
        table { width:100%; border-collapse:collapse; font-size:12px; }
        th, td { border-bottom:1px solid rgba(71,85,105,.14); padding:6px; text-align:left; vertical-align:top; }
        .hint { cursor:help; border:1px solid rgba(71,85,105,.35); border-radius:50%; display:inline-flex; width:16px; height:16px; align-items:center; justify-content:center; font-size:11px; margin-left:6px; color:#475569; }
        .hero { margin:10px auto 0; width:min(1480px, calc(100% - 28px)); min-height:180px; color:#fff; position:relative; overflow:hidden; border:1px solid rgba(121,184,255,.25); border-radius:18px; background:linear-gradient(135deg,#1e293b 0%, #475569 50%, #64748b 100%); }
        .hero::before { content:""; position:absolute; inset:0; background-image:linear-gradient(rgba(255,255,255,.08) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.08) 1px, transparent 1px); background-size:60px 60px; opacity:.7; }
        .hero-content { position:relative; z-index:1; padding:24px; }
        .hero h1 { margin:0 0 8px; font-size:clamp(1.8rem,3vw,2.4rem); }
        .hero p { margin:0; max-width:76ch; opacity:.95; }
        @media (max-width:1150px){ .grid{grid-template-columns:1fr;} .summary{grid-template-columns:1fr 1fr;} .row3{grid-template-columns:1fr;} }
      </style>
    </head>
    <body>
      <div class="top">
        <a class="btn" href="/">Back to Home</a>
        <button class="btn" id="runBtn">Run Model</button>
        <button class="btn" id="downloadBtn">Download CSV</button>
        <button class="btn" id="downloadRawBtn">Download Raw CSV</button>
        <a class="btn" href="#tableSection">Show Table Breakdown</a>
        <span class="muted" id="status">Loading...</span>
      </div>
      <section class="hero">
        <div class="hero-content">
          <h1>AWS Cost Projection Builder</h1>
          <p>Apply high-level assumptions and conditional rules to project low/high cloud cost ranges with runtime overlays and a drilldown table.</p>
        </div>
      </section>
      <div class="wrap">
        <div class="grid">
          <section class="panel">
            <h2 style="margin:0 0 8px;">AWS Cost Model Inputs
              <span class="hint" title="This model intentionally uses constrained assumptions for usability: general-purpose VM families, EBS storage, no host/metal, no preinstalled software, and reserved no-upfront terms.">?</span>
            </h2>
            <div class="row">
              <div><label>Region</label><select id="region"></select></div>
              <div><label>Exclude Out-of-Scope</label><select id="excludeScope"><option value="true" selected>true</option><option value="false">false</option></select></div>
            </div>
            <div class="row3">
              <div><label>CPU Vendor</label><select id="cpuVendor"><option>amd</option><option>intel</option><option>aws</option></select></div>
              <div><label>Term Type</label><select id="termType"><option selected>Reserved</option><option>OnDemand</option></select></div>
              <div><label>Lease Length</label><select id="leaseLength"><option selected>1yr</option><option>3yr</option></select></div>
            </div>
            <div class="row3">
              <div><label>License</label><select id="licenseModel"><option selected>No License required</option><option>Bring your own license</option><option>License Included - Infrastructure</option></select></div>
              <div><label>Prod Tenancy</label><select id="tenancyProd"><option selected>Dedicated</option><option>Shared</option></select></div>
              <div><label>Non-Prod Tenancy</label><select id="tenancyNonProd"><option selected>Shared</option><option>Dedicated</option></select></div>
            </div>
            <div class="row">
              <div><label>Network Preference</label><input id="networkPref" type="text" value="5000" /></div>
              <div><label>Default Uptime %</label><input id="uptimeDefault" type="number" min="0" max="100" value="100" /></div>
            </div>
            <h3 style="margin:10px 0 6px;">Conditional Rules</h3>
            <div class="muted" style="margin-bottom:8px;">Conditions format: <code>Field|Operator|Value</code> one per line. Operators: <code>= != contains regex in notin</code>.</div>
            <div id="rules"></div>
            <button class="btn" id="addRuleBtn">Add Rule</button>
          </section>
          <section class="panel">
            <h2 style="margin:0 0 8px;">Projection Summary</h2>
            <div class="summary" id="summary"></div>
            <h3 style="margin:10px 0 6px;">Drivers of Low/High Delta</h3>
            <div id="delta" class="muted">Run model to see variance drivers.</div>
            <h3 style="margin:10px 0 6px;">Inventory Breakdown</h3>
            <div id="breakdown" class="muted">Run model to see inventory tree.</div>
          </section>
        </div>
        <section class="panel" id="tableSection">
          <h2 style="margin:0 0 8px;">Detailed Breakdown (Preview)</h2>
          <div class="muted" id="previewMeta">Run model to load preview table.</div>
          <div style="overflow:auto; max-height:560px;">
            <table id="previewTable"></table>
          </div>
        </section>
      </div>
      <script>
        let meta = { regions: [] };
        let lastPayload = null;
        let rules = [];

        function esc(v){ return (v==null?"":String(v)).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"','&quot;'); }
        function setStatus(msg){ document.getElementById("status").textContent = msg; }

        function newRule(){
          return {
            name: "rule",
            match_mode: "all",
            conditions_text: "Environment|=|DEV",
            set: { tenancy: "Shared", term_type: "", lease_length: "", license_model: "", cpu_vendor: "", network_performance: "", uptime_pct: "20", exclude: false }
          };
        }

        function parseConditions(text){
          return String(text || "").split("\\n").map(s => s.trim()).filter(Boolean).map(line => {
            const parts = line.split("|");
            return { field: (parts[0]||"").trim(), op: (parts[1]||"").trim(), value: (parts.slice(2).join("|")||"").trim() };
          }).filter(c => c.field && c.op);
        }

        function renderRules(){
          const box = document.getElementById("rules");
          box.innerHTML = "";
          if (!rules.length){
            box.innerHTML = "<div class='muted'>No rules. Defaults apply to all servers.</div>";
            return;
          }
          rules.forEach((r, i) => {
            const div = document.createElement("div");
            div.className = "rule";
            div.innerHTML = `
              <div class="row">
                <div><label>Rule Name</label><input data-r="${i}" data-k="name" value="${esc(r.name)}" /></div>
                <div><label>Match Mode</label><select data-r="${i}" data-k="match_mode"><option value="all" ${r.match_mode==="all"?"selected":""}>all</option><option value="any" ${r.match_mode==="any"?"selected":""}>any</option></select></div>
              </div>
              <label>Conditions (one per line: Field|Operator|Value)</label>
              <textarea data-r="${i}" data-k="conditions_text">${esc(r.conditions_text)}</textarea>
              <div class="row3" style="margin-top:8px;">
                <div><label>Set Tenancy</label><select data-r="${i}" data-s="tenancy"><option value=""></option><option ${r.set.tenancy==="Shared"?"selected":""}>Shared</option><option ${r.set.tenancy==="Dedicated"?"selected":""}>Dedicated</option></select></div>
                <div><label>Set Uptime %</label><input data-r="${i}" data-s="uptime_pct" type="number" min="0" max="100" value="${esc(r.set.uptime_pct)}" /></div>
                <div><label>Exclude</label><select data-r="${i}" data-s="exclude"><option value="false" ${!r.set.exclude?"selected":""}>false</option><option value="true" ${r.set.exclude?"selected":""}>true</option></select></div>
              </div>
              <div class="row3">
                <div><label>Set Term</label><select data-r="${i}" data-s="term_type"><option value=""></option><option ${r.set.term_type==="Reserved"?"selected":""}>Reserved</option><option ${r.set.term_type==="OnDemand"?"selected":""}>OnDemand</option></select></div>
                <div><label>Set Lease</label><select data-r="${i}" data-s="lease_length"><option value=""></option><option ${r.set.lease_length==="1yr"?"selected":""}>1yr</option><option ${r.set.lease_length==="3yr"?"selected":""}>3yr</option></select></div>
                <div><label>Set License</label><select data-r="${i}" data-s="license_model"><option value=""></option><option ${r.set.license_model==="No License required"?"selected":""}>No License required</option><option ${r.set.license_model==="Bring your own license"?"selected":""}>Bring your own license</option><option ${r.set.license_model==="License Included - Infrastructure"?"selected":""}>License Included - Infrastructure</option></select></div>
              </div>
              <div class="row">
                <div><label>Set CPU Vendor</label><select data-r="${i}" data-s="cpu_vendor"><option value=""></option><option ${r.set.cpu_vendor==="amd"?"selected":""}>amd</option><option ${r.set.cpu_vendor==="intel"?"selected":""}>intel</option><option ${r.set.cpu_vendor==="aws"?"selected":""}>aws</option></select></div>
                <div><label>Set Network Preference</label><input data-r="${i}" data-s="network_performance" value="${esc(r.set.network_performance)}" /></div>
              </div>
              <button class="btn" data-del="${i}">Delete Rule</button>
            `;
            box.appendChild(div);
          });

          document.querySelectorAll("[data-del]").forEach(btn => btn.onclick = () => {
            const i = Number(btn.getAttribute("data-del"));
            rules.splice(i, 1);
            renderRules();
          });

          document.querySelectorAll("[data-r][data-k]").forEach(el => el.onchange = () => {
            const i = Number(el.getAttribute("data-r"));
            const k = el.getAttribute("data-k");
            rules[i][k] = el.value;
          });

          document.querySelectorAll("[data-r][data-s]").forEach(el => el.onchange = () => {
            const i = Number(el.getAttribute("data-r"));
            const s = el.getAttribute("data-s");
            rules[i].set[s] = s === "exclude" ? (el.value === "true") : el.value;
          });
        }

        function buildPayload(){
          return {
            region: document.getElementById("region").value,
            defaults: {
              cpu_vendor: document.getElementById("cpuVendor").value,
              term_type: document.getElementById("termType").value,
              lease_length: document.getElementById("leaseLength").value,
              license_model: document.getElementById("licenseModel").value,
              tenancy_prod: document.getElementById("tenancyProd").value,
              tenancy_nonprod: document.getElementById("tenancyNonProd").value,
              network_performance: document.getElementById("networkPref").value,
              uptime_pct: Number(document.getElementById("uptimeDefault").value || 100),
              exclude_out_of_scope: document.getElementById("excludeScope").value === "true"
            },
            rules: rules.map(r => ({ name: r.name, match_mode: r.match_mode, conditions: parseConditions(r.conditions_text), set: r.set }))
          };
        }

        function renderSummary(summary){
          const s = summary || {};
          const cards = [
            ["Servers", s.server_total ?? 0],
            ["Excluded", `${s.excluded_total ?? 0} (${s.excluded_pct ?? 0}%)`],
            ["Matched", s.matched_total ?? 0],
            ["Unmatched", s.unmatched_total ?? 0],
            ["Low Monthly", `$${Number(s.low_total_monthly || 0).toLocaleString()}`],
            ["High Monthly", `$${Number(s.high_total_monthly || 0).toLocaleString()}`],
            ["Low Monthly (Runtime)", `$${Number(s.low_total_monthly_runtime || 0).toLocaleString()}`],
            ["High Monthly (Runtime)", `$${Number(s.high_total_monthly_runtime || 0).toLocaleString()}`]
          ];
          document.getElementById("summary").innerHTML = cards.map(([k,v]) => `<div class="card"><div class="label">${esc(k)}</div><div class="value">${esc(v)}</div></div>`).join("");
        }

        function renderDelta(items){
          if (!items || !items.length){
            document.getElementById("delta").textContent = "No major variance drivers detected.";
            return;
          }
          document.getElementById("delta").innerHTML = items.map(x => `<div>${esc(x.field)}: ${esc(x.affected_servers)} server(s)</div>`).join("");
        }

        function money(v){
          return `$${Number(v || 0).toLocaleString()}`;
        }

        function renderBreakdown(b){
          if (!b){
            document.getElementById("breakdown").textContent = "No breakdown available.";
            return;
          }
          const branch = (name, x) => {
            if (!x) return "";
            return `<div style="margin-left:14px;"><strong>${esc(name)}</strong>: ${esc(x.servers || 0)} server(s) | Low ${money(x.low_monthly)} / High ${money(x.high_monthly)} | Runtime Low ${money(x.low_monthly_runtime)} / Runtime High ${money(x.high_monthly_runtime)}</div>`;
          };
          document.getElementById("breakdown").innerHTML = [
            `<div><strong>Total Servers</strong>: ${esc(b.total_servers || 0)}</div>`,
            `<div><strong>Lacking Match Criteria</strong>: ${esc(b.lacking_match_criteria || 0)}</div>`,
            `<div><strong>Out of Scope</strong>: ${esc(b.out_of_scope || 0)}</div>`,
            branch("Prod", b.prod),
            branch("Prod Dedicated", b.prod_dedicated),
            branch("Non-Prod", b.non_prod),
            branch("Non-Prod Shared", b.non_prod_shared),
            `<div style="margin-top:6px;"><strong>Total Modeled Footprint</strong>: ${esc((b.total || {}).servers || 0)} server(s) | Low ${money((b.total || {}).low_monthly)} / High ${money((b.total || {}).high_monthly)} | Runtime Low ${money((b.total || {}).low_monthly_runtime)} / Runtime High ${money((b.total || {}).high_monthly_runtime)}</div>`,
          ].join("");
        }

        function renderTable(columns, rows, total){
          const table = document.getElementById("previewTable");
          if (!columns || !columns.length){
            table.innerHTML = "";
            document.getElementById("previewMeta").textContent = "No rows returned.";
            return;
          }
          const head = `<thead><tr>${columns.map(c => `<th>${esc(c)}</th>`).join("")}</tr></thead>`;
          const body = `<tbody>${rows.map(r => `<tr>${columns.map(c => `<td>${esc(r[c])}</td>`).join("")}</tr>`).join("")}</tbody>`;
          table.innerHTML = head + body;
          document.getElementById("previewMeta").textContent = `Previewing ${rows.length} row(s) of ${total}.`;
        }

        async function runModel(){
          const payload = buildPayload();
          lastPayload = payload;
          setStatus("Running model...");
          const res = await fetch("/api/aws-cost-model/run", { method:"POST", headers:{ "Content-Type":"application/json" }, body: JSON.stringify(payload) });
          const data = await res.json();
          if (!res.ok){
            setStatus(`Run failed: ${data.detail || res.status}`);
            return;
          }
          renderSummary(data.summary);
          renderDelta(data.delta_reasons || []);
          renderBreakdown(data.breakdown || {});
          renderTable(data.preview_columns || [], data.preview_rows || [], data.table_total_rows || 0);
          setStatus("Model complete");
        }

        async function downloadCsv(){
          const payload = lastPayload || buildPayload();
          setStatus("Building CSV...");
          const res = await fetch("/api/aws-cost-model/export", { method:"POST", headers:{ "Content-Type":"application/json" }, body: JSON.stringify(payload) });
          if (!res.ok){
            const err = await res.json().catch(() => ({}));
            setStatus(`Export failed: ${err.detail || res.status}`);
            return;
          }
          const blob = await res.blob();
          const url = window.URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          a.download = "aws_cost_model_projection.csv";
          document.body.appendChild(a);
          a.click();
          a.remove();
          window.URL.revokeObjectURL(url);
          setStatus("CSV download ready");
        }

        async function downloadRawCsv(){
          const payload = lastPayload || buildPayload();
          setStatus("Building raw CSV...");
          const res = await fetch("/api/aws-cost-model/export-raw", { method:"POST", headers:{ "Content-Type":"application/json" }, body: JSON.stringify(payload) });
          if (!res.ok){
            const err = await res.json().catch(() => ({}));
            setStatus(`Raw export failed: ${err.detail || res.status}`);
            return;
          }
          const blob = await res.blob();
          const url = window.URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          a.download = "aws_cost_model_raw.csv";
          document.body.appendChild(a);
          a.click();
          a.remove();
          window.URL.revokeObjectURL(url);
          setStatus("Raw CSV download ready");
        }

        async function init(){
          const res = await fetch("/api/aws-cost-model/meta");
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || res.status);
          meta = data;
          const sel = document.getElementById("region");
          sel.innerHTML = (meta.regions || []).map(r => `<option value="${esc(r)}">${esc(r)}</option>`).join("");
          if (!sel.value && meta.regions && meta.regions.length) sel.value = meta.regions[0];

          rules = [newRule(), { ...newRule(), name: "out_of_scope_skip", conditions_text: "Server_Scope|=|Out of Scope", set: { tenancy:"", term_type:"", lease_length:"", license_model:"", cpu_vendor:"", network_performance:"", uptime_pct:"", exclude:true } }];
          renderRules();

          document.getElementById("addRuleBtn").onclick = () => { rules.push(newRule()); renderRules(); };
          document.getElementById("runBtn").onclick = runModel;
          document.getElementById("downloadBtn").onclick = downloadCsv;
          document.getElementById("downloadRawBtn").onclick = downloadRawCsv;
          setStatus("Ready");
          await runModel();
        }
        init().catch(err => setStatus(`Failed: ${err}`));
      </script>
    </body>
    </html>
    """
    return HTMLResponse(html)


@app.get("/api/aws-cost-model/meta", response_class=JSONResponse)
async def aws_cost_model_meta(request: Request):
    redirect = require_login(request)
    if redirect:
        raise HTTPException(status_code=401, detail="Login required")
    regions = get_aws_shortlist_regions()
    return JSONResponse({"regions": regions})


@app.post("/api/aws-cost-model/run", response_class=JSONResponse)
async def aws_cost_model_run(request: Request):
    redirect = require_login(request)
    if redirect:
        raise HTTPException(status_code=401, detail="Login required")
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected JSON object")
    data = build_aws_cost_model(payload)
    data.pop("export_df", None)
    data.pop("raw_df", None)
    return JSONResponse(data)


@app.post("/api/aws-cost-model/export")
async def aws_cost_model_export(request: Request):
    redirect = require_login(request)
    if redirect:
        raise HTTPException(status_code=401, detail="Login required")
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected JSON object")
    data = build_aws_cost_model(payload)
    export_df = data.get("export_df")
    if not isinstance(export_df, pd.DataFrame):
        raise HTTPException(status_code=500, detail="Failed to build export table")
    buf = io.StringIO()
    export_df.to_csv(buf, index=False)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=aws_cost_model_projection.csv"},
    )


@app.post("/api/aws-cost-model/export-raw")
async def aws_cost_model_export_raw(request: Request):
    redirect = require_login(request)
    if redirect:
        raise HTTPException(status_code=401, detail="Login required")
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected JSON object")
    data = build_aws_cost_model(payload)
    raw_df = data.get("raw_df")
    if not isinstance(raw_df, pd.DataFrame):
        raise HTTPException(status_code=500, detail="Failed to build raw export table")
    buf = io.StringIO()
    raw_df.to_csv(buf, index=False)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=aws_cost_model_raw.csv"},
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    user = get_current_user(request)
    if user:
        # Already logged in; if must_change still true, send to change-password
        auth = load_auth_config()
        if auth["must_change"]:
            return RedirectResponse(url="/change-password", status_code=303)
        return RedirectResponse(url="/", status_code=303)

    error = request.query_params.get("error")
    html = f"""
    <html>
      <head>
        <title>VTX Login</title>
      </head>
      <body style="background:#121212;color:#eee;font-family:sans-serif;">
        <h1>VTX Login</h1>
        {"<div style='color:#ff8080;margin-bottom:1rem;'>" + error + "</div>" if error else ""}
        <form method="post" action="/login">
          <div style="margin-bottom:0.5rem;">
            <label>Username:
              <input name="username" type="text" />
            </label>
          </div>
          <div style="margin-bottom:0.5rem;">
            <label>Password:
              <input name="password" type="password" />
            </label>
          </div>
          <div>
            <button type="submit">Sign in</button>
          </div>
        </form>
      </body>
    </html>
    """
    return HTMLResponse(html)


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    auth = load_auth_config()
    expected_user = auth["username"]
    expected_hash = auth["password_hash"]
    must_change = auth["must_change"]

    if username == expected_user and hash_password(password) == expected_hash:
        request.session["user"] = username
        log("info", f"User '{username}' logged in")

        # If they are still on default/must-change credentials, force change-password
        if must_change:
            return RedirectResponse(url="/change-password", status_code=303)

        next_url = request.session.pop("next_url", "/")
        return RedirectResponse(url=next_url, status_code=303)

    log("warning", f"Failed login attempt for '{username}'")
    return RedirectResponse(url="/login?error=Invalid+username+or+password", status_code=303)


@app.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request) -> HTMLResponse:
    redirect = require_login(request)
    if redirect:
        return redirect

    user = get_current_user(request) or "unknown"
    error = request.query_params.get("error")
    success = request.query_params.get("success")

    html = f"""
    <html>
      <head>
        <title>Change Password</title>
      </head>
      <body style="background:#121212;color:#eee;font-family:sans-serif;">
        <h1>Change Password</h1>
        <p>User: {user}</p>
        {"<div style='color:#ff8080;margin-bottom:1rem;'>" + error + "</div>" if error else ""}
        {"<div style='color:#80ff80;margin-bottom:1rem;'>" + success + "</div>" if success else ""}
        <form method="post" action="/change-password">
          <div style="margin-bottom:0.5rem;">
            <label>Current password:
              <input name="current_password" type="password" required />
            </label>
          </div>
          <div style="margin-bottom:0.5rem;">
            <label>New password:
              <input name="new_password" type="password" required />
            </label>
          </div>
          <div style="margin-bottom:0.5rem;">
            <label>Confirm new password:
              <input name="confirm_password" type="password" required />
            </label>
          </div>
          <div>
            <button type="submit">Update password</button>
          </div>
        </form>
        <p><a href="/" style="color:#8ab4ff;">Back to home</a></p>
      </body>
    </html>
    """
    return HTMLResponse(html)


@app.post("/change-password")
async def change_password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    redirect = require_login(request)
    if redirect:
        return redirect

    auth = load_auth_config()
    user = get_current_user(request) or auth["username"]

    # Verify current password
    if hash_password(current_password) != auth["password_hash"]:
        return RedirectResponse(
            url="/change-password?error=Current+password+is+incorrect",
            status_code=303,
        )

    if new_password != confirm_password:
        return RedirectResponse(
            url="/change-password?error=New+passwords+do+not+match",
            status_code=303,
        )

    if not new_password:
        return RedirectResponse(
            url="/change-password?error=New+password+cannot+be+empty",
            status_code=303,
        )

    # Save new password & clear must_change
    save_auth_config(username=user, password_hash=hash_password(new_password), must_change=False)
    log("info", f"User '{user}' changed password")

    return RedirectResponse(
        url="/change-password?success=Password+updated+successfully",
        status_code=303,
    )


@app.get("/logout")
async def logout(request: Request):
    user = get_current_user(request)
    if user:
        log("info", f"User '{user}' logged out")
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    redirect = require_login(request)
    if redirect:
        return redirect

    index_path = VTX_ROOT_PATH / "usr" / "web" / "templates" / "index.html"
    if not index_path.exists():
        log("error", f"index.html not found at {index_path}")
        raise HTTPException(status_code=500, detail="Landing page not found")

    html = index_path.read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/config/server-inventory-matrix", response_class=HTMLResponse)
async def server_inventory_matrix_editor(request: Request) -> HTMLResponse:
    redirect = require_login(request)
    if redirect:
        return redirect
    custom_path = get_server_inventory_custom_yaml()
    source_path = get_server_inventory_source_yaml()
    if not custom_path.exists() and not source_path.exists():
        raise HTTPException(
            status_code=404,
            detail="No server inventory matrix configuration found in usr/config/custom or usr/config/auto_v2",
        )

    html = """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>Server Inventory Matrix Editor</title>
      <style>
        :root { --ink:#1a202c; --ink-soft:#475569; --line:rgba(71,85,105,.18); --accent:#334155; --surface:#ffffff; --surface-2:#f8fafc; }
        * { box-sizing:border-box; }
        body { margin:0; font:14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color:var(--ink); background:#f8fafc; }
        .navbar { position:fixed; top:0; width:100%; background:#fff; padding:1rem 5%; z-index:1000; border-bottom:1px solid rgba(71,85,105,.1); }
        .nav-container { max-width:1280px; margin:0 auto; display:flex; align-items:center; justify-content:space-between; gap:14px; }
        .logo { font-size:1.2rem; font-weight:700; display:flex; align-items:center; gap:.7rem; color:var(--ink); text-decoration:none; }
        .logo-icon { width:28px; height:28px; background:linear-gradient(135deg,#475569,#64748b); border-radius:8px; display:flex; align-items:center; justify-content:center; color:#fff; font-size:.9rem; flex-shrink:0; }
        .hero { margin-top:68px; min-height:290px; color:#fff; position:relative; overflow:hidden; background:linear-gradient(135deg,#1e293b 0%, #475569 50%, #64748b 100%); }
        .hero::before { content:""; position:absolute; inset:0; background-image:linear-gradient(rgba(255,255,255,.08) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.08) 1px, transparent 1px); background-size:60px 60px; opacity:.7; }
        .geometric-shapes { position:absolute; inset:0; pointer-events:none; }
        .geo-shape { position:absolute; opacity:.16; background:linear-gradient(45deg, #fff, transparent); animation:float 12s ease-in-out infinite; }
        .geo-shape:nth-child(1){ top:14%; left:9%; width:86px; height:86px; clip-path:polygon(30% 0%,70% 0%,100% 30%,100% 70%,70% 100%,30% 100%,0 70%,0 30%);}
        .geo-shape:nth-child(2){ top:68%; left:17%; width:58px; height:58px; clip-path:polygon(50% 0,100% 50%,50% 100%,0 50%); animation-delay:-4s;}
        .geo-shape:nth-child(3){ top:22%; right:14%; width:96px; height:96px; border-radius:50%; animation-delay:-8s;}
        .geo-shape:nth-child(4){ top:68%; right:22%; width:70px; height:70px; clip-path:polygon(50% 0,0 100%,100% 100%); animation-delay:-2s;}
        @keyframes float { 50% { transform:translateY(-12px) rotate(8deg); } }
        .hero-content { position:relative; z-index:1; width:min(1280px, calc(100% - 32px)); margin:0 auto; padding:44px 0 38px; }
        .hero h1 { margin:0 0 10px; font-size:clamp(2rem,4vw,2.7rem); letter-spacing:-.5px; }
        .hero p { margin:0; max-width:72ch; font-size:1rem; opacity:.95; }
        .wrap { width:min(1400px, calc(100% - 32px)); margin:20px auto 34px; position:relative; z-index:2; }
        .top { position:sticky; top:84px; z-index:900; display:flex; gap:10px; align-items:center; margin-bottom:14px; background:#fff; border:1px solid var(--line); border-radius:14px; padding:10px 12px; box-shadow:0 12px 22px rgba(2,6,23,.06);}
        .btn { border:1px solid rgba(71,85,105,.25); background:#fff; color:#1e293b; padding:9px 14px; border-radius:999px; cursor:pointer; text-decoration:none; font-weight:600; }
        .btn:hover { border-color:#64748b; background:#f8fafc; }
        .grid { display:grid; grid-template-columns:320px 1fr; gap:14px; }
        .panel { border:1px solid var(--line); border-radius:16px; background:#fff; padding:16px; box-shadow:0 12px 28px rgba(15,23,42,.07); }
        .muted { color:#64748b; }
        h1 { margin:0 0 2px; font-size:24px; color:#0f172a; }
        h2 { margin:0 0 10px; font-size:17px; color:#0f172a; }
        h3 { margin:11px 0 8px; font-size:14px; color:#1e293b; font-weight:700; }
        input[type="text"], input[type="number"], select, textarea {
          width:100%; border:1px solid rgba(71,85,105,.28); border-radius:9px; background:#fff; color:#0f172a; padding:8px 9px;
        }
        textarea { min-height:62px; resize:vertical; }
        .row { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:8px; }
        .row3 { display:grid; grid-template-columns:1.1fr .6fr 1.3fr auto; gap:8px; align-items:end; margin-bottom:8px; }
        .list { display:grid; gap:8px; max-height:560px; overflow:auto; }
        .job-item { border:1px solid rgba(71,85,105,.24); border-radius:10px; padding:10px; cursor:pointer; background:#fff; transition:all .2s ease; }
        .job-item:hover { transform:translateY(-1px); box-shadow:0 8px 18px rgba(15,23,42,.08); }
        .job-item.active { border-color:#64748b; background:#f8fafc; }
        .source-card { border:1px solid rgba(71,85,105,.22); border-radius:12px; padding:11px; margin-bottom:10px; background:#fbfdff; }
        table { width:100%; border-collapse:collapse; margin-top:6px; }
        th, td { border-bottom:1px solid rgba(71,85,105,.16); padding:6px; text-align:left; vertical-align:top; font-size:13px; }
        th { color:#334155; font-weight:700; }
        code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background:#eef2f7; border:1px solid rgba(71,85,105,.2); border-radius:6px; padding:0 5px; color:#1e293b; }
        .status { margin-left:auto; font-size:13px; color:#475569; }
        .rank-grid { margin-top:8px; overflow-x:auto; }
        .rank-grid-inner { display:grid; grid-template-columns:repeat(auto-fit, minmax(200px,1fr)); gap:10px; min-width:760px; }
        .rank-col { border:1px solid rgba(71,85,105,.18); border-radius:12px; background:#fff; padding:10px; }
        .rank-col-title { font-weight:700; color:#0f172a; margin-bottom:8px; }
        .rank-list { list-style:none; padding:0; margin:0; display:grid; gap:8px; min-height:44px; }
        .rank-item { border:1px solid rgba(71,85,105,.25); border-radius:10px; padding:7px 9px; background:#f8fafc; cursor:grab; }
        .rank-item.dragging { opacity:.55; }
        .rank-item strong { font-size:12px; color:#334155; margin-right:6px; }
        @media (max-width: 1040px) { .grid { grid-template-columns:1fr; } .row3 { grid-template-columns:1fr; } }
      </style>
    </head>
    <body>
      <nav class="navbar">
        <div class="nav-container">
          <a class="logo" href="/">
            <div class="logo-icon">◆</div>
            <span>Server Matrix Builder</span>
          </a>
        </div>
      </nav>
      <section class="hero">
        <div class="geometric-shapes">
          <div class="geo-shape"></div>
          <div class="geo-shape"></div>
          <div class="geo-shape"></div>
          <div class="geo-shape"></div>
        </div>
        <div class="hero-content">
          <h1>Configure Server Inventory Matrix Jobs</h1>
          <p>Use this workspace to refine sources, column mappings, and trust ranking logic before publishing to custom configuration.</p>
        </div>
      </section>
      <div class="wrap">
        <div class="top">
          <a class="btn" href="/">Back to Home</a>
          <button class="btn" id="addJobBtn">Add Job</button>
          <button class="btn" id="saveBtn">Save Configuration</button>
          <button class="btn" id="discardBtn">Discard Changes</button>
          <div class="status" id="status">Loading...</div>
        </div>
        <div class="grid">
          <section class="panel">
            <h1>Server Inventory Matrix</h1>
            <div class="muted">Edit jobs, sources, mapping, attributes, and trust rankings.</div>
            <h3 style="margin-top:14px;">Jobs</h3>
            <div class="list" id="jobList"></div>
          </section>
          <section class="panel">
            <h2>Job Details</h2>
            <div class="muted" style="margin-bottom:8px;">Editing <code>server_inventory_matrix_vtx.yaml</code></div>
            <div id="editor"></div>
          </section>
        </div>
      </div>
      <script>
        let state = null;
        let initialState = null;
        let selectedInventoryIndex = 0;
        let isDirty = false;
        const headerCache = {};

        function text(v) { return v == null ? "" : String(v); }
        function esc(v) { return text(v).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"','&quot;'); }
        function cloneObj(v) { return JSON.parse(JSON.stringify(v)); }
        function setStatus(msg) { document.getElementById("status").textContent = msg; }
        function markDirty() { isDirty = true; setStatus("Changes pending"); }
        function markClean(msg = "Saved") { isDirty = false; setStatus(msg); }
        function splitLines(v) { return text(v).split("\\n").map(x => x.trim()).filter(Boolean); }
        function joinLines(arr) { return (Array.isArray(arr) ? arr : []).map(text).join("\\n"); }

        function ensurePayload() {
          state.config = state.config || {};
          state.config.payload = state.config.payload || {};
          state.config.payload.inventories = Array.isArray(state.config.payload.inventories) ? state.config.payload.inventories : [];
          const existingJobs = Array.isArray(state.config.payload.jobs) ? state.config.payload.jobs : [];
          const byInventory = {};
          for (const j of existingJobs) {
            const iid = text((j || {}).inventory_id);
            if (iid && !byInventory[iid]) byInventory[iid] = j;
          }
          // Enforce 1:1 inventory->job unit.
          state.config.payload.jobs = state.config.payload.inventories.map((inv) => {
            const iid = text((inv || {}).id);
            return byInventory[iid] || newExecJob(iid);
          });
        }

        function defaultAttributes() {
          return ["Hostname","IP Address","OS","Environment","CPU","Memory","Storage"];
        }

        function defaultJobOutputs() {
          return {
            inventory_table: {
              path: "var/masterdata/unified_server_inventory.csv",
              include_presence_flags: false,
              include_value_provenance: false,
              include_ranked_values: false,
              ranked_values_delimiter: ";",
              fields: defaultAttributes()
            },
            presence_matrix_reports: [
              {
                id: "hostname_presence",
                attribute: "Hostname",
                normalize_as: "hostname",
                source_of_truth: "consolidated_server_view",
                display_attributes: ["IP Address"],
                path: "var/analysis/presence_hostname_matrix.csv",
                html_path: "var/analysis/presence_hostname_matrix.html"
              }
            ]
          };
        }

        function newInventory() {
          const attrs = defaultAttributes();
          const rank = {};
          for (const a of attrs) rank[a] = [];
          return {
            id: "New_Server_Inventory",
            enabled: true,
            attributes: attrs.slice(),
            match: { condition: "any", keys: [{name:"Hostname",type:"hostname"},{name:"IP Address",type:"ip_address"}] },
            trust_ranking: rank,
            sources: []
          };
        }

        function newExecJob(inventoryId) {
          return {
            id: "server_inventory_matrix",
            enabled: true,
            inventory_id: text(inventoryId),
            outputs: defaultJobOutputs()
          };
        }

        function syncInventoryShape(inv) {
          inv.attributes = Array.isArray(inv.attributes) ? inv.attributes : defaultAttributes();
          inv.sources = Array.isArray(inv.sources) ? inv.sources : [];
          inv.trust_ranking = inv.trust_ranking || {};
          for (const attr of inv.attributes) {
            if (!Array.isArray(inv.trust_ranking[attr])) inv.trust_ranking[attr] = [];
          }
          for (const source of inv.sources) {
            source.map = source.map || {};
            for (const attr of inv.attributes) {
              if (!(attr in source.map)) source.map[attr] = null;
            }
          }
          inv.match = inv.match || { condition: "any", keys: [] };
          if (!Array.isArray(inv.match.keys)) inv.match.keys = [];
        }

        function syncExecJobShape(job) {
          job.id = text(job.id) || "server_inventory_matrix";
          if (job.enabled === undefined) job.enabled = true;
          job.inventory_id = text(job.inventory_id);
          job.outputs = job.outputs || {};
          const d = defaultJobOutputs();
          if (!job.outputs.inventory_table || typeof job.outputs.inventory_table !== "object") {
            job.outputs.inventory_table = cloneObj(d.inventory_table);
          }
          if (!Array.isArray(job.outputs.inventory_table.fields)) {
            job.outputs.inventory_table.fields = defaultAttributes();
          }
          if (!Array.isArray(job.outputs.presence_matrix_reports)) {
            job.outputs.presence_matrix_reports = cloneObj(d.presence_matrix_reports);
          }
          for (const r of job.outputs.presence_matrix_reports) {
            r.display_attributes = Array.isArray(r.display_attributes) ? r.display_attributes : [];
          }
        }

        async function fetchHeaders(path) {
          const p = text(path);
          if (!p) return [];
          if (headerCache[p]) return headerCache[p];
          const res = await fetch(`/api/config/csv-headers?path=${encodeURIComponent(p)}`);
          if (!res.ok) return [];
          const data = await res.json();
          headerCache[p] = data.headers || [];
          return headerCache[p];
        }

        function renderInventories() {
          const box = document.getElementById("jobList");
          box.innerHTML = "";
          const inventories = state.config.payload.inventories || [];
          inventories.forEach((inv, idx) => {
            const div = document.createElement("div");
            div.className = "job-item" + (idx === selectedInventoryIndex ? " active" : "");
            div.innerHTML = `<div><strong>${text(inv.id) || "(unnamed inventory)"}</strong></div>
                             <div class="muted">${inv.enabled === false ? "Disabled" : "Enabled"} • ${Array.isArray(inv.sources) ? inv.sources.length : 0} source(s)</div>`;
            div.onclick = () => { selectedInventoryIndex = idx; renderInventories(); renderEditor(); };
            box.appendChild(div);
          });
        }

        function attrTable(inv) {
          const rows = inv.attributes.map((attr, idx) => `
            <tr>
              <td><input type="text" data-role="attr-name" data-idx="${idx}" value="${attr.replaceAll('"','&quot;')}" /></td>
              <td><button class="btn" data-role="remove-attr" data-idx="${idx}">Remove</button></td>
            </tr>`).join("");
          return `
            <h3>Attributes</h3>
            <table><thead><tr><th>Field</th><th></th></tr></thead><tbody>${rows}</tbody></table>
            <div class="row" style="margin-top:8px;"><input id="newAttr" type="text" placeholder="New attribute name" /><button class="btn" id="addAttrBtn">Add Attribute</button></div>
          `;
        }

        function rankingColumns(inv) {
          const sourceIds = inv.sources.map(s => text(s.id)).filter(Boolean);
          const cols = inv.attributes.map((attr) => {
            const fromYaml = Array.isArray(inv.trust_ranking[attr]) ? inv.trust_ranking[attr].map(text).filter(Boolean) : [];
            const ordered = [];
            const seen = new Set();
            for (const sid of fromYaml) { if (!seen.has(sid)) { seen.add(sid); ordered.push(sid); } }
            for (const sid of sourceIds) { if (!seen.has(sid)) { seen.add(sid); ordered.push(sid); } }
            if (!ordered.length) {
              ordered.push("(no sources)");
            }
            return `
              <div class="rank-col">
                <div class="rank-col-title">${esc(attr)}</div>
                <ol class="rank-list" data-role="rank-list" data-attr="${esc(attr)}">
                  ${ordered.map((sid, i) => `<li class="rank-item" draggable="true" data-source-id="${esc(sid)}"><strong>#${i + 1}</strong>${esc(sid)}</li>`).join("")}
                </ol>
              </div>
            `;
          }).join("");
          return `
            <h3>Trust Ranking</h3>
            <div class="muted">Drag source IDs top-to-bottom under each attribute. Top is highest priority.</div>
            <div class="rank-grid">
              <div class="rank-grid-inner">${cols}</div>
            </div>
          `;
        }

        function renumberRankList(list) {
          const items = [...list.querySelectorAll(".rank-item")];
          items.forEach((item, idx) => {
            const tag = item.querySelector("strong");
            if (tag) tag.textContent = `#${idx + 1}`;
          });
        }

        async function sourceCards(job) {
          const cards = [];
          for (let i = 0; i < job.sources.length; i++) {
            const s = job.sources[i];
            const headers = await fetchHeaders(s.path);
            const datalistId = `headers_${i}`;
            const mapRows = job.attributes.map(attr => {
              const val = s.map && s.map[attr] != null ? String(s.map[attr]) : "";
              return `<tr><td>${attr}</td><td><input type="text" list="${datalistId}" data-role="map" data-source="${i}" data-attr="${attr}" value="${val.replaceAll('"','&quot;')}" placeholder="column header" /></td></tr>`;
            }).join("");
            cards.push(`
              <div class="source-card">
                <div class="row3">
                  <div><label>Source ID</label><input type="text" data-role="source-id" data-source="${i}" value="${text(s.id).replaceAll('"','&quot;')}" /></div>
                  <div><label>Priority</label><input type="number" data-role="source-priority" data-source="${i}" value="${Number(s.priority || 0)}" /></div>
                  <div><label>Path (CSV)</label><input type="text" data-role="source-path" data-source="${i}" value="${text(s.path).replaceAll('"','&quot;')}" /></div>
                  <div><button class="btn" data-role="remove-source" data-source="${i}">Remove</button></div>
                </div>
                <div class="row">
                  <label><input type="checkbox" data-role="source-enabled" data-source="${i}" ${s.enabled === false ? "" : "checked"} /> Enabled</label>
                  <span class="muted">Headers detected: ${headers.length}</span>
                </div>
                <datalist id="${datalistId}">${headers.map(h => `<option value="${String(h).replaceAll('"','&quot;')}"></option>`).join("")}</datalist>
                <table><thead><tr><th>Attribute</th><th>Mapped Column</th></tr></thead><tbody>${mapRows}</tbody></table>
              </div>
            `);
          }
          return cards.join("");
        }

        function execJobEditor(inv) {
          const jobs = state.config.payload.jobs || [];
          if (!jobs.length) return `<div class="muted">No execution jobs configured.</div>`;
          const idx = Math.max(0, Math.min(selectedInventoryIndex, jobs.length - 1));
          const job = jobs[idx];
          syncExecJobShape(job);
          job.inventory_id = text(inv.id);

          const it = job.outputs.inventory_table || {};
          const pmReports = job.outputs.presence_matrix_reports || [];
          const report = pmReports[0] || {
            id: "hostname_presence",
            attribute: "Hostname",
            normalize_as: "hostname",
            source_of_truth: "consolidated_server_view",
            display_attributes: ["IP Address"],
            path: "var/analysis/presence_hostname_matrix.csv",
            html_path: "var/analysis/presence_hostname_matrix.html"
          };
          job.outputs.presence_matrix_reports = [report];
          const pmHtml = `
            <div class="source-card">
              <div class="row">
                <div><label>Presence Report ID</label><input type="text" data-role="pm-id" data-pm="0" value="${esc(report.id)}" /></div>
                <div><label>Attribute</label><input type="text" data-role="pm-attr" data-pm="0" value="${esc(report.attribute)}" /></div>
              </div>
              <div class="row">
                <div><label>Normalize As</label><input type="text" data-role="pm-norm" data-pm="0" value="${esc(report.normalize_as)}" /></div>
                <div><label>Source Of Truth</label><input type="text" data-role="pm-sot" data-pm="0" value="${esc(report.source_of_truth)}" /></div>
              </div>
              <div class="row">
                <div><label>CSV Path</label><input type="text" data-role="pm-path" data-pm="0" value="${esc(report.path)}" /></div>
                <div><label>HTML Path</label><input type="text" data-role="pm-html" data-pm="0" value="${esc(report.html_path)}" /></div>
              </div>
              <div class="row">
                <div><label>Display Attributes (comma-separated)</label><input type="text" data-role="pm-disp" data-pm="0" value="${esc((report.display_attributes || []).join(', '))}" /></div>
                <div class="muted" style="display:flex;align-items:end;">One fixed presence report per inventory.</div>
              </div>
            </div>
          `;

          return `
            <h3 style="margin-top:16px;">Execution Jobs</h3>
            <div class="source-card">
              <div class="row">
                <div><label>Job ID</label><input id="execJobId" type="text" value="${esc(job.id)}" /></div>
                <div style="display:flex;align-items:end;gap:12px;">
                  <label><input id="execJobEnabled" type="checkbox" ${job.enabled === false ? "" : "checked"} /> Enabled</label>
                  <div class="muted">Linked to this inventory</div>
                </div>
              </div>
              <div class="row">
                <div><label>Inventory</label><input id="execInventoryId" type="text" value="${esc(job.inventory_id)}" readonly /></div>
                <div><label>Inventory Table Path</label><input id="outInventoryPath" type="text" value="${esc(it.path)}" /></div>
              </div>
              <div class="row">
                <label><input id="outPresenceFlags" type="checkbox" ${it.include_presence_flags ? "checked" : ""} /> include_presence_flags</label>
                <label><input id="outValueProv" type="checkbox" ${it.include_value_provenance ? "checked" : ""} /> include_value_provenance</label>
              </div>
              <div class="row">
                <label><input id="outRankedVals" type="checkbox" ${it.include_ranked_values ? "checked" : ""} /> include_ranked_values</label>
                <div><label>ranked_values_delimiter</label><input id="outRankDelim" type="text" value="${esc(it.ranked_values_delimiter)}" /></div>
              </div>
              <div class="row">
                <div><label>Inventory Fields (one per line)</label><textarea id="outFields">${esc(joinLines(it.fields))}</textarea></div>
                <div></div>
              </div>
              <h3>Presence Matrix Reports</h3>
              <div id="pmWrap">${pmHtml}</div>
            </div>
          `;
        }

        async function renderEditor() {
          ensurePayload();
          const inventories = state.config.payload.inventories;
          if (!inventories.length) {
            inventories.push(newInventory());
            selectedInventoryIndex = 0;
          }
          selectedInventoryIndex = Math.max(0, Math.min(selectedInventoryIndex, inventories.length - 1));
          const inv = inventories[selectedInventoryIndex];
          syncInventoryShape(inv);
          ensurePayload();

          const editor = document.getElementById("editor");
          editor.innerHTML = `
            <div class="row">
              <div><label>Inventory ID</label><input id="jobId" type="text" value="${text(inv.id).replaceAll('"','&quot;')}" /></div>
              <div style="display:flex;align-items:end;gap:12px;"><label><input id="jobEnabled" type="checkbox" ${inv.enabled === false ? "" : "checked"} /> Enabled</label><button class="btn" id="deleteJobBtn">Delete Job</button></div>
            </div>
            ${attrTable(inv)}
            <h3>Sources</h3>
            <div id="sourcesWrap">${await sourceCards(inv)}</div>
            <button class="btn" id="addSourceBtn">Add Source</button>
            ${rankingColumns(inv)}
            ${execJobEditor(inv)}
          `;

          bindEditorEvents();
        }

        function bindEditorEvents() {
          const inv = state.config.payload.inventories[selectedInventoryIndex];
          const jobs = state.config.payload.jobs;
          const execJob = jobs[Math.max(0, Math.min(selectedInventoryIndex, Math.max(0, jobs.length - 1)))];
          syncExecJobShape(execJob);

          document.getElementById("jobId").oninput = (e) => { inv.id = e.target.value; ensurePayload(); renderInventories(); renderEditor(); markDirty(); };
          document.getElementById("jobEnabled").onchange = (e) => { inv.enabled = !!e.target.checked; renderInventories(); markDirty(); };
          document.getElementById("deleteJobBtn").onclick = () => {
            if (state.config.payload.inventories.length <= 1) return;
            state.config.payload.inventories.splice(selectedInventoryIndex, 1);
            selectedInventoryIndex = Math.max(0, selectedInventoryIndex - 1);
            renderInventories();
            renderEditor();
            markDirty();
          };
          document.getElementById("addAttrBtn").onclick = () => {
            const el = document.getElementById("newAttr");
            const name = (el.value || "").trim();
            if (!name || inv.attributes.includes(name)) return;
            inv.attributes.push(name);
            inv.trust_ranking[name] = [];
            for (const s of inv.sources) {
              s.map = s.map || {};
              s.map[name] = null;
            }
            el.value = "";
            renderEditor();
            markDirty();
          };
          document.getElementById("addSourceBtn").onclick = () => {
            const sid = `source_${inv.sources.length + 1}`;
            const map = {};
            for (const a of inv.attributes) map[a] = null;
            inv.sources.push({ id: sid, enabled: true, priority: inv.sources.length + 1, path: "", format: "csv", map, filters: [] });
            renderInventories();
            renderEditor();
            markDirty();
          };

          for (const el of document.querySelectorAll('[data-role="remove-attr"]')) {
            el.onclick = () => {
              const idx = Number(el.dataset.idx);
              const name = inv.attributes[idx];
              inv.attributes.splice(idx, 1);
              delete inv.trust_ranking[name];
              for (const s of inv.sources) { if (s.map) delete s.map[name]; }
              renderEditor();
              markDirty();
            };
          }
          for (const el of document.querySelectorAll('[data-role="attr-name"]')) {
            el.onchange = () => {
              const idx = Number(el.dataset.idx);
              const old = inv.attributes[idx];
              const next = (el.value || "").trim();
              if (!next || next === old) return;
              inv.attributes[idx] = next;
              if (inv.trust_ranking[old] !== undefined) {
                inv.trust_ranking[next] = inv.trust_ranking[old];
                delete inv.trust_ranking[old];
              } else if (!Array.isArray(inv.trust_ranking[next])) {
                inv.trust_ranking[next] = [];
              }
              for (const s of inv.sources) {
                if (s.map && s.map[old] !== undefined) {
                  s.map[next] = s.map[old];
                  delete s.map[old];
                }
              }
              renderEditor();
              markDirty();
            };
          }
          for (const el of document.querySelectorAll('[data-role="source-id"]')) {
            el.onchange = () => { inv.sources[Number(el.dataset.source)].id = el.value.trim(); renderInventories(); renderEditor(); markDirty(); };
          }
          for (const el of document.querySelectorAll('[data-role="source-priority"]')) {
            el.onchange = () => { inv.sources[Number(el.dataset.source)].priority = Number(el.value || 0); markDirty(); };
          }
          for (const el of document.querySelectorAll('[data-role="source-enabled"]')) {
            el.onchange = () => { inv.sources[Number(el.dataset.source)].enabled = !!el.checked; markDirty(); };
          }
          for (const el of document.querySelectorAll('[data-role="source-path"]')) {
            el.onchange = async () => { inv.sources[Number(el.dataset.source)].path = el.value.trim(); await renderEditor(); markDirty(); };
          }
          for (const el of document.querySelectorAll('[data-role="remove-source"]')) {
            el.onclick = () => { inv.sources.splice(Number(el.dataset.source), 1); renderInventories(); renderEditor(); markDirty(); };
          }
          for (const el of document.querySelectorAll('[data-role="map"]')) {
            el.onchange = () => {
              const srcIdx = Number(el.dataset.source);
              const attr = el.dataset.attr;
              inv.sources[srcIdx].map = inv.sources[srcIdx].map || {};
              const v = (el.value || "").trim();
              inv.sources[srcIdx].map[attr] = v ? v : null;
              markDirty();
            };
          }
          bindRankDnD(inv);

          document.getElementById("execJobId").oninput = (e) => { execJob.id = e.target.value; markDirty(); };
          document.getElementById("execJobEnabled").onchange = (e) => { execJob.enabled = !!e.target.checked; markDirty(); };
          document.getElementById("outInventoryPath").onchange = (e) => { execJob.outputs.inventory_table.path = e.target.value.trim(); markDirty(); };
          document.getElementById("outPresenceFlags").onchange = (e) => { execJob.outputs.inventory_table.include_presence_flags = !!e.target.checked; markDirty(); };
          document.getElementById("outValueProv").onchange = (e) => { execJob.outputs.inventory_table.include_value_provenance = !!e.target.checked; markDirty(); };
          document.getElementById("outRankedVals").onchange = (e) => { execJob.outputs.inventory_table.include_ranked_values = !!e.target.checked; markDirty(); };
          document.getElementById("outRankDelim").onchange = (e) => { execJob.outputs.inventory_table.ranked_values_delimiter = e.target.value; markDirty(); };
          document.getElementById("outFields").onchange = (e) => { execJob.outputs.inventory_table.fields = splitLines(e.target.value); markDirty(); };

          for (const el of document.querySelectorAll('[data-role="pm-id"]')) {
            el.onchange = () => { execJob.outputs.presence_matrix_reports[Number(el.dataset.pm)].id = el.value.trim(); markDirty(); };
          }
          for (const el of document.querySelectorAll('[data-role="pm-attr"]')) {
            el.onchange = () => { execJob.outputs.presence_matrix_reports[Number(el.dataset.pm)].attribute = el.value.trim(); markDirty(); };
          }
          for (const el of document.querySelectorAll('[data-role="pm-norm"]')) {
            el.onchange = () => { execJob.outputs.presence_matrix_reports[Number(el.dataset.pm)].normalize_as = el.value.trim(); markDirty(); };
          }
          for (const el of document.querySelectorAll('[data-role="pm-sot"]')) {
            el.onchange = () => { execJob.outputs.presence_matrix_reports[Number(el.dataset.pm)].source_of_truth = el.value.trim(); markDirty(); };
          }
          for (const el of document.querySelectorAll('[data-role="pm-path"]')) {
            el.onchange = () => { execJob.outputs.presence_matrix_reports[Number(el.dataset.pm)].path = el.value.trim(); markDirty(); };
          }
          for (const el of document.querySelectorAll('[data-role="pm-html"]')) {
            el.onchange = () => { execJob.outputs.presence_matrix_reports[Number(el.dataset.pm)].html_path = el.value.trim(); markDirty(); };
          }
          for (const el of document.querySelectorAll('[data-role="pm-disp"]')) {
            el.onchange = () => {
              execJob.outputs.presence_matrix_reports[Number(el.dataset.pm)].display_attributes = text(el.value).split(",").map(x => x.trim()).filter(Boolean);
              markDirty();
            };
          }
        }

        function syncRankingFromDom(job) {
          for (const list of document.querySelectorAll('[data-role="rank-list"]')) {
            const attr = list.dataset.attr;
            const rank = [];
            for (const li of list.querySelectorAll('.rank-item')) {
              const sid = text(li.dataset.sourceId);
              if (sid && sid !== "(no sources)") rank.push(sid);
            }
            job.trust_ranking[attr] = rank;
          }
        }

        function bindRankDnD(job) {
          let dragged = null;
          for (const item of document.querySelectorAll('.rank-item')) {
            item.addEventListener('dragstart', () => { dragged = item; item.classList.add('dragging'); });
            item.addEventListener('dragend', () => {
              item.classList.remove('dragging');
              dragged = null;
              syncRankingFromDom(job);
              for (const list of document.querySelectorAll('[data-role="rank-list"]')) renumberRankList(list);
              markDirty();
            });
          }
          for (const list of document.querySelectorAll('[data-role="rank-list"]')) {
            renumberRankList(list);
            list.addEventListener('dragover', (e) => {
              e.preventDefault();
              if (!dragged || dragged.parentElement !== list) return;
              const after = [...list.querySelectorAll('.rank-item:not(.dragging)')].find((el) => {
                const box = el.getBoundingClientRect();
                return e.clientY < box.top + box.height / 2;
              });
              if (!after) list.appendChild(dragged); else list.insertBefore(dragged, after);
              renumberRankList(list);
            });
          }
        }

        async function save() {
          ensurePayload();
          const status = document.getElementById("status");
          status.textContent = "Saving...";
          const res = await fetch("/api/config/server-inventory-matrix", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(state)
          });
          const data = await res.json();
          if (!res.ok) {
            status.textContent = `Save failed: ${data.detail || res.status}`;
            return;
          }
          initialState = cloneObj(state);
          markClean("Saved");
        }

        async function init() {
          const res = await fetch("/api/config/server-inventory-matrix");
          if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${res.status}`);
          }
          const data = await res.json();
          state = data;
          initialState = cloneObj(data);
          ensurePayload();
          markClean("Ready");
          document.getElementById("addJobBtn").onclick = () => {
            state.config.payload.inventories.push(newInventory());
            selectedInventoryIndex = state.config.payload.inventories.length - 1;
            renderInventories();
            renderEditor();
            markDirty();
          };
          document.getElementById("saveBtn").onclick = save;
          document.getElementById("discardBtn").onclick = async () => {
            state = cloneObj(initialState);
            ensurePayload();
            const invs = state.config.payload.inventories || [];
            selectedInventoryIndex = Math.max(0, Math.min(selectedInventoryIndex, Math.max(0, invs.length - 1)));
            renderInventories();
            await renderEditor();
            markClean("Discarded");
          };
          renderInventories();
          await renderEditor();
        }

        init().catch((err) => {
          document.getElementById("status").textContent = `Failed to load: ${err}`;
        });
      </script>
    </body>
    </html>
    """
    return HTMLResponse(html)


@app.get("/api/config/server-inventory-matrix", response_class=JSONResponse)
async def get_server_inventory_matrix_config(request: Request):
    redirect = require_login(request)
    if redirect:
        raise HTTPException(status_code=401, detail="Login required")

    custom_path = get_server_inventory_custom_yaml()
    source_path = get_server_inventory_source_yaml()
    load_path = custom_path if custom_path.exists() else source_path
    if not load_path.exists():
        raise HTTPException(
            status_code=404,
            detail="No server inventory matrix configuration found in usr/config/custom or usr/config/auto_v2",
        )

    data = load_yaml_dict(load_path)
    data = normalize_server_inventory_doc(data)

    return JSONResponse(
        {
            "loaded_from": str(load_path),
            "save_path": str(custom_path),
            **data,
        }
    )


@app.post("/api/config/server-inventory-matrix", response_class=JSONResponse)
async def save_server_inventory_matrix_config(request: Request):
    redirect = require_login(request)
    if redirect:
        raise HTTPException(status_code=401, detail="Login required")

    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected JSON object payload")
    payload = normalize_server_inventory_doc(payload)
    payload.pop("loaded_from", None)
    payload.pop("save_path", None)

    cfg = payload.get("config")
    if not isinstance(cfg, dict):
        raise HTTPException(status_code=400, detail="Missing config object")

    pdata = cfg.get("payload") or {}
    inventories = (pdata.get("inventories") or [])
    if not isinstance(inventories, list):
        raise HTTPException(status_code=400, detail="config.payload.inventories must be a list")
    jobs = (pdata.get("jobs") or [])
    if not isinstance(jobs, list):
        raise HTTPException(status_code=400, detail="config.payload.jobs must be a list")

    inv_ids = {str((i or {}).get("id") or "").strip() for i in inventories if isinstance(i, dict)}
    for j in jobs:
        if not isinstance(j, dict):
            raise HTTPException(status_code=400, detail="config.payload.jobs entries must be objects")
        inventory_id = str(j.get("inventory_id") or "").strip()
        if not inventory_id:
            raise HTTPException(status_code=400, detail="Each job requires inventory_id")
        if inventory_id not in inv_ids:
            raise HTTPException(status_code=400, detail=f"Job inventory_id '{inventory_id}' does not match any inventory id")
        if not isinstance(j.get("outputs"), dict):
            raise HTTPException(status_code=400, detail="Each job requires outputs object")

    save_path = get_server_inventory_custom_yaml()
    save_yaml_dict(save_path, payload)
    log("info", f"Saved server inventory matrix config to {save_path}")
    return JSONResponse({"status": "ok", "path": str(save_path), "jobs": len(jobs)})


@app.get("/config/table-aggregator", response_class=HTMLResponse)
async def table_aggregator_editor(request: Request) -> HTMLResponse:
    redirect = require_login(request)
    if redirect:
        return redirect

    custom_path = get_table_aggregator_custom_yaml()
    source_path = get_table_aggregator_source_yaml()
    if not custom_path.exists() and not source_path.exists():
        raise HTTPException(
            status_code=404,
            detail="No table aggregator configuration found in usr/config/custom or usr/config/auto_v2",
        )

    html = """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>Table Aggregator Editor</title>
      <style>
        :root { --ink:#1a202c; --ink-soft:#475569; --line:rgba(71,85,105,.18); --surface:#ffffff; --accent:#334155; }
        * { box-sizing:border-box; }
        body { margin:0; font:14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color:var(--ink); background:#f8fafc; }
        .navbar { position:fixed; top:0; width:100%; background:#fff; padding:1rem 5%; z-index:1000; border-bottom:1px solid rgba(71,85,105,.1); }
        .nav-container { max-width:1280px; margin:0 auto; display:flex; align-items:center; justify-content:space-between; gap:14px; }
        .logo { font-size:1.2rem; font-weight:700; display:flex; align-items:center; gap:.7rem; color:var(--ink); text-decoration:none; }
        .logo-icon { width:28px; height:28px; background:linear-gradient(135deg,#475569,#64748b); border-radius:8px; display:flex; align-items:center; justify-content:center; color:#fff; font-size:.9rem; flex-shrink:0; }
        .hero { margin-top:68px; min-height:290px; color:#fff; position:relative; overflow:hidden; background:linear-gradient(135deg,#1e293b 0%, #475569 50%, #64748b 100%); }
        .hero::before { content:""; position:absolute; inset:0; background-image:linear-gradient(rgba(255,255,255,.08) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.08) 1px, transparent 1px); background-size:60px 60px; opacity:.7; }
        .geometric-shapes { position:absolute; inset:0; pointer-events:none; }
        .geo-shape { position:absolute; opacity:.16; background:linear-gradient(45deg, #fff, transparent); animation:float 12s ease-in-out infinite; }
        .geo-shape:nth-child(1){ top:14%; left:9%; width:86px; height:86px; clip-path:polygon(30% 0%,70% 0%,100% 30%,100% 70%,70% 100%,30% 100%,0 70%,0 30%);}
        .geo-shape:nth-child(2){ top:68%; left:17%; width:58px; height:58px; clip-path:polygon(50% 0,100% 50%,50% 100%,0 50%); animation-delay:-4s;}
        .geo-shape:nth-child(3){ top:22%; right:14%; width:96px; height:96px; border-radius:50%; animation-delay:-8s;}
        .geo-shape:nth-child(4){ top:68%; right:22%; width:70px; height:70px; clip-path:polygon(50% 0,0 100%,100% 100%); animation-delay:-2s;}
        @keyframes float { 50% { transform:translateY(-12px) rotate(8deg); } }
        .hero-content { position:relative; z-index:1; width:min(1280px, calc(100% - 32px)); margin:0 auto; padding:44px 0 38px; }
        .hero h1 { margin:0 0 10px; font-size:clamp(2rem,4vw,2.7rem); letter-spacing:-.5px; }
        .hero p { margin:0; max-width:76ch; font-size:1rem; opacity:.95; }
        .wrap { width:min(1420px, calc(100% - 32px)); margin:20px auto 34px; position:relative; z-index:2; }
        .top { position:sticky; top:84px; z-index:900; display:flex; gap:10px; align-items:center; margin-bottom:14px; background:#fff; border:1px solid var(--line); border-radius:14px; padding:10px 12px; box-shadow:0 12px 22px rgba(2,6,23,.06);}
        .btn { border:1px solid rgba(71,85,105,.25); background:#fff; color:#1e293b; padding:9px 14px; border-radius:999px; cursor:pointer; text-decoration:none; font-weight:600; }
        .btn:hover { border-color:#64748b; background:#f8fafc; }
        .grid { display:grid; grid-template-columns:320px 1fr; gap:14px; }
        .panel { border:1px solid var(--line); border-radius:16px; background:#fff; padding:16px; box-shadow:0 12px 28px rgba(15,23,42,.07); }
        .muted { color:#64748b; }
        h1 { margin:0 0 2px; font-size:24px; color:#0f172a; }
        h2 { margin:0 0 10px; font-size:17px; color:#0f172a; }
        h3 { margin:11px 0 8px; font-size:14px; color:#1e293b; font-weight:700; }
        h4 { margin:10px 0 8px; font-size:13px; color:#0f172a; font-weight:700; }
        input[type="text"], input[type="number"], select, textarea {
          width:100%; border:1px solid rgba(71,85,105,.28); border-radius:9px; background:#fff; color:#0f172a; padding:8px 9px;
        }
        textarea { min-height:72px; resize:vertical; }
        .row { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:8px; }
        .row3 { display:grid; grid-template-columns:1fr 1fr 1fr; gap:10px; margin-bottom:8px; }
        .list { display:grid; gap:8px; max-height:560px; overflow:auto; }
        .job-item, .txn-item { border:1px solid rgba(71,85,105,.24); border-radius:10px; padding:10px; cursor:pointer; background:#fff; transition:all .2s ease; }
        .job-item:hover, .txn-item:hover { transform:translateY(-1px); box-shadow:0 8px 18px rgba(15,23,42,.08); }
        .job-item.active, .txn-item.active { border-color:#64748b; background:#f8fafc; }
        .txn-item { cursor:grab; }
        .txn-item.dragging { opacity:.55; }
        .txn-meta { display:flex; flex-wrap:wrap; gap:6px; margin-top:6px; }
        .pill { display:inline-flex; align-items:center; border:1px solid rgba(71,85,105,.22); background:#f8fafc; color:#334155; border-radius:999px; padding:3px 8px; font-size:12px; }
        .hint { display:inline-flex; align-items:center; justify-content:center; width:16px; height:16px; border-radius:50%; border:1px solid rgba(71,85,105,.35); font-size:11px; margin-left:6px; color:#475569; cursor:help; }
        .status { margin-left:auto; font-size:13px; color:#475569; }
        .txn-shell { border:1px solid rgba(71,85,105,.2); border-radius:12px; padding:12px; margin-top:10px; background:#fbfdff; }
        .txn-toolbar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:8px; }
        .field-row { display:grid; grid-template-columns:1fr 1fr auto; gap:8px; margin-top:8px; align-items:end; }
        .tx-modal-backdrop { position:fixed; inset:0; z-index:1200; background:rgba(15,23,42,.44); display:flex; align-items:center; justify-content:center; padding:24px; }
        .tx-modal-card { width:min(980px, 96vw); max-height:90vh; overflow:auto; background:#eef3fa; border:1px solid rgba(71,85,105,.35); border-radius:16px; box-shadow:0 26px 60px rgba(2,6,23,.35); padding:16px; }
        .tx-modal-head { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:10px; }
        code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background:#eef2f7; border:1px solid rgba(71,85,105,.2); border-radius:6px; padding:0 5px; color:#1e293b; }
        @media (max-width: 1040px) { .grid { grid-template-columns:1fr; } .row3 { grid-template-columns:1fr; } .row { grid-template-columns:1fr; } }
      </style>
    </head>
    <body>
      <nav class="navbar">
        <div class="nav-container">
          <a class="logo" href="/">
            <div class="logo-icon">◆</div>
            <span>Table Aggregator Builder</span>
          </a>
        </div>
      </nav>
      <section class="hero">
        <div class="geometric-shapes">
          <div class="geo-shape"></div><div class="geo-shape"></div><div class="geo-shape"></div><div class="geo-shape"></div>
        </div>
        <div class="hero-content">
          <h1>Configure Table Aggregator Jobs</h1>
          <p>Create and tune aggregation jobs by managing inputs, seed/anchor behavior, and ordered transactions. Transaction order is preserved and editable top-to-bottom.</p>
        </div>
      </section>
      <div class="wrap">
        <div class="top">
          <a class="btn" href="/">Back to Home</a>
          <button class="btn" id="addJobBtn">Add Job</button>
          <button class="btn" id="saveBtn">Save Configuration</button>
          <button class="btn" id="discardBtn">Discard Changes</button>
          <div class="status" id="status">Loading...</div>
        </div>
        <div class="grid">
          <section class="panel">
            <h1>Table Aggregator</h1>
            <div class="muted">Edit jobs and ordered transaction stacks.</div>
            <h3 style="margin-top:14px;">Jobs</h3>
            <div class="list" id="jobList"></div>
          </section>
          <section class="panel">
            <h2>Job Details</h2>
            <div class="muted" style="margin-bottom:8px;">Editing <code>table_aggregator_vtx.yaml</code></div>
            <div id="editor"></div>
          </section>
        </div>
      </div>
      <script>
        let state = null;
        let initialState = null;
        let selectedJobIndex = 0;
        let selectedTxnIndex = 0;
        let txnModalOpen = false;
        let isDirty = false;
        const headerCache = {};

        function text(v) { return v == null ? "" : String(v); }
        function esc(v) { return text(v).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"','&quot;'); }
        function cloneObj(v) { return JSON.parse(JSON.stringify(v)); }
        function setStatus(msg) { document.getElementById("status").textContent = msg; }
        function markDirty() { isDirty = true; setStatus("Changes pending"); }
        function markClean(msg = "Saved") { isDirty = false; setStatus(msg); }
        function splitLines(v) { return text(v).split("\\n").map(x => x.trim()).filter(Boolean); }
        function joinLines(arr) { return (Array.isArray(arr) ? arr : []).map(text).join("\\n"); }
        function filename(path) {
          const s = text(path).trim();
          if (!s) return "";
          return s.split(/[\\\\/]/).filter(Boolean).pop() || s;
        }
        function help(description) { return `<span class="hint" title="${esc(description)}">?</span>`; }

        async function fetchHeaders(path) {
          const p = text(path).trim();
          if (!p) return [];
          if (headerCache[p]) return headerCache[p];
          try {
            const res = await fetch(`/api/config/csv-headers?path=${encodeURIComponent(p)}`);
            if (!res.ok) return [];
            const data = await res.json();
            headerCache[p] = Array.isArray(data.headers) ? data.headers : [];
            return headerCache[p];
          } catch {
            return [];
          }
        }

        function ensurePayload() {
          state.config = state.config || {};
          state.config.payload = state.config.payload || {};
          state.config.payload.jobs = Array.isArray(state.config.payload.jobs) ? state.config.payload.jobs : [];
        }

        function newJob() {
          return {
            id: "new_table_aggregator_job",
            enabled: true,
            inputs: [],
            outputs: [],
            seed: "",
            anchor: "ID",
            delimiter: " ; ",
            transactions: []
          };
        }

        function newTxn(type = "simple_lookup") {
          if (type === "lookup_replace") {
            return { type, lookup_table: "", key: "ID", seed_field: "", lookup_field: "", output_field: "" };
          }
          if (type === "transform_tidy") {
            return { type, anchor: "", column_field: "", value_field: "", drop_fields: [], delimiter: " ; " };
          }
          return { type: "simple_lookup", lookup_table: "", key: "ID", seed_field: "", fields: [] };
        }

        function syncJobShape(job) {
          job.inputs = Array.isArray(job.inputs) ? job.inputs : [];
          job.outputs = Array.isArray(job.outputs) ? job.outputs : [];
          job.transactions = Array.isArray(job.transactions) ? job.transactions : [];
          if (!("enabled" in job)) job.enabled = true;
          if (!job.anchor) job.anchor = "ID";
          if (!job.delimiter) job.delimiter = " ; ";
        }

        function parseFieldEntry(entry) {
          const raw = text(entry).trim();
          if (!raw) return { source: "", target: "" };
          if (raw.includes("->")) {
            const [left, right] = raw.split("->", 2);
            return { source: text(left).trim(), target: text(right).trim() };
          }
          return { source: raw, target: "" };
        }

        function stringifyFieldEntry(source, target) {
          const src = text(source).trim();
          const tgt = text(target).trim();
          if (!src) return "";
          return tgt ? `${src} -> ${tgt}` : src;
        }

        function syncSimpleLookupFields(txn) {
          txn.fields = Array.isArray(txn.fields) ? txn.fields : [];
          const normalized = [];
          for (const f of txn.fields) {
            const parsed = parseFieldEntry(f);
            const line = stringifyFieldEntry(parsed.source, parsed.target);
            if (line) normalized.push(line);
          }
          txn.fields = normalized;
        }

        function renderJobs() {
          const box = document.getElementById("jobList");
          box.innerHTML = "";
          const jobs = state.config.payload.jobs || [];
          jobs.forEach((job, idx) => {
            const div = document.createElement("div");
            div.className = "job-item" + (idx === selectedJobIndex ? " active" : "");
            const txnCount = Array.isArray(job.transactions) ? job.transactions.length : 0;
            div.innerHTML = `<div><strong>${esc(job.id || "(unnamed job)")}</strong></div>
                             <div class="muted">${job.enabled === false ? "Disabled" : "Enabled"} • ${txnCount} transaction(s)</div>`;
            div.onclick = () => { selectedJobIndex = idx; selectedTxnIndex = 0; txnModalOpen = false; renderJobs(); renderEditor(); };
            box.appendChild(div);
          });
        }

        function renderTxnList(job) {
          const txns = job.transactions || [];
          if (!txns.length) return '<div class="muted">No transactions yet.</div>';
          return txns.map((t, idx) => {
            const type = text(t.type || "simple_lookup");
            const titleName = type === "transform_tidy" ? "transform_tidy" : (filename(t.lookup_table) || "(lookup table)");
            return `<div class="txn-item${idx === selectedTxnIndex ? " active" : ""}" draggable="true" data-role="txn-select" data-txn="${idx}">
                <div><strong>${esc(`${idx + 1}. ${titleName}`)}</strong></div>
                <div class="muted">${esc(type)}</div>
              </div>`;
          }).join("");
        }

        async function renderTxnModal(job) {
          const txns = job.transactions || [];
          if (!txns.length || !txnModalOpen) return "";
          selectedTxnIndex = Math.max(0, Math.min(selectedTxnIndex, txns.length - 1));
          const t = txns[selectedTxnIndex];
          const type = text(t.type || "simple_lookup");
          const txName = type === "transform_tidy" ? "transform_tidy" : (filename(t.lookup_table) || "(lookup table)");
          const lookupHeaders = await fetchHeaders(t.lookup_table);
          const seedHeaders = await fetchHeaders(job.seed);
          const lookupListId = `lookup_headers_${selectedTxnIndex}`;
          const seedListId = `seed_headers_${selectedTxnIndex}`;
          const lookupOptions = lookupHeaders.map((h) => `<option value="${esc(h)}"></option>`).join("");
          const seedOptions = seedHeaders.map((h) => `<option value="${esc(h)}"></option>`).join("");

          let body = "";
          if (type === "lookup_replace") {
            body = `
              <div class="row3">
                <div><label>Lookup Table</label><input type="text" id="txLookupTable" value="${esc(t.lookup_table)}" /></div>
                <div><label>Key ${help("The field in the lookup table used to map to the seed file")}</label><input type="text" list="${lookupListId}" id="txKey" value="${esc(t.key)}" /></div>
                <div><label>Seed Field ${help("The field in the seed file used to map to the lookup table")}</label><input type="text" list="${seedListId}" id="txSeedField" value="${esc(t.seed_field)}" /></div>
              </div>
              <div class="row">
                <div><label>Lookup Field</label><input type="text" list="${lookupListId}" id="txLookupField" value="${esc(t.lookup_field)}" /></div>
                <div><label>Output Field (rename target column)</label><input type="text" id="txOutputField" value="${esc(t.output_field)}" /></div>
              </div>
            `;
          } else if (type === "transform_tidy") {
            body = `
              <div class="row3">
                <div><label>Anchor ${help("The value in the aggregated table for which all other values are subsequent to. This is typically an Application Name or ID")}</label><input type="text" list="${seedListId}" id="txAnchor" value="${esc(t.anchor)}" /></div>
                <div><label>Column Field ${help("This is the field for which the values will be transposed to column headers.")}</label><input type="text" id="txColumnField" value="${esc(t.column_field)}" /></div>
                <div><label>Value Field ${help("These will be the values to associate to the Anchor Value + Tidy Column Field values.")}</label><input type="text" id="txValueField" value="${esc(t.value_field)}" /></div>
              </div>
              <div class="row">
                <div><label>Delimiter ${help("Used when a row will end up with multiple values for value separation.")}</label><input type="text" id="txDelimiter" value="${esc(t.delimiter)}" /></div>
                <div><label>Drop Fields (one per line)</label><textarea id="txDropFields">${esc(joinLines(t.drop_fields))}</textarea></div>
              </div>
            `;
          } else {
            syncSimpleLookupFields(t);
            const rows = t.fields.map((entry, idx) => {
              const p = parseFieldEntry(entry);
              return `<div class="field-row" data-role="field-row" data-field-index="${idx}">
                <div><label>Lookup Field</label><input type="text" list="${lookupListId}" data-role="field-source" data-field-index="${idx}" value="${esc(p.source)}" /></div>
                <div><label>Output Name (optional)</label><input type="text" data-role="field-target" data-field-index="${idx}" value="${esc(p.target)}" /></div>
                <button class="btn" data-role="remove-field" data-field-index="${idx}">Remove</button>
              </div>`;
            }).join("");
            body = `
              <div class="row3">
                <div><label>Lookup Table</label><input type="text" id="txLookupTable" value="${esc(t.lookup_table)}" /></div>
                <div><label>Key ${help("The field in the lookup table used to map to the seed file")}</label><input type="text" list="${lookupListId}" id="txKey" value="${esc(t.key)}" /></div>
                <div><label>Seed Field ${help("The field in the seed file used to map to the lookup table")}</label><input type="text" list="${seedListId}" id="txSeedField" value="${esc(t.seed_field)}" /></div>
              </div>
              <h4>Fields to Add</h4>
              <div class="muted">Choose from lookup-table headers or type your own value.</div>
              <div id="fieldRows">${rows || '<div class="muted">No fields selected yet.</div>'}</div>
              <button class="btn" id="addFieldBtn">Add Field</button>
            `;
          }

          return `
            <div class="tx-modal-backdrop" id="txModalBackdrop">
              <div class="tx-modal-card">
                <div class="tx-modal-head">
                  <div>
                    <h3 style="margin:0;">Transaction Details: ${esc(txName)}</h3>
                    <div class="muted">Type: ${esc(type)}${type !== "transform_tidy" ? ` • Filename: ${esc(txName)}` : ""}</div>
                  </div>
                  <button class="btn" id="txModalClose">Close</button>
                </div>
                <div class="txn-shell">
              <div class="txn-toolbar">
                <label>Type</label>
                <select id="txType">
                  <option value="simple_lookup" ${type === "simple_lookup" ? "selected" : ""}>simple_lookup</option>
                  <option value="lookup_replace" ${type === "lookup_replace" ? "selected" : ""}>lookup_replace</option>
                  <option value="transform_tidy" ${type === "transform_tidy" ? "selected" : ""}>transform_tidy</option>
                </select>
                <button class="btn" id="txDeleteBtn">Delete</button>
              </div>
              <datalist id="${lookupListId}">${lookupOptions}</datalist>
              <datalist id="${seedListId}">${seedOptions}</datalist>
              ${body}
            </div>
              </div>
            </div>
          `;
        }

        function gatherSimpleLookupFields(txn) {
          const rows = [...document.querySelectorAll('[data-role="field-row"]')];
          const out = [];
          rows.forEach((row) => {
            const idx = Number(row.dataset.fieldIndex);
            const src = document.querySelector(`[data-role="field-source"][data-field-index="${idx}"]`);
            const tgt = document.querySelector(`[data-role="field-target"][data-field-index="${idx}"]`);
            const line = stringifyFieldEntry(src ? src.value : "", tgt ? tgt.value : "");
            if (line) out.push(line);
          });
          txn.fields = out;
        }

        function bindTxnDnD(job) {
          let draggedIndex = null;
          const list = document.getElementById("txnList");
          if (!list) return;
          const cards = [...list.querySelectorAll('.txn-item')];
          cards.forEach((card) => {
            card.addEventListener("dragstart", () => {
              draggedIndex = Number(card.dataset.txn);
              card.classList.add("dragging");
            });
            card.addEventListener("dragend", () => {
              card.classList.remove("dragging");
              draggedIndex = null;
            });
            card.addEventListener("dragover", (e) => {
              e.preventDefault();
            });
            card.addEventListener("drop", (e) => {
              e.preventDefault();
              const targetIndex = Number(card.dataset.txn);
              if (draggedIndex == null || targetIndex === draggedIndex) return;
              const txns = job.transactions || [];
              const [moved] = txns.splice(draggedIndex, 1);
              txns.splice(targetIndex, 0, moved);
              selectedTxnIndex = targetIndex;
              renderEditor();
              renderJobs();
              markDirty();
            });
          });
        }

        function bindTxnEditor(job) {
          const txns = job.transactions || [];
          if (!txns.length || !txnModalOpen) return;
          const t = txns[selectedTxnIndex];

          const closeModal = () => {
            txnModalOpen = false;
            renderEditor();
          };
          const closeBtn = document.getElementById("txModalClose");
          if (closeBtn) closeBtn.onclick = closeModal;
          const backdrop = document.getElementById("txModalBackdrop");
          if (backdrop) {
            backdrop.onclick = (e) => {
              if (e.target && e.target.id === "txModalBackdrop") closeModal();
            };
          }

          document.getElementById("txType").onchange = (e) => {
            job.transactions[selectedTxnIndex] = newTxn(e.target.value);
            renderEditor();
            markDirty();
          };

          document.getElementById("txDeleteBtn").onclick = () => {
            job.transactions.splice(selectedTxnIndex, 1);
            selectedTxnIndex = Math.max(0, selectedTxnIndex - 1);
            if (!job.transactions.length) txnModalOpen = false;
            renderEditor();
            renderJobs();
            markDirty();
          };

          const type = text(t.type || "simple_lookup");
          if (type === "lookup_replace") {
            document.getElementById("txLookupTable").onchange = (e) => { t.lookup_table = e.target.value.trim(); renderEditor(); markDirty(); };
            document.getElementById("txKey").onchange = (e) => { t.key = e.target.value.trim(); markDirty(); };
            document.getElementById("txSeedField").onchange = (e) => { t.seed_field = e.target.value.trim(); markDirty(); };
            document.getElementById("txLookupField").onchange = (e) => { t.lookup_field = e.target.value.trim(); markDirty(); };
            document.getElementById("txOutputField").onchange = (e) => { t.output_field = e.target.value.trim(); markDirty(); };
          } else if (type === "transform_tidy") {
            document.getElementById("txAnchor").onchange = (e) => { t.anchor = e.target.value.trim(); markDirty(); };
            document.getElementById("txColumnField").onchange = (e) => { t.column_field = e.target.value.trim(); markDirty(); };
            document.getElementById("txValueField").onchange = (e) => { t.value_field = e.target.value.trim(); markDirty(); };
            document.getElementById("txDelimiter").onchange = (e) => { t.delimiter = e.target.value; markDirty(); };
            document.getElementById("txDropFields").onchange = (e) => { t.drop_fields = splitLines(e.target.value); markDirty(); };
          } else {
            syncSimpleLookupFields(t);
            document.getElementById("txLookupTable").onchange = (e) => { t.lookup_table = e.target.value.trim(); renderEditor(); markDirty(); };
            document.getElementById("txKey").onchange = (e) => { t.key = e.target.value.trim(); markDirty(); };
            document.getElementById("txSeedField").onchange = (e) => { t.seed_field = e.target.value.trim(); markDirty(); };
            document.getElementById("addFieldBtn").onclick = () => {
              t.fields.push("NEW_FIELD");
              renderEditor();
              markDirty();
            };
            for (const el of document.querySelectorAll('[data-role="remove-field"]')) {
              el.onclick = () => {
                const idx = Number(el.dataset.fieldIndex);
                t.fields.splice(idx, 1);
                renderEditor();
                markDirty();
              };
            }
            for (const el of document.querySelectorAll('[data-role="field-source"], [data-role="field-target"]')) {
              el.onchange = () => { gatherSimpleLookupFields(t); markDirty(); };
            }
          }
        }

        async function renderEditor() {
          ensurePayload();
          const jobs = state.config.payload.jobs;
          if (!jobs.length) {
            jobs.push(newJob());
            selectedJobIndex = 0;
          }
          selectedJobIndex = Math.max(0, Math.min(selectedJobIndex, jobs.length - 1));
          const job = jobs[selectedJobIndex];
          syncJobShape(job);

          const editor = document.getElementById("editor");
          editor.innerHTML = `
            <div class="row">
              <div><label>Job ID</label><input id="jobId" type="text" value="${esc(job.id)}" /></div>
              <div style="display:flex;align-items:end;gap:12px;">
                <label><input id="jobEnabled" type="checkbox" ${job.enabled === false ? "" : "checked"} /> Enabled</label>
                <button class="btn" id="deleteJobBtn">Delete Job</button>
              </div>
            </div>
            <div class="row3">
              <div><label>Seed Path</label><input id="jobSeed" type="text" value="${esc(job.seed)}" /></div>
              <div><label>Anchor</label><input id="jobAnchor" type="text" value="${esc(job.anchor)}" /></div>
              <div><label>Delimiter</label><input id="jobDelimiter" type="text" value="${esc(job.delimiter)}" /></div>
            </div>
            <div class="row">
              <div><label>Inputs (one path per line)</label><textarea id="jobInputs">${esc(joinLines(job.inputs))}</textarea></div>
              <div><label>Outputs (one path per line)</label><textarea id="jobOutputs">${esc(joinLines(job.outputs))}</textarea></div>
            </div>
            <h3>Transactions</h3>
            <div class="txn-toolbar">
              <button class="btn" id="addSimpleBtn">Add simple_lookup</button>
              <button class="btn" id="addReplaceBtn">Add lookup_replace</button>
              <button class="btn" id="addTidyBtn">Add transform_tidy</button>
            </div>
            <div class="muted">Click a transaction to edit details. Drag transactions to reorder execution.</div>
            <div class="list" id="txnList">${renderTxnList(job)}</div>
            ${await renderTxnModal(job)}
          `;

          document.getElementById("jobId").oninput = (e) => { job.id = e.target.value; renderJobs(); markDirty(); };
          document.getElementById("jobEnabled").onchange = (e) => { job.enabled = !!e.target.checked; renderJobs(); markDirty(); };
          document.getElementById("jobSeed").onchange = (e) => { job.seed = e.target.value.trim(); renderEditor(); markDirty(); };
          document.getElementById("jobAnchor").onchange = (e) => { job.anchor = e.target.value.trim(); markDirty(); };
          document.getElementById("jobDelimiter").onchange = (e) => { job.delimiter = e.target.value; markDirty(); };
          document.getElementById("jobInputs").onchange = (e) => { job.inputs = splitLines(e.target.value); markDirty(); };
          document.getElementById("jobOutputs").onchange = (e) => { job.outputs = splitLines(e.target.value); markDirty(); };

          document.getElementById("deleteJobBtn").onclick = () => {
            if (state.config.payload.jobs.length <= 1) return;
            state.config.payload.jobs.splice(selectedJobIndex, 1);
            selectedJobIndex = Math.max(0, selectedJobIndex - 1);
            selectedTxnIndex = 0;
            txnModalOpen = false;
            renderJobs();
            renderEditor();
            markDirty();
          };

          document.getElementById("addSimpleBtn").onclick = () => {
            job.transactions.push(newTxn("simple_lookup"));
            selectedTxnIndex = job.transactions.length - 1;
            txnModalOpen = true;
            renderJobs();
            renderEditor();
            markDirty();
          };
          document.getElementById("addReplaceBtn").onclick = () => {
            job.transactions.push(newTxn("lookup_replace"));
            selectedTxnIndex = job.transactions.length - 1;
            txnModalOpen = true;
            renderJobs();
            renderEditor();
            markDirty();
          };
          document.getElementById("addTidyBtn").onclick = () => {
            job.transactions.push(newTxn("transform_tidy"));
            selectedTxnIndex = job.transactions.length - 1;
            txnModalOpen = true;
            renderJobs();
            renderEditor();
            markDirty();
          };

          for (const el of document.querySelectorAll('[data-role="txn-select"]')) {
            el.onclick = () => {
              selectedTxnIndex = Number(el.dataset.txn || 0);
              txnModalOpen = true;
              renderEditor();
            };
          }
          bindTxnDnD(job);
          bindTxnEditor(job);
        }

        async function save() {
          setStatus("Saving...");
          const res = await fetch("/api/config/table-aggregator", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(state)
          });
          const data = await res.json();
          if (!res.ok) {
            setStatus(`Save failed: ${data.detail || res.status}`);
            return;
          }
          initialState = cloneObj(state);
          markClean("Saved");
        }

        async function init() {
          const res = await fetch("/api/config/table-aggregator");
          if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${res.status}`);
          }
          const data = await res.json();
          state = data;
          initialState = cloneObj(data);
          ensurePayload();
          markClean("Ready");

          document.getElementById("addJobBtn").onclick = () => {
            state.config.payload.jobs.push(newJob());
            selectedJobIndex = state.config.payload.jobs.length - 1;
            selectedTxnIndex = 0;
            renderJobs();
            renderEditor();
            markDirty();
          };
          document.getElementById("saveBtn").onclick = save;
          document.getElementById("discardBtn").onclick = async () => {
            state = cloneObj(initialState);
            ensurePayload();
            const jobs = state.config.payload.jobs || [];
            selectedJobIndex = Math.max(0, Math.min(selectedJobIndex, Math.max(0, jobs.length - 1)));
            selectedTxnIndex = 0;
            txnModalOpen = false;
            renderJobs();
            await renderEditor();
            markClean("Discarded");
          };

          renderJobs();
          await renderEditor();
        }

        init().catch((err) => {
          document.getElementById("status").textContent = `Failed to load: ${err}`;
        });
      </script>
    </body>
    </html>
    """
    return HTMLResponse(html)


@app.get("/api/config/table-aggregator", response_class=JSONResponse)
async def get_table_aggregator_config(request: Request):
    redirect = require_login(request)
    if redirect:
        raise HTTPException(status_code=401, detail="Login required")

    custom_path = get_table_aggregator_custom_yaml()
    source_path = get_table_aggregator_source_yaml()
    load_path = custom_path if custom_path.exists() else source_path
    if not load_path.exists():
        raise HTTPException(
            status_code=404,
            detail="No table aggregator configuration found in usr/config/custom or usr/config/auto_v2",
        )

    data = load_yaml_dict(load_path)
    data = normalize_table_aggregator_doc(data)
    return JSONResponse(
        {
            "loaded_from": str(load_path),
            "save_path": str(custom_path),
            **data,
        }
    )


@app.post("/api/config/table-aggregator", response_class=JSONResponse)
async def save_table_aggregator_config(request: Request):
    redirect = require_login(request)
    if redirect:
        raise HTTPException(status_code=401, detail="Login required")

    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected JSON object payload")
    payload = normalize_table_aggregator_doc(payload)
    payload.pop("loaded_from", None)
    payload.pop("save_path", None)

    cfg = payload.get("config")
    if not isinstance(cfg, dict):
        raise HTTPException(status_code=400, detail="Missing config object")

    jobs = ((cfg.get("payload") or {}).get("jobs") or [])
    if not isinstance(jobs, list):
        raise HTTPException(status_code=400, detail="config.payload.jobs must be a list")

    save_path = get_table_aggregator_custom_yaml()
    save_yaml_dict(save_path, payload)
    log("info", f"Saved table aggregator config to {save_path}")
    return JSONResponse({"status": "ok", "path": str(save_path), "jobs": len(jobs)})


@app.get("/api/config/csv-headers", response_class=JSONResponse)
async def get_csv_headers(path: str, request: Request):
    redirect = require_login(request)
    if redirect:
        raise HTTPException(status_code=401, detail="Login required")

    resolved = resolve_vtx_path(path)
    if not resolved.exists() or not resolved.is_file():
        return JSONResponse({"path": str(resolved), "headers": []})

    headers = read_table_headers_for_ui(path)
    return JSONResponse({"path": str(resolved), "headers": headers})


@app.get("/api/config/list", response_class=JSONResponse)
async def list_config_catalog(request: Request) -> JSONResponse:
    redirect = require_login(request)
    if redirect:
        raise HTTPException(status_code=401, detail="Login required")
    return JSONResponse({"items": get_config_catalog_items()})


@app.get("/config/{config_slug}", response_class=HTMLResponse)
async def generic_config_editor(request: Request, config_slug: str) -> HTMLResponse:
    redirect = require_login(request)
    if redirect:
        return redirect

    spec = get_config_spec(config_slug)
    if spec["slug"] == "tag_engine":
        html = """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>Tag Engine Builder</title>
      <style>
        :root { --ink:#e6eefc; --line:rgba(121,184,255,.25); --bg:#0b1320; --surface:#122238; --muted:#a9bdd7; --accent:#f0b24a; --panel:#102034; --panel2:#142842; --warn:#f7d37d; --danger:#f18f8f; --ok:#7cc7a2; }
        * { box-sizing:border-box; }
        body { margin:0; font:14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color:var(--ink); background:radial-gradient(circle at top right, rgba(121,184,255,.14), transparent 32%), radial-gradient(circle at bottom left, rgba(240,178,74,.1), transparent 36%), linear-gradient(180deg,#0b1320,#101d2f); }
        .top { position:sticky; top:0; z-index:100; backdrop-filter: blur(8px); background:rgba(8,16,28,.86); border-bottom:1px solid var(--line); padding:10px 16px; display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
        .btn { border:1px solid rgba(67,100,135,.7); background:rgba(19,34,56,.92); color:var(--ink); border-radius:999px; padding:8px 12px; cursor:pointer; text-decoration:none; font-weight:600; }
        .btn:hover { background:rgba(27,48,77,.98); border-color:rgba(121,184,255,.7); }
        .btn.alt { background:rgba(14,24,40,.92); }
        .btn.warn { border-color:rgba(240,178,74,.5); color:var(--warn); }
        .wrap { width:min(1500px, calc(100% - 28px)); margin:16px auto 28px; display:grid; gap:12px; }
        .hero { margin:10px auto 0; width:min(1500px, calc(100% - 28px)); min-height:150px; color:#fff; position:relative; overflow:hidden; border:1px solid rgba(121,184,255,.25); border-radius:18px; background:linear-gradient(135deg,#1e293b 0%, #475569 50%, #64748b 100%); }
        .hero::before { content:""; position:absolute; inset:0; background-image:linear-gradient(rgba(255,255,255,.08) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.08) 1px, transparent 1px); background-size:60px 60px; opacity:.7; }
        .hero-content { position:relative; z-index:1; padding:24px; }
        .hero h1 { margin:0 0 8px; font-size:clamp(1.7rem,3vw,2.25rem); }
        .grid { display:grid; grid-template-columns: 320px 1fr; gap:12px; align-items:start; }
        .panel { background:linear-gradient(180deg, rgba(19,35,57,.96), rgba(15,28,45,.96)); border:1px solid var(--line); border-radius:14px; padding:14px; box-shadow:0 18px 48px rgba(0,0,0,.28); }
        .section { border:1px solid rgba(121,184,255,.14); border-radius:14px; padding:12px; background:rgba(9,18,31,.34); margin-top:12px; }
        .summary-list { display:grid; gap:8px; max-height:780px; overflow:auto; }
        .item-card { border:1px solid rgba(71,85,105,.26); border-radius:12px; padding:10px; background:rgba(12,22,36,.76); cursor:pointer; }
        .item-card.active { border-color:rgba(121,184,255,.7); background:rgba(24,40,62,.9); }
        .item-title { margin:0 0 4px; font-weight:700; }
        .item-copy { margin:0; color:var(--muted); font-size:12px; }
        .row { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:10px; }
        .row3 { display:grid; grid-template-columns:1fr 1fr 1fr; gap:10px; margin-bottom:10px; }
        .field { margin-bottom:10px; }
        label { display:block; font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin-bottom:4px; }
        label.required::after { content:" *"; color:var(--accent); }
        input, select, textarea { width:100%; border:1px solid rgba(67,100,135,.65); border-radius:8px; padding:8px; background:rgba(14,24,40,.92); color:var(--ink); }
        textarea { min-height:72px; resize:vertical; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
        .inline-actions { display:flex; gap:8px; flex-wrap:wrap; margin-top:8px; }
        .muted { color:var(--muted); }
        .status-ok { color:var(--ok); }
        .status-error { color:var(--danger); }
        details { border:1px solid rgba(71,85,105,.24); border-radius:12px; padding:10px; background:rgba(10,20,34,.6); margin-bottom:8px; }
        summary { cursor:pointer; font-weight:700; }
        .pill { display:inline-block; border:1px solid rgba(121,184,255,.25); border-radius:999px; padding:2px 8px; font-size:11px; color:var(--muted); margin-left:6px; }
        .helper { font-size:12px; color:var(--muted); margin-top:4px; }
        @media (max-width:1150px){ .grid{grid-template-columns:1fr;} .row,.row3{grid-template-columns:1fr;} }
      </style>
    </head>
    <body>
      <div class="top">
        <a class="btn" href="/">Back to Home</a>
        <button class="btn" id="newJobBtn">New Job</button>
        <button class="btn" id="copyJobBtn">Copy Job</button>
        <button class="btn" id="saveAllBtn">Save To YAML</button>
        <span class="muted" id="status">Loading...</span>
      </div>
      <section class="hero">
        <div class="hero-content">
          <h1>Tag Engine Builder</h1>
          <p id="subtitle" class="muted">Configure Tag Engine jobs with guided values, nested tags, rules, and conditions. Changes save to usr/config/custom/tag_engine.yaml.</p>
        </div>
      </section>
      <div class="wrap">
        <div class="grid">
          <section class="panel">
            <h2 style="margin:0 0 8px;">Jobs</h2>
            <div id="jobList" class="summary-list"></div>
            <div id="jobEmpty" class="muted">No jobs yet. Click “New Job” to create the first job.</div>
          </section>
          <section class="panel">
            <h2 style="margin:0 0 8px;">Job Editor</h2>
            <div id="editor" class="muted">Select a job to begin.</div>
          </section>
        </div>
      </div>
      <script>
        const CONFIG_SLUG = "tag_engine";
        let state = null;
        let selectedJobIndex = -1;
        let jobFieldOptions = [];

        function esc(v) {
          return (v == null ? "" : String(v))
            .replaceAll("&","&amp;")
            .replaceAll("<","&lt;")
            .replaceAll(">","&gt;")
            .replaceAll('"','&quot;');
        }

        function clone(v) {
          return JSON.parse(JSON.stringify(v));
        }

        function setStatus(message, level = "") {
          const node = document.getElementById("status");
          node.textContent = message;
          node.className = level === "error" ? "status-error" : (level === "ok" ? "status-ok" : "muted");
        }

        function boolValue(value, fallback = false) {
          if (typeof value === "boolean") return value;
          if (typeof value === "string") return ["true", "1", "yes"].includes(value.toLowerCase());
          return fallback;
        }

        function buildDefaultJob() {
          const job = clone(state.template_job || {});
          job.id = "";
          job.enabled = false;
          job.inputs = [];
          job.outputs = [];
          job.anchor_field = "";
          job.entity_type = "";
          job.key_delimiter = job.key_delimiter || ";";
          job.tags = [];
          job.derived_tags = Array.isArray(job.derived_tags) ? job.derived_tags : [];
          return job;
        }

        function buildDefaultTag() {
          return clone(state.default_tag);
        }

        function buildDefaultRule() {
          return clone(state.default_rule);
        }

        function buildDefaultCondition() {
          return clone(state.default_condition);
        }

        function getSelectedJob() {
          if (selectedJobIndex < 0 || selectedJobIndex >= (state.jobs || []).length) return null;
          return state.jobs[selectedJobIndex];
        }

        function normalizeOutputPath(selectedValue, customValue) {
          let raw = selectedValue === "__new__" ? String(customValue || "").trim() : String(selectedValue || "").trim();
          if (!raw) return "";
          raw = raw.replace(/^var\\/metadata\\//, "");
          const stem = raw.replace(/\\.[^/.]+$/, "") || "output";
          const safe = stem.replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^[_\\.]+|[_\\.]+$/g, "") || "output";
          return `var/metadata/${safe}.csv`;
        }

        async function refreshFieldOptions(inputPath) {
          if (!inputPath) {
            jobFieldOptions = [];
            return;
          }
          const res = await fetch(`/api/config/csv-headers?path=${encodeURIComponent(inputPath)}`);
          const data = await res.json();
          jobFieldOptions = data.headers || [];
        }

        function renderJobList() {
          const list = document.getElementById("jobList");
          const empty = document.getElementById("jobEmpty");
          list.innerHTML = "";
          const jobs = state.jobs || [];
          empty.style.display = jobs.length ? "none" : "";
          jobs.forEach((job, idx) => {
            const node = document.createElement("div");
            node.className = "item-card" + (idx === selectedJobIndex ? " active" : "");
            node.innerHTML = `
              <p class="item-title">${esc(job.id || `Job ${idx + 1}`)}<span class="pill">${job.tags?.length || 0} tags</span></p>
              <p class="item-copy">${esc((job.inputs || [])[0] || "No input selected")} -> ${esc((job.outputs || [])[0] || "No output selected")}</p>
            `;
            node.onclick = async () => {
              selectedJobIndex = idx;
              await refreshFieldOptions(((state.jobs[idx] || {}).inputs || [])[0] || "");
              renderJobList();
              renderEditor();
            };
            list.appendChild(node);
          });
        }

        function validateCondition(condition, ruleId) {
          if (!condition.field) return `Rule '${ruleId}' has a condition without a field.`;
          if (!condition.operator) return `Rule '${ruleId}' has a condition without an operator.`;
          if (!state.operator_options.includes(condition.operator)) return `Rule '${ruleId}' uses unsupported operator '${condition.operator}'.`;
          if (condition.agg && !state.aggregate_options.includes(condition.agg)) return `Rule '${ruleId}' uses unsupported aggregation '${condition.agg}'.`;
          return "";
        }

        function validateRule(rule, tagId) {
          if (!rule.id) return `Tag '${tagId}' has a rule without an id.`;
          if (!rule.tag_value) return `Rule '${rule.id}' in tag '${tagId}' requires tag_value.`;
          if (!Array.isArray(rule.conditions) || !rule.conditions.length) return `Rule '${rule.id}' in tag '${tagId}' must contain at least one condition.`;
          for (const condition of rule.conditions) {
            const error = validateCondition(condition, rule.id);
            if (error) return error;
          }
          return "";
        }

        function validateTag(tag, jobId) {
          if (!tag.id) return `Job '${jobId}' has a tag without an id.`;
          if (!tag.tag_key) return `Tag '${tag.id}' in job '${jobId}' requires tag_key.`;
          if (!Array.isArray(tag.rules) || !tag.rules.length) return `Tag '${tag.id}' in job '${jobId}' must contain at least one rule.`;
          const ruleIds = new Set();
          for (const rule of tag.rules) {
            if (rule.id && ruleIds.has(rule.id)) return `Tag '${tag.id}' contains duplicate rule id '${rule.id}'.`;
            if (rule.id) ruleIds.add(rule.id);
            const error = validateRule(rule, tag.id);
            if (error) return error;
          }
          return "";
        }

        function validateJob(job) {
          if (!job.id) return "Each job requires an id.";
          if (!state.available_inputs.includes((job.inputs || [])[0] || "")) return `Job '${job.id}' input must be selected from var/masterdata/.`;
          const output = (job.outputs || [])[0] || "";
          if (!output.startsWith("var/metadata/") || !output.endsWith(".csv")) return `Job '${job.id}' output must resolve to var/metadata/*.csv.`;
          if (!job.anchor_field) return `Job '${job.id}' requires anchor_field.`;
          if (!job.entity_type) return `Job '${job.id}' requires entity_type.`;
          if (!Array.isArray(job.tags) || !job.tags.length) return `Job '${job.id}' must contain at least one tag.`;
          const tagIds = new Set();
          for (const tag of job.tags) {
            if (tag.id && tagIds.has(tag.id)) return `Job '${job.id}' contains duplicate tag id '${tag.id}'.`;
            if (tag.id) tagIds.add(tag.id);
            const error = validateTag(tag, job.id);
            if (error) return error;
          }
          return "";
        }

        function validateAllJobs() {
          const ids = new Set();
          for (const job of state.jobs || []) {
            if (job.id && ids.has(job.id)) return `Duplicate job id '${job.id}'.`;
            if (job.id) ids.add(job.id);
            const error = validateJob(job);
            if (error) return error;
          }
          return "";
        }

        function tagSummary(tag) {
          return `${tag.id || "Unnamed Tag"} · ${tag.tag_key || "no tag_key"} · ${tag.rules?.length || 0} rules`;
        }

        function ruleSummary(rule) {
          return `${rule.id || "Unnamed Rule"} · ${rule.tag_value || "no tag_value"} · ${rule.conditions?.length || 0} conditions`;
        }

        function conditionSummary(condition) {
          const agg = condition.agg ? ` [${condition.agg}]` : "";
          return `${condition.field || "field"} ${condition.operator || "op"} ${condition.value || ""}${agg}`;
        }

        function copyWithDuplicateId(item) {
          const duplicated = clone(item);
          if (duplicated.id != null) duplicated.id = item.id || "";
          return duplicated;
        }

        function bindJobFields(job) {
          const enabled = document.getElementById("jobEnabled");
          const input = document.getElementById("jobInput");
          const outputChoice = document.getElementById("jobOutput");
          const outputNew = document.getElementById("jobOutputNew");
          const anchor = document.getElementById("jobAnchor");
          const entity = document.getElementById("jobEntityType");
          const delimiter = document.getElementById("jobDelimiter");
          const saveJobBtn = document.getElementById("saveJobBtn");

          enabled.onchange = () => { job.enabled = enabled.value === "true"; };
          input.onchange = async () => {
            job.inputs = input.value ? [input.value] : [];
            await refreshFieldOptions(input.value);
            renderEditor();
          };
          const syncOutput = () => {
            const normalized = normalizeOutputPath(outputChoice.value, outputNew.value);
            job.outputs = normalized ? [normalized] : [];
          };
          outputChoice.onchange = () => { syncOutput(); renderEditor(); };
          outputNew.oninput = syncOutput;
          anchor.oninput = () => { job.anchor_field = anchor.value.trim(); };
          entity.oninput = () => { job.entity_type = entity.value.trim(); };
          delimiter.oninput = () => { job.key_delimiter = delimiter.value; };
          saveJobBtn.onclick = () => {
            syncOutput();
            const error = validateJob(job);
            if (error) { setStatus(error, "error"); return; }
            renderJobList();
            setStatus(`Job '${job.id}' validated in context.`, "ok");
          };
          document.getElementById("copyJobLocalBtn").onclick = async () => {
            const duplicated = copyWithDuplicateId(job);
            state.jobs.push(duplicated);
            selectedJobIndex = state.jobs.length - 1;
            await refreshFieldOptions((duplicated.inputs || [])[0] || "");
            renderJobList();
            renderEditor();
            setStatus("Copied job. Change the job id before saving.", "error");
          };
          document.getElementById("deleteJobBtn").onclick = () => {
            state.jobs.splice(selectedJobIndex, 1);
            selectedJobIndex = state.jobs.length ? Math.max(0, selectedJobIndex - 1) : -1;
            renderJobList();
            renderEditor();
            setStatus("Job removed from working copy.");
          };
          document.getElementById("addTagBtn").onclick = () => {
            job.tags = Array.isArray(job.tags) ? job.tags : [];
            job.tags.push(buildDefaultTag());
            renderEditor();
            setStatus("New tag added to the job draft.");
          };
        }

        function renderConditionEditor(condition, tagIndex, ruleIndex, idx) {
          const tag = state.jobs[selectedJobIndex].tags[tagIndex];
          const rule = tag.rules[ruleIndex];
          const fieldListId = `field-options-${tagIndex}-${ruleIndex}-${idx}-${Math.random().toString(36).slice(2)}`;
          const operatorOptions = state.operator_options.map(op => `<option value="${esc(op)}" ${condition.operator === op ? "selected" : ""}>${esc(op)}</option>`).join("");
          const aggregateOptions = [`<option value=""></option>`].concat(state.aggregate_options.map(agg => `<option value="${esc(agg)}" ${condition.agg === agg ? "selected" : ""}>${esc(agg)}</option>`)).join("");
          const fieldOptions = jobFieldOptions.map(field => `<option value="${esc(field)}"></option>`).join("");
          return `
            <div class="section">
              <div class="row3">
                <div class="field">
                  <label class="required">Field</label>
                  <input value="${esc(condition.field || "")}" list="${fieldListId}" data-role="cond-field" data-tag-index="${tagIndex}" data-rule-index="${ruleIndex}" data-index="${idx}">
                  <datalist id="${fieldListId}">${fieldOptions}</datalist>
                  <div class="helper">Searchable suggestions come from the selected masterdata input when available.</div>
                </div>
                <div class="field">
                  <label class="required">Operator</label>
                  <select data-role="cond-operator" data-tag-index="${tagIndex}" data-rule-index="${ruleIndex}" data-index="${idx}">${operatorOptions}</select>
                </div>
                <div class="field">
                  <label>Aggregation</label>
                  <select data-role="cond-agg" data-tag-index="${tagIndex}" data-rule-index="${ruleIndex}" data-index="${idx}">${aggregateOptions}</select>
                </div>
              </div>
              <div class="row">
                <div class="field">
                  <label>Value</label>
                  <input value="${esc(condition.value == null ? "" : condition.value)}" data-role="cond-value" data-tag-index="${tagIndex}" data-rule-index="${ruleIndex}" data-index="${idx}">
                </div>
                <div class="inline-actions" style="align-items:end;">
                  <button class="btn alt" data-role="save-condition" data-tag-index="${tagIndex}" data-rule-index="${ruleIndex}" data-index="${idx}">Save Condition</button>
                  <button class="btn alt" data-role="copy-condition" data-tag-index="${tagIndex}" data-rule-index="${ruleIndex}" data-index="${idx}">Copy Condition</button>
                  <button class="btn warn" data-role="delete-condition" data-tag-index="${tagIndex}" data-rule-index="${ruleIndex}" data-index="${idx}">Delete Condition</button>
                </div>
              </div>
            </div>
          `;
        }

        function renderRuleEditor(tag, tagIndex, rule, ruleIndex) {
          return `
            <details>
              <summary>${esc(ruleSummary(rule))}</summary>
              <div class="section">
                <div class="row">
                  <div class="field">
                    <label class="required">Rule Id</label>
                    <input value="${esc(rule.id || "")}" data-role="rule-id" data-tag-index="${tagIndex}" data-rule-index="${ruleIndex}">
                  </div>
                  <div class="field">
                    <label class="required">Tag Value</label>
                    <input value="${esc(rule.tag_value || "")}" data-role="rule-value" data-tag-index="${tagIndex}" data-rule-index="${ruleIndex}">
                  </div>
                </div>
                <div class="inline-actions">
                  <button class="btn alt" data-role="save-rule" data-tag-index="${tagIndex}" data-rule-index="${ruleIndex}">Save Rule</button>
                  <button class="btn alt" data-role="copy-rule" data-tag-index="${tagIndex}" data-rule-index="${ruleIndex}">Copy Rule</button>
                  <button class="btn warn" data-role="delete-rule" data-tag-index="${tagIndex}" data-rule-index="${ruleIndex}">Delete Rule</button>
                  <button class="btn" data-role="add-condition" data-tag-index="${tagIndex}" data-rule-index="${ruleIndex}">Add Condition</button>
                </div>
                <div class="helper">Rules live inside the parent tag and evaluate their conditions using the tag's condition logic unless a rule-specific override is added later.</div>
                ${(rule.conditions || []).map((condition, conditionIndex) => renderConditionEditor(condition, tagIndex, ruleIndex, conditionIndex)).join("")}
              </div>
            </details>
          `;
        }

        function renderTagEditor(tag, tagIndex) {
          return `
            <details>
              <summary>${esc(tagSummary(tag))}</summary>
              <div class="section">
                <div class="row3">
                  <div class="field">
                    <label class="required">Tag Id</label>
                    <input value="${esc(tag.id || "")}" data-role="tag-id" data-tag-index="${tagIndex}">
                  </div>
                  <div class="field">
                    <label class="required">Tag Key</label>
                    <input value="${esc(tag.tag_key || "")}" data-role="tag-key" data-tag-index="${tagIndex}">
                  </div>
                  <div class="field">
                    <label>Source</label>
                    <input value="rule" disabled>
                  </div>
                </div>
                <div class="row3">
                  <div class="field">
                    <label>Condition Logic</label>
                    <select data-role="tag-logic" data-tag-index="${tagIndex}">
                      <option value="all" ${tag.condition_logic !== "any" ? "selected" : ""}>all</option>
                      <option value="any" ${tag.condition_logic === "any" ? "selected" : ""}>any</option>
                    </select>
                  </div>
                  <div class="field">
                    <label>First Match Only</label>
                    <select data-role="tag-first-match" data-tag-index="${tagIndex}">
                      <option value="false" ${!boolValue(tag.first_match_only) ? "selected" : ""}>false</option>
                      <option value="true" ${boolValue(tag.first_match_only) ? "selected" : ""}>true</option>
                    </select>
                  </div>
                  <div class="field">
                    <label>Key Fields</label>
                    <input value="${esc((tag.key_fields || []).join(", "))}" data-role="tag-key-fields" data-tag-index="${tagIndex}">
                    <div class="helper">Optional comma-separated grouping fields.</div>
                  </div>
                </div>
                <div class="inline-actions">
                  <button class="btn alt" data-role="save-tag" data-tag-index="${tagIndex}">Save Tag</button>
                  <button class="btn alt" data-role="copy-tag" data-tag-index="${tagIndex}">Copy Tag</button>
                  <button class="btn warn" data-role="delete-tag" data-tag-index="${tagIndex}">Delete Tag</button>
                  <button class="btn" data-role="add-rule" data-tag-index="${tagIndex}">Add Rule</button>
                </div>
                <div class="helper">A single tag_key can emit multiple tag values through different rules. Each rule can have multiple conditions.</div>
                ${(tag.rules || []).map((rule, ruleIndex) => renderRuleEditor(tag, tagIndex, rule, ruleIndex)).join("")}
              </div>
            </details>
          `;
        }

        function renderEditor() {
          const host = document.getElementById("editor");
          const job = getSelectedJob();
          if (!job) {
            host.textContent = "Select a job to begin.";
            return;
          }
          const currentOutput = (job.outputs || [])[0] || "";
          const outputIsExisting = state.available_outputs.includes(currentOutput);
          const outputSelectValue = outputIsExisting ? currentOutput : (currentOutput ? "__new__" : "");
          const newOutputName = outputIsExisting ? "" : currentOutput.replace(/^var\\/metadata\\//, "");
          const inputOptions = [`<option value=""></option>`].concat(state.available_inputs.map(path => `<option value="${esc(path)}" ${((job.inputs || [])[0] || "") === path ? "selected" : ""}>${esc(path)}</option>`)).join("");
          const outputOptions = [`<option value=""></option>`].concat(state.available_outputs.map(path => `<option value="${esc(path)}" ${outputSelectValue === path ? "selected" : ""}>${esc(path)}</option>`)).concat([`<option value="__new__" ${outputSelectValue === "__new__" ? "selected" : ""}>Create new metadata table</option>`]).join("");
          const entityTypeList = state.entity_type_options.map(value => `<option value="${esc(value)}"></option>`).join("");
          host.innerHTML = `
            <div class="section">
              <div class="row3">
                <div class="field">
                  <label class="required">Job Id</label>
                  <input id="jobId" value="${esc(job.id || "")}">
                </div>
                <div class="field">
                  <label>Enabled</label>
                  <select id="jobEnabled">
                    <option value="false" ${!boolValue(job.enabled) ? "selected" : ""}>false</option>
                    <option value="true" ${boolValue(job.enabled) ? "selected" : ""}>true</option>
                  </select>
                </div>
                <div class="field">
                  <label>Key Delimiter</label>
                  <input id="jobDelimiter" value="${esc(job.key_delimiter || ";")}">
                </div>
              </div>
              <div class="row">
                <div class="field">
                  <label class="required">Input</label>
                  <select id="jobInput">${inputOptions}</select>
                  <div class="helper">Inputs are limited to tables in var/masterdata/.</div>
                </div>
                <div class="field">
                  <label class="required">Anchor Field</label>
                  <input id="jobAnchor" list="jobFieldOptions" value="${esc(job.anchor_field || "")}">
                  <datalist id="jobFieldOptions">${jobFieldOptions.map(field => `<option value="${esc(field)}"></option>`).join("")}</datalist>
                </div>
              </div>
              <div class="row">
                <div class="field">
                  <label class="required">Output</label>
                  <select id="jobOutput">${outputOptions}</select>
                  <input id="jobOutputNew" placeholder="table_name.csv" value="${esc(newOutputName)}" style="${outputSelectValue === "__new__" ? "" : "display:none; margin-top:8px;"}">
                  <div class="helper">Existing metadata CSV tables can be reused, or you can create a new one under var/metadata/. Names are normalized to .csv.</div>
                </div>
                <div class="field">
                  <label class="required">Entity Type</label>
                  <input id="jobEntityType" list="entityTypeOptions" value="${esc(job.entity_type || "")}">
                  <datalist id="entityTypeOptions">${entityTypeList}</datalist>
                  <div class="helper">Choose an existing value or enter a new one.</div>
                </div>
              </div>
              <div class="inline-actions">
                <button class="btn alt" id="saveJobBtn">Save Job In Context</button>
                <button class="btn alt" id="copyJobLocalBtn">Copy This Job</button>
                <button class="btn warn" id="deleteJobBtn">Delete This Job</button>
              </div>
            </div>
            <div class="section">
              <h3 style="margin:0 0 8px;">Tags</h3>
              <div class="helper">Tags are nested inside the job. Each tag can generate multiple values through its rules.</div>
              <div class="inline-actions">
                <button class="btn" id="addTagBtn">Create New Tag</button>
              </div>
              ${(job.tags || []).length ? job.tags.map((tag, tagIndex) => renderTagEditor(tag, tagIndex)).join("") : '<div class="muted">No tags yet. Create the first tag for this job.</div>'}
            </div>
          `;

          document.getElementById("jobId").oninput = (event) => { job.id = event.target.value.trim(); renderJobList(); };
          document.getElementById("jobOutput").onchange = () => {
            document.getElementById("jobOutputNew").style.display = document.getElementById("jobOutput").value === "__new__" ? "" : "none";
          };
          bindJobFields(job);
          bindNestedEditors(job);
        }

        function bindNestedEditors(job) {
          document.querySelectorAll("[data-role='tag-id']").forEach(node => node.oninput = (event) => {
            job.tags[Number(event.target.dataset.tagIndex)].id = event.target.value.trim();
          });
          document.querySelectorAll("[data-role='tag-key']").forEach(node => node.oninput = (event) => {
            job.tags[Number(event.target.dataset.tagIndex)].tag_key = event.target.value.trim();
          });
          document.querySelectorAll("[data-role='tag-logic']").forEach(node => node.onchange = (event) => {
            job.tags[Number(event.target.dataset.tagIndex)].condition_logic = event.target.value;
          });
          document.querySelectorAll("[data-role='tag-first-match']").forEach(node => node.onchange = (event) => {
            job.tags[Number(event.target.dataset.tagIndex)].first_match_only = event.target.value === "true";
          });
          document.querySelectorAll("[data-role='tag-key-fields']").forEach(node => node.oninput = (event) => {
            job.tags[Number(event.target.dataset.tagIndex)].key_fields = event.target.value.split(",").map(v => v.trim()).filter(Boolean);
          });
          document.querySelectorAll("[data-role='save-tag']").forEach(node => node.onclick = () => {
            const tag = job.tags[Number(node.dataset.tagIndex)];
            const error = validateTag(tag, job.id || "job");
            if (error) { setStatus(error, "error"); return; }
            renderEditor();
            setStatus(`Tag '${tag.id}' validated in context.`, "ok");
          });
          document.querySelectorAll("[data-role='copy-tag']").forEach(node => node.onclick = () => {
            const idx = Number(node.dataset.tagIndex);
            job.tags.splice(idx + 1, 0, copyWithDuplicateId(job.tags[idx]));
            renderEditor();
            setStatus("Copied tag. Change the tag id before saving.", "error");
          });
          document.querySelectorAll("[data-role='delete-tag']").forEach(node => node.onclick = () => {
            job.tags.splice(Number(node.dataset.tagIndex), 1);
            renderEditor();
            setStatus("Tag removed from the job draft.");
          });
          document.querySelectorAll("[data-role='add-rule']").forEach(node => node.onclick = () => {
            const tag = job.tags[Number(node.dataset.tagIndex)];
            tag.rules = Array.isArray(tag.rules) ? tag.rules : [];
            tag.rules.push(buildDefaultRule());
            renderEditor();
            setStatus("New rule added to the tag draft.");
          });
          document.querySelectorAll("[data-role='rule-id']").forEach(node => node.oninput = (event) => {
            job.tags[Number(event.target.dataset.tagIndex)].rules[Number(event.target.dataset.ruleIndex)].id = event.target.value.trim();
          });
          document.querySelectorAll("[data-role='rule-value']").forEach(node => node.oninput = (event) => {
            job.tags[Number(event.target.dataset.tagIndex)].rules[Number(event.target.dataset.ruleIndex)].tag_value = event.target.value.trim();
          });
          document.querySelectorAll("[data-role='save-rule']").forEach(node => node.onclick = () => {
            const tagIndex = Number(node.dataset.tagIndex);
            const ruleIndex = Number(node.dataset.ruleIndex);
            const currentRule = job.tags[tagIndex].rules[ruleIndex];
            const error = validateRule(currentRule, job.tags[tagIndex].id || "tag");
            if (error) { setStatus(error, "error"); return; }
            renderEditor();
            setStatus(`Rule '${currentRule.id}' validated in context.`, "ok");
          });
          document.querySelectorAll("[data-role='copy-rule']").forEach(node => node.onclick = () => {
            const tagIndex = Number(node.dataset.tagIndex);
            const ruleIndex = Number(node.dataset.ruleIndex);
            job.tags[tagIndex].rules.splice(ruleIndex + 1, 0, copyWithDuplicateId(job.tags[tagIndex].rules[ruleIndex]));
            renderEditor();
            setStatus("Copied rule. Change the rule id before saving.", "error");
          });
          document.querySelectorAll("[data-role='delete-rule']").forEach(node => node.onclick = () => {
            const tagIndex = Number(node.dataset.tagIndex);
            const ruleIndex = Number(node.dataset.ruleIndex);
            job.tags[tagIndex].rules.splice(ruleIndex, 1);
            renderEditor();
            setStatus("Rule removed from the tag draft.");
          });
          document.querySelectorAll("[data-role='add-condition']").forEach(node => node.onclick = () => {
            const tagIndex = Number(node.dataset.tagIndex);
            const ruleIndex = Number(node.dataset.ruleIndex);
            const currentRule = job.tags[tagIndex].rules[ruleIndex];
            currentRule.conditions = Array.isArray(currentRule.conditions) ? currentRule.conditions : [];
            currentRule.conditions.push(buildDefaultCondition());
            renderEditor();
            setStatus("New condition added to the rule draft.");
          });
          document.querySelectorAll("[data-role='cond-field']").forEach(node => node.oninput = (event) => {
            job.tags[Number(event.target.dataset.tagIndex)].rules[Number(event.target.dataset.ruleIndex)].conditions[Number(event.target.dataset.index)].field = event.target.value.trim();
          });
          document.querySelectorAll("[data-role='cond-operator']").forEach(node => node.onchange = (event) => {
            job.tags[Number(event.target.dataset.tagIndex)].rules[Number(event.target.dataset.ruleIndex)].conditions[Number(event.target.dataset.index)].operator = event.target.value;
          });
          document.querySelectorAll("[data-role='cond-agg']").forEach(node => node.onchange = (event) => {
            job.tags[Number(event.target.dataset.tagIndex)].rules[Number(event.target.dataset.ruleIndex)].conditions[Number(event.target.dataset.index)].agg = event.target.value;
          });
          document.querySelectorAll("[data-role='cond-value']").forEach(node => node.oninput = (event) => {
            job.tags[Number(event.target.dataset.tagIndex)].rules[Number(event.target.dataset.ruleIndex)].conditions[Number(event.target.dataset.index)].value = event.target.value;
          });
          document.querySelectorAll("[data-role='save-condition']").forEach(node => node.onclick = () => {
            const tagIndex = Number(node.dataset.tagIndex);
            const ruleIndex = Number(node.dataset.ruleIndex);
            const conditionIndex = Number(node.dataset.index);
            const currentCondition = job.tags[tagIndex].rules[ruleIndex].conditions[conditionIndex];
            const error = validateCondition(currentCondition, job.tags[tagIndex].rules[ruleIndex].id || "rule");
            if (error) { setStatus(error, "error"); return; }
            renderEditor();
            setStatus("Condition validated in context.", "ok");
          });
          document.querySelectorAll("[data-role='copy-condition']").forEach(node => node.onclick = () => {
            const tagIndex = Number(node.dataset.tagIndex);
            const ruleIndex = Number(node.dataset.ruleIndex);
            const conditionIndex = Number(node.dataset.index);
            const currentRule = job.tags[tagIndex].rules[ruleIndex];
            currentRule.conditions.splice(conditionIndex + 1, 0, clone(currentRule.conditions[conditionIndex]));
            renderEditor();
            setStatus("Copied condition.");
          });
          document.querySelectorAll("[data-role='delete-condition']").forEach(node => node.onclick = () => {
            const tagIndex = Number(node.dataset.tagIndex);
            const ruleIndex = Number(node.dataset.ruleIndex);
            const conditionIndex = Number(node.dataset.index);
            job.tags[tagIndex].rules[ruleIndex].conditions.splice(conditionIndex, 1);
            renderEditor();
            setStatus("Condition removed from the rule draft.");
          });
        }

        async function saveAll() {
          const error = validateAllJobs();
          if (error) {
            setStatus(error, "error");
            return;
          }
          setStatus("Saving...");
          const res = await fetch(`/api/config/${CONFIG_SLUG}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ jobs: state.jobs })
          });
          const data = await res.json();
          if (!res.ok) {
            setStatus(`Save failed: ${data.detail || res.status}`, "error");
            return;
          }
          state.jobs = data.jobs || state.jobs;
          if (selectedJobIndex < 0 && state.jobs.length) selectedJobIndex = 0;
          await refreshFieldOptions(((getSelectedJob() || {}).inputs || [])[0] || "");
          renderJobList();
          renderEditor();
          setStatus("Saved to usr/config/custom/tag_engine.yaml.", "ok");
        }

        async function init() {
          const res = await fetch(`/api/config/${CONFIG_SLUG}`);
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
          state = data;
          document.getElementById("subtitle").textContent = `Loaded from ${data.loaded_from}. Saves go to ${data.save_path}.`;
          selectedJobIndex = data.jobs.length ? 0 : -1;
          await refreshFieldOptions(((getSelectedJob() || {}).inputs || [])[0] || "");
          renderJobList();
          renderEditor();
          setStatus("Ready.");
        }

        document.getElementById("newJobBtn").onclick = async () => {
          state.jobs.push(buildDefaultJob());
          selectedJobIndex = state.jobs.length - 1;
          jobFieldOptions = [];
          renderJobList();
          renderEditor();
          setStatus("New job created in the working draft.");
        };
        document.getElementById("copyJobBtn").onclick = async () => {
          const job = getSelectedJob();
          if (!job) return;
          state.jobs.push(copyWithDuplicateId(job));
          selectedJobIndex = state.jobs.length - 1;
          await refreshFieldOptions(((getSelectedJob() || {}).inputs || [])[0] || "");
          renderJobList();
          renderEditor();
          setStatus("Copied job. Change the job id before saving.", "error");
        };
        document.getElementById("saveAllBtn").onclick = saveAll;
        init().catch((err) => setStatus(`Failed to load: ${err}`, "error"));
      </script>
    </body>
    </html>
        """
        return HTMLResponse(html)

    html = f"""
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>Configuration Editor</title>
      <style>
        :root {{ --ink:#e6eefc; --line:rgba(121,184,255,.25); --bg:#0b1320; --surface:#122238; --muted:#a9bdd7; --accent:#f0b24a; }}
        * {{ box-sizing:border-box; }}
        body {{ margin:0; font:14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color:var(--ink); background:radial-gradient(circle at top right, rgba(121,184,255,.14), transparent 32%), radial-gradient(circle at bottom left, rgba(240,178,74,.1), transparent 36%), linear-gradient(180deg,#0b1320,#101d2f); }}
        .top {{ position:sticky; top:0; z-index:100; backdrop-filter: blur(8px); background:rgba(8,16,28,.86); border-bottom:1px solid var(--line); padding:10px 16px; display:flex; gap:10px; align-items:center; }}
        .btn {{ border:1px solid rgba(67,100,135,.7); background:rgba(19,34,56,.92); color:var(--ink); border-radius:999px; padding:8px 12px; cursor:pointer; text-decoration:none; font-weight:600; }}
        .btn:hover {{ background:rgba(27,48,77,.98); border-color:rgba(121,184,255,.7); }}
        .wrap {{ width:min(1480px, calc(100% - 28px)); margin:16px auto 28px; display:grid; gap:12px; }}
        .panel {{ background:linear-gradient(180deg, rgba(19,35,57,.96), rgba(15,28,45,.96)); border:1px solid var(--line); border-radius:14px; padding:14px; box-shadow:0 18px 48px rgba(0,0,0,.28); }}
        .grid {{ display:grid; grid-template-columns: 320px 1fr; gap:12px; }}
        .list {{ display:grid; gap:8px; max-height:640px; overflow:auto; }}
        .job-item {{ border:1px solid rgba(71,85,105,.26); border-radius:12px; padding:10px; background:rgba(12,22,36,.76); cursor:pointer; }}
        .job-item.active {{ border-color:rgba(121,184,255,.7); background:rgba(24,40,62,.9); }}
        .field {{ margin-bottom:10px; }}
        label {{ display:block; font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin-bottom:4px; }}
        label.required::after {{ content:" *"; color:var(--accent); }}
        input, textarea {{ width:100%; border:1px solid rgba(67,100,135,.65); border-radius:8px; padding:8px; background:rgba(14,24,40,.92); color:var(--ink); }}
        textarea {{ min-height:80px; resize:vertical; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
        .muted {{ color:var(--muted); }}
        .hero {{ margin:10px auto 0; width:min(1480px, calc(100% - 28px)); min-height:140px; color:#fff; position:relative; overflow:hidden; border:1px solid rgba(121,184,255,.25); border-radius:18px; background:linear-gradient(135deg,#1e293b 0%, #475569 50%, #64748b 100%); }}
        .hero::before {{ content:""; position:absolute; inset:0; background-image:linear-gradient(rgba(255,255,255,.08) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.08) 1px, transparent 1px); background-size:60px 60px; opacity:.7; }}
        .hero-content {{ position:relative; z-index:1; padding:24px; }}
        .hero h1 {{ margin:0 0 8px; font-size:clamp(1.6rem,3vw,2.1rem); }}
        @media (max-width:1150px){{ .grid{{grid-template-columns:1fr;}} }}
      </style>
    </head>
    <body>
      <div class="top">
        <a class="btn" href="/">Back to Home</a>
        <button class="btn" id="newJobBtn">New Job</button>
        <button class="btn" id="saveBtn">Save</button>
        <span class="muted" id="status">Loading...</span>
      </div>
      <section class="hero">
        <div class="hero-content">
          <h1 id="pageTitle">Configuration</h1>
          <p class="muted" id="pageSubtitle">Edit jobs for this configuration. Changes are saved to usr/config/custom.</p>
        </div>
      </section>
      <div class="wrap">
        <div class="grid">
          <section class="panel">
            <h2 style="margin:0 0 8px;">Jobs</h2>
            <div class="list" id="jobList"></div>
            <div class="muted" id="jobEmpty">No jobs yet. Click “New Job” to create one.</div>
          </section>
          <section class="panel">
            <h2 style="margin:0 0 8px;">Job Editor</h2>
            <div id="jobEditor" class="muted">Select a job to edit.</div>
          </section>
        </div>
      </div>
      <script>
        const CONFIG_SLUG = {json.dumps(spec["slug"])};
        let state = {{
          jobs: [],
          template_job: {json.dumps(spec["template_job"])},
          required_fields: {json.dumps(spec["required_fields"])},
          title: {json.dumps(spec["title"])},
          loaded_from: "",
          save_path: ""
        }};
        let selectedIndex = -1;

        function esc(v) {{
          return (v == null ? "" : String(v))
            .replaceAll("&","&amp;")
            .replaceAll("<","&lt;")
            .replaceAll(">","&gt;")
            .replaceAll('"','&quot;');
        }}

        function clone(obj) {{
          return JSON.parse(JSON.stringify(obj || {{}}));
        }}

        function setStatus(msg) {{
          document.getElementById("status").textContent = msg;
        }}

        function buildEmptyJob() {{
          const template = state.template_job && Object.keys(state.template_job).length ? state.template_job : {{ id: "" }};
          return clone(template);
        }}

        function renderJobList() {{
          const list = document.getElementById("jobList");
          const empty = document.getElementById("jobEmpty");
          list.innerHTML = "";
          if (!state.jobs.length) {{
            empty.style.display = "";
            return;
          }}
          empty.style.display = "none";
          state.jobs.forEach((job, idx) => {{
            const item = document.createElement("div");
            item.className = "job-item" + (idx === selectedIndex ? " active" : "");
            const label = job.id ? job.id : `Job ${{idx + 1}}`;
            item.innerHTML = `<strong>${{esc(label)}}</strong><div class="muted">${{esc(job.description || "")}}</div>`;
            item.onclick = () => {{
              selectedIndex = idx;
              renderJobList();
              renderEditor();
            }};
            list.appendChild(item);
          }});
        }}

        function fieldType(value) {{
          if (Array.isArray(value)) {{
            if (value.length && typeof value[0] === "object") {{
              return {{ type: "json", kind: "list" }};
            }}
            return {{ type: "list", kind: "list" }};
          }}
          if (value && typeof value === "object") {{
            return {{ type: "json", kind: "object" }};
          }}
          return {{ type: "scalar", kind: "scalar" }};
        }}

        function renderEditor() {{
          const host = document.getElementById("jobEditor");
          host.innerHTML = "";
          if (selectedIndex < 0 || selectedIndex >= state.jobs.length) {{
            host.textContent = "Select a job to edit.";
            return;
          }}
          const job = state.jobs[selectedIndex];
          const fields = Object.keys(job);
          if (!fields.length) {{
            host.textContent = "No fields available for this job.";
            return;
          }}
          fields.forEach((field) => {{
            const value = job[field];
            const meta = fieldType(value);
            const wrapper = document.createElement("div");
            wrapper.className = "field";
            const label = document.createElement("label");
            label.textContent = field;
            if (state.required_fields.includes(field)) {{
              label.classList.add("required");
            }}
            wrapper.appendChild(label);
            let input;
            if (meta.type === "scalar") {{
              input = document.createElement("input");
              input.value = value == null ? "" : String(value);
            }} else {{
              input = document.createElement("textarea");
              if (meta.type === "list") {{
                input.value = Array.isArray(value) ? value.join("\\n") : "";
              }} else {{
                input.value = value && Object.keys(value || {{}}).length ? JSON.stringify(value, null, 2) : "";
              }}
            }}
            input.dataset.field = field;
            input.dataset.type = meta.type;
            input.dataset.kind = meta.kind;
            input.oninput = () => {{
              const updated = readFieldValue(input);
              if (updated !== undefined) {{
                job[field] = updated;
                if (field === "id") {{
                  renderJobList();
                }}
              }}
            }};
            wrapper.appendChild(input);
            host.appendChild(wrapper);
          }});
        }}

        function readFieldValue(input) {{
          const type = input.dataset.type;
          const kind = input.dataset.kind;
          if (type === "scalar") {{
            return input.value.trim();
          }}
          if (type === "list") {{
            return input.value.split("\\n").map(s => s.trim()).filter(Boolean);
          }}
          if (type === "json") {{
            const raw = input.value.trim();
            if (!raw) {{
              return kind === "list" ? [] : {{}};
            }}
            try {{
              return JSON.parse(raw);
            }} catch (err) {{
              setStatus(`Invalid JSON in field: ${{input.dataset.field}}`);
              return undefined;
            }}
          }}
          return input.value;
        }}

        function validateJobs() {{
          const required = state.required_fields || [];
          const ids = new Set();
          for (const job of state.jobs) {{
            if (!job.id) {{
              return "Each job requires an id.";
            }}
            if (ids.has(job.id)) {{
              return `Duplicate job id: ${{job.id}}`;
            }}
            ids.add(job.id);
            for (const key of required) {{
              if (job[key] == null) {{
                return `Missing required field: ${{key}}`;
              }}
              if (Array.isArray(job[key]) && job[key].length === 0) {{
                return `Missing required field: ${{key}}`;
              }}
              if (typeof job[key] === "string" && job[key].trim() === "") {{
                return `Missing required field: ${{key}}`;
              }}
            }}
          }}
          return "";
        }}

        async function save() {{
          setStatus("Saving...");
          const error = validateJobs();
          if (error) {{
            setStatus(error);
            return;
          }}
          const res = await fetch(`/api/config/${{CONFIG_SLUG}}`, {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{ jobs: state.jobs }})
          }});
          const data = await res.json();
          if (!res.ok) {{
            setStatus(`Save failed: ${{data.detail || res.status}}`);
            return;
          }}
          setStatus("Saved.");
        }}

        async function init() {{
          const res = await fetch(`/api/config/${{CONFIG_SLUG}}`);
          if (!res.ok) {{
            const err = await res.json().catch(() => ({{}}));
            throw new Error(err.detail || `HTTP ${{res.status}}`);
          }}
          const data = await res.json();
          state.jobs = data.jobs || [];
          state.template_job = data.template_job || {{}};
          state.required_fields = data.required_fields || [];
          state.title = data.title || "Configuration";
          state.loaded_from = data.loaded_from || "";
          state.save_path = data.save_path || "";
          document.getElementById("pageTitle").textContent = state.title;
          document.getElementById("pageSubtitle").textContent = state.loaded_from
            ? `Loaded from ${{data.loaded_from}}. Saves go to ${{state.save_path}}.`
            : "Edit configuration jobs. Saves go to usr/config/custom.";
          selectedIndex = state.jobs.length ? 0 : -1;
          renderJobList();
          renderEditor();
          setStatus("Ready.");
        }}

        document.getElementById("newJobBtn").onclick = () => {{
          state.jobs.push(buildEmptyJob());
          selectedIndex = state.jobs.length - 1;
          renderJobList();
          renderEditor();
        }};
        document.getElementById("saveBtn").onclick = save;

        init().catch((err) => setStatus(`Failed to load: ${{err}}`));
      </script>
    </body>
    </html>
    """
    return HTMLResponse(html)


@app.get("/api/config/{config_slug}", response_class=JSONResponse)
async def get_generic_config(config_slug: str, request: Request) -> JSONResponse:
    redirect = require_login(request)
    if redirect:
        raise HTTPException(status_code=401, detail="Login required")

    spec = get_config_spec(config_slug)
    if spec["slug"] == "tag_engine":
        return JSONResponse(build_tag_engine_response(spec))
    return JSONResponse(
        {
            "slug": spec["slug"],
            "title": spec["title"],
            "description": spec["description"],
            "jobs": spec["jobs"],
            "template_job": spec["template_job"],
            "required_fields": spec["required_fields"],
            "loaded_from": str(spec["loaded_from"]),
            "save_path": str(spec["custom_path"]),
        }
    )


@app.post("/api/config/{config_slug}", response_class=JSONResponse)
async def save_generic_config(config_slug: str, request: Request) -> JSONResponse:
    redirect = require_login(request)
    if redirect:
        raise HTTPException(status_code=401, detail="Login required")

    spec = get_config_spec(config_slug)
    payload = await request.json()
    jobs = payload.get("jobs")
    if spec["slug"] == "tag_engine":
        sanitized_jobs = sanitize_tag_engine_jobs(jobs)
        custom_doc = copy.deepcopy(spec["custom_doc"]) if isinstance(spec["custom_doc"], dict) else {}
        if not custom_doc:
            custom_doc = _skeleton_for_path(spec["default_doc"], spec["jobs_path"])
        target_jobs = _ensure_path(custom_doc, spec["jobs_path"])
        target_jobs[:] = sanitized_jobs
        save_yaml_dict(spec["custom_path"], custom_doc)
        refreshed_spec = get_config_spec(config_slug)
        return JSONResponse(
            {
                "status": "ok",
                "saved_to": str(spec["custom_path"]),
                "job_count": len(sanitized_jobs),
                "jobs": refreshed_spec["jobs"],
            }
        )
    if not isinstance(jobs, list):
        raise HTTPException(status_code=400, detail="Payload must include a jobs list")

    sanitized_jobs: List[Dict[str, Any]] = []
    seen_ids = set()
    for index, job in enumerate(jobs):
        if not isinstance(job, dict):
            raise HTTPException(status_code=400, detail=f"Job at index {index} must be an object")
        copied_job = copy.deepcopy(job)
        job_id = str(copied_job.get("id") or "").strip()
        if not job_id:
            raise HTTPException(status_code=400, detail="Each job requires an id")
        if job_id in seen_ids:
            raise HTTPException(status_code=400, detail=f"Duplicate job id: {job_id}")
        seen_ids.add(job_id)
        copied_job["id"] = job_id
        sanitized_jobs.append(copied_job)

    custom_doc = copy.deepcopy(spec["custom_doc"]) if isinstance(spec["custom_doc"], dict) else {}
    if not custom_doc:
        custom_doc = _skeleton_for_path(spec["default_doc"], spec["jobs_path"])
    target_jobs = _ensure_path(custom_doc, spec["jobs_path"])
    target_jobs[:] = sanitized_jobs

    save_yaml_dict(spec["custom_path"], custom_doc)
    return JSONResponse(
        {
            "status": "ok",
            "saved_to": str(spec["custom_path"]),
            "job_count": len(sanitized_jobs),
        }
    )


@app.get("/reports", response_class=HTMLResponse)
async def list_reports(request: Request) -> HTMLResponse:
    redirect = require_login(request)
    if redirect:
        return redirect

    user = get_current_user(request) or "unknown"

    if not REPORT_DIR.exists():
        log("warning", f"REPORT_DIR does not exist: {REPORT_DIR}")
        files: List[str] = []
    else:
        files = sorted(
            [f.name for f in REPORT_DIR.glob("*.html") if f.is_file()]
        )

    items_html = ""
    if files:
        for fname in files:
            items_html += f"<li><a href='/reports/static/{fname}' target='_blank' style='color:#8ab4ff;'>{fname}</a></li>"
    else:
        items_html = "<li>No reports found.</li>"

    html = f"""
    <html>
      <head>
        <title>VTX Reports</title>
      </head>
      <body style="background:#121212;color:#eee;font-family:sans-serif;">
        <h1>Reports</h1>
        <p>User: {user}</p>
        <ul>
          {items_html}
        </ul>
        <p><a href="/" style="color:#8ab4ff;">Back to home</a></p>
      </body>
    </html>
    """
    return HTMLResponse(html)


# Static mounts for all first-level var directories.
# This keeps new content folders available at /<folder>/static without code changes.
if VAR_DIR.exists():
    for child in sorted(VAR_DIR.iterdir()):
        if not child.is_dir() or child.name.startswith('.'):
            continue
        route = f"/{child.name}/static"
        app.mount(route, StaticFiles(directory=str(child)), name=f"{child.name}_static")

        # Preserve legacy aliases that existing templates already link to.
        if child.name == "reporting":
            app.mount("/reports/static", StaticFiles(directory=str(child)), name="reports_static")
        elif child.name == "analysis":
            app.mount("/analysis/static", StaticFiles(directory=str(child)), name="analysis_static")
else:
    log("warning", f"Cannot mount var directory (not found): {VAR_DIR}")

# Optional static for future CSS/JS
if STATIC_DIR.exists():
    app.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="static",
    )
