"""System messages API — persistent warning/info notifications stored in Cosmos DB."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/messages")
async def list_messages(unread_only: bool = False) -> dict:
    """Return system messages ordered newest-first.

    Pass ?unread_only=true to get only unread messages (used for the badge count).
    """
    from db.cosmos import get_messages_container

    try:
        container = get_messages_container()
        query = (
            "SELECT * FROM c WHERE c.read = false ORDER BY c.created_at DESC"
            if unread_only
            else "SELECT * FROM c ORDER BY c.created_at DESC"
        )
        items = [item async for item in container.query_items(query=query)]
        return {"items": items, "total": len(items)}
    except Exception as exc:
        logger.error("Failed to fetch messages: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.patch("/messages/{msg_id}/read")
async def mark_read(msg_id: str) -> dict:
    """Mark a single message as read."""
    from db.cosmos import get_messages_container

    try:
        container = get_messages_container()
        item = await container.read_item(item=msg_id, partition_key=msg_id)
        item["read"] = True
        await container.replace_item(item=msg_id, body=item)
        return {"id": msg_id, "read": True}
    except Exception as exc:
        logger.error("Failed to mark message %s as read: %s", msg_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete("/messages", status_code=204)
async def clear_all_messages() -> None:
    """Delete all messages."""
    from db.cosmos import get_messages_container

    try:
        container = get_messages_container()
        ids = [
            item["id"]
            async for item in container.query_items(query="SELECT c.id FROM c")
        ]
        for msg_id in ids:
            await container.delete_item(item=msg_id, partition_key=msg_id)
    except Exception as exc:
        logger.error("Failed to clear messages: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete("/messages/{msg_id}", status_code=204)
async def delete_message(msg_id: str) -> None:
    """Delete a single message."""
    from db.cosmos import get_messages_container

    try:
        container = get_messages_container()
        await container.delete_item(item=msg_id, partition_key=msg_id)
    except Exception as exc:
        logger.error("Failed to delete message %s: %s", msg_id, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
