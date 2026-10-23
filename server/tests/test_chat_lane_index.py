"""The chat lane (models/chat.py): at most one live run per session, on both
databases. A shared PostgreSQL database once got the lane as a plain
``UNIQUE(session_id)``, which refused every run after a conversation's
first; startup rebuilds that index as the declared partial one."""

import sys
from datetime import datetime, timezone

import pytest
from sqlalchemy import Index, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateIndex
from sqlmodel import SQLModel

from models.chat import ChatRun


def _lane_index() -> Index:
    return next(index for index in ChatRun.__table__.indexes if index.name == "ux_chat_runs_lane")


def test_lane_index_is_partial_on_postgresql():
    ddl = str(CreateIndex(_lane_index()).compile(dialect=postgresql.dialect()))

    assert "WHERE state IN ('queued', 'pending', 'running', 'stopping')" in ddl


async def test_every_partial_index_names_its_condition_for_both_databases(real_database):
    for table in SQLModel.metadata.tables.values():
        for index in table.indexes:
            sqlite_where = index.dialect_options["sqlite"].get("where")
            postgresql_where = index.dialect_options["postgresql"].get("where")
            assert (sqlite_where is None) == (postgresql_where is None), index.name
            if sqlite_where is not None:
                assert str(sqlite_where) == str(postgresql_where), index.name


class _Connection:
    def __init__(self, definition):
        self.definition = definition

    def execute(self, statement):
        return self

    def scalar(self):
        return self.definition


@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        ("CREATE UNIQUE INDEX ux_chat_runs_lane ON public.chat_runs USING btree (session_id)", ["drop", "create"]),
        (None, ["create"]),
        ("CREATE UNIQUE INDEX ux_chat_runs_lane ON public.chat_runs USING btree (session_id) WHERE ((state)::text = ANY "
         "((ARRAY['queued'::character varying])::text[]))", []),
    ],
)
async def test_startup_rebuilds_only_a_lane_index_without_its_condition(real_database, monkeypatch, definition, expected):
    rebuild = sys.modules[type(real_database).__module__]._rebuild_chat_lane_index
    calls = []
    monkeypatch.setattr(Index, "drop", lambda index, bind: calls.append("drop"))
    monkeypatch.setattr(Index, "create", lambda index, bind: calls.append("create"))

    rebuild(_Connection(definition))

    assert calls == expected


async def _add_run(database, run_id: str, state: str) -> None:
    async with database.get_session() as session:
        session.add(ChatRun(run_id=run_id, session_id="session", state=state, created_at=datetime.now(timezone.utc)))
        await session.commit()


async def test_a_conversation_takes_a_new_run_after_the_last_one_ended(real_database):
    await _add_run(real_database, "r1", "finished")
    await _add_run(real_database, "r2", "pending")

    with pytest.raises(IntegrityError):
        await _add_run(real_database, "r3", "running")


async def test_postgresql_startup_rebuilds_a_plain_lane_index(real_database):
    if real_database.engine.dialect.name != "postgresql":
        pytest.skip("TEST_POSTGRES_URL is required for the PostgreSQL index rebuild")
    rebuild = sys.modules[type(real_database).__module__]._rebuild_chat_lane_index
    async with real_database.engine.begin() as connection:
        await connection.execute(text("DROP INDEX ux_chat_runs_lane"))
        await connection.execute(text("CREATE UNIQUE INDEX ux_chat_runs_lane ON chat_runs (session_id)"))
        await connection.run_sync(rebuild)
        definition = (await connection.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'ux_chat_runs_lane'"))).scalar()

    assert " WHERE " in definition
    await test_a_conversation_takes_a_new_run_after_the_last_one_ended(real_database)
