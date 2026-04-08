from __future__ import annotations

from typing import Annotated, AsyncGenerator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client

from db.session import get_db  # re-export for use in routes

__all__ = ["DBSession", "TemporalClient"]

DBSession = Annotated[AsyncSession, Depends(get_db)]


def _get_temporal_client(request: Request) -> Client:
    """Retrieve the shared Temporal client stored in app state."""
    return request.app.state.temporal_client


TemporalClient = Annotated[Client, Depends(_get_temporal_client)]
