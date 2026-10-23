"""A deployment's Start node fires once per generation (RFC-0004 C4).

Every backend restart deploys a running generation again to re-arm it,
and the Start node's run used to start again each time. The first fire
now records a claim in the runtime mutation ledger.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from services.deployment.manager import DeploymentManager
from services.deployment.state import DeploymentState


def _deployed(database, generation: int) -> DeploymentManager:
    broadcaster = MagicMock()
    broadcaster.update_node_status = AsyncMock()
    manager = DeploymentManager(
        database=database,
        execute_workflow_fn=AsyncMock(),
        store_output_fn=AsyncMock(),
        broadcaster=broadcaster,
    )
    manager._spawn_run = AsyncMock()
    manager._deployments["wf-1"] = DeploymentState(
        deployment_id="deploy-1",
        workflow_id="wf-1",
        is_running=True,
        nodes=[],
        edges=[],
        session_id="session",
        generation=generation,
    )
    return manager


async def test_a_restart_does_not_fire_the_start_node_again(real_database):
    first = _deployed(real_database, generation=3)
    assert (await first._fire_start_trigger({"id": "start-1"}, "wf-1")).fired is True

    # The backend restarts and a new manager re-arms generation 3.
    rearmed = _deployed(real_database, generation=3)
    info = await rearmed._fire_start_trigger({"id": "start-1"}, "wf-1")

    assert first._spawn_run.await_count == 1
    rearmed._spawn_run.assert_not_awaited()
    assert info.fired is False


async def test_a_new_generation_fires_the_start_node_again(real_database):
    await _deployed(real_database, generation=3)._fire_start_trigger({"id": "start-1"}, "wf-1")
    next_generation = _deployed(real_database, generation=4)

    await next_generation._fire_start_trigger({"id": "start-1"}, "wf-1")

    next_generation._spawn_run.assert_awaited_once()


async def test_a_deploy_outside_a_generation_fires_every_time(real_database):
    manager = _deployed(real_database, generation=0)

    await manager._fire_start_trigger({"id": "start-1"}, "wf-1")
    await manager._fire_start_trigger({"id": "start-1"}, "wf-1")

    assert manager._spawn_run.await_count == 2
