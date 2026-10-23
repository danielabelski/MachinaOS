"""Browser profile metadata. Profile files and cookie values stay outside SQL."""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import JSON, Column, UniqueConstraint, Index, event, func, text
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BrowserProfileRow(SQLModel, table=True):
    __tablename__ = "browser_profiles"
    __table_args__ = (UniqueConstraint("owner_id", "workflow_id", "kind", name="uq_browser_profile_workflow"),)
    id: str = Field(primary_key=True, max_length=40)
    owner_id: str = Field(default="owner", index=True, max_length=255)
    name: str = Field(max_length=120)
    kind: str = Field(default="shared", max_length=20)
    workflow_id: Optional[str] = Field(default=None, index=True, max_length=255)
    chrome_major: Optional[int] = Field(default=None)
    sites: Optional[List[Dict[str, Any]]] = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class BrowserProfileReservation(SQLModel, table=True):
    """Immutable provenance for a newly allocated, agent-private resource.

    Old profiles deliberately have no reservation. This is additive: no old
    row or login state is upgraded when the runtime is released.
    """
    __tablename__ = "browser_profile_reservations"
    __table_args__ = (UniqueConstraint("owner_id", "workflow_id", "browser_node_id", name="uq_private_browser_binding"),)
    profile_id: str = Field(primary_key=True, max_length=40)
    owner_id: str = Field(index=True, max_length=255)
    workflow_id: str = Field(index=True, max_length=255)
    agent_node_id: str = Field(index=True, max_length=255)
    browser_node_id: str = Field(index=True, max_length=255)
    runtime_version: int = Field(default=1)


@event.listens_for(BrowserProfileReservation, "before_update")
def _immutable_browser_reservation(_mapper, _connection, _target):
    raise ValueError("Private Browser reservations are immutable; create a separate profile")


Index("uq_browser_profile_shared_name", BrowserProfileRow.owner_id, func.lower(BrowserProfileRow.name),
      unique=True, sqlite_where=text("kind = 'shared'"), postgresql_where=text("kind = 'shared'"))
