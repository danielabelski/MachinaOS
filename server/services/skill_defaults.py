"""Explicit builtin defaults, isolated from the legacy skill registry cache."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

_VARIANTS = {"browser-skill": "web_agent/browser-skill", "process-manager-skill": "terminal/process-manager-skill"}


def v1_default(name: str, skill=None):
    relative = _VARIANTS.get(name)
    if relative is None:
        return skill
    from services.skill_loader import get_skill_loader
    skill = skill or get_skill_loader().load_skill(name)
    expected = Path(__file__).resolve().parents[1] / "skills" / relative
    # A project or user override is authoritative, even with the same name.
    if skill is None or skill.metadata.path is None or Path(skill.metadata.path).resolve() != expected.resolve():
        return skill
    body = (expected / "SKILL.v1.md").read_text(encoding="utf-8")
    return replace(skill, instructions=body)


def new_skill_parameters(parameters):
    from services.agent_runtime_policy import new_agent_parameters
    result = new_agent_parameters(parameters)
    if result.get("runtime_behavior_version") != 1:
        return result
    result["skills_config"] = {name: dict(config) for name, config in (result.get("skills_config") or {}).items()}
    for name, config in result["skills_config"].items():
        # Preserve configured/library text; empty defaults are resolved only
        # when consumed or explicitly reset by the owner.
        if not config.get("instructions"):
            skill = v1_default(name)
            if skill:
                config["instructions"] = skill.instructions
    return result


async def scoped_defaults_version(database, websocket, node_id):
    if not node_id:
        return 0
    from services.agent_runtime_policy import runtime_behavior_version
    from services.plugin import NodeUserError
    principal = str(getattr(getattr(websocket, "state", None), "user_id", None) or "owner")
    for workflow in await database.get_all_workflows():
        graph = workflow.data or {}
        if str(graph.get("owner_id") or "owner") != principal:
            continue
        if any(node.get("id") == node_id and node.get("type") == "masterSkill" for node in graph.get("nodes", [])):
            return runtime_behavior_version(await database.get_node_parameters(node_id))
    raise NodeUserError("Master Skill node not found")
