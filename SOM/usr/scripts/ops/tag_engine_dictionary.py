#!/usr/bin/env python3
from __future__ import annotations

import os
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Iterable, List

import yaml


def resolve_vtx_root() -> Path:
    env_root = os.environ.get("VTX_ROOT") or os.environ.get("BTDM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


VTX_ROOT = resolve_vtx_root()
INPUT_PATH = VTX_ROOT / "usr" / "config" / "run" / "tag_engine.yaml"
OUTPUT_PATH = VTX_ROOT / "var" / "dictionaries" / "tag_engine_dictionary.md"


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"", "nan", "none", "null"} else text


def prettify(value: str) -> str:
    return normalize_text(value).replace("_", " ")


def load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Tag Engine config not found: {path}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"Malformed YAML root in {path}")
    cfg = doc.get("config") if isinstance(doc.get("config"), dict) else doc
    if not isinstance(cfg, dict):
        raise ValueError(f"Malformed config section in {path}")
    return cfg


def extract_jobs(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    payload = cfg.get("payload") if isinstance(cfg.get("payload"), dict) else {}
    jobs = payload.get("jobs") if isinstance(payload.get("jobs"), list) else cfg.get("jobs")
    return [job for job in (jobs or []) if isinstance(job, dict) and job.get("enabled", True)]


def english_list(values: Iterable[str], conjunction: str = "or") -> str:
    parts = [normalize_text(v) for v in values if normalize_text(v)]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} {conjunction} {parts[1]}"
    return f"{', '.join(parts[:-1])}, {conjunction} {parts[-1]}"


def operator_phrase(subject: str, operator: str, value: Any) -> str:
    op = normalize_text(operator).lower()
    if op in {"==", "=", "eq"}:
        return f"{subject} equals {normalize_text(value)}"
    if op in {"!=", "ne"}:
        return f"{subject} does not equal {normalize_text(value)}"
    if op == ">":
        return f"{subject} is greater than {normalize_text(value)}"
    if op == ">=":
        return f"{subject} is at least {normalize_text(value)}"
    if op == "<":
        return f"{subject} is less than {normalize_text(value)}"
    if op == "<=":
        return f"{subject} is at most {normalize_text(value)}"
    if op == "contains":
        return f"{subject} contains {normalize_text(value)}"
    if op == "notcontains":
        return f"{subject} does not contain {normalize_text(value)}"
    if op in {"isnull", "is_null"}:
        return f"{subject} is blank"
    if op in {"isnotnull", "is_not_null"}:
        return f"{subject} is present"
    if op == "in_range":
        if isinstance(value, list) and len(value) >= 2:
            low = normalize_text(value[0])
            high = normalize_text(value[1])
            return f"{subject} is between {low} and {high}"
        return f"{subject} is in the allowed range"
    return f"{subject} matches {normalize_text(value)}"


def condition_subject(cond: Dict[str, Any]) -> str:
    field = normalize_text(cond.get("field"))
    tag_key = normalize_text(cond.get("tag_key"))
    agg = normalize_text(cond.get("agg") or cond.get("aggregate")).lower()
    if field:
        label = prettify(field)
        if agg == "count":
            return f"the count of {label}"
        if agg == "unique_count":
            return f"the unique count of {label}"
        if agg == "sum":
            return f"the total of {label}"
        if agg == "avg":
            return f"the average of {label}"
        if agg == "min":
            return f"the minimum {label}"
        if agg == "max":
            return f"the maximum {label}"
        return label
    if tag_key:
        return f"the {prettify(tag_key)} tag"
    return "the value"


def condition_sentence(cond: Dict[str, Any]) -> str:
    subject = condition_subject(cond)
    return operator_phrase(subject, cond.get("operator", "=="), cond.get("value"))


def combine_conditions(conditions: List[Dict[str, Any]], logic: str) -> str:
    parts = [condition_sentence(cond) for cond in conditions if isinstance(cond, dict)]
    if not parts:
        return ""
    joiner = " or " if logic == "any" else " and "
    if len(parts) == 1:
        return f"If {parts[0]}"
    return f"If {joiner.join(parts)}"


def rule_sentence(rule: Dict[str, Any], default_logic: str) -> str:
    conditions = rule.get("conditions") or []
    if not isinstance(conditions, list) or not conditions:
        return "No conditions were defined."
    logic = normalize_text(rule.get("condition_logic")).lower() or default_logic
    return combine_conditions(conditions, "any" if logic == "any" else "all")


def append_rule(
    grouped: "OrderedDict[str, OrderedDict[str, OrderedDict[str, List[str]]]]",
    category: str,
    key: str,
    value: str,
    sentence: str,
) -> None:
    grouped.setdefault(category, OrderedDict())
    grouped[category].setdefault(key, OrderedDict())
    grouped[category][key].setdefault(value, [])
    if sentence and sentence not in grouped[category][key][value]:
        grouped[category][key][value].append(sentence)


def collect_tag_dictionary(jobs: List[Dict[str, Any]]) -> "OrderedDict[str, OrderedDict[str, OrderedDict[str, List[str]]]]":
    grouped: "OrderedDict[str, OrderedDict[str, OrderedDict[str, List[str]]]]" = OrderedDict()
    for job in jobs:
        for tag in job.get("tags") or []:
            if not isinstance(tag, dict):
                continue
            category = prettify(normalize_text(tag.get("tag_category")))
            key = prettify(normalize_text(tag.get("tag_key")))
            default_logic = normalize_text(tag.get("condition_logic")).lower() or "all"
            if not category or not key:
                continue
            for rule in tag.get("rules") or []:
                if not isinstance(rule, dict):
                    continue
                value = prettify(normalize_text(rule.get("tag_value")))
                if not value:
                    continue
                append_rule(grouped, category, key, value, rule_sentence(rule, default_logic))

        for tag in job.get("derived_tags") or []:
            if not isinstance(tag, dict):
                continue
            category = prettify(normalize_text(tag.get("tag_category")))
            key = prettify(normalize_text(tag.get("tag_key")))
            default_logic = normalize_text(tag.get("condition_logic")).lower() or "all"
            if not category or not key:
                continue
            for rule in tag.get("rules") or []:
                if not isinstance(rule, dict):
                    continue
                value = prettify(normalize_text(rule.get("tag_value")))
                if not value:
                    continue
                sentence = rule_sentence(rule, default_logic)
                exclude = rule.get("exclude") or []
                if isinstance(exclude, list) and exclude:
                    exclude_text = combine_conditions(exclude, "any")
                    if exclude_text:
                        sentence = f"{sentence}. Excluding cases where {exclude_text[3:].lower()}"
                append_rule(grouped, category, key, value, sentence)
    return grouped


def render_markdown(grouped: "OrderedDict[str, OrderedDict[str, OrderedDict[str, List[str]]]]") -> str:
    lines: List[str] = ["# Tag Engine Dictionary", ""]
    if not grouped:
        lines.extend(["No tags were found in the active Tag Engine configuration.", ""])
        return "\n".join(lines)

    for category, keys in grouped.items():
        lines.append(f"## {category}")
        lines.append("")
        for key, values in keys.items():
            lines.append(f"### {key}")
            lines.append("")
            for value, sentences in values.items():
                lines.append(f"- {value}")
                for sentence in sentences:
                    lines.append(f"  - {sentence}")
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    cfg = load_yaml(INPUT_PATH)
    jobs = extract_jobs(cfg)
    if not jobs:
        raise ValueError(f"No enabled Tag Engine jobs found in {INPUT_PATH}")
    grouped = collect_tag_dictionary(jobs)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(render_markdown(grouped), encoding="utf-8")
    print(f"Wrote dictionary to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
