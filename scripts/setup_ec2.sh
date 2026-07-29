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

# ──── 1. System Update ────
echo "▸ [1/10] Updating system packages..."
sudo apt update && sudo apt upgrade -y

# ──── 2. Install Python 3.11 ────
echo "▸ [2/10] Installing Python 3.11..."
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev python3-pip
sudo update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1

# ──── 3. Install Nginx ────
echo "▸ [3/10] Installing Nginx..."
sudo apt install -y nginx

# ──── 4. Install kubectl ────
echo "▸ [4/10] Installing kubectl..."
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.29/deb/Release.key | sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.29/deb/ /' | sudo tee /etc/apt/sources.list.d/kubernetes.list
sudo apt update
sudo apt install -y kubectl

# ──── 5. Install AWS CLI v2 ────
echo "▸ [5/10] Installing AWS CLI v2..."
if ! command -v aws &> /dev/null; then
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "/tmp/awscliv2.zip"
    cd /tmp && unzip -q awscliv2.zip
    sudo ./aws/install
    rm -rf /tmp/aws /tmp/awscliv2.zip
    cd -
fi
aws --version

# ──── 6. Create Project Directory ────
echo "▸ [6/10] Setting up project directory..."
PROJECT_DIR="$HOME/eks-mcp-agent"
if [ ! -d "$PROJECT_DIR" ]; then
    echo "  Please clone or copy the project to $PROJECT_DIR first."
    echo "  Example: git clone <repo-url> $PROJECT_DIR"
fi

# ──── 7. Create Python Virtual Environment ────
echo "▸ [7/10] Creating Python virtual environment..."
cd "$PROJECT_DIR"
python3 -m venv venv
source venv/bin/activate

# ──── 8. Install Python Dependencies ────
echo "▸ [8/10] Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# ──── 9. Generate Self-Signed SSL Certificate ────
echo "▸ [9/10] Generating self-signed SSL certificate..."
sudo mkdir -p /etc/nginx/ssl
sudo openssl req -x509 -nodes \
    -days 365 \
    -newkey rsa:2048 \
    -keyout /etc/nginx/ssl/kubeguardian.key \
    -out /etc/nginx/ssl/kubeguardian.crt \
    -subj "/CN=kubeguardian/O=KubeGuardian/C=US"

# ──── 10. Configure Nginx ────
echo "▸ [10/10] Configuring Nginx..."
sudo cp "$PROJECT_DIR/nginx/mcp-server.conf" /etc/nginx/sites-available/kubeguardian
sudo ln -sf /etc/nginx/sites-available/kubeguardian /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx
sudo systemctl enable nginx

# ──── Create logs directory ────
mkdir -p "$PROJECT_DIR/logs"

# ──── Copy .env.example → .env if not exists ────
if [ ! -f "$PROJECT_DIR/.env" ]; then
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    echo ""
    echo "⚠️  Created .env from .env.example — please edit it with your API keys!"
fi

echo ""
echo "═══════════════════════════════════════════"
echo "  ✅ Setup Complete!"
echo "═══════════════════════════════════════════"
echo ""
echo "  Next steps:"
echo "  1. Edit .env file with your API keys:"
echo "     nano $PROJECT_DIR/.env"
echo ""
echo "  2. Configure EKS access:"
echo "     aws eks update-kubeconfig --name <cluster-name> --region <region>"
echo ""
echo "  3. Deploy sample workloads (optional):"
echo "     bash $PROJECT_DIR/scripts/deploy_workloads.sh"
echo ""
echo "  4. Start the servers:"
echo "     bash $PROJECT_DIR/scripts/start_server.sh"
echo ""
echo "  5. Open browser at:"
echo "     https://$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo '<EC2_PUBLIC_IP>')"
echo ""
