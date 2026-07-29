#!/usr/bin/env bash
# ────────────────────────────────────────
# scripts/deploy_workloads.sh
# KubeGuardian — Deploy Sample Workloads to EKS
# Creates test environments with varying configurations
# to demonstrate all KubeGuardian features.
# ────────────────────────────────────────

set -euo pipefail

echo "═══════════════════════════════════════════"
echo "  KubeGuardian — Deploy Sample Workloads"
echo "═══════════════════════════════════════════"
echo ""

# ──── Create Namespaces ────
echo "▸ Creating namespaces..."
kubectl create namespace production --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace staging --dry-run=client -o yaml | kubectl apply -f -
echo "  ✅ Namespaces: production, staging"

# ══════════════════════════════════════
# DEFAULT NAMESPACE
# ══════════════════════════════════════

echo ""
echo "▸ Deploying to default namespace..."

# nginx deployment (3 replicas)
cat <<EOF | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx
  namespace: default
  labels:
    app: nginx
    env: default
spec:
  replicas: 3
  selector:
    matchLabels:
      app: nginx
  template:
    metadata:
      labels:
        app: nginx
        env: default
    spec:
      containers:
      - name: nginx
        image: nginx:1.24.0
        ports:
        - containerPort: 80
        resources:
          requests:
            cpu: 50m
            memory: 64Mi
          limits:
            cpu: 100m
            memory: 128Mi
EOF

# redis deployment (1 replica — intentionally single to test warnings)
cat <<EOF | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: redis
  namespace: default
  labels:
    app: redis
    env: default
spec:
  replicas: 1
  selector:
    matchLabels:
      app: redis
  template:
    metadata:
      labels:
        app: redis
        env: default
    spec:
      containers:
      - name: redis
        image: redis:7-alpine
        ports:
        - containerPort: 6379
        resources:
          requests:
            cpu: 50m
            memory: 64Mi
          limits:
            cpu: 100m
            memory: 128Mi
EOF

# nginx service (ClusterIP)
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Service
metadata:
  name: nginx
  namespace: default
spec:
  type: ClusterIP
  selector:
    app: nginx
  ports:
  - port: 80
    targetPort: 80
    protocol: TCP
EOF

echo "  ✅ default: nginx (3 replicas), redis (1 replica), nginx service"

# ══════════════════════════════════════
# PRODUCTION NAMESPACE
# ══════════════════════════════════════

echo ""
echo "▸ Deploying to production namespace..."

# api-server deployment (2 replicas)
cat <<EOF | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api-server
  namespace: production
  labels:
    app: api-server
    env: production
    tier: backend
spec:
  replicas: 2
  selector:
    matchLabels:
      app: api-server
  template:
    metadata:
      labels:
        app: api-server
        env: production
        tier: backend
    spec:
      containers:
      - name: api-server
        image: nginx:1.25.0
        ports:
        - containerPort: 80
        resources:
          requests:
            cpu: 100m
            memory: 128Mi
          limits:
            cpu: 200m
            memory: 256Mi
EOF

# frontend deployment (2 replicas)
cat <<EOF | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: frontend
  namespace: production
  labels:
    app: frontend
    env: production
    tier: frontend
spec:
  replicas: 2
  selector:
    matchLabels:
      app: frontend
  template:
    metadata:
      labels:
        app: frontend
        env: production
        tier: frontend
    spec:
      containers:
      - name: frontend
        image: nginx:alpine
        ports:
        - containerPort: 80
        resources:
          requests:
            cpu: 50m
            memory: 64Mi
          limits:
            cpu: 100m
            memory: 128Mi
EOF

# api-server service (ClusterIP)
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Service
metadata:
  name: api-server
  namespace: production
spec:
  type: ClusterIP
  selector:
    app: api-server
  ports:
  - port: 80
    targetPort: 80
    protocol: TCP
EOF

# frontend service (LoadBalancer)
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Service
metadata:
  name: frontend
  namespace: production
spec:
  type: LoadBalancer
  selector:
    app: frontend
  ports:
  - port: 80
    targetPort: 80
    protocol: TCP
EOF

# PDB for api-server (minAvailable: 1)
cat <<EOF | kubectl apply -f -
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: api-server-pdb
  namespace: production
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: api-server
EOF

# PDB for frontend (minAvailable: 1)
cat <<EOF | kubectl apply -f -
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: frontend-pdb
  namespace: production
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: frontend
EOF

echo "  ✅ production: api-server (2r), frontend (2r), services, PDBs"

# ══════════════════════════════════════
# STAGING NAMESPACE
# ══════════════════════════════════════

echo ""
echo "▸ Deploying to staging namespace..."

# test-app deployment (1 replica)
cat <<EOF | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: test-app
  namespace: staging
  labels:
    app: test-app
    env: staging
spec:
  replicas: 1
  selector:
    matchLabels:
      app: test-app
  template:
    metadata:
      labels:
        app: test-app
        env: staging
    spec:
      containers:
      - name: test-app
        image: nginx:alpine
        ports:
        - containerPort: 80
        resources:
          requests:
            cpu: 25m
            memory: 32Mi
          limits:
            cpu: 50m
            memory: 64Mi
EOF

# test-app service (ClusterIP)
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Service
metadata:
  name: test-app
  namespace: staging
spec:
  type: ClusterIP
  selector:
    app: test-app
  ports:
  - port: 80
    targetPort: 80
    protocol: TCP
EOF

echo "  ✅ staging: test-app (1 replica), test-app service"

# ══════════════════════════════════════
# STANDALONE POD (no deployment controller)
# ══════════════════════════════════════

echo ""
echo "▸ Creating standalone pod (for drain detection testing)..."

cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: standalone-debug-pod
  namespace: default
  labels:
    app: debug
    purpose: drain-test
spec:
  containers:
  - name: debug
    image: busybox:1.36
    command: ["sleep", "3600"]
    resources:
      requests:
        cpu: 10m
        memory: 16Mi
      limits:
        cpu: 50m
        memory: 32Mi
EOF

echo "  ✅ default: standalone-debug-pod (no controller — tests drain warnings)"

# ══════════════════════════════════════
# Summary
# ══════════════════════════════════════

echo ""
echo "═══════════════════════════════════════════"
echo "  ✅ All Sample Workloads Deployed!"
echo "═══════════════════════════════════════════"
echo ""
echo "  Namespace:   default"
echo "    - nginx (3 replicas)"
echo "    - redis (1 replica — single replica warning)"
echo "    - standalone-debug-pod (no controller — drain risk)"
echo ""
echo "  Namespace:   production"
echo "    - api-server (2 replicas + PDB)"
echo "    - frontend (2 replicas + PDB + LoadBalancer)"
echo ""
echo "  Namespace:   staging"
echo "    - test-app (1 replica)"
echo ""
echo "  Verify with: kubectl get pods -A"
echo ""
