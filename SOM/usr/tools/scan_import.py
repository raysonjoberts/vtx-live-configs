import ast
import os
from pathlib import Path

ROOTS = [
    Path(r"C:\BTDM_7.1\usr\scripts\default"),
    Path(r"C:\BTDM_7.1\usr\utils"),
    Path(r"C:\BTDM_7.1\usr\utils\OnDemand"),
]

stdlib_prefixes = {
    "os", "sys", "time", "json", "csv", "re", "math", "pathlib",
    "logging", "argparse", "datetime", "subprocess", "threading",
    "queue", "shutil", "glob", "itertools", "functools", "typing",
    "collections", "dataclasses", "signal", "platform"
}

imports = set()

def scan_file(pyfile):
    try:
        tree = ast.parse(pyfile.read_text(encoding="utf-8"))
    except Exception:
        return

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                imports.add(n.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.split(".")[0])

for root in ROOTS:
    if not root.exists():
        continue
    for py in root.rglob("*.py"):
        scan_file(py)

third_party = sorted(i for i in imports if i not in stdlib_prefixes)

print("\n".join(third_party))
