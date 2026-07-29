# 🛡️ KubeGuardian

**AI-Powered EKS Cluster Management via Natural Language**

KubeGuardian is a full-stack tool that lets DevOps engineers manage Amazon EKS clusters using plain English through a browser-based chat interface. It combines an MCP (Model Context Protocol) Server as the secure execution layer, AI language models (Groq / Claude / Gemini) as the brain, FastAPI as the backend bridge, and a vanilla HTML/CSS/JS frontend — all running on a single AWS EC2 instance.

---

## 📐 Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Browser (User)                          │
│                    https://<EC2_PUBLIC_IP>                       │
└───────────────────────────┬─────────────────────────────────────┘
                            │ HTTPS (443)
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Nginx Reverse Proxy                         │
│                  (TLS termination, port 443)                    │
└───────────────────────────┬─────────────────────────────────────┘
                            │ HTTP (8000)
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│               FastAPI Backend (port 8000)                       │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────────────┐      │
│  │ app.py   │  │routes.py │  │ agent.py                 │      │
│  │(startup) │  │ (API)    │  │ (AI + MCP Client)        │      │
│  └──────────┘  └──────────┘  └─────────┬────────────────┘      │
│                                        │ Streamable HTTP        │
│                                        │ (localhost:8080)       │
│  ┌─────────────────────────────────────┼───────────────────┐    │
│  │           MCP Server (port 8080)    ▼                   │    │
│  │  ┌──────────┐  ┌────────────┐  ┌────────────────┐      │    │
│  │  │ main.py  │  │guardrails  │  │  audit_log.py  │      │    │
│  │  │(21 tools)│  │   .py      │  │                │      │    │
│  │  └────┬─────┘  └────────────┘  └────────────────┘      │    │
│  │       │                                                 │    │
│  │  ┌────▼─────────────────────────────────┐               │    │
│  │  │         k8s_client.py                │               │    │
│  │  │  (All Kubernetes API calls)          │               │    │
│  │  └────┬────────────────────────┬────────┘               │    │
│  │       │                        │                        │    │
│  └───────┼────────────────────────┼────────────────────────┘    │
└──────────┼────────────────────────┼─────────────────────────────┘
           │ kubernetes API         │ boto3 (STS)
           ▼                        ▼
    ┌──────────────┐        ┌──────────────┐
    │  EKS Cluster │        │  IAM Instance│
    │              │        │  Profile     │
    └──────────────┘        └──────────────┘
```

---

## 🚀 Quick Start

### Prerequisites

- AWS EC2 instance (Ubuntu 22.04) with:
  - IAM Instance Profile with EKS access permissions
  - Security group allowing ports 80, 443, 8000
- An EKS cluster already running
- An API key for at least one AI provider (Groq, Claude, or Gemini)

### Step 1: Clone & Setup

```bash
# Clone the repository
git clone <your-repo-url> ~/eks-mcp-agent
cd ~/eks-mcp-agent

# Run the automated setup script
chmod +x scripts/setup_ec2.sh
bash scripts/setup_ec2.sh
```

This script will:
1. Update system packages
2. Install Python 3.11, pip, python3-venv
3. Install Nginx
4. Install kubectl (v1.29)
5. Install AWS CLI v2
6. Create Python virtual environment
7. Install all pip dependencies
8. Generate self-signed SSL certificate
9. Configure and enable Nginx
10. Create required directories

### Step 2: Configure Environment

```bash
# Edit the .env file with your API keys
nano .env
```

**Required settings:**

```bash
# Choose your AI provider: "groq", "claude", or "gemini"
AI_PROVIDER=groq

# Set the API key for your chosen provider
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxx
# CLAUDE_API_KEY=sk-ant-xxxxxxxxxxxx
# GEMINI_API_KEY=AIzaSyxxxxxxxxxxxxxxxxx

# AWS / EKS
AWS_REGION=us-east-1
EKS_CLUSTER_NAME=your-cluster-name
```

### Step 3: Configure EKS Access

The EC2 instance authenticates to EKS via its IAM Instance Profile. Ensure the IAM role has:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "eks:DescribeCluster",
        "eks:ListClusters",
        "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    }
  ]
}
```

Also add the IAM role to the EKS `aws-auth` ConfigMap:

```bash
# Update kubeconfig (for kubectl verification)
aws eks update-kubeconfig --name <cluster-name> --region <region>

# Verify access
kubectl get nodes
```

### Step 4: Deploy Sample Workloads (Optional)

```bash
chmod +x scripts/deploy_workloads.sh
bash scripts/deploy_workloads.sh
```

This deploys test workloads across `default`, `production`, and `staging` namespaces including:
- Multi-replica deployments (nginx, api-server, frontend)
- Single-replica deployments (redis, test-app) — for testing warnings
- Pod Disruption Budgets (production namespace)
- A standalone pod (no controller) — for drain risk testing
- Services (ClusterIP, LoadBalancer)

### Step 5: Start the Servers

```bash
chmod +x scripts/start_server.sh
bash scripts/start_server.sh
```

This starts:
1. **MCP Server** on port 8080 (internal only)
2. **FastAPI Backend** on port 8000 (proxied via Nginx)

### Step 6: Access the UI

Open your browser:
```
https://<EC2_PUBLIC_IP>
```

> ⚠️ You'll see a certificate warning because we use a self-signed SSL cert. Click "Advanced" → "Proceed" to continue.

For local development:
```
http://localhost:8000
```

---

## 🧰 Features

### Read-Only Operations (Instant, no approval needed)
| Command | What it does |
|---------|-------------|
| "Show cluster health overview" | Node count, pod counts, K8s version, health status |
| "List all nodes" | Node status, instance type, capacity, conditions |
| "Show pods in production" | Pod list with status, containers, age |
| "Get logs from pod X" | Tail pod logs (supports crashed container logs) |
| "Describe pod X" | Full pod details, events, resources, volumes |
| "List deployments" | Replica counts, strategy, images |
| "Show services" | Service types, endpoints, load balancers |
| "Show namespaces" | All namespaces with status |
| "Show warning events" | Recent cluster events filtered by type |
| "Check PDBs" | Pod Disruption Budget status |
| "Find unhealthy pods" | All Pending/Failed pods with reasons |
| "Inspect node X" | Deep node details, taints, conditions |
| "Is it safe to drain node X?" | **Full maintenance assessment** |

### Write Operations (Require approval)
| Command | Approval Gate |
|---------|--------------|
| "Scale deployment X to 5" | Shows current vs. target replicas, capacity check |
| "Restart deployment X" | Shows current state, risk assessment |
| "Add label env=prod to deployment X" | Shows patch details |
| "Cordon node X" | Shows pods on node, impact |

### Safety Guardrails (Cannot be overridden)
- ❌ Never scale to 0 or above 20 replicas
- ❌ Never modify kube-system, kube-public, kube-node-lease
- ❌ Never patch anything except labels/annotations
- ❌ Never auto-execute drain (advisory only)
- ❌ Never delete resources
- ❌ Never expose secrets or credentials

---

## 🔄 Approval Gate Flow

```
User: "Scale nginx to 5 replicas"
        │
        ▼
┌─────────────────────┐
│ 1. Guardrail Check  │──▶ Blocked? Return error
│    (namespace, etc.) │
└─────────┬───────────┘
          │ Pass
          ▼
┌─────────────────────┐
│ 2. Pre-check        │──▶ Deployment exists?
│    (capacity, state) │──▶ Cluster has capacity?
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│ 3. Approval Card    │──▶ Shown in chat UI
│    (risk, impact)    │
└─────────┬───────────┘
          │
    ┌─────┴─────┐
    ▼           ▼
  ✅ YES      ❌ NO
    │           │
    ▼           ▼
  Execute    Cancel
  + Audit    + Audit
    Log        Log
```

---

## 📁 Directory Structure

```
eks-mcp-agent/
│
├── server/                 # MCP Server (execution layer)
│   ├── __init__.py
│   ├── main.py             # FastMCP entry point, 21 tools
│   ├── k8s_client.py       # All Kubernetes API calls
│   ├── guardrails.py       # Safety validation rules
│   └── audit_log.py        # JSON audit logging
│
├── backend/                # FastAPI Backend (AI bridge)
│   ├── __init__.py
│   ├── app.py              # FastAPI app setup, lifespan
│   ├── agent.py            # AI agent (Groq/Claude/Gemini + MCP)
│   └── routes.py           # API endpoints
│
├── frontend/               # Browser UI (no build step)
│   ├── index.html          # Single page chat interface
│   ├── style.css           # Dark theme, glassmorphism
│   └── app.js              # Chat logic, markdown rendering
│
├── config/                 # Configuration
│   ├── __init__.py
│   └── settings.py         # Central config from env vars
│
├── logs/                   # Audit logs
│   └── .gitkeep
│
├── nginx/                  # Reverse proxy
│   └── mcp-server.conf     # Nginx configuration
│
├── scripts/                # Automation
│   ├── setup_ec2.sh        # EC2 instance setup
│   ├── start_server.sh     # Start all servers
│   └── deploy_workloads.sh # Deploy sample workloads
│
├── .env.example            # Environment template
├── .gitignore
├── requirements.txt        # Python dependencies
└── README.md               # This file
```

---

## 🔧 Configuration

All configuration is managed through environment variables (`.env` file). See `.env.example` for all options.

### AI Provider Setup

**Groq (Recommended for speed):**
```bash
AI_PROVIDER=groq
GROQ_API_KEY=gsk_your_key_here
GROQ_MODEL=llama-3.3-70b-versatile
```

**Claude (Anthropic):**
```bash
AI_PROVIDER=claude
CLAUDE_API_KEY=sk-ant-your_key_here
CLAUDE_MODEL=claude-sonnet-4-20250514
```

**Gemini (Google, fallback):**
```bash
AI_PROVIDER=gemini
GEMINI_API_KEY=AIzaSy_your_key_here
GEMINI_MODEL=gemini-2.5-flash
```

---

## 🛠️ Development

### Running Locally (without EKS)

For local development, the K8s client falls back to your local `~/.kube/config`:

```bash
# Create and activate venv
python3 -m venv venv
source venv/bin/activate  # Linux/Mac
# or: venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt

# Copy env and set your API key
cp .env.example .env
nano .env

# Start MCP server (terminal 1)
python -m server.main

# Start backend (terminal 2)
uvicorn backend.app:app --host 0.0.0.0 --port 8000 --reload

# Open browser
open http://localhost:8000
```

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/chat` | Send message, get AI response or approval request |
| POST | `/api/approve` | Approve a pending write operation |
| POST | `/api/cancel` | Cancel a pending write operation |
| GET | `/api/health` | Health check + MCP connectivity |
| GET | `/api/cluster/quick-status` | Live cluster overview for UI header |

---

## 📋 Audit Logging

All write operations are permanently logged to `logs/audit.log` in JSON Lines format:

```json
{"timestamp":"2025-01-15T10:30:00+00:00","status":"APPROVED","operation":"scale_deployment","target":"nginx","namespace":"default"}
{"timestamp":"2025-01-15T10:30:01+00:00","status":"EXECUTED","operation":"scale_deployment","target":"nginx","namespace":"default","before_state":{"replicas":3},"after_state":{"replicas":5},"result":"success"}
```

Logged events:
- `APPROVED` — User clicked approve
- `DENIED` — User clicked cancel
- `EXECUTED` — Write operation completed
- `BLOCKED` — Guardrail prevented operation
- `ERROR` — Operation failed

---

## 🔐 Security

- **No hardcoded credentials** — All secrets via environment variables
- **IAM Instance Profile** — EKS auth via EC2 role, no kubeconfig
- **Token rotation** — EKS bearer tokens auto-refresh every 14 minutes
- **Approval gate** — All write operations require explicit human approval
- **Guardrails** — Hardcoded safety rules the AI cannot override
- **Audit trail** — Every action permanently logged
- **Namespace protection** — System namespaces are untouchable
- **Secret masking** — Environment variable values never exposed

---

## 🐛 Troubleshooting

### MCP Server won't start
```bash
# Check logs
python -m server.main
# Look for import errors or port conflicts
```

### Can't connect to EKS
```bash
# Verify IAM role
aws sts get-caller-identity

# Verify EKS access
aws eks describe-cluster --name <cluster-name> --region <region>

# Check kubectl
kubectl get nodes
```

### Frontend not loading
```bash
# Verify backend is running
curl http://localhost:8000/api/health

# Check nginx
sudo nginx -t
sudo systemctl status nginx
```

### Approval expired
The approval window is 300 seconds (5 minutes). If you see "Action expired", simply request the operation again.

---

## 📜 License

MIT License — see LICENSE file for details.
