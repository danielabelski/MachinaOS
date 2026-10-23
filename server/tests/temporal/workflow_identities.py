"""Command identity inventory; payload/retry/timeout changes do not alter it."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

SERVER = Path(__file__).resolve().parents[2]
COMMANDS = {"execute_activity", "start_activity", "execute_child_workflow", "start_child_workflow", "patched", "deprecate_patch"}


def identity_snapshot(source_root: Path = SERVER) -> dict:
    paths = sorted((source_root / "services" / "temporal").glob("*.py"))
    paths.append(source_root / "nodes" / "scheduler" / "cron_scheduler" / "_workflow.py")
    result = {}
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        has_workflow = any(isinstance(node, ast.ClassDef) and any(
            isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == "defn"
            for dec in node.decorator_list
        ) for node in tree.body)
        has_commands = any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                           and isinstance(node.func.value, ast.Name) and node.func.value.id == "workflow"
                           and node.func.attr in COMMANDS for node in ast.walk(tree))
        if not has_workflow and not has_commands:
            continue
        strings = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                strings.update({target.id: node.value.value for target in node.targets if isinstance(target, ast.Name)})
        commands, identifiers, bindings = [], [], []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in COMMANDS:
                if not node.args:
                    continue
                first = node.args[0]
                command = {"command": node.func.attr, "type": strings.get(first.id, ast.unparse(first)) if isinstance(first, ast.Name) else ast.unparse(first)}
                command.update({keyword.arg: ast.unparse(keyword.value) for keyword in node.keywords if keyword.arg in {"id", "activity_id"}})
                commands.append(command)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name.endswith("_id") or node.name.endswith("_identity")
            ):
                # Include helper bodies, because commands often reference a
                # computed local id rather than an inline format string.
                identifiers.append(ast.unparse(node))
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and (target.id.endswith("_id") or target.id.endswith("_identity")):
                        bindings.append({target.id: ast.unparse(node.value)})
        result[str(path.relative_to(source_root)).replace("\\", "/")] = {
            "commands": sorted(commands, key=lambda item: json.dumps(item, sort_keys=True)),
            "identifier_helpers": sorted(identifiers),
            "id_bindings": sorted(bindings, key=lambda item: json.dumps(item, sort_keys=True)),
        }
    return result


def digest(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
