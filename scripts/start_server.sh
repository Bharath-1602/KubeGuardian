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
if [ ! -f "venv/bin/activate" ]; then
    echo "❌ Virtual environment not found at venv/"
    echo "   Run setup first: bash scripts/setup_ec2.sh"
    exit 1
fi
source venv/bin/activate
echo "▸ Virtual environment activated"

# ──── Verify .env exists ────
if [ ! -f ".env" ]; then
    echo "❌ .env file not found"
    echo "   Copy and edit: cp .env.example .env && nano .env"
    exit 1
fi

# Load .env into environment
set -a
source .env
set +a
echo "▸ Environment variables loaded"

# ──── Verify API key is set ────
AI_PROVIDER="${AI_PROVIDER:-groq}"
if [ "$AI_PROVIDER" = "groq" ] && [ -z "${GROQ_API_KEY:-}" ]; then
    echo "❌ GROQ_API_KEY is not set in .env"
    echo "   Get your free key at: https://console.groq.com"
    exit 1
fi
if [ "$AI_PROVIDER" = "claude" ] && [ -z "${CLAUDE_API_KEY:-}" ]; then
    echo "❌ CLAUDE_API_KEY is not set in .env"
    exit 1
fi
if [ "$AI_PROVIDER" = "gemini" ] && [ -z "${GEMINI_API_KEY:-}" ]; then
    echo "❌ GEMINI_API_KEY is not set in .env"
    exit 1
fi
echo "▸ AI provider: $AI_PROVIDER ✅"

# ──── Ensure logs directory exists ────
mkdir -p logs

# ──── Stop any existing processes on those ports ────
echo "▸ Stopping any existing KubeGuardian processes..."
pkill -f "server.main"      2>/dev/null || true
pkill -f "uvicorn.*backend" 2>/dev/null || true
sleep 1

MCP_PORT="${MCP_SERVER_PORT:-8080}"
BACKEND_PORT="${BACKEND_PORT:-8000}"

# ──── Start MCP Server ────
echo ""
echo "▸ Starting MCP Server on port ${MCP_PORT}..."
nohup python3 -m server.main \
    > logs/mcp-server.log 2>&1 &
MCP_PID=$!
echo "  PID: $MCP_PID  |  Logs: logs/mcp-server.log"

# Wait for MCP server to actually be listening
echo "  Waiting for MCP server to be ready..."
MCP_READY=false
for i in $(seq 1 15); do
    if curl -sf "http://127.0.0.1:${MCP_PORT}/mcp" \
        --max-time 1 >/dev/null 2>&1; then
        MCP_READY=true
        break
    fi
    # Also check the process is still alive
    if ! kill -0 "$MCP_PID" 2>/dev/null; then
        echo "❌ MCP Server process died. Check logs:"
        echo "   cat logs/mcp-server.log"
        exit 1
    fi
    sleep 1
done

# MCP uses streamable-http — a 404/405 on /mcp is also "alive"
# so we just check the process is running after the wait
if ! kill -0 "$MCP_PID" 2>/dev/null; then
    echo "❌ MCP Server failed to start. Check logs:"
    echo "   cat logs/mcp-server.log"
    exit 1
fi
echo "  ✅ MCP Server running (PID: $MCP_PID)"

# ──── Start FastAPI Backend ────
echo ""
echo "▸ Starting FastAPI Backend on port ${BACKEND_PORT}..."
nohup uvicorn backend.app:app \
    --host "${BACKEND_HOST:-0.0.0.0}" \
    --port "${BACKEND_PORT}" \
    --log-level info \
    > logs/backend.log 2>&1 &
BACKEND_PID=$!
echo "  PID: $BACKEND_PID  |  Logs: logs/backend.log"

# Wait for FastAPI to be ready
echo "  Waiting for FastAPI to be ready..."
BACKEND_READY=false
for i in $(seq 1 20); do
    if curl -sf "http://127.0.0.1:${BACKEND_PORT}/api/health" \
        --max-time 1 >/dev/null 2>&1; then
        BACKEND_READY=true
        break
    fi
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
        echo "❌ FastAPI process died. Check logs:"
        echo "   cat logs/backend.log"
        kill "$MCP_PID" 2>/dev/null || true
        exit 1
    fi
    sleep 1
done

if [ "$BACKEND_READY" = false ]; then
    echo "⚠️  FastAPI did not respond in time — may still be"
    echo "   connecting to MCP. Check: cat logs/backend.log"
else
    echo "  ✅ FastAPI Backend ready (PID: $BACKEND_PID)"
fi

# ──── Save PIDs for later use ────
echo "$MCP_PID"     > logs/mcp.pid
echo "$BACKEND_PID" > logs/backend.pid

# ──── Summary ────
EC2_IP=$(curl -s --connect-timeout 2 \
    http://169.254.169.254/latest/meta-data/public-ipv4 \
    2>/dev/null || echo "localhost")

echo ""
echo "═══════════════════════════════════════════"
echo "  ✅ KubeGuardian is Running!"
echo "═══════════════════════════════════════════"
echo ""
echo "  Processes:"
echo "    MCP Server:      PID $MCP_PID"
echo "    FastAPI Backend: PID $BACKEND_PID"
echo ""
echo "  Logs:"
echo "    MCP Server:  tail -f $PROJECT_DIR/logs/mcp-server.log"
echo "    Backend:     tail -f $PROJECT_DIR/logs/backend.log"
echo "    Audit:       tail -f $PROJECT_DIR/logs/audit.log"
echo ""
echo "  🌐 Open browser at: https://${EC2_IP}"
echo "     (Accept the self-signed certificate warning)"
echo ""
echo "  To stop all servers:"
echo "    kill \$(cat logs/mcp.pid) \$(cat logs/backend.pid)"
echo "    OR: pkill -f 'server.main'; pkill -f 'uvicorn.*backend'"
echo ""
