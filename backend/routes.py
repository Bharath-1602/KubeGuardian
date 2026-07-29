"""
backend/routes.py
────────────────────────────────────────
FastAPI route definitions for the KubeGuardian API.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("kubeguardian.routes")

router = APIRouter(prefix="/api")

# Agent instance — set by app.py on startup
_agent = None


def set_agent(agent):
    """Set the agent instance for route handlers."""
    global _agent
    _agent = agent


# ──────────────────────────────────────────────
# Request/Response Models
# ──────────────────────────────────────────────

class ChatRequest(BaseModel):
    """Chat message request body."""
    message: str
    session_id: str = "default"


class ApprovalRequest(BaseModel):
    """Approval/cancel request body."""
    action_id: str
    session_id: str = "default"


# ──────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────

@router.post("/chat")
async def chat(request: ChatRequest):
    """
    Main chat endpoint. Processes a user message through
    the AI agent and returns either a direct response or
    an approval request for write operations.

    Returns:
        {"type": "response", "message": str}
        OR
        {"type": "APPROVAL_REQUIRED", "action_id": str, "action_plan": dict}
    """
    if _agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    logger.info("Chat request: %s", request.message[:100])

    try:
        result = await _agent.process_message(request.message)
        return result
    except Exception as e:
        logger.error("Chat processing failed: %s", e, exc_info=True)
        return {
            "type": "response",
            "message": f"❌ **Internal error:** {str(e)}",
        }


@router.post("/approve")
async def approve(request: ApprovalRequest):
    """
    Approve a pending write operation.
    Executes the previously pre-checked action.

    Returns:
        {"type": "response", "message": str}
    """
    if _agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    logger.info("Approval request for action: %s", request.action_id)

    try:
        result = await _agent.execute_approved_action(request.action_id)
        return result
    except Exception as e:
        logger.error("Approval processing failed: %s", e, exc_info=True)
        return {
            "type": "response",
            "message": f"❌ **Approval execution error:** {str(e)}",
        }


@router.post("/cancel")
async def cancel(request: ApprovalRequest):
    """
    Cancel a pending write operation.
    Removes the action from pending state and logs the denial.

    Returns:
        {"type": "response", "message": str}
    """
    if _agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    logger.info("Cancel request for action: %s", request.action_id)

    try:
        result = await _agent.cancel_action(request.action_id)
        return result
    except Exception as e:
        logger.error("Cancel processing failed: %s", e, exc_info=True)
        return {
            "type": "response",
            "message": f"❌ **Cancel error:** {str(e)}",
        }


@router.get("/health")
async def health():
    """
    Health check endpoint.

    Returns:
        {"status": "ok", "mcp_connected": bool}
    """
    mcp_connected = False
    if _agent and _agent._mcp_session:
        mcp_connected = True

    return {
        "status": "ok",
        "mcp_connected": mcp_connected,
    }


@router.get("/cluster/quick-status")
async def quick_status():
    """
    Quick cluster overview for the UI header display.
    Returns node count, pod count, and health status.
    Non-blocking: returns last known state on failure.
    """
    if _agent is None or _agent._mcp_session is None:
        return {
            "nodes": "—",
            "pods": "—",
            "health": "Unknown",
            "error": "Agent not connected",
        }

    try:
        result = await _agent._mcp_session.call_tool("get_cluster_overview", {})
        result_text = ""
        for content_item in result.content:
            if hasattr(content_item, 'text'):
                result_text += content_item.text

        data = json.loads(result_text)

        if "error" in data:
            return {
                "nodes": "—",
                "pods": "—",
                "health": "Error",
                "error": data["error"],
            }

        return {
            "nodes": f"{data.get('ready_nodes', 0)}/{data.get('total_nodes', 0)}",
            "pods": data.get("total_pods", 0),
            "running_pods": data.get("running_pods", 0),
            "pending_pods": data.get("pending_pods", 0),
            "failed_pods": data.get("failed_pods", 0),
            "health": data.get("health", "Unknown"),
            "cluster_name": data.get("cluster_name", ""),
            "kubernetes_version": data.get("kubernetes_version", ""),
        }
    except Exception as e:
        logger.warning("quick_status failed: %s", e)
        return {
            "nodes": "—",
            "pods": "—",
            "health": "Error",
            "error": str(e),
        }
