"""
backend/app.py
────────────────────────────────────────
FastAPI application setup.
Initializes the app, CORS, static files, and
the MCPGroqAgent on startup.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.settings import LOG_LEVEL
from backend.agent import MCPGroqAgent
from backend.routes import router, set_agent

logger = logging.getLogger("kubeguardian.app")

# ──────────────────────────────────────────────
# Global agent instance
# ──────────────────────────────────────────────
agent: MCPGroqAgent | None = None


# ──────────────────────────────────────────────
# Lifespan — start/stop MCP connection
# ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan handler.
    Initializes MCPGroqAgent and connects to MCP server on startup.
    Disconnects on shutdown.
    """
    global agent
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    logger.info("Starting KubeGuardian Backend...")

    try:
        agent = MCPGroqAgent()
        await agent.connect_mcp()
        set_agent(agent)
        logger.info("KubeGuardian Backend ready!")
    except Exception as e:
        logger.error("Failed to initialize agent: %s", e, exc_info=True)
        # Still start the app — health endpoint will report MCP disconnected
        agent = MCPGroqAgent()
        set_agent(agent)

    yield

    # Shutdown
    logger.info("Shutting down KubeGuardian Backend...")
    if agent:
        await agent.disconnect_mcp()


# ──────────────────────────────────────────────
# FastAPI App
# ──────────────────────────────────────────────

app = FastAPI(
    title="KubeGuardian",
    description="AI-powered EKS Cluster Management Agent",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — allow all origins (internal tool)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routes
app.include_router(router)

# ──────────────────────────────────────────────
# Static files & Frontend serving
# ──────────────────────────────────────────────

# Path to frontend directory
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# Mount static files (CSS, JS)
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def serve_frontend():
    """Serve the frontend index.html at the root path."""
    index_path = FRONTEND_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return {"message": "Frontend not found. Place index.html in frontend/ directory."}


@app.get("/favicon.ico")
async def favicon():
    """Serve favicon (returns 204 if not found)."""
    favicon_path = FRONTEND_DIR / "favicon.ico"
    if favicon_path.exists():
        return FileResponse(str(favicon_path))
    return FileResponse(str(FRONTEND_DIR / "index.html"), status_code=204)
