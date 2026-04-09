"""Reverse-proxy — forwards LLM requests to the active Spot VM."""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException, Request, Response

from db.cosmos import get_instances_container
from db.models import VMStatus

logger = logging.getLogger(__name__)
router = APIRouter()


@router.api_route(
    "/proxy/{model_name}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def proxy_to_vm(model_name: str, path: str, request: Request) -> Response:
    """Forward any LLM API request to the running Spot VM for the given model.

    Upstream URL: ``http://<vm_ip>:11434/<path>``

    Example (OpenAI-compatible Ollama endpoint):
        POST /proxy/llama3-8b/v1/chat/completions
    """
    container = get_instances_container()

    # model_name is denormalised on VMInstance — single cross-partition query
    items = [
        item
        async for item in container.query_items(
            query=(
                "SELECT * FROM c "
                "WHERE c.model_name = @mn AND c.status = @st "
                "ORDER BY c.created_at DESC "
                "OFFSET 0 LIMIT 1"
            ),
            parameters=[
                {"name": "@mn", "value": model_name},
                {"name": "@st", "value": VMStatus.running.value},
            ],
        )
    ]

    if not items or not items[0].get("ip_address"):
        raise HTTPException(
            status_code=503,
            detail=f"No running instance for model '{model_name}'",
        )

    ip = items[0]["ip_address"]
    target_url = f"http://{ip}:11434/{path}"
    body = await request.body()
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
            logger.error("Proxy error → %s: %s", target_url, exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=dict(upstream.headers),
    )
