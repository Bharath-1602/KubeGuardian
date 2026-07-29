#!/usr/bin/env bash
# ────────────────────────────────────────
# scripts/setup_ec2.sh
# KubeGuardian — EC2 Instance Setup Script
# Targets: Ubuntu 22.04 LTS
# ────────────────────────────────────────

set -euo pipefail

echo "═══════════════════════════════════════════"
echo "  KubeGuardian — EC2 Setup Script"
echo "═══════════════════════════════════════════"
echo ""

PROJECT_DIR="$HOME/eks-mcp-agent"

# ──── 0. Verify project directory exists ────
echo "▸ [0/10] Checking project directory..."
if [ ! -d "$PROJECT_DIR" ]; then
    echo ""
    echo "❌ Project directory not found: $PROJECT_DIR"
    echo ""
    echo "   Please clone the project first:"
    echo "   git clone <your-repo-url> $PROJECT_DIR"
    echo ""
    exit 1
fi
echo "  ✅ Project directory found: $PROJECT_DIR"

# ──── 1. System Update ────
echo ""
echo "▸ [1/10] Updating system packages..."
sudo apt-get update -y
sudo apt-get upgrade -y

# ──── 2. Install Python 3.11 ────
echo ""
echo "▸ [2/10] Installing Python 3.11..."
sudo apt-get install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt-get update -y
sudo apt-get install -y python3.11 python3.11-venv python3.11-dev python3-pip
# Make python3 point to 3.11
sudo update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1
python3 --version

# ──── 3. Install Nginx ────
echo ""
echo "▸ [3/10] Installing Nginx..."
sudo apt-get install -y nginx

# ──── 4. Install kubectl ────
echo ""
echo "▸ [4/10] Installing kubectl v1.29..."
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.29/deb/Release.key \
    | sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.29/deb/ /' \
    | sudo tee /etc/apt/sources.list.d/kubernetes.list
sudo apt-get update -y
sudo apt-get install -y kubectl
kubectl version --client

# ──── 5. Install AWS CLI v2 ────
echo ""
echo "▸ [5/10] Installing AWS CLI v2..."
if command -v aws &>/dev/null; then
    echo "  AWS CLI already installed: $(aws --version)"
else
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" \
        -o "/tmp/awscliv2.zip"
    cd /tmp
    unzip -q awscliv2.zip
    sudo ./aws/install
    rm -rf /tmp/aws /tmp/awscliv2.zip
    cd "$PROJECT_DIR"
    echo "  AWS CLI installed: $(aws --version)"
fi

# ──── 6. Create Python Virtual Environment ────
echo ""
echo "▸ [6/10] Creating Python virtual environment..."
cd "$PROJECT_DIR"
python3 -m venv venv
echo "  ✅ Virtual environment created at $PROJECT_DIR/venv"

# ──── 7. Install Python Dependencies ────
echo ""
echo "▸ [7/10] Installing Python dependencies..."
source venv/bin/activate
# Use python -m pip to guarantee we use the venv pip, not system pip
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
echo "  ✅ Python dependencies installed"

# ──── 8. Create required directories ────
echo ""
echo "▸ [8/10] Creating required directories..."
mkdir -p "$PROJECT_DIR/logs"
echo "  ✅ logs/ directory ready"

# ──── 9. Generate Self-Signed SSL Certificate ────
echo ""
echo "▸ [9/10] Generating self-signed SSL certificate..."
sudo mkdir -p /etc/nginx/ssl
sudo openssl req -x509 -nodes \
    -days 365 \
    -newkey rsa:2048 \
    -keyout /etc/nginx/ssl/kubeguardian.key \
    -out    /etc/nginx/ssl/kubeguardian.crt \
    -subj   "/CN=kubeguardian/O=KubeGuardian/C=US"
sudo chmod 600 /etc/nginx/ssl/kubeguardian.key
echo "  ✅ SSL certificate generated at /etc/nginx/ssl/"

# ──── 10. Configure and Enable Nginx ────
echo ""
echo "▸ [10/10] Configuring Nginx..."
sudo cp "$PROJECT_DIR/nginx/mcp-server.conf" \
    /etc/nginx/sites-available/kubeguardian
sudo ln -sf \
    /etc/nginx/sites-available/kubeguardian \
    /etc/nginx/sites-enabled/kubeguardian
# Remove default site to avoid port conflicts
sudo rm -f /etc/nginx/sites-enabled/default
# Test config before restarting
sudo nginx -t
sudo systemctl restart nginx
sudo systemctl enable nginx
echo "  ✅ Nginx configured and started"

# ──── Copy .env.example → .env if not exists ────
echo ""
if [ ! -f "$PROJECT_DIR/.env" ]; then
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    echo "⚠️  Created .env from .env.example"
    echo "   You MUST edit it with your API keys before starting:"
    echo "   nano $PROJECT_DIR/.env"
else
    echo "ℹ️  .env file already exists — skipping copy"
fi

# ──── Final summary ────
echo ""
echo "═══════════════════════════════════════════"
echo "  ✅ EC2 Setup Complete!"
echo "═══════════════════════════════════════════"
echo ""
echo "  NEXT STEPS (in order):"
echo ""
echo "  1. Edit .env with your API keys:"
echo "     nano $PROJECT_DIR/.env"
echo ""
echo "  2. Verify EKS access (IAM role must be attached to EC2):"
echo "     aws sts get-caller-identity"
echo "     aws eks describe-cluster --name <cluster-name> --region <region>"
echo ""
echo "  3. Deploy sample workloads (optional):"
echo "     bash $PROJECT_DIR/scripts/deploy_workloads.sh"
echo ""
echo "  4. Start the servers:"
echo "     bash $PROJECT_DIR/scripts/start_server.sh"
echo ""
EC2_IP=$(curl -s --connect-timeout 2 \
    http://169.254.169.254/latest/meta-data/public-ipv4 \
    2>/dev/null || echo "<EC2_PUBLIC_IP>")
echo "  5. Open browser at: https://${EC2_IP}"
echo "     (Accept the self-signed cert warning)"
echo ""