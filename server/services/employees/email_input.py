"""Bound managed Gmail intake without changing its durable original source.

Template values are frozen once. Only Gmail body substitutions may shrink;
literal instructions and every other field remain intact.
"""
from __future__ import annotations

import re
from typing import Any

MISSION_LIMIT = 20000
SHORTENED_NOTICE = "[Email body shortened for initial context. Use employee_job_source to read the complete body.]"
_TEMPLATE = re.compile(r"\{\{([^}]+)\}\}")


class EmployeeEmailInputLimit(ValueError):
    code = "employee_email_input_limit"

    def __init__(self):
        super().__init__("employee_email_input_limit: Instructions and non-body email fields exceed the 20,000-character mission limit")


def readable_email_body(output: dict) -> str:
    text = output.get("body_text")
    if isinstance(text, str):
        return text
    raw = str(output.get("body") or "")
    if output.get("body_mime_type") == "text/html" or re.search(r"<(?:html|body|div|p|span|table|a|style|script)(?:\s|>)", raw, re.I):
        from nodes.google._gmail import html_body_text
        return html_body_text(raw)
    return raw


def _value(data: dict, expression: str) -> str:
    from services.parameter_resolver import ParameterResolver
    path = expression.split(".")
    value = ParameterResolver._navigate_path(None, data.get(path[0].lower()), path[1:])
    return "" if value is None else str(value)


def project_gmail_mission(template: str, connected_data: dict, *, source_node_id: str,
                          source_key: str, limit: int = MISSION_LIMIT) -> dict:
    """Render original and readable missions from the same frozen values.

    The result is ``{mission, metadata}``; metadata is server-owned source
    material, never model arguments. Repeated body placeholders share one
    budget, and each shortened substitution carries the pagination notice.
    """
    data = {str(key).lower(): value for key, value in connected_data.items()}
    email = data.get(source_key.lower()) or {}
    if not isinstance(email, dict):
        raise ValueError("The Gmail source output must be an object")
    body_text = readable_email_body(email)
    pieces: list[tuple[str, bool]] = []
    original_parts: list[str] = []
    position = 0
    for match in _TEMPLATE.finditer(template):
        literal = template[position:match.start()]
        pieces.append((literal, False))
        original_parts.append(literal)
        expression = match.group(1)
        path = expression.split(".")
        body = len(path) == 2 and path[0].lower() == source_key.lower() and path[1] in {"body", "body_text"}
        value = _value(data, expression)
        # Old recordings lack body_text; render the intended readable value.
        if body and path[1] == "body_text" and email.get("body_text") is None:
            value = body_text
        original_parts.append(value)
        pieces.append((body_text if body else value, body))
        position = match.end()
    pieces.append((template[position:], False))
    original_parts.append(template[position:])
    original_mission = "".join(original_parts)
    fixed = sum(len(value) for value, body in pieces if not body)
    if fixed > limit:
        raise EmployeeEmailInputLimit()
    mission = "".join(value for value, _ in pieces)
    if len(mission) > limit:
        count = sum(body for _, body in pieces)
        if not count or fixed + count * len(SHORTENED_NOTICE) > limit:
            raise EmployeeEmailInputLimit()
        available = limit - fixed - count * len(SHORTENED_NOTICE)
        per_body = available // count
        # Include a separating newline only when it fits inside the allocation.
        replacement = body_text[:max(0, per_body - 1)] + ("\n" if per_body else "") + SHORTENED_NOTICE
        mission = "".join(replacement if body else value for value, body in pieces)
    return {"mission": mission, "metadata": {"version": 1, "source_node_id": source_node_id,
            "original_mission": original_mission, "body_text": body_text}}


async def project_managed_gmail_parameters(database: Any, node_id: str, parameters: dict,
                                           context: dict, resolve_params_fn=None) -> tuple[dict, dict, bool]:
    """Resolve managed v1 intake/v2 lead templates before Params validation.

    ``handled`` means all parameters were resolved against one frozen map, so
    callers must not resolve them a second time. Current-run outputs win over
    session storage; Gmail selection itself requires a current-run output.
    """
    nodes = context.get("nodes") or []
    node = next((item for item in nodes if item.get("id") == node_id), {})
    node_type = node.get("type") or context.get("node_type")
    if node_type == "employeeJob" and parameters.get("operation") == "intake":
        field = "mission"
    elif (context.get("employee_team_plan") or {}).get("lead_node_id") == node_id:
        # Prepare resolved the plan from the generation's admitted graph.
        field = "prompt"
    else:
        return parameters, context, False
    template = parameters.get(field)
    if not isinstance(template, str) or not template:
        return parameters, context, False
    from services.employees.store import get_by_workflow
    employee = await get_by_workflow(database, str(context.get("workflow_id") or ""))
    if not employee or (field == "mission" and not employee.team_plan):
        return parameters, context, False
    outputs = context.get("outputs") or context.get("inputs") or {}
    if not isinstance(outputs, dict):
        return parameters, context, False
    from services.parameter_resolver import ParameterResolver
    resolver = getattr(resolve_params_fn, "__self__", None)
    if not isinstance(resolver, ParameterResolver):
        async def get_output(session_id, source_id, output_name):
            callback = context.get("get_output_fn")
            return await callback(session_id, source_id, output_name) if callback else None
        resolver = ParameterResolver(database, get_output)
    sources = [item for item in nodes if item.get("type") == "googleGmailReceive" and item.get("id") in outputs
               and isinstance(outputs[item["id"]], dict)]
    referenced = [source for source in sources if any(match.group(1).split(".")[0].lower() == resolver._get_template_key(source).lower()
                  for match in _TEMPLATE.finditer(template))]
    if len(referenced) != 1:
        return parameters, context, False
    source = referenced[0]
    frozen = await resolver._gather_connected_outputs(node_id, nodes, context.get("edges") or [],
                    context.get("session_id", "default"), outputs)
    projected = project_gmail_mission(template, frozen, source_node_id=str(source["id"]), source_key=resolver._get_template_key(source))
    resolved = resolver._resolve_templates(parameters, frozen)
    resolved[field] = projected["mission"]
    return resolved, {**context, "email_input": projected["metadata"]}, True


def project_stored_mission(mission: str, source: dict, *, limit: int = MISSION_LIMIT) -> str:
    """Project oversized historical SQLite missions at read time only.

    When no captured body can be identified safely, fail rather than remove
    unknown custom instructions. Stored source/identity are never rewritten.
    """
    if len(mission) <= limit:
        return mission
    metadata = source.get("email_input") or {}
    source_id = metadata.get("source_node_id")
    outputs = source.get("outputs") or {}
    if not source_id:
        candidates = [node.get("id") for node in source.get("nodes", [])
                      if node.get("type") == "googleGmailReceive" and node.get("id") in outputs]
        if len(candidates) != 1:
            raise EmployeeEmailInputLimit()
        source_id = candidates[0]
    email = outputs.get(source_id) or {}
    # Saved intake templates identify body substitutions even when literal
    # instructions contain an identical copy of the body. Prefer that proof
    # over guessing which occurrence was inserted by the resolver.
    from services.parameter_resolver import ParameterResolver
    frozen = {ParameterResolver._get_template_key(None, node).lower(): outputs[node["id"]]
              for node in source.get("nodes", []) if node.get("id") in outputs}
    source_node = next((node for node in source.get("nodes", []) if node.get("id") == source_id), None)
    source_key = ParameterResolver._get_template_key(None, source_node) if source_node else "source"
    parameters = source.get("parameters") or {}
    for node in source.get("nodes", []):
        settings = parameters.get(node.get("id")) or {}
        if node.get("type") == "employeeJob" and settings.get("operation") == "intake":
            template = settings.get("mission")
        elif node.get("id") == (source.get("team_plan") or {}).get("lead_node_id"):
            template = settings.get("prompt")
        else:
            continue
        if not isinstance(template, str) or "{{" not in template:
            continue
        # Compare the complete original first, avoiding limit failures from
        # unrelated graph templates that happen to reference the same source.
        rendered = _TEMPLATE.sub(lambda match: _value(frozen, match.group(1)), template)
        if rendered == mission:
            return project_gmail_mission(template, frozen, source_node_id=str(source_id), source_key=source_key, limit=limit)["mission"]
        if rendered and mission.count(rendered) == 1:
            # V1 lead prompts can surround the admitted intake with custom
            # instructions. Reserve their full budget and preserve them as
            # literals rather than reinterpreting incidental {{...}} text.
            prefix, suffix = mission.split(rendered, 1)
            remaining = limit - len(prefix) - len(suffix)
            if remaining < 0:
                raise EmployeeEmailInputLimit()
            inner = project_gmail_mission(template, frozen, source_node_id=str(source_id), source_key=source_key, limit=remaining)["mission"]
            return prefix + inner + suffix
    bodies = [str(email.get("body") or ""), str(email.get("body_text") or "")]
    original_body = next((body for body in bodies if body and body in mission), None)
    if not original_body or mission.count(original_body) != 1:
        raise EmployeeEmailInputLimit()
    # Match only the complete captured source body; do not truncate a prefix
    # that happens to resemble user instructions.
    prefix, suffix = mission.split(original_body, 1)
    remaining = limit - len(prefix) - len(suffix)
    if remaining < 0:
        raise EmployeeEmailInputLimit()
    inner = project_gmail_mission("{{source.body}}", {"source": email}, source_node_id=str(source_id),
                                 source_key="source", limit=remaining)["mission"]
    return prefix + inner + suffix
