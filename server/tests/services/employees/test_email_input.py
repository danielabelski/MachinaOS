"""Regression cases for #144, including original-source authorization."""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from models.database import TeamMember, TeamTask
from models.employees import Employee, EmployeeJob
from nodes.google._gmail import format_message, html_body_text
from nodes.workflow.employee_job import EmployeeJobParams
from nodes.workflow.employee_job_source import EmployeeJobSourceNode, EmployeeJobSourceParams
from services.employees.email_input import (
    EmployeeEmailInputLimit, MISSION_LIMIT, SHORTENED_NOTICE,
    project_gmail_mission, project_managed_gmail_parameters, project_stored_mission,
)
from services.employees.job_source import authorize_employee_job_source, read_employee_job_source
from services.parameter_resolver import ParameterResolver
from services.tool_output import bound_tool_output, tool_output_is_capped


def mime(body, kind="text/plain", **extra):
    return {"mimeType": kind, "body": {"data": base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")}, **extra}


def formatted(payload):
    return format_message({"id": "mail", "threadId": "thread", "payload": payload}, include_body=True)


def project(body, template="New email from {{gmail.from}}\nSubject: {{gmail.subject}}\n\n{{gmail.body}}", **email):
    return project_gmail_mission(template, {"gmail": {"from": "sender@example.com", "subject": "Subject", "body": body, **email}},
                                 source_node_id="trigger", source_key="gmail")


def test_mime_readable_prefers_later_plain_alternative_without_changing_original():
    payload = {"mimeType": "multipart/mixed", "parts": [
        mime("private attachment", filename="note.txt"),
        {"mimeType": "multipart/alternative", "parts": [mime("<p>HTML original</p>", "text/html")]},
        {"mimeType": "multipart/alternative", "parts": [mime("Readable plain ✓")]}]}
    output = formatted(payload)
    assert output["body"] == "private attachment"  # Existing raw-body choice remains compatible.
    assert output["body_mime_type"] == "text/plain"
    assert output["body_text"] == "Readable plain ✓"


def test_mime_html_readable_removes_scripts_styles_and_preserves_links():
    html = '<style>.hidden{color:red}</style><p>Hello 世界 <a href="https://example.com/path?q=1&amp;x=2">details</a></p><script>secret()</script><div>Tail</div>'
    output = formatted(mime(html, "text/html"))
    assert output["body"] == html
    assert output["body_mime_type"] == "text/html"
    assert "Hello 世界" in output["body_text"] and "Tail" in output["body_text"]
    assert "https://example.com/path?q=1&x=2" in output["body_text"]
    assert "secret()" not in output["body_text"] and "color:red" not in output["body_text"]


def test_mime_excludes_attachment_disposition_without_filename():
    output = formatted({"mimeType": "multipart/mixed", "parts": [
        mime("hidden", headers=[{"name": "Content-Disposition", "value": "attachment; name=a.txt"}]),
        mime("<div>actual</div>", "text/html")]})
    assert output["body_text"] == "actual"


def test_readable_mime_respects_charset_and_falls_back_for_unknown_charset():
    payload = {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode("Résumé".encode("iso-8859-1")).decode()},
               "headers": [{"name": "Content-Type", "value": "text/plain; charset=iso-8859-1"}]}
    output = formatted(payload)
    assert output["body_text"] == "Résumé"
    assert output["body"] == "Rsum"  # Original decoding is intentionally unchanged.
    unknown = mime("Unicode 世界", headers=[{"name": "Content-Type", "value": "text/plain; charset=unknown-codec"}])
    assert formatted(unknown)["body_text"] == "Unicode 世界"


def test_malformed_html_is_local_and_tolerant():
    assert html_body_text('<div>One<a href="/target">two</a><div>三 &amp; four') == "Onetwo (/target)\n三 & four"
    assert html_body_text('<a href="/target">unclosed<img alt=" diagram">') == "unclosed diagram (/target)"


def test_reported_body_is_bounded_before_params_and_preserves_original_tail():
    body = "a" * 86306 + "TAIL"
    projected = project(body)
    EmployeeJobParams(operation="intake", mission=projected["mission"])
    assert len(projected["mission"]) <= MISSION_LIMIT
    assert projected["mission"].endswith(SHORTENED_NOTICE)
    assert projected["metadata"]["original_mission"].endswith("TAIL")
    assert len(projected["metadata"]["body_text"]) == 86310


@pytest.mark.parametrize("length", [19999, 20000, 20001])
def test_whole_mission_boundary(length):
    prefix = "Instructions: "
    projected = project("🙂" * (length - len(prefix)), template=prefix + "{{gmail.body}}")
    assert len(projected["mission"]) <= 20000
    assert (SHORTENED_NOTICE in projected["mission"]) == (length > 20000)
    assert len(projected["metadata"]["original_mission"]) == length


def test_repeated_body_placeholders_share_budget_and_preserve_literal_instructions():
    template = "Read both; preserve this ending.\n{{gmail.body}}\nSecond copy:\n{{GMAIL.body_text}}\nCUSTOM END"
    result = project("x" * 86310, template=template)
    assert result["mission"].startswith("Read both; preserve this ending.")
    assert result["mission"].endswith("CUSTOM END")
    assert result["mission"].count(SHORTENED_NOTICE) == 2
    assert len(result["mission"]) <= 20000


def test_rendered_body_placeholders_are_never_resolved_again():
    output = project("Email says {{gmail.subject}}", template="subject={{gmail.subject}}\n{{gmail.body}}")
    assert output["mission"] == "subject=Subject\nEmail says {{gmail.subject}}"


def test_html_projection_uses_readable_text_even_below_limit():
    result = project('<div>Useful <a href="https://example.com">link</a></div>', body_mime_type="text/html")
    assert "Useful link (https://example.com)" in result["mission"]
    assert "<div>" not in result["mission"]
    assert "<div>" in result["metadata"]["original_mission"]


def test_oversized_nonbody_fields_fail_with_specific_error():
    with pytest.raises(EmployeeEmailInputLimit, match="employee_email_input_limit"):
        project("tiny", template="{{gmail.subject}}\n{{gmail.body}}", subject="x" * 20001)
    with pytest.raises(EmployeeEmailInputLimit):
        project("long" * 10000, template="i" * 19999 + "{{gmail.body}}")


def test_historical_projection_does_not_mutate_original_source():
    body = "x" * 86310
    source = {"nodes": [{"id": "gmail", "type": "googleGmailReceive"}], "outputs": {"gmail": {"body": body}}}
    before = json.dumps(source)
    mission = "CUSTOM PREFIX\n" + body + "\nCUSTOM TAIL"
    result = project_stored_mission(mission, source)
    assert result.startswith("CUSTOM PREFIX") and result.endswith("CUSTOM TAIL")
    assert len(result) <= 20000 and json.dumps(source) == before


def test_historical_fallback_does_not_reinterpret_literal_template_text():
    body = "x" * 86310
    source = {"nodes": [{"id": "gmail", "type": "googleGmailReceive"}], "outputs": {"gmail": {"body": body}}}
    mission = "Literal {{custom.instruction}}\n" + body + "\nLiteral {{ending.rule}}"
    result = project_stored_mission(mission, source)
    assert result.startswith("Literal {{custom.instruction}}\n")
    assert result.endswith("\nLiteral {{ending.rule}}")
    assert len(result) <= 20000


def test_historical_ambiguous_source_fails_closed():
    with pytest.raises(EmployeeEmailInputLimit):
        project_stored_mission("x" * 21000, {"nodes": [], "outputs": {}})


def test_historical_saved_template_distinguishes_identical_literal_and_body():
    body = "x" * 11000
    template = body + "\nLITERAL END\n{{gmail.body}}"
    source = {"nodes": [{"id": "gmail", "type": "googleGmailReceive", "data": {"label": "Gmail"}},
                        {"id": "intake", "type": "employeeJob"}],
              "outputs": {"gmail": {"body": body}},
              "parameters": {"intake": {"operation": "intake", "mission": template}}}
    result = project_stored_mission(body + "\nLITERAL END\n" + body, source)
    assert result.startswith(body + "\nLITERAL END\n")
    assert result.count(SHORTENED_NOTICE) == 1 and len(result) <= 20000


def test_historical_custom_lead_prompt_reserves_literals_around_repeated_intake():
    body = "x" * 86310
    template = "Email:\n{{gmail.body}}\nRepeat:\n{{gmail.body}}"
    source = {"nodes": [{"id": "gmail", "type": "googleGmailReceive", "data": {"label": "Gmail"}},
                        {"id": "intake", "type": "employeeJob"}],
              "outputs": {"gmail": {"body": body}},
              "parameters": {"intake": {"operation": "intake", "mission": template}}}
    original_intake = "Email:\n" + body + "\nRepeat:\n" + body
    mission = "Custom prefix {{untouched.literal}}\n" + original_intake + "\nCustom suffix"
    result = project_stored_mission(mission, source)
    assert result.startswith("Custom prefix {{untouched.literal}}\n") and result.endswith("\nCustom suffix")
    assert result.count(SHORTENED_NOTICE) == 2 and len(result) <= 20000


async def install_employee(database):
    async with database.reserved_session() as session:
        session.add(Employee(id="employee", workflow_id="wf", owner_id="owner", hire_state="ready",
                             team_plan={"lead_node_id": "lead", "members": [{"node_id": "member"}]}))
        await session.commit()


def intake_context(body, *, version=1):
    plan = {"lead_node_id": "lead", "members": [{"node_id": "member"}]}
    node = {"id": "intake", "type": "employeeJob", "data": {}}
    params = {"operation": "intake", "mission": "Custom instructions\n{{gmail.subject}}\n{{gmail.body}}"}
    if version == 2:
        node = {"id": "lead", "type": "ai_employee", "data": {"employee_recipe_version": 2, "employee_team_plan": plan}}
        params = {"prompt": "Custom instructions\n{{gmail.subject}}\n{{gmail.body_text}}"}
    return params, {"workflow_id": "wf", "node_type": node["type"], "nodes": [
        {"id": "gmail", "type": "googleGmailReceive", "data": {"label": "Gmail"}}, node],
        "edges": [{"source": "gmail", "target": node["id"]}],
        "outputs": {"gmail": {"subject": "Actual email", "body": body}}, "node_id": node["id"],
        # Prepare puts the plan it resolved from the admitted graph on the context.
        **({"employee_team_plan": plan} if version == 2 else {})}


@pytest.mark.parametrize("version", [1, 2])
async def test_managed_projection_freezes_current_run_output(real_database, version):
    await install_employee(real_database)
    params, context = intake_context("x" * 86310, version=version)
    shared = AsyncMock(return_value={"subject": "WRONG CONCURRENT EMAIL", "body": "wrong"})
    resolver = ParameterResolver(real_database, shared)
    projected, updated, handled = await project_managed_gmail_parameters(real_database, context["node_id"], params, context, resolver.resolve)
    assert handled and "Actual email" in projected["mission" if version == 1 else "prompt"]
    assert "WRONG CONCURRENT EMAIL" not in projected["mission" if version == 1 else "prompt"]
    assert len(projected["mission" if version == 1 else "prompt"]) <= 20000
    assert updated["email_input"]["original_mission"].endswith("x" * 86310)
    shared.assert_not_awaited()


async def test_node_executor_saved_template_projects_before_validation(real_database):
    from services.node_executor import NodeExecutor
    await install_employee(real_database)
    parameters, context = intake_context("x" * 86310)
    snapshot = {"intake": parameters}
    executor = NodeExecutor(real_database, SimpleNamespace(), None, None, None, SimpleNamespace())
    resolver = ParameterResolver(real_database, AsyncMock(return_value={}))
    resolved = await executor._prepare_parameters("intake", "employeeJob", {}, "default", parameter_snapshot=snapshot,
                                                 context=context, resolve_params_fn=resolver.resolve)
    EmployeeJobParams.model_validate(resolved)
    assert len(resolved["mission"]) <= 20000
    assert context["email_input"]["original_mission"].endswith("x" * 86310)
    assert context["_employee_email_templates_resolved"]


async def test_executor_freezes_once_without_leaking_resolution_state_to_next_node(real_database):
    from services.node_executor import NodeExecutor
    await install_employee(real_database)
    parameters, context = intake_context("Email contains {{gmail.subject}}")
    context["parameter_snapshot"] = {"intake": parameters, "next": {}}
    executor = NodeExecutor(real_database, SimpleNamespace(), None, None, None, SimpleNamespace())
    dispatch = AsyncMock(return_value={"success": True, "result": {}})
    executor._dispatch = dispatch
    resolver = ParameterResolver(real_database, AsyncMock(return_value={}))
    resolve = AsyncMock(wraps=resolver.resolve)
    result = await executor.execute("intake", "employeeJob", {}, context, resolve)
    assert result["success"]
    admitted = dispatch.await_args.args[2]
    assert admitted["mission"].endswith("Email contains {{gmail.subject}}")
    EmployeeJobParams.model_validate(admitted)
    resolve.assert_not_awaited()
    assert "_employee_email_templates_resolved" not in dispatch.await_args.args[3]
    await executor.execute("next", "console", {"text": "{{gmail.subject}}"}, context, resolve)
    assert resolve.await_count == 1
    assert dispatch.await_args.args[2]["text"] == "Actual email"


async def install_source(database, *, job_id="job", team_id="team", workflow_id="wf"):
    body = "🙂" * 86306 + "TAIL"
    email = {"message_id": "mail-" + job_id, "thread_id": "thread", "from": "sender@example.com", "to": "owner@example.com",
             "subject": "Context", "body": "<p>" + body + "</p>", "body_text": body, "body_mime_type": "text/html"}
    source = {"nodes": [{"id": "gmail", "type": "googleGmailReceive"}], "outputs": {"gmail": email},
              "parameters": {"secret": {"api_key": "DO NOT EXPOSE"}},
              "email_input": {"version": 1, "source_node_id": "gmail", "body_text": body, "original_mission": "Read: " + email["body"]},
              "team_plan": {"lead_node_id": "lead", "members": [{"node_id": "member"}]}}
    async with database.reserved_session() as session:
        session.add(EmployeeJob(id=job_id, workflow_id=workflow_id, origin_execution_id="origin", lead_node_id="lead", team_id=team_id,
                                mission="Read bounded email", state="working", source=source))
        session.add(TeamMember(team_id=team_id, agent_node_id="member", agent_type="ai_employee"))
        session.add(TeamTask(id="task-" + job_id, team_id=team_id, title="Read", created_by="lead", assigned_to="member",
                             workflow_id=workflow_id, execution_id="origin"))
        await session.commit()
    return source


def source_context(*, caller="lead", job_id="job", task_id=None):
    return {"workflow_id": "wf", "employee_job_id": job_id, "parent_node_id": caller,
            "node_id": "intrinsic-source", "team_task_id": task_id}


@pytest.mark.parametrize("content", ["body_text", "original_body", "original_mission"])
async def test_source_reconstructs_entire_original_and_tail(real_database, content):
    source = await install_source(real_database)
    text, offset = "", 0
    while True:
        page = await read_employee_job_source(real_database, source_context(), content=content, offset=offset, limit=9999)
        assert page["from"] == "sender@example.com" and "parameters" not in page
        text += page["text"]
        assert page["next_offset"] == page["offset"] + len(page["text"])
        offset = page["next_offset"]
        if page["complete"]:
            break
    expected = source["email_input"]["original_mission"] if content == "original_mission" else source["outputs"]["gmail"]["body" if content == "original_body" else "body_text"]
    assert text == expected and "TAIL" in text and "DO NOT EXPOSE" not in text


async def test_source_teammate_requires_captured_membership_and_current_assignment(real_database):
    await install_source(real_database)
    good = source_context(caller="member", task_id="task-job")
    assert (await read_employee_job_source(real_database, good, content="body_text", limit=1))["text"] == "🙂"
    for bad in [source_context(caller="outsider", task_id="task-job"), source_context(caller="member"),
                {**good, "workflow_id": "other"}, {**good, "employee_job_id": "missing"}]:
        with pytest.raises(PermissionError):
            await authorize_employee_job_source(real_database, bad)
    async with real_database.reserved_session() as session:
        task = await session.get(TeamTask, "task-job")
        task.assigned_to = "other"
        await session.commit()
    with pytest.raises(PermissionError):
        await authorize_employee_job_source(real_database, good)


async def test_source_cross_job_task_is_denied_with_same_lead_and_workflow(real_database):
    await install_source(real_database)
    await install_source(real_database, job_id="other", team_id="other-team")
    with pytest.raises(PermissionError):
        await authorize_employee_job_source(real_database, source_context(caller="member", job_id="other", task_id="task-job"))
    own = await read_employee_job_source(real_database, source_context(job_id="other"), content="body_text", limit=1)
    assert own["message_id"] == "mail-other"


@pytest.mark.parametrize("change", ["cancel", "offline", "wrong_origin", "wrong_creator", "remove_member"])
async def test_source_assignment_revocation_and_execution_authority(real_database, change):
    from sqlmodel import select
    await install_source(real_database)
    async with real_database.reserved_session() as session:
        task = await session.get(TeamTask, "task-job")
        member = (await session.execute(select(TeamMember).where(TeamMember.team_id == "team", TeamMember.agent_node_id == "member"))).scalar_one()
        if change == "cancel":
            task.cancellation_requested = True
        elif change == "offline":
            member.status = "offline"
        elif change == "wrong_origin":
            task.execution_id = "other-execution"
        elif change == "wrong_creator":
            task.created_by = "other-lead"
        else:
            await session.delete(member)
        await session.commit()
    with pytest.raises(PermissionError):
        await authorize_employee_job_source(real_database, source_context(caller="member", task_id="task-job"))


async def test_source_captured_teammate_created_assignment_remains_authorized(real_database):
    await install_source(real_database)
    async with real_database.reserved_session() as session:
        task = await session.get(TeamTask, "task-job")
        task.created_by = "member"
        await session.commit()
    page = await read_employee_job_source(real_database, source_context(caller="member", task_id="task-job"), content="body_text", limit=1)
    assert page["text"] == "🙂"


@pytest.mark.parametrize("task_execution", ["job", "origin"])
async def test_source_accepts_job_specific_and_exact_legacy_team_execution(real_database, task_execution):
    await install_source(real_database)
    async with real_database.reserved_session() as session:
        task = await session.get(TeamTask, "task-job")
        task.execution_id = task_execution
        await session.commit()
    assert (await read_employee_job_source(real_database, source_context(caller="member", task_id="task-job"), content="body_text", limit=1))["text"] == "🙂"


@pytest.mark.parametrize("arguments", [{"offset": -1}, {"limit": 0}, {"limit": 10001}, {"offset": True}, {"limit": True}, {"job_id": "other"}, {"agent_id": "lead"}, {"workflow_id": "other"}])
def test_source_schema_rejects_invalid_offsets_limits_and_spoofed_identity(arguments):
    with pytest.raises(ValidationError):
        EmployeeJobSourceParams.model_validate(arguments)


async def test_intrinsic_output_retains_metadata_wire_names_and_uncapped_page(real_database, monkeypatch):
    await install_source(real_database)
    monkeypatch.setattr("services.plugin.deps.get_database", lambda: real_database)
    from services.plugin import NodeContext
    node = EmployeeJobSourceNode()
    page = await node.read(NodeContext.from_legacy("source", "employeeJobSource", source_context()), EmployeeJobSourceParams(limit=10000))
    serialized = node._serialize_result(page)
    assert serialized["from"] == "sender@example.com" and "from_" not in serialized
    assert len(serialized["text"]) == serialized["next_offset"] == 10000
    text = json.dumps(serialized, ensure_ascii=False)
    assert not tool_output_is_capped(node.type)
    output = bound_tool_output(text, 5) if tool_output_is_capped(node.type) else text
    assert json.loads(output)["text"] == serialized["text"]
    assert "truncated" in bound_tool_output("external output", 5)


async def test_source_eof_cursor_and_legacy_source_without_metadata(real_database):
    await install_source(real_database)
    async with real_database.reserved_session() as session:
        job = await session.get(EmployeeJob, "job")
        source = dict(job.source)
        source.pop("email_input")
        job.source = source
        await session.commit()
    page = await read_employee_job_source(real_database, source_context(), content="original_body", offset=1000000)
    assert page["text"] == "" and page["complete"]
    assert page["offset"] == page["next_offset"] == page["total_chars"]
