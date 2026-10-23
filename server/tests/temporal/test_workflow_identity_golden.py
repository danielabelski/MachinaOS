"""Release command identities and worker/replayer registration cannot drift."""
from __future__ import annotations

import json
from pathlib import Path

from tests.temporal.workflow_identities import digest, identity_snapshot

GOLDEN = Path(__file__).with_name("fixtures") / "workflow_identities.json"


def test_workflow_command_identities_match_release_or_explicit_patch_entry():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    current = identity_snapshot()
    if current == golden["identities"]:
        return
    before, after = digest(golden["identities"]), digest(current)
    entries = [entry for entry in golden.get("patch_entries", []) if entry["before"] == before and entry["after"] == after]
    assert entries, "Temporal activity names/child IDs/markers changed: add a reviewed versioning patch entry and replay N-1 histories"
    markers = {command["type"].strip("'\"") for module in current.values() for command in module["commands"]
               if command["command"] in {"patched", "deprecate_patch"}}
    assert all(entry.get("patch") in markers and entry.get("reason") for entry in entries)


def test_worker_and_replayer_use_the_shared_production_registry():
    from services.temporal.worker import _framework_workflows
    from services.temporal.workflow_registry import framework_workflows, replay_workflows

    assert _framework_workflows is framework_workflows
    names = [item.__temporal_workflow_definition.name for item in replay_workflows()]
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert sorted(names) == golden["workflow_types"]
    assert len(names) == len(set(names)), "Duplicate workflow registrations"


def test_plugin_activity_type_versions_retain_the_release_names():
    import nodes  # noqa: F401 -- discover the production node classes
    from services.node_registry import registered_node_classes

    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    current = {name: f"node.{cls.type}.v{cls.version}" for name, cls in registered_node_classes().items()}
    # New node types are additive; old names must remain registerable until
    # their N-1 workflows have drained or a reviewed migration is supplied.
    changed = {name: expected for name, expected in golden["plugin_activity_types"].items() if current.get(name) != expected}
    assert not changed, f"Historical plugin activity names changed without compatibility registration: {changed}"
