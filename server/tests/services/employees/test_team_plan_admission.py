"""A hired team's plan reaches its runs through the admitted generation (#148).

The saved graph keeps node data to UI fields, so the plan is saved on the
employee row; Start and Apply copy it onto the lead in the generation's
snapshot. A run holds only what its trigger reaches, so the runtime reads the
plan, and a job its graph, from that whole snapshot.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import nodes  # noqa: F401

from models.employees import Employee
from services.deployment.control import WorkflowControlService
from services.employees.apps import get_app
from services.employees.builder import BuildInputs, build_employee_graph
from services.employees.hire_request import HireEmployeeRequest
from services.employees.jobs import create_job
from services.employees.llm import LLMChoice
from services.employees.prompt import OwnerProfile
from services.employees.team_runtime import admit_team_plan, employee_runtime_plan
from services.plugin import NodeContext
from services.temporal.trigger_listener_workflow import _build_run_graph
from services.workflow_migrations import normalize_workflow_graph
from services.workflow_sanitizer import sanitize_workflow_graph


def inbox_assistant():
    request = HireEmployeeRequest.model_validate({
        "idempotency_key": "inbox", "job": "Read new email, answer what you can and flag the rest",
        "name": "Leo", "role": "Inbox assistant", "apps": ["Gmail"],
        "steps": [{"title": "Read new email", "role": "trigger", "app": "Gmail"}], "rules": {"ask_first": True}})
    return build_employee_graph(BuildInputs(
        workflow_id="7", request=request, apps=[get_app("gmail")], connected_app_ids={"gmail"},
        owner=OwnerProfile(name="Alex", role="Owner", preferences=""), owner_values={"google_email": "alex@example.com"},
        timezone="UTC", llm=LLMChoice(provider="openai", model="gpt-x", local=False), memory=True,
        now=datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc), team=True))


@pytest.fixture
async def started(real_database, monkeypatch):
    """A hire saved the way commit_hire saves it, then admitted the way Start admits it."""
    built = inbox_assistant()
    normalized = normalize_workflow_graph("7", built.nodes, built.edges, built.parameters)
    saved = sanitize_workflow_graph({**normalized.graph_data(), "owner_id": "owner"})
    async with real_database.reserved_session() as session:
        session.add(Employee(id="employee", workflow_id="7", owner_id="owner", hire_state="ready",
                             team_plan=built.team_plan, node_roles=built.node_roles))
        await session.commit()
    monkeypatch.setattr(real_database, "get_workflow", AsyncMock(return_value=SimpleNamespace(data=saved, slug="Leo_1")))
    control, _ = await WorkflowControlService(real_database).begin_generation(
        workflow_id="7", nodes=await admit_team_plan(real_database, "7", saved["nodes"]), edges=saved["edges"],
        session_id="7", idempotency_key="start", graph_version=normalized.graph_version, owner_id="owner",
        parameters=normalized.node_parameters)
    return SimpleNamespace(database=real_database, saved=saved, control=control, roles=built.node_roles, plan=built.team_plan)


def run(started, trigger, node_id):
    """The context one agent gets in a run its trigger started."""
    snapshot = started.control.graph_snapshot
    nodes, edges = _build_run_graph(trigger_node_id=trigger, trigger_output={"subject": "Hello"},
                                    nodes=snapshot["nodes"], edges=snapshot["edges"])
    return {"workflow_id": "7", "generation": started.control.generation, "data_scope_id": started.control.data_scope_id,
            "execution_id": f"run-{node_id}", "session_id": "7", "user_id": "owner", "node_id": node_id,
            "nodes": nodes, "edges": edges, "outputs": {trigger: {"subject": "Hello"}},
            "parameter_snapshot": snapshot["parameters"]}


async def test_start_admits_the_plan_the_saved_graph_cannot_hold(started):
    lead = started.roles["agent"]
    assert set(next(node for node in started.saved["nodes"] if node["id"] == lead)["data"]) == {"label"}
    admitted = next(node for node in started.control.graph_snapshot["nodes"] if node["id"] == lead)["data"]
    assert admitted["employee_recipe_version"] == 2 and admitted["employee_team_plan"] == started.plan


async def test_a_trigger_run_resolves_the_plan_and_its_job_holds_the_delivery_nodes(started):
    roles, plan = started.roles, started.plan
    context = run(started, roles["trigger"], roles["agent"])
    delivery = set(plan["delivery_node_ids"])
    assert {roles["gate"], roles["reply"]} <= delivery
    # The run itself never reaches the delivery nodes.
    assert not delivery & {node["id"] for node in context["nodes"]}
    # AgentWorkflow and the Gmail projection read the marker on the run's own lead node.
    assert next(node for node in context["nodes"] if node["id"] == roles["agent"])["data"]["employee_recipe_version"] == 2
    assert await employee_runtime_plan(started.database, context) == plan
    job = await create_job(started.database, NodeContext.from_legacy(roles["agent"], "ai_employee", context),
                           mission="Answer the email", lead_node_id=roles["agent"],
                           delivery_node_ids=plan["delivery_node_ids"], dispatch=False)
    assert delivery <= {node["id"] for node in job.source["nodes"]} and delivery <= set(job.source["parameters"])
    assert any(edge["source"] == roles["gate"] and edge["target"] == roles["reply"] for edge in job.source["edges"])


async def test_a_talk_run_resolves_the_plan_and_submits_a_job_that_holds_the_lead(started):
    roles, plan = started.roles, started.plan
    context = run(started, roles["talk_trigger"], roles["talk_agent"])
    assert roles["agent"] not in {node["id"] for node in context["nodes"]}
    assert await employee_runtime_plan(started.database, context) == plan
    job = await create_job(started.database, NodeContext.from_legacy(roles["talk_agent"], "aiAgent", context),
                           mission="Draft a reply to Sam", lead_node_id=plan["lead_node_id"],
                           delivery_node_ids=plan["talk_delivery_node_ids"], dispatch=True)
    assert roles["agent"] in {node["id"] for node in job.source["nodes"]}


async def test_a_run_outside_a_started_generation_has_no_plan(started):
    context = {**run(started, started.roles["trigger"], started.roles["agent"]), "generation": 0}
    assert await employee_runtime_plan(started.database, context) is None


async def test_talk_gets_submit_job_from_the_plan_prepare_resolved():
    from services.plugin.edge_walker import collect_agent_connections
    plan = {"version": 2, "lead_node_id": "lead", "talk_node_id": "talk"}
    context = {"workflow_id": "7", "edges": [{"source": "chat", "target": "talk", "targetHandle": "input-main"}],
               "nodes": [{"id": "chat", "type": "chatTrigger", "data": {"label": "Chat"}},
                         {"id": "talk", "type": "aiAgent", "data": {"label": "Talk"}}]}
    _, _, tools, _, _ = await collect_agent_connections("talk", {**context, "employee_team_plan": plan}, object())
    assert [tool["node_type"] for tool in tools] == ["taskManager"]
    _, _, tools, _, _ = await collect_agent_connections("talk", context, object())
    assert tools == []
