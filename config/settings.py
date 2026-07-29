"""
config/settings.py
────────────────────────────────────────
Single source of all configuration for KubeGuardian.
Every other module imports from here.
Reads values from environment variables / .env file.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# ──────────────────────────────────────────────
# Load .env file from project root
# ──────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# ──────────────────────────────────────────────
# AI Provider API Keys (set whichever you use)
# ──────────────────────────────────────────────
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
CLAUDE_API_KEY: str = os.getenv("CLAUDE_API_KEY", "")
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

# Which AI provider to use: "groq", "claude", or "gemini"
AI_PROVIDER: str = os.getenv("AI_PROVIDER", "groq").lower()

# ──────────────────────────────────────────────
# AI Model Configuration
# ──────────────────────────────────────────────
GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# ──────────────────────────────────────────────
# MCP Server Configuration
# ──────────────────────────────────────────────
MCP_SERVER_HOST: str = os.getenv("MCP_SERVER_HOST", "0.0.0.0")
MCP_SERVER_PORT: int = int(os.getenv("MCP_SERVER_PORT", "8080"))
MCP_SERVER_URL: str = os.getenv(
    "MCP_SERVER_URL",
    f"http://127.0.0.1:{MCP_SERVER_PORT}/mcp"
)

# ──────────────────────────────────────────────
# FastAPI Backend Configuration
# ──────────────────────────────────────────────
BACKEND_HOST: str = os.getenv("BACKEND_HOST", "0.0.0.0")
BACKEND_PORT: int = int(os.getenv("BACKEND_PORT", "8000"))

# ──────────────────────────────────────────────
# AWS / EKS Configuration
# ──────────────────────────────────────────────
AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")
EKS_CLUSTER_NAME: str = os.getenv("EKS_CLUSTER_NAME", "eks-mcp-demo")

# ──────────────────────────────────────────────
# Guardrail Limits
# ──────────────────────────────────────────────
MAX_REPLICAS: int = int(os.getenv("MAX_REPLICAS", "20"))
MIN_REPLICAS: int = int(os.getenv("MIN_REPLICAS", "1"))

PROTECTED_NAMESPACES: list[str] = [
    "kube-system",
    "kube-public",
    "kube-node-lease",
]

ALLOWED_PATCH_FIELDS: list[str] = [
    "labels",
    "annotations",
]

# ──────────────────────────────────────────────
# Audit Logging
# ──────────────────────────────────────────────
AUDIT_LOG_PATH: str = os.getenv(
    "AUDIT_LOG_PATH",
    str(PROJECT_ROOT / "logs" / "audit.log")
)

# ──────────────────────────────────────────────
# Pending Action Timeout
# ──────────────────────────────────────────────
PENDING_ACTION_TIMEOUT_SECONDS: int = int(
    os.getenv("PENDING_ACTION_TIMEOUT_SECONDS", "300")
)

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
