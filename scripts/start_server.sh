#!/usr/bin/env bash
# ────────────────────────────────────────
# scripts/start_server.sh
# KubeGuardian — Start MCP Server + FastAPI Backend
# ────────────────────────────────────────

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "═══════════════════════════════════════════"
echo "  KubeGuardian — Starting Servers"
echo "═══════════════════════════════════════════"
echo ""

# ──── Activate virtual environment ────
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
    echo "▸ Virtual environment activated"
else
    echo "⚠️  No virtual environment found at venv/"
    echo "   Run: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

# ──── Load environment variables ────
if [ -f ".env" ]; then
    set -a
    source .env
    set +a
    echo "▸ Environment variables loaded from .env"
else
    echo "⚠️  No .env file found. Copy .env.example to .env and fill in your API keys."
    exit 1
fi

# ──── Ensure logs directory exists ────
mkdir -p logs

# ──── Stop any existing processes ────
echo "▸ Checking for existing processes..."
pkill -f "python.*server/main.py" 2>/dev/null || true
pkill -f "uvicorn.*backend.app" 2>/dev/null || true
sleep 1

# ──── Start MCP Server ────
echo "▸ Starting MCP Server on port ${MCP_SERVER_PORT:-8080}..."
python -m server.main &
MCP_PID=$!
echo "  MCP Server PID: $MCP_PID"

# Wait for MCP server to start
echo "  Waiting for MCP server to be ready..."
sleep 3

# Verify MCP server is running
if ! kill -0 $MCP_PID 2>/dev/null; then
    echo "❌ MCP Server failed to start!"
    exit 1
fi
echo "  ✅ MCP Server started"

# ──── Start FastAPI Backend ────
echo "▸ Starting FastAPI Backend on port ${BACKEND_PORT:-8000}..."
uvicorn backend.app:app \
    --host "${BACKEND_HOST:-0.0.0.0}" \
    --port "${BACKEND_PORT:-8000}" \
    --log-level info &
BACKEND_PID=$!
echo "  FastAPI Backend PID: $BACKEND_PID"

# Wait for backend to start
sleep 2

# Verify backend is running
if ! kill -0 $BACKEND_PID 2>/dev/null; then
    echo "❌ FastAPI Backend failed to start!"
    kill $MCP_PID 2>/dev/null || true
    exit 1
fi
echo "  ✅ FastAPI Backend started"

echo ""
echo "═══════════════════════════════════════════"
echo "  ✅ Both Servers Running!"
echo "═══════════════════════════════════════════"
echo ""
echo "  MCP Server:     http://localhost:${MCP_SERVER_PORT:-8080} (internal)"
echo "  FastAPI Backend: http://localhost:${BACKEND_PORT:-8000}"
echo ""
echo "  Process IDs:"
echo "    MCP Server:     $MCP_PID"
echo "    FastAPI Backend: $BACKEND_PID"
echo ""

# Determine access URL
EC2_IP=$(curl -s --connect-timeout 2 http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo "localhost")
echo "  🌐 Open your browser at:"
echo "     https://${EC2_IP}"
echo "     (or http://localhost:${BACKEND_PORT:-8000} for local dev)"
echo ""
echo "  To stop servers:"
echo "     kill $MCP_PID $BACKEND_PID"
echo ""

# Wait for both processes
wait
