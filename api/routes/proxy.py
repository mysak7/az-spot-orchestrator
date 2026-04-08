"""Reverse-proxy route — forwards LLM requests to the active Spot VM."""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select

from api.deps import DBSession
from db.models import LLMModel, VMInstance, VMStatus

logger = logging.getLogger(__name__)

router = APIRouter()


@router.api_route(
    "/proxy/{model_name}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def proxy_to_vm(
    model_name: str,
    path: str,
    request: Request,
    db: DBSession,
) -> Response:
    """Forward any LLM API request to the running Spot VM for the given model.

    Upstream URL: ``http://<vm_ip>:11434/<path>``

    Example (OpenAI-compatible):
        POST /proxy/llama3-8b/v1/chat/completions
    """
    # Look up the most recently created running instance for this model
    row = await db.execute(
        select(VMInstance)
        .join(LLMModel)
        .where(LLMModel.name == model_name)
        .where(VMInstance.status == VMStatus.running)
        .order_by(VMInstance.created_at.desc())
        .limit(1)
    )
    instance = row.scalar_one_or_none()

    if not instance or not instance.ip_address:
        raise HTTPException(
            status_code=503,
            detail=f"No running instance available for model '{model_name}'",
        )

    target_url = f"http://{instance.ip_address}:11434/{path}"
    body = await request.body()
    # Strip hop-by-hop headers that shouldn't be forwarded
    forward_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "transfer-encoding")
    }

    async with httpx.AsyncClient(timeout=300.0) as client:
        try:
            upstream = await client.request(
                method=request.method,
                url=target_url,
                content=body,
                headers=forward_headers,
                params=dict(request.query_params),
            )
        except httpx.RequestError as exc:
            logger.error("Proxy error for %s: %s", target_url, exc)
            raise HTTPException(status_code=502, detail=f"Upstream error: {exc}") from exc

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=dict(upstream.headers),
    )
