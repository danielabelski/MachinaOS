"""Managed Gmail admission, exact source scope, and the owner delivery boundary."""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from models.database import AgentTeam, TeamMember, TeamTask
from models.employees import Employee, EmployeeJob
from nodes.google._gmail import format_message
from services.employees.email_input import project_managed_gmail_parameters
from services.employees.jobs import create_job, bind_job_team, deliver_job, _identity
from services.employees.team_runtime import attach_employee_job, employee_lead_prompt, capture_employee_runtime
from services.parameter_resolver import ParameterResolver
from services.plugin import NodeContext
from services.plugin.edge_walker import collect_agent_connections as real_collect_agent_connections


@pytest.fixture
def prepare_descriptor(monkeypatch):
    from tests.temporal.test_prepare_payload_context import descriptor
    return descriptor.__wrapped__(monkeypatch)


async def admitted(database, monkeypatch, version=1, *, tail="TAIL", request="origin"):
    body = "x" * 86306 + tail
    email = format_message({"id": "message-" + tail, "threadId": "thread", "payload": {
        "mimeType": "text/plain", "headers": [{"name": "From", "value": "captured@example.com"},
        {"name": "Subject", "value": "Actual email"}],
        "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}}, include_body=True)
    plan = {"version": version, "lead_node_id": "lead", "talk_node_id": "talk", "members": [{"node_id": "member"}],
        "delivery_node_ids": ["gate", "send"], "talk_delivery_node_ids": []}
    nodes = [{"id": name, "type": kind, "data": {"label": name}} for name, kind in (
        ("gmail", "googleGmailReceive"), ("intake", "employeeJob"), ("lead", "ai_employee"),
        ("talk", "chatAgent"), ("member", "aiAgent"), ("gate", "approvalGate"), ("send", "googleGmailSend"))]
    if version == 2:
        nodes[2]["data"].update(employee_recipe_version=2, employee_team_plan=plan)
    field, node_id = ("mission", "intake") if version == 1 else ("prompt", "lead")
    template = "Read subject={{gmail.subject}}\n{{gmail.body}}"
    parameters = {node["id"]: {} for node in nodes}
    parameters[node_id] = {"operation": "intake", field: template, "lead_node_id": "lead"}
    parameters["lead"].update(provider="openai", model="test-model",
        prompt=template if version == 2 else "CUSTOM PREFIX\n{{intake.mission}}\nCUSTOM TAIL")
    parameters["send"] = {"to": "{{gate.recipient}}", "body": "{{gate.text}}"}
    parameters["gate"] = {"draft": "{{lead.response}}", "recipient": "{{gmail.from}}", "channel": "Gmail"}
    edges = [{"source": "gmail", "target": node_id}, {"source": "gate", "target": "send",
        "data": {"condition": {"field": "result.approved", "operator": "is_true"}}}]
    edges.append({"source": "member", "target": "lead", "targetHandle": "input-teammates"})
    if version == 1:
        edges.append({"source": "intake", "target": "lead"})
    async with database.reserved_session() as session:
        if not await session.get(Employee, "employee"):
            session.add(Employee(id="employee", workflow_id="wf", owner_id="owner", hire_state="ready",
                team_plan=plan, node_roles={"agent": "lead", "gate": "gate", "reply": "send"}))
            await session.commit()
    monkeypatch.setattr(database, "get_workflow", AsyncMock(return_value=SimpleNamespace(data={"nodes": nodes, "edges": edges})))
    context = {"workflow_id": "wf", "node_id": node_id, "node_type": "employeeJob" if version == 1 else "ai_employee",
        "execution_id": request, "session_id": "wf", "nodes": nodes, "edges": edges,
        "outputs": {"gmail": email}, "parameter_snapshot": parameters, "user_id": "owner",
        # Prepare puts the plan it resolved from the admitted graph on the context.
        **({"employee_team_plan": plan} if version == 2 else {})}
    resolver = ParameterResolver(database, AsyncMock(return_value=None))
    resolved, context, handled = await project_managed_gmail_parameters(database, node_id, parameters[node_id], context, resolver.resolve)
    assert handled
    ctx = NodeContext.from_legacy(node_id, context["node_type"], context)
    job = await create_job(database, ctx, mission=resolved[field].strip() if version == 1 else resolved[field],
        lead_node_id="lead", delivery_node_ids=["gate", "send"], dispatch=False)
    assert job.id == _identity(ctx, context["email_input"]["original_mission"])
    assert len(job.mission) <= 20000 and job.source["outputs"]["gmail"]["body"].endswith(tail)
    return job, context


@pytest.mark.parametrize("version", [1, 2])
async def test_original_identity_survives_bounded_retry_and_distinguishes_same_generation(real_database, monkeypatch, version):
    first, context = await admitted(real_database, monkeypatch, version)
    retry = await create_job(real_database, NodeContext.from_legacy(context["node_id"], context["node_type"], context),
        mission=first.mission, lead_node_id="lead", delivery_node_ids=["gate", "send"], dispatch=False)
    second, _ = await admitted(real_database, monkeypatch, version, tail="LAST")
    assert retry.id == first.id and second.id != first.id


async def test_v1_exact_intake_attachment_preserves_custom_prompt_and_rejects_ambiguity(real_database, monkeypatch):
    job, context = await admitted(real_database, monkeypatch)
    second, _ = await admitted(real_database, monkeypatch, tail="LAST")
    lead = {**context, "node_id": "lead", "inputs": {"intake": {"job_id": job.id, "mission": job.mission}}, "outputs": {}}
    assert (await attach_employee_job(real_database, lead)).id == job.id
    prompt = "CUSTOM PREFIX\n" + job.mission + "\nCUSTOM TAIL"
    bounded = await employee_lead_prompt(real_database, lead, prompt)
    assert bounded.startswith("CUSTOM PREFIX\n") and bounded.endswith("\nCUSTOM TAIL") and len(bounded) <= 20000
    ambiguous = {**lead, "employee_job_id": None, "nodes": context["nodes"] + [{"id": "other-intake", "type": "employeeJob"}],
        "inputs": {**lead["inputs"], "other-intake": {"job_id": second.id}}}
    with pytest.raises(ValueError, match="ambiguous"):
        await attach_employee_job(real_database, ambiguous)
    with pytest.raises(ValueError, match="execution"):
        await attach_employee_job(real_database, {**lead, "employee_job_id": None, "execution_id": "other"})


async def test_runtime_metadata_is_actual_activity_scoped_and_preserves_initial_chain(real_database, monkeypatch):
    job, context = await admitted(real_database, monkeypatch)
    from temporalio import activity
    monkeypatch.setattr(activity, "in_activity", lambda: True)
    info = SimpleNamespace(workflow_namespace="default", workflow_id="lead-workflow", workflow_run_id="lead-run")
    monkeypatch.setattr(activity, "info", lambda: info)
    runtime = {"namespace": "default", "lead_workflow_id": "lead-workflow", "lead_initial_run_id": "lead-run",
        "lead_first_execution_run_id": "lead-first", "parent_workflow_id": "graph-root", "parent_run_id": "graph-run"}
    lead = {**context, "node_id": "lead", "employee_job_id": job.id, "_employee_runtime": runtime}
    await capture_employee_runtime(real_database, lead)
    async with real_database.get_session() as session:
        saved = await session.get(EmployeeJob, job.id)
    assert saved.source["runtime"]["root_workflow_id"] == "graph-root"
    assert saved.source["runtime"]["lead_first_execution_run_id"] == "lead-first"
    info.workflow_run_id = "continued-run"
    await capture_employee_runtime(real_database, {**lead, "_employee_runtime": {**runtime, "lead_initial_run_id": "continued-run"}})
    with pytest.raises(ValueError, match="activity execution"):
        await capture_employee_runtime(real_database, lead)
    async with real_database.get_session() as session:
        assert (await session.get(EmployeeJob, job.id)).source["runtime"]["lead_initial_run_id"] == "lead-run"


@pytest.mark.parametrize("version", [1, 2])
async def test_prepare_separates_concurrent_email_teams_and_reuses_legacy_bound_team(real_database, monkeypatch, prepare_descriptor, version):
    from services.temporal.agent_activities import prepare_agent_payload
    from services.agent_team import AgentTeamService
    first, context = await admitted(real_database, monkeypatch, version)
    second, other = await admitted(real_database, monkeypatch, version, tail="LAST")
    service = AgentTeamService(real_database)
    monkeypatch.setattr("services.agent_team.get_agent_team_service", lambda: service)
    def build(info):
        name = info.get("delegate_tool_name") or {"taskManager": "task_manager", "employeeJobSource": "employee_job_source"}.get(info["node_type"], info["node_type"])
        return SimpleNamespace(name=name, description="test capability", parameters={}, args_schema={"type": "object", "properties": {}}), {}
    ai = SimpleNamespace(_build_tool_from_node=AsyncMock(side_effect=build))
    monkeypatch.setattr("core.container.container", SimpleNamespace(database=lambda: real_database,
        auth_service=lambda: SimpleNamespace(has_valid_key=AsyncMock(return_value=True)), ai_service=lambda: ai,
        workflow_service=lambda: SimpleNamespace(_param_resolver=ParameterResolver(real_database, AsyncMock(return_value=None)))))
    # Use the production edge walker, rather than the lightweight descriptor fixture.
    import importlib
    walker = importlib.import_module("services.plugin.edge_walker")
    monkeypatch.setattr(walker, "collect_agent_connections", real_collect_agent_connections)
    def lead_context(job, initial):
        return {**initial, "node_id": "lead", "node_type": "ai_employee", "employee_job_id": job.id,
            "inputs": {"intake": {"job_id": job.id, "mission": job.mission}} if version == 1 else initial["outputs"]}
    a = await prepare_agent_payload(lead_context(first, context))
    b = await prepare_agent_payload(lead_context(second, other))
    assert a["team_id"] != b["team_id"]
    assert a["team_execution_id"] == first.id and b["team_execution_id"] == second.id
    assert "employee_job_source" in {tool["name"] for tool in a["tools"]}
    # Retry a preparation after upgrading an already bound legacy generation team.
    async with real_database.reserved_session() as session:
        team = await session.get(AgentTeam, a["team_id"])
        team.execution_id = "origin"
        await session.commit()
    retried = await prepare_agent_payload(lead_context(first, context))
    assert retried["team_id"] == a["team_id"] and retried["team_execution_id"] == "origin"


async def test_historical_review_uses_captured_intake_and_inline_source_before_prompt(real_database, monkeypatch):
    from services.employees.team_runtime import employee_review_input
    from nodes.agent._inline import prepare_agent_call
    from services.status_broadcaster import get_status_broadcaster
    job, context = await admitted(real_database, monkeypatch)
    async with real_database.reserved_session() as session:
        stored = await session.get(EmployeeJob, job.id)
        stored.source = {key: value for key, value in stored.source.items() if key != "intake_node_id"}
        stored.team_id = "team"
        session.add(AgentTeam(id="team", workflow_id="wf", team_lead_node_id="lead", execution_id=job.id))
        session.add(TeamTask(id="task", team_id="team", title="Work", workflow_id="wf", execution_id=job.id,
            created_by="lead", assigned_to="member", status="submitted"))
        await session.commit()
    task_event = {"task_id": "task", "team_id": "team", "status": "submitted"}
    review = {**context, "node_id": "lead", "execution_id": "review", "nodes": context["nodes"] + [{"id": "task-trigger", "type": "taskTrigger"}],
        "edges": context["edges"] + [{"source": "task-trigger", "target": "lead", "targetHandle": "input-task"}],
        "inputs": {"task-trigger": task_event}, "outputs": {"unrelated": {"text": "partial current map"}}}
    assert employee_review_input(review) == task_event
    monkeypatch.setattr("services.status_broadcaster.get_status_broadcaster", lambda: get_status_broadcaster())
    result = await prepare_agent_call(node_id="lead", node_type="ai_employee", parameters={"prompt": "WRONG LATEST EMAIL"},
        context=review, database=real_database)
    assert result["context"]["employee_job_id"] == job.id
    assert "Actual email" in result["parameters"]["prompt"] and "WRONG LATEST EMAIL" not in result["parameters"]["prompt"]
    assert "employeeJobSource" in {tool["node_type"] for tool in result["tool_data"]}


async def test_unmanaged_task_review_does_not_require_employee_job_scope(real_database):
    context = {"workflow_id": "wf", "node_id": "lead"}
    assert await attach_employee_job(real_database, context, {"team_id": "ordinary-team", "task_id": "legacy-task"}) is None
    assert "employee_job_id" not in context


@pytest.mark.parametrize("transport", ["native", "pi", "temporal"])
async def test_intrinsic_source_pipeline_transports_trusted_task_scope(real_database, monkeypatch, transport):
    from tests.services.employees.test_email_input import install_source
    from services.plugin.edge_walker import bind_employee_job_source
    from services.handlers.tools import _dispatch_tool
    from services.temporal.pi_inline_delegation import forward_inline_scope
    from services.temporal.agent_workflow import _trusted_tool_scope
    from services.approvals import tool_calls
    await install_source(real_database)
    scope = {"node_id": "member", "workflow_id": "wf", "employee_job_id": "job", "team_task_id": "task-job"}
    tools = []
    await bind_employee_job_source("member", scope, real_database, tools)
    assert [tool["node_type"] for tool in tools] == ["employeeJobSource"]
    if transport == "pi":
        scope = forward_inline_scope({**scope, "pi_harness_version": 1})
    elif transport == "temporal":
        scope = _trusted_tool_scope(scope)
    scope["workflow_id"] = "wf"  # Dispatch's explicit workflow identity wraps inherited scope.
    monkeypatch.setattr("services.plugin.deps.get_database", lambda: real_database)
    monkeypatch.setattr(tool_calls, "check_in_process", AsyncMock(return_value=SimpleNamespace(result=None, node_data=None, tool_args=None)))
    page = await _dispatch_tool("employee_job_source", {"content": "original_body", "offset": 86300, "limit": 20},
        {**scope, "node_id": "builtin-source", "parent_node_id": "member", "node_type": "employeeJobSource", "parameters": {}})
    assert "TAIL" in str(page)


@pytest.mark.parametrize("version", [1, 2])
async def test_email_accepted_work_owner_approval_and_one_captured_recipient_send(real_database, monkeypatch, version):
    from services.approvals import decisions, store, waiter
    from nodes.workflow.approval_gate import ApprovalGateNode, ApprovalGateParams
    from nodes.tool.task_manager import _execute_task_manager
    from services.agent_team import AgentTeamService
    job, context = await admitted(real_database, monkeypatch, version)
    async with real_database.reserved_session() as session:
        session.add(AgentTeam(id="team", workflow_id="wf", team_lead_node_id="lead", execution_id="origin"))
        session.add(TeamMember(team_id="team", agent_node_id="member", agent_type="aiAgent"))
        await session.commit()
    await bind_job_team(real_database, workflow_id="wf", lead_node_id="lead", execution_id="origin", team_id="team")
    service = AgentTeamService(real_database)
    monkeypatch.setattr("services.agent_team.get_agent_team_service", lambda: service)
    task_scope = {**context, "parent_node_id": "lead", "employee_job_id": job.id, "team_execution_id": "origin"}
    assigned = await _execute_task_manager({"operation": "assign_task", "assignee_node_id": "member", "title": "Review email",
        "mission": "Read original source", "context": {"subject": "Actual email"}, "acceptance_criteria": {"complete": True}}, task_scope)
    task_id = assigned["task"]["id"]
    assert assigned["task"]["status"] == "queued"
    assert await service.claim_task("team", task_id, "member")
    assert await service.complete_task("team", task_id, {"response": "Reviewed reply"})
    accepted = await _execute_task_manager({"operation": "accept_task", "task_id": task_id}, task_scope)
    assert accepted["task"]["status"] == "accepted"
    sent = []
    resolver = ParameterResolver(real_database, AsyncMock(return_value=None))
    async def execute_node(**kwargs):
        params = await resolver.resolve(kwargs["parameters"], kwargs["node_id"], kwargs["nodes"], kwargs["edges"],
            kwargs["session_id"], run_outputs=kwargs["outputs"])
        if kwargs["node_type"] == "approvalGate":
            ctx = NodeContext.from_legacy("gate", "approvalGate", {**kwargs, **kwargs["extras"]})
            outcome = await ApprovalGateNode().request(ctx, ApprovalGateParams.model_validate(params))
            return {"success": True, "result": outcome.model_dump()}
        sent.append(params)
        assert params["to"] == "captured@example.com" and params["body"] == "Owner-approved reply"
        return {"success": True, "result": {"message_id": "sent-once"}}
    monkeypatch.setattr("core.container.container", SimpleNamespace(database=lambda: real_database,
        workflow_service=lambda: SimpleNamespace(execute_node=execute_node)))
    boundary = NodeContext(node_id="boundary", node_type="employeeJob", workflow_id="wf", execution_id="review",
        outputs={"lead": {"response": "Reviewed reply", "team_id": "team", "execution_id": "origin"}})
    task = asyncio.create_task(deliver_job(real_database, boundary, lead_node_id="lead"))
    try:
        for _ in range(300):
            rows = await store.list_approvals(real_database, status="pending")
            if rows:
                break
            if task.done():
                await task
            await asyncio.sleep(.01)
        assert len(rows) == 1 and not sent
        row, _ = await decisions.decide(real_database, rows[0], decision="send", decision_key="owner-send", actor="owner",
            text="Owner-approved reply", now=datetime.now(timezone.utc) - timedelta(seconds=30))
        waiter.notify(row.id)
        assert (await asyncio.wait_for(task, 5))["delivered"]
        assert (await deliver_job(real_database, boundary, lead_node_id="lead"))["delivered"]
        assert len(sent) == 1
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
