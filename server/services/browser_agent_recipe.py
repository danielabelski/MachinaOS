"""The shared Browser Agent graph recipe and its saved resource binding."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from services.graph_build import GraphAdditions, NewNode, skill_edge, tool_edge
from services.plugin import NodeUserError

BROWSER_AGENT_ROLE = (
    "Complete browser tasks using your connected Browser tool and browser skill. "
    "Observe the page before acting and use current element references. Respect saved browser policy and Ask first. "
    "Use approved credential binding IDs; never ask for passwords in chat or reveal credential values. "
    "Use request_user for human login, MFA, challenges, uncertain outcomes or actions the policy requires the owner to perform. "
    "Do not repeat an uncertain action. Verify its outcome first. Use screenshots with visionAnalyze when text observation is insufficient. "
    "Return the result, evidence and any limitation in the standard agent response."
)


BROWSER_TOOL_REQUIRED = "Browser AI Agent needs exactly one connected, saved Browser tool."


def connected_browser_tools(graph: Mapping[str, Any], agent_id: str) -> set[str]:
    nodes = {node["id"]: node for node in graph.get("nodes", []) if node.get("id")}
    return {edge.get("source") for edge in graph.get("edges", [])
            if edge.get("target") == agent_id and edge.get("targetHandle") == "input-tools"
            and nodes.get(edge.get("source"), {}).get("type") == "browser"}


def browser_tool_id(graph: Mapping[str, Any], agent_id: str) -> str:
    tools = connected_browser_tools(graph, agent_id)
    if len(tools) != 1:
        raise NodeUserError(BROWSER_TOOL_REQUIRED)
    return str(next(iter(tools)))


def browser_agent_additions(*, agent_parameters: Mapping[str, Any] | None = None,
                            position: tuple[float, float] = (0, 0), agent_ref: str = "agent",
                            browser_node_id: str | None = None, create_agent: bool = True,
                            create_context: bool = True, create_skills: bool = True,
                            fresh_skills: bool = False) -> GraphAdditions:
    x, y = position
    nodes, edges = [], []
    if create_agent:
        from services.agent_runtime_policy import new_agent_parameters
        parameters = new_agent_parameters({"system_message": BROWSER_AGENT_ROLE, **dict(agent_parameters or {})})
        nodes.append(NewNode(agent_ref, "browser_agent", "Browser AI Agent", parameters, position=(x, y)))
    if create_context:
        nodes.append(NewNode("context", "context", "Browser context", position=(x, y - 180), context_of=agent_ref))
    browser_ref = browser_node_id or "browser"
    if browser_node_id is None:
        nodes.append(NewNode(browser_ref, "browser", "Browser", {}, position=(x - 240, y + 180)))
    edges.append(tool_edge(browser_ref, agent_ref))
    if create_skills:
        instructions = (Path(__file__).resolve().parents[1] / "skills/web_agent/browser-skill/SKILL.md").read_text(encoding="utf-8")
        from services.agent_runtime_policy import new_agent_runtime_enabled
        if (create_agent or fresh_skills) and new_agent_runtime_enabled():
            from services.skill_defaults import v1_default
            variant = v1_default("browser-skill")
            if variant is not None:
                instructions = variant.instructions
        nodes.append(NewNode("skills", "masterSkill", "Browser instructions", {
            "skill_folder": "web_agent", "skills_config": {
                "browser-skill": {"enabled": True, "instructions": instructions, "isCustomized": False},
                "skill": {"enabled": True, "instructions": "", "required": True, "isCustomized": False},
            }}, position=(x - 240, y)))
        edges.append(skill_edge("skills", agent_ref))
    nodes.append(NewNode("vision", "visionAnalyze", "Browser vision", {}, position=(x + 240, y + 180)))
    edges.append(tool_edge("vision", agent_ref))
    return GraphAdditions(nodes=tuple(nodes), edges=tuple(edges))
