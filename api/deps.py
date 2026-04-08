from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from temporalio.client import Client


def _get_temporal_client(request: Request) -> Client:
    return request.app.state.temporal_client


TemporalClient = Annotated[Client, Depends(_get_temporal_client)]
