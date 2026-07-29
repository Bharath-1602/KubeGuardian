"""
server/k8s_client.py
────────────────────────────────────────
All Kubernetes API calls live here and ONLY here.
Uses kubernetes client-python.
Authenticates via EC2 IAM Instance Profile using boto3/botocore
to generate EKS auth token — no kubeconfig file needed.
Falls back to load_kube_config() for local development.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Optional

import boto3
import botocore.session
from botocore.signers import RequestSigner
from kubernetes import client, config as k8s_config
from kubernetes.client.rest import ApiException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.settings import AWS_REGION, EKS_CLUSTER_NAME

logger = logging.getLogger("kubeguardian.k8s")

# ──────────────────────────────────────────────
# STS Token Generator (replaces aws-iam-authenticator)
# ──────────────────────────────────────────────

STS_TOKEN_EXPIRES_IN = 60  # presigned URL validity in seconds
TOKEN_PREFIX = "k8s-aws-v1."
CLUSTER_NAME_HEADER = "x-k8s-aws-id"


def _get_bearer_token(cluster_name: str, region: str) -> str:
    """
    Generate an EKS-compatible bearer token.
    Uses the same mechanism as aws-iam-authenticator / aws eks get-token.
    """
    import urllib.parse
    from botocore.auth import SigV4QueryAuth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials
    import botocore.session as bcs

    # Get credentials from the EC2 instance profile
    botocore_session = bcs.Session()
    credentials = botocore_session.get_credentials()
    credentials = credentials.get_frozen_credentials()

    # Build the STS URL
    url = (
        "https://sts.amazonaws.com/"
        "?Action=GetCallerIdentity&Version=2011-06-15"
    )

    # Create request with cluster name header
    request = AWSRequest(
        method="GET",
        url=url,
        headers={"x-k8s-aws-id": cluster_name},
    )

    # Sign with SigV4 Query (presigned URL style)
    signer = SigV4QueryAuth(credentials, "sts", region, expires=STS_TOKEN_EXPIRES_IN)
    signer.add_auth(request)

    prepared = request.prepare()

    token = TOKEN_PREFIX + base64.urlsafe_b64encode(
        prepared.url.encode("utf-8")
    ).decode("utf-8").rstrip("=")

    return token


# ──────────────────────────────────────────────
# Kubernetes Client Class
# ──────────────────────────────────────────────

class K8sClient:
    """
    Encapsulates all Kubernetes API interactions.
    Authenticates via IAM Instance Profile (EKS) with automatic
    token refresh, falling back to local kubeconfig for dev.
    """

    # Token cache: refresh 1 minute before the 15-min expiry
    _TOKEN_REFRESH_INTERVAL = 14 * 60  # 14 minutes

    def __init__(self, cluster_name: str = EKS_CLUSTER_NAME,
                 region: str = AWS_REGION):
        """
        Initialize the Kubernetes client.

        Args:
            cluster_name: EKS cluster name.
            region:       AWS region.
        """
        self.cluster_name = cluster_name
        self.region = region
        self._token: str | None = None
        self._token_time: float = 0
        self._ca_file: str | None = None
        self._endpoint: str | None = None
        self._api_client: client.ApiClient | None = None
        self._using_eks = False

        self._init_client()

    # ────────────── Initialization ──────────────

    def _init_client(self) -> None:
        """Build the kubernetes client configuration."""
        try:
            self._init_eks_client()
            self._using_eks = True
            logger.info("Initialized K8s client via EKS IAM auth")
        except Exception as exc:
            logger.warning(
                "EKS IAM auth failed (%s), falling back to kubeconfig", exc
            )
            try:
                k8s_config.load_incluster_config()
                self._api_client = client.ApiClient()
                logger.info("Initialized K8s client via in-cluster config")
            except Exception:
                k8s_config.load_kube_config()
                self._api_client = client.ApiClient()
                logger.info("Initialized K8s client via local kubeconfig")

    def _init_eks_client(self) -> None:
        """
        Initialize using EKS: fetch cluster info via boto3,
        generate bearer token, and build the API client.
        """
        eks = boto3.client("eks", region_name=self.region)
        cluster_info = eks.describe_cluster(name=self.cluster_name)["cluster"]

        self._endpoint = cluster_info["endpoint"]
        ca_data = cluster_info["certificateAuthority"]["data"]

        # Write CA cert to a temp file
        ca_file = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
        ca_file.write(base64.b64decode(ca_data))
        ca_file.close()
        self._ca_file = ca_file.name

        self._refresh_token()
        self._build_api_client()

    def _refresh_token(self) -> None:
        """Generate a fresh bearer token and record the timestamp."""
        self._token = _get_bearer_token(self.cluster_name, self.region)
        self._token_time = time.time()

    def _ensure_token_fresh(self) -> None:
        """Refresh the token if it is close to expiry."""
        if self._using_eks and (
            time.time() - self._token_time > self._TOKEN_REFRESH_INTERVAL
        ):
            self._refresh_token()
            self._build_api_client()

    def _build_api_client(self) -> None:
        """Build a kubernetes ApiClient from current credentials."""
        configuration = client.Configuration()
        configuration.host = self._endpoint
        configuration.api_key["authorization"] = self._token
        configuration.api_key_prefix["authorization"] = "Bearer"
        configuration.ssl_ca_cert = self._ca_file
        self._api_client = client.ApiClient(configuration)

    # ────────────── API accessors ──────────────

    @property
    def core_v1(self) -> client.CoreV1Api:
        """CoreV1Api accessor with auto-refresh."""
        self._ensure_token_fresh()
        return client.CoreV1Api(self._api_client)

    @property
    def apps_v1(self) -> client.AppsV1Api:
        """AppsV1Api accessor with auto-refresh."""
        self._ensure_token_fresh()
        return client.AppsV1Api(self._api_client)

    @property
    def policy_v1(self) -> client.PolicyV1Api:
        """PolicyV1Api accessor with auto-refresh."""
        self._ensure_token_fresh()
        return client.PolicyV1Api(self._api_client)

    # ──────────────────────────────────────────────
    # Helper utilities
    # ──────────────────────────────────────────────

    @staticmethod
    def _age(creation_timestamp) -> str:
        """
        Calculate human-readable age from a creation timestamp.

        Args:
            creation_timestamp: datetime object from K8s API.

        Returns:
            Age string like '3d12h' or '45m'.
        """
        if creation_timestamp is None:
            return "Unknown"
        if creation_timestamp.tzinfo is None:
            creation_timestamp = creation_timestamp.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - creation_timestamp
        days = delta.days
        hours, remainder = divmod(delta.seconds, 3600)
        minutes = remainder // 60
        if days > 0:
            return f"{days}d{hours}h"
        if hours > 0:
            return f"{hours}h{minutes}m"
        return f"{minutes}m"

    @staticmethod
    def _parse_cpu(cpu_str: str) -> int:
        """
        Parse CPU string to millicores.

        Args:
            cpu_str: e.g. '500m', '2', '1.5'

        Returns:
            Integer millicores.
        """
        if not cpu_str:
            return 0
        if cpu_str.endswith("m"):
            return int(cpu_str[:-1])
        return int(float(cpu_str) * 1000)

    @staticmethod
    def _parse_memory(mem_str: str) -> int:
        """
        Parse memory string to MiB.

        Args:
            mem_str: e.g. '512Mi', '2Gi', '1048576Ki'

        Returns:
            Integer MiB.
        """
        if not mem_str:
            return 0
        mem_str = str(mem_str)
        multipliers = {
            "Ki": 1 / 1024,
            "Mi": 1,
            "Gi": 1024,
            "Ti": 1024 * 1024,
        }
        for suffix, mult in multipliers.items():
            if mem_str.endswith(suffix):
                return int(float(mem_str[: -len(suffix)]) * mult)
        # Plain bytes
        try:
            return int(int(mem_str) / (1024 * 1024))
        except ValueError:
            return 0

    # ──────────────────────────────────────────────
    # READ METHODS
    # ──────────────────────────────────────────────

    def get_cluster_info(self) -> dict:
        """
        Get overall EKS cluster health summary.

        Returns:
            Dict with total nodes, ready nodes, pod counts by status,
            total namespaces, kubernetes version, cluster name.
        """
        nodes = self.core_v1.list_node()
        total_nodes = len(nodes.items)
        ready_nodes = 0
        k8s_version = ""
        for node in nodes.items:
            if k8s_version == "" and node.status.node_info:
                k8s_version = node.status.node_info.kubelet_version
            for cond in (node.status.conditions or []):
                if cond.type == "Ready" and cond.status == "True":
                    ready_nodes += 1

        pods = self.core_v1.list_pod_for_all_namespaces()
        total_pods = len(pods.items)
        running = pending = failed = succeeded = 0
        for pod in pods.items:
            phase = pod.status.phase
            if phase == "Running":
                running += 1
            elif phase == "Pending":
                pending += 1
            elif phase == "Failed":
                failed += 1
            elif phase == "Succeeded":
                succeeded += 1

        namespaces = self.core_v1.list_namespace()

        return {
            "cluster_name": self.cluster_name,
            "kubernetes_version": k8s_version,
            "total_nodes": total_nodes,
            "ready_nodes": ready_nodes,
            "total_pods": total_pods,
            "running_pods": running,
            "pending_pods": pending,
            "failed_pods": failed,
            "succeeded_pods": succeeded,
            "total_namespaces": len(namespaces.items),
            "health": "Healthy" if (ready_nodes == total_nodes and failed == 0 and pending == 0) else "Warning",
        }

    def get_nodes(self) -> list[dict]:
        """
        Get all nodes with detailed status.

        Returns:
            List of dicts: name, status, roles, instance_type,
            availability_zone, capacity, allocatable, conditions,
            age, kubelet_version, internal_ip.
        """
        nodes = self.core_v1.list_node()
        result = []
        for node in nodes.items:
            labels = node.metadata.labels or {}
            # Determine roles
            roles = []
            for k, v in labels.items():
                if k.startswith("node-role.kubernetes.io/"):
                    roles.append(k.split("/")[-1])
            if not roles:
                roles = ["worker"]

            # Status
            status = "NotReady"
            conditions = []
            for cond in (node.status.conditions or []):
                conditions.append({
                    "type": cond.type,
                    "status": cond.status,
                    "message": cond.message,
                })
                if cond.type == "Ready" and cond.status == "True":
                    status = "Ready"

            capacity = node.status.capacity or {}
            allocatable = node.status.allocatable or {}

            # Internal IP
            internal_ip = ""
            for addr in (node.status.addresses or []):
                if addr.type == "InternalIP":
                    internal_ip = addr.address
                    break

            result.append({
                "name": node.metadata.name,
                "status": status,
                "roles": roles,
                "instance_type": labels.get(
                    "node.kubernetes.io/instance-type",
                    labels.get("beta.kubernetes.io/instance-type", "Unknown"),
                ),
                "availability_zone": labels.get(
                    "topology.kubernetes.io/zone",
                    labels.get("failure-domain.beta.kubernetes.io/zone", "Unknown"),
                ),
                "capacity": {
                    "cpu": capacity.get("cpu", "0"),
                    "memory": capacity.get("memory", "0"),
                    "pods": capacity.get("pods", "0"),
                },
                "allocatable": {
                    "cpu": allocatable.get("cpu", "0"),
                    "memory": allocatable.get("memory", "0"),
                    "pods": allocatable.get("pods", "0"),
                },
                "conditions": conditions,
                "age": self._age(node.metadata.creation_timestamp),
                "kubelet_version": node.status.node_info.kubelet_version
                if node.status.node_info else "Unknown",
                "internal_ip": internal_ip,
            })
        return result

    def get_pods(
        self,
        namespace: str = "all",
        label_selector: Optional[str] = None,
        field_selector: Optional[str] = None,
    ) -> list[dict]:
        """
        Get pods in a namespace or all namespaces.

        Args:
            namespace:      Target namespace or 'all'.
            label_selector: K8s label selector string.
            field_selector: K8s field selector string.

        Returns:
            List of pod dicts.
        """
        kwargs: dict[str, Any] = {}
        if label_selector:
            kwargs["label_selector"] = label_selector
        if field_selector:
            kwargs["field_selector"] = field_selector

        if namespace == "all":
            pods = self.core_v1.list_pod_for_all_namespaces(**kwargs)
        else:
            pods = self.core_v1.list_namespaced_pod(namespace, **kwargs)

        result = []
        for pod in pods.items:
            containers = []
            for cs in (pod.status.container_statuses or []):
                state = "Unknown"
                if cs.state:
                    if cs.state.running:
                        state = "Running"
                    elif cs.state.waiting:
                        state = f"Waiting ({cs.state.waiting.reason})"
                    elif cs.state.terminated:
                        state = f"Terminated ({cs.state.terminated.reason})"
                containers.append({
                    "name": cs.name,
                    "image": cs.image,
                    "ready": cs.ready,
                    "restart_count": cs.restart_count,
                    "state": state,
                })

            conditions = []
            for cond in (pod.status.conditions or []):
                conditions.append({
                    "type": cond.type,
                    "status": cond.status,
                })

            result.append({
                "name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "status": pod.status.phase,
                "phase": pod.status.phase,
                "node_name": pod.spec.node_name,
                "pod_ip": pod.status.pod_ip,
                "containers": containers,
                "conditions": conditions,
                "age": self._age(pod.metadata.creation_timestamp),
                "labels": pod.metadata.labels or {},
            })
        return result

    def get_pod_logs(
        self,
        pod_name: str,
        namespace: str,
        container: Optional[str] = None,
        tail_lines: int = 100,
        previous: bool = False,
    ) -> str:
        """
        Get logs from a specific pod.

        Args:
            pod_name:   Pod name.
            namespace:  Namespace.
            container:  Container name (optional for multi-container pods).
            tail_lines: Number of recent lines to retrieve.
            previous:   Get logs from a previous (crashed) container instance.

        Returns:
            Log string.
        """
        kwargs: dict[str, Any] = {
            "tail_lines": tail_lines,
            "previous": previous,
        }
        if container:
            kwargs["container"] = container

        return self.core_v1.read_namespaced_pod_log(pod_name, namespace, **kwargs)

    def describe_pod(self, pod_name: str, namespace: str) -> dict:
        """
        Get full pod details including events, conditions, volumes,
        init containers, resource requests/limits, and environment
        variable names (values hidden for security).

        Args:
            pod_name:  Pod name.
            namespace: Namespace.

        Returns:
            Detailed pod dict.
        """
        pod = self.core_v1.read_namespaced_pod(pod_name, namespace)

        # Containers with resource details
        containers = []
        for c in (pod.spec.containers or []):
            env_names = [e.name for e in (c.env or [])]
            resources = {}
            if c.resources:
                resources = {
                    "requests": {
                        "cpu": (c.resources.requests or {}).get("cpu", "Not set"),
                        "memory": (c.resources.requests or {}).get("memory", "Not set"),
                    } if c.resources.requests else {},
                    "limits": {
                        "cpu": (c.resources.limits or {}).get("cpu", "Not set"),
                        "memory": (c.resources.limits or {}).get("memory", "Not set"),
                    } if c.resources.limits else {},
                }
            containers.append({
                "name": c.name,
                "image": c.image,
                "ports": [
                    {"containerPort": p.container_port, "protocol": p.protocol}
                    for p in (c.ports or [])
                ],
                "env_variable_names": env_names,
                "resources": resources,
            })

        # Init containers
        init_containers = []
        for c in (pod.spec.init_containers or []):
            init_containers.append({
                "name": c.name,
                "image": c.image,
            })

        # Volumes
        volumes = []
        for v in (pod.spec.volumes or []):
            vol_type = "Unknown"
            if v.empty_dir is not None:
                vol_type = "emptyDir"
            elif v.host_path is not None:
                vol_type = f"hostPath ({v.host_path.path})"
            elif v.config_map is not None:
                vol_type = f"configMap ({v.config_map.name})"
            elif v.secret is not None:
                vol_type = f"secret ({v.secret.secret_name})"
            elif v.persistent_volume_claim is not None:
                vol_type = f"PVC ({v.persistent_volume_claim.claim_name})"
            volumes.append({"name": v.name, "type": vol_type})

        # Conditions
        conditions = []
        for cond in (pod.status.conditions or []):
            conditions.append({
                "type": cond.type,
                "status": cond.status,
                "reason": cond.reason,
                "message": cond.message,
            })

        # Events for this pod
        field_sel = f"involvedObject.name={pod_name},involvedObject.namespace={namespace}"
        events_list = self.core_v1.list_namespaced_event(
            namespace, field_selector=field_sel
        )
        events = []
        for ev in events_list.items:
            events.append({
                "type": ev.type,
                "reason": ev.reason,
                "message": ev.message,
                "count": ev.count,
                "last_timestamp": str(ev.last_timestamp) if ev.last_timestamp else None,
            })

        return {
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "status": pod.status.phase,
            "node_name": pod.spec.node_name,
            "pod_ip": pod.status.pod_ip,
            "service_account": pod.spec.service_account_name,
            "age": self._age(pod.metadata.creation_timestamp),
            "labels": pod.metadata.labels or {},
            "annotations": pod.metadata.annotations or {},
            "containers": containers,
            "init_containers": init_containers,
            "volumes": volumes,
            "conditions": conditions,
            "events": events,
        }

    def get_deployments(self, namespace: str = "all") -> list[dict]:
        """
        Get all deployments with replica counts and health status.

        Args:
            namespace: Target namespace or 'all'.

        Returns:
            List of deployment dicts.
        """
        if namespace == "all":
            deps = self.apps_v1.list_deployment_for_all_namespaces()
        else:
            deps = self.apps_v1.list_namespaced_deployment(namespace)

        result = []
        for dep in deps.items:
            images = []
            for c in (dep.spec.template.spec.containers or []):
                images.append(c.image)

            conditions = []
            for cond in (dep.status.conditions or []):
                conditions.append({
                    "type": cond.type,
                    "status": cond.status,
                    "message": cond.message,
                })

            strategy = "Unknown"
            if dep.spec.strategy:
                strategy = dep.spec.strategy.type

            result.append({
                "name": dep.metadata.name,
                "namespace": dep.metadata.namespace,
                "desired_replicas": dep.spec.replicas,
                "ready_replicas": dep.status.ready_replicas or 0,
                "available_replicas": dep.status.available_replicas or 0,
                "updated_replicas": dep.status.updated_replicas or 0,
                "strategy": strategy,
                "images": images,
                "age": self._age(dep.metadata.creation_timestamp),
                "conditions": conditions,
                "labels": dep.metadata.labels or {},
            })
        return result

    def get_services(self, namespace: str = "all") -> list[dict]:
        """
        Get all services including load balancers and their endpoints.

        Args:
            namespace: Target namespace or 'all'.

        Returns:
            List of service dicts.
        """
        if namespace == "all":
            svcs = self.core_v1.list_service_for_all_namespaces()
        else:
            svcs = self.core_v1.list_namespaced_service(namespace)

        result = []
        for svc in svcs.items:
            ports = []
            for p in (svc.spec.ports or []):
                ports.append({
                    "port": p.port,
                    "target_port": str(p.target_port),
                    "protocol": p.protocol,
                    "name": p.name,
                })

            external_ip = "None"
            if svc.status.load_balancer and svc.status.load_balancer.ingress:
                ing = svc.status.load_balancer.ingress[0]
                external_ip = ing.hostname or ing.ip or "Pending"

            result.append({
                "name": svc.metadata.name,
                "namespace": svc.metadata.namespace,
                "type": svc.spec.type,
                "cluster_ip": svc.spec.cluster_ip,
                "external_ip": external_ip,
                "ports": ports,
                "selector": svc.spec.selector or {},
                "age": self._age(svc.metadata.creation_timestamp),
            })
        return result

    def get_namespaces(self) -> list[dict]:
        """
        Get all namespaces with their status.

        Returns:
            List of namespace dicts.
        """
        nss = self.core_v1.list_namespace()
        result = []
        for ns in nss.items:
            result.append({
                "name": ns.metadata.name,
                "status": ns.status.phase,
                "age": self._age(ns.metadata.creation_timestamp),
                "labels": ns.metadata.labels or {},
            })
        return result

    def get_events(
        self,
        namespace: str = "all",
        event_type: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """
        Get recent cluster events sorted by timestamp descending.

        Args:
            namespace:  Target namespace or 'all'.
            event_type: Filter by event type (e.g. 'Warning').
            limit:      Maximum events to return.

        Returns:
            List of event dicts.
        """
        kwargs: dict[str, Any] = {}
        if event_type:
            kwargs["field_selector"] = f"type={event_type}"

        if namespace == "all":
            events = self.core_v1.list_event_for_all_namespaces(**kwargs)
        else:
            events = self.core_v1.list_namespaced_event(namespace, **kwargs)

        items = []
        for ev in events.items:
            items.append({
                "type": ev.type,
                "reason": ev.reason,
                "message": ev.message,
                "involved_object": {
                    "kind": ev.involved_object.kind,
                    "name": ev.involved_object.name,
                    "namespace": ev.involved_object.namespace,
                },
                "namespace": ev.metadata.namespace,
                "count": ev.count,
                "first_time": str(ev.first_timestamp) if ev.first_timestamp else None,
                "last_time": str(ev.last_timestamp) if ev.last_timestamp else None,
            })

        # Sort by last_time descending
        items.sort(key=lambda x: x["last_time"] or "", reverse=True)
        return items[:limit]

    def get_pdb(self, namespace: str = "all") -> list[dict]:
        """
        Get all Pod Disruption Budgets and their current status.

        Args:
            namespace: Target namespace or 'all'.

        Returns:
            List of PDB dicts.
        """
        if namespace == "all":
            pdbs = self.policy_v1.list_pod_disruption_budget_for_all_namespaces()
        else:
            pdbs = self.policy_v1.list_namespaced_pod_disruption_budget(namespace)

        result = []
        for pdb in pdbs.items:
            result.append({
                "name": pdb.metadata.name,
                "namespace": pdb.metadata.namespace,
                "min_available": str(pdb.spec.min_available)
                if pdb.spec.min_available is not None else "N/A",
                "max_unavailable": str(pdb.spec.max_unavailable)
                if pdb.spec.max_unavailable is not None else "N/A",
                "current_healthy": pdb.status.current_healthy,
                "desired_healthy": pdb.status.desired_healthy,
                "disruptions_allowed": pdb.status.disruptions_allowed,
                "selector": pdb.spec.selector.match_labels
                if pdb.spec.selector else {},
            })
        return result

    def get_node_details(self, node_name: str) -> dict:
        """
        Get deep details about a specific node.

        Args:
            node_name: Node name.

        Returns:
            Detailed node dict with conditions, taints, labels,
            annotations, allocated resources, events.
        """
        node = self.core_v1.read_node(node_name)

        conditions = []
        for cond in (node.status.conditions or []):
            conditions.append({
                "type": cond.type,
                "status": cond.status,
                "reason": cond.reason,
                "message": cond.message,
                "last_transition_time": str(cond.last_transition_time)
                if cond.last_transition_time else None,
            })

        taints = []
        for t in (node.spec.taints or []):
            taints.append({
                "key": t.key,
                "value": t.value,
                "effect": t.effect,
            })

        capacity = node.status.capacity or {}
        allocatable = node.status.allocatable or {}

        # Events for this node
        events = []
        try:
            field_sel = f"involvedObject.name={node_name}"
            evts = self.core_v1.list_event_for_all_namespaces(
                field_selector=field_sel
            )
            for ev in evts.items:
                events.append({
                    "type": ev.type,
                    "reason": ev.reason,
                    "message": ev.message,
                    "last_time": str(ev.last_timestamp) if ev.last_timestamp else None,
                })
        except Exception:
            pass

        return {
            "name": node.metadata.name,
            "age": self._age(node.metadata.creation_timestamp),
            "conditions": conditions,
            "taints": taints,
            "labels": node.metadata.labels or {},
            "annotations": {
                k: v for k, v in (node.metadata.annotations or {}).items()
                if not k.startswith("kubectl.kubernetes.io/")
            },
            "capacity": {
                "cpu": capacity.get("cpu", "0"),
                "memory": capacity.get("memory", "0"),
                "pods": capacity.get("pods", "0"),
            },
            "allocatable": {
                "cpu": allocatable.get("cpu", "0"),
                "memory": allocatable.get("memory", "0"),
                "pods": allocatable.get("pods", "0"),
            },
            "unschedulable": node.spec.unschedulable or False,
            "events": events,
        }

    def check_scheduling_capacity(
        self,
        additional_cpu_millicores: int,
        additional_memory_mi: int,
        namespace: Optional[str] = None,
    ) -> dict:
        """
        Check if the cluster can schedule additional workloads.

        Args:
            additional_cpu_millicores: CPU millicores needed.
            additional_memory_mi:     Memory MiB needed.
            namespace:                Optional namespace filter.

        Returns:
            Dict with can_schedule, available_nodes, free resources,
            and recommendation.
        """
        nodes = self.core_v1.list_node()
        pods = self.core_v1.list_pod_for_all_namespaces()

        # Sum allocatable per node
        node_resources: dict[str, dict] = {}
        for node in nodes.items:
            # Skip unschedulable nodes
            if node.spec.unschedulable:
                continue
            # Skip not-ready nodes
            ready = False
            for cond in (node.status.conditions or []):
                if cond.type == "Ready" and cond.status == "True":
                    ready = True
            if not ready:
                continue

            alloc = node.status.allocatable or {}
            node_resources[node.metadata.name] = {
                "cpu_alloc_m": self._parse_cpu(alloc.get("cpu", "0")),
                "mem_alloc_mi": self._parse_memory(alloc.get("memory", "0")),
                "cpu_used_m": 0,
                "mem_used_mi": 0,
            }

        # Sum requests per node
        for pod in pods.items:
            if pod.status.phase not in ("Running", "Pending"):
                continue
            nn = pod.spec.node_name
            if nn and nn in node_resources:
                for c in (pod.spec.containers or []):
                    req = (c.resources.requests or {}) if c.resources else {}
                    node_resources[nn]["cpu_used_m"] += self._parse_cpu(
                        req.get("cpu", "0")
                    )
                    node_resources[nn]["mem_used_mi"] += self._parse_memory(
                        req.get("memory", "0")
                    )

        total_free_cpu = 0
        total_free_mem = 0
        available_nodes = []
        for name, res in node_resources.items():
            free_cpu = res["cpu_alloc_m"] - res["cpu_used_m"]
            free_mem = res["mem_alloc_mi"] - res["mem_used_mi"]
            total_free_cpu += max(0, free_cpu)
            total_free_mem += max(0, free_mem)
            if free_cpu >= additional_cpu_millicores and free_mem >= additional_memory_mi:
                available_nodes.append(name)

        can_schedule = len(available_nodes) > 0

        recommendation = (
            f"Cluster can schedule the workload on {len(available_nodes)} node(s)."
            if can_schedule
            else "Cluster does NOT have sufficient capacity for this workload."
        )

        return {
            "can_schedule": can_schedule,
            "available_nodes": available_nodes,
            "total_free_cpu_millicores": total_free_cpu,
            "total_free_memory_mi": total_free_mem,
            "recommendation": recommendation,
        }

    def get_pods_on_node(self, node_name: str) -> list[dict]:
        """
        Get all pods running on a specific node.

        Args:
            node_name: Node name.

        Returns:
            List of pod dicts with namespace, controller, resources.
        """
        pods = self.core_v1.list_pod_for_all_namespaces(
            field_selector=f"spec.nodeName={node_name}"
        )
        result = []
        for pod in pods.items:
            controller_type = "Standalone"
            controller_name = ""
            for ref in (pod.metadata.owner_references or []):
                if ref.controller:
                    controller_type = ref.kind
                    controller_name = ref.name
                    break

            cpu_req = 0
            mem_req = 0
            for c in (pod.spec.containers or []):
                req = (c.resources.requests or {}) if c.resources else {}
                cpu_req += self._parse_cpu(req.get("cpu", "0"))
                mem_req += self._parse_memory(req.get("memory", "0"))

            result.append({
                "name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "status": pod.status.phase,
                "controller_type": controller_type,
                "controller_name": controller_name,
                "cpu_request_m": cpu_req,
                "memory_request_mi": mem_req,
            })
        return result

    def check_pending_failed_pods(self, namespace: str = "all") -> list[dict]:
        """
        Find all pods that are Pending or Failed across the cluster.

        Args:
            namespace: Target namespace or 'all'.

        Returns:
            List of problematic pod dicts with reason.
        """
        if namespace == "all":
            pods = self.core_v1.list_pod_for_all_namespaces()
        else:
            pods = self.core_v1.list_namespaced_pod(namespace)

        result = []
        for pod in pods.items:
            if pod.status.phase in ("Pending", "Failed"):
                reason = ""
                for cond in (pod.status.conditions or []):
                    if cond.status != "True" and cond.message:
                        reason = cond.message
                        break
                # Check container statuses for waiting reason
                if not reason:
                    for cs in (pod.status.container_statuses or []):
                        if cs.state and cs.state.waiting:
                            reason = cs.state.waiting.reason or ""
                            break

                result.append({
                    "name": pod.metadata.name,
                    "namespace": pod.metadata.namespace,
                    "phase": pod.status.phase,
                    "reason": reason,
                    "age": self._age(pod.metadata.creation_timestamp),
                    "node_name": pod.spec.node_name,
                })
        return result

    def get_daemonsets_on_node(self, node_name: str) -> list[dict]:
        """
        Get daemonsets running on a node (these are safely ignored during drain).

        Args:
            node_name: Node name.

        Returns:
            List of daemonset pod dicts.
        """
        pods = self.get_pods_on_node(node_name)
        return [p for p in pods if p["controller_type"] == "DaemonSet"]

    def check_standalone_pods_on_node(self, node_name: str) -> list[dict]:
        """
        Find pods with no ownerReference controller on a node.
        These pods will be LOST during drain — highlight as high risk.

        Args:
            node_name: Node name.

        Returns:
            List of standalone pod dicts.
        """
        pods = self.get_pods_on_node(node_name)
        return [p for p in pods if p["controller_type"] == "Standalone"]

    def check_local_storage_pods_on_node(self, node_name: str) -> list[dict]:
        """
        Find pods using emptyDir or hostPath volumes on a node.
        These need --delete-emptydir-data flag for drain.

        Args:
            node_name: Node name.

        Returns:
            List of pod dicts using local storage.
        """
        pods = self.core_v1.list_pod_for_all_namespaces(
            field_selector=f"spec.nodeName={node_name}"
        )
        result = []
        for pod in pods.items:
            has_local = False
            for v in (pod.spec.volumes or []):
                if v.empty_dir is not None or v.host_path is not None:
                    has_local = True
                    break
            if has_local:
                result.append({
                    "name": pod.metadata.name,
                    "namespace": pod.metadata.namespace,
                    "status": pod.status.phase,
                })
        return result

    # ──────────────────────────────────────────────
    # WRITE METHODS
    # ──────────────────────────────────────────────

    def scale_deployment(
        self, deployment_name: str, namespace: str, replicas: int
    ) -> dict:
        """
        Scale a deployment to specified replica count.

        Args:
            deployment_name: Deployment name.
            namespace:       Namespace.
            replicas:        Target replica count.

        Returns:
            Dict confirming new state.
        """
        body = {"spec": {"replicas": replicas}}
        self.apps_v1.patch_namespaced_deployment_scale(
            deployment_name, namespace, body
        )
        # Read back to confirm
        dep = self.apps_v1.read_namespaced_deployment(deployment_name, namespace)
        return {
            "deployment": deployment_name,
            "namespace": namespace,
            "desired_replicas": dep.spec.replicas,
            "ready_replicas": dep.status.ready_replicas or 0,
            "status": "Scaling in progress",
        }

    def restart_deployment(self, deployment_name: str, namespace: str) -> dict:
        """
        Perform a rolling restart by patching the restart annotation.

        Args:
            deployment_name: Deployment name.
            namespace:       Namespace.

        Returns:
            Dict confirming restart initiated.
        """
        now = datetime.now(timezone.utc).isoformat()
        body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": now,
                        }
                    }
                }
            }
        }
        self.apps_v1.patch_namespaced_deployment(
            deployment_name, namespace, body
        )
        return {
            "deployment": deployment_name,
            "namespace": namespace,
            "status": "Rolling restart initiated",
            "restarted_at": now,
        }

    def patch_labels_annotations(
        self,
        resource_type: str,
        resource_name: str,
        namespace: str,
        labels: Optional[dict] = None,
        annotations: Optional[dict] = None,
    ) -> dict:
        """
        Patch labels and/or annotations on a resource.

        Args:
            resource_type:  Kind (e.g. 'deployment', 'pod', 'service').
            resource_name:  Resource name.
            namespace:      Namespace.
            labels:         Labels to set/update.
            annotations:    Annotations to set/update.

        Returns:
            Dict with updated metadata.
        """
        body: dict[str, Any] = {"metadata": {}}
        if labels:
            body["metadata"]["labels"] = labels
        if annotations:
            body["metadata"]["annotations"] = annotations

        rt = resource_type.lower()
        if rt in ("deployment", "deployments"):
            self.apps_v1.patch_namespaced_deployment(
                resource_name, namespace, body
            )
            res = self.apps_v1.read_namespaced_deployment(resource_name, namespace)
        elif rt in ("service", "services"):
            self.core_v1.patch_namespaced_service(
                resource_name, namespace, body
            )
            res = self.core_v1.read_namespaced_service(resource_name, namespace)
        elif rt in ("pod", "pods"):
            self.core_v1.patch_namespaced_pod(
                resource_name, namespace, body
            )
            res = self.core_v1.read_namespaced_pod(resource_name, namespace)
        else:
            raise ValueError(f"Unsupported resource type for patching: {resource_type}")

        return {
            "resource_type": resource_type,
            "resource_name": resource_name,
            "namespace": namespace,
            "labels": res.metadata.labels or {},
            "annotations": {
                k: v for k, v in (res.metadata.annotations or {}).items()
                if not k.startswith("kubectl.kubernetes.io/")
            },
            "status": "Patched successfully",
        }

    def cordon_node(self, node_name: str) -> dict:
        """
        Mark a node as unschedulable (cordon).

        Args:
            node_name: Node name.

        Returns:
            Dict confirming cordon.
        """
        body = {"spec": {"unschedulable": True}}
        self.core_v1.patch_node(node_name, body)
        return {
            "node": node_name,
            "unschedulable": True,
            "status": "Node cordoned successfully — no new pods will be scheduled",
        }

    # ──────────────────────────────────────────────
    # ADVISORY METHODS (never execute drain)
    # ──────────────────────────────────────────────

    def assess_maintenance_readiness(self, node_name: str) -> dict:
        """
        PRIMARY MAINTENANCE USE CASE.
        Perform a complete safety assessment before draining a node.
        Runs ALL checks and generates exact drain commands.

        THIS IS READ-ONLY — it does NOT execute any drain commands.

        Args:
            node_name: Node to assess.

        Returns:
            Structured assessment dict.
        """
        risks: list[str] = []
        prerequisites: list[str] = []

        # 1. Check node exists and health
        try:
            node_detail = self.get_node_details(node_name)
        except ApiException as e:
            return {
                "node_name": node_name,
                "overall_safety": "NOT_SAFE",
                "summary": f"Node '{node_name}' not found: {e.reason}",
                "error": str(e),
            }

        node_status = {
            "unschedulable": node_detail["unschedulable"],
            "conditions": node_detail["conditions"],
        }

        # 2. Count other available worker nodes
        all_nodes = self.get_nodes()
        other_ready = [
            n for n in all_nodes
            if n["name"] != node_name and n["status"] == "Ready"
        ]
        if len(other_ready) < 1:
            risks.append("CRITICAL: No other ready nodes available — cluster has single point of failure!")
            prerequisites.append("Add at least one more healthy node before proceeding.")

        # 3. Check scheduling capacity on remaining nodes
        # Estimate capacity needed: sum of non-daemonset pods on target node
        pods_on_node = self.get_pods_on_node(node_name)
        total_cpu_needed = sum(
            p["cpu_request_m"] for p in pods_on_node
            if p["controller_type"] != "DaemonSet"
        )
        total_mem_needed = sum(
            p["memory_request_mi"] for p in pods_on_node
            if p["controller_type"] != "DaemonSet"
        )

        capacity_check = self.check_scheduling_capacity(
            total_cpu_needed, total_mem_needed
        )
        if not capacity_check["can_schedule"]:
            risks.append(
                f"CRITICAL: Remaining nodes cannot absorb workloads "
                f"(need {total_cpu_needed}m CPU, {total_mem_needed}Mi memory)."
            )
            prerequisites.append("Scale up cluster or reduce workloads first.")

        # 4-5. Classify pods on node
        daemonsets = self.get_daemonsets_on_node(node_name)
        standalone = self.check_standalone_pods_on_node(node_name)
        local_storage = self.check_local_storage_pods_on_node(node_name)

        # 5. Check PDB for each pod's owner
        pdbs = self.get_pdb()
        pdb_constrained = []
        safely_reschedulable = []

        for pod in pods_on_node:
            if pod["controller_type"] == "DaemonSet":
                continue
            if pod["controller_type"] == "Standalone":
                continue

            # Check if any PDB constrains this pod
            is_pdb_constrained = False
            for pdb in pdbs:
                if pdb["namespace"] == pod["namespace"]:
                    if pdb.get("disruptions_allowed", 1) == 0:
                        pdb_constrained.append({
                            **pod,
                            "pdb": pdb["name"],
                            "disruptions_allowed": 0,
                        })
                        is_pdb_constrained = True
                        risks.append(
                            f"PDB '{pdb['name']}' in '{pdb['namespace']}' allows 0 disruptions — "
                            f"pod '{pod['name']}' cannot be evicted."
                        )
                        prerequisites.append(
                            f"Scale up '{pod['controller_name']}' or adjust PDB '{pdb['name']}' first."
                        )
                        break

            if not is_pdb_constrained:
                safely_reschedulable.append(pod)

        # 6. Single-replica deployments
        single_replica_risk = []
        deployments = self.get_deployments()
        dep_map = {
            (d["name"], d["namespace"]): d["desired_replicas"]
            for d in deployments
        }
        for pod in pods_on_node:
            if pod["controller_type"] == "ReplicaSet":
                # ReplicaSet name is usually <deployment>-<hash>
                dep_name = "-".join(pod["controller_name"].split("-")[:-1])
                replicas = dep_map.get((dep_name, pod["namespace"]))
                if replicas is not None and replicas <= 1:
                    single_replica_risk.append({
                        **pod,
                        "deployment": dep_name,
                        "replicas": replicas,
                    })
                    risks.append(
                        f"Deployment '{dep_name}' in '{pod['namespace']}' has only "
                        f"{replicas} replica — will cause DOWNTIME during drain."
                    )
                    prerequisites.append(
                        f"Scale '{dep_name}' to at least 2 replicas before draining."
                    )

        # 7. Standalone pods (will be lost)
        if standalone:
            for p in standalone:
                risks.append(
                    f"Standalone pod '{p['name']}' in '{p['namespace']}' has no controller — "
                    "will be PERMANENTLY LOST during drain."
                )

        # 8. Local storage pods
        needs_delete_local = len(local_storage) > 0
        if needs_delete_local:
            risks.append(
                f"{len(local_storage)} pod(s) use local storage (emptyDir/hostPath) — "
                "data will be lost; need --delete-emptydir-data flag."
            )

        # 9. Existing cluster issues
        existing_issues = self.check_pending_failed_pods()

        # 10. Determine overall safety
        critical_risks = [r for r in risks if "CRITICAL" in r]
        if critical_risks:
            overall_safety = "NOT_SAFE"
            summary = (
                f"Node '{node_name}' is NOT safe to drain. "
                f"{len(critical_risks)} critical risk(s) found. "
                "Address prerequisites before proceeding."
            )
        elif risks:
            overall_safety = "CONDITIONAL"
            summary = (
                f"Node '{node_name}' can be drained CONDITIONALLY. "
                f"{len(risks)} risk(s) found that should be reviewed. "
                "Address prerequisites for a safe drain."
            )
        else:
            overall_safety = "SAFE"
            summary = (
                f"Node '{node_name}' is SAFE to drain. "
                "All checks passed with no significant risks."
            )

        # Build recommended drain command
        drain_flags = [
            f"kubectl drain {node_name}",
            "--ignore-daemonsets",
            "--grace-period=60",
        ]
        if needs_delete_local:
            drain_flags.append("--delete-emptydir-data")
        if standalone:
            drain_flags.append("--force")
        recommended_drain_command = " \\\n  ".join(drain_flags)

        # Step-by-step commands
        steps = []
        if prerequisites:
            steps.append("# Step 0: Address prerequisites listed above")
        steps.append(f"kubectl cordon {node_name}")
        steps.append(f"kubectl get pods --field-selector spec.nodeName={node_name} -A")
        steps.append(recommended_drain_command)
        steps.append(f"kubectl get nodes {node_name}")

        # Full English assessment
        details_parts = [
            f"## Maintenance Assessment for Node: {node_name}",
            f"\n**Overall Safety: {overall_safety}**",
            f"\n{summary}",
            f"\n### Node Status",
            f"- Unschedulable: {node_detail['unschedulable']}",
            f"- Other ready nodes: {len(other_ready)}",
            f"\n### Capacity Check",
            f"- Pods need: {total_cpu_needed}m CPU, {total_mem_needed}Mi memory",
            f"- Can schedule: {capacity_check['can_schedule']}",
            f"- Available nodes: {', '.join(capacity_check['available_nodes']) or 'None'}",
            f"\n### Pods on Node ({len(pods_on_node)} total)",
            f"- DaemonSets (safely ignored): {len(daemonsets)}",
            f"- Safely reschedulable: {len(safely_reschedulable)}",
            f"- PDB constrained: {len(pdb_constrained)}",
            f"- Single-replica risk: {len(single_replica_risk)}",
            f"- Standalone (will be LOST): {len(standalone)}",
            f"- Local storage: {len(local_storage)}",
        ]
        if risks:
            details_parts.append("\n### Risks")
            for i, r in enumerate(risks, 1):
                details_parts.append(f"{i}. {r}")
        if prerequisites:
            details_parts.append("\n### Prerequisites")
            for i, p in enumerate(prerequisites, 1):
                details_parts.append(f"{i}. {p}")
        details_parts.append("\n### Recommended Drain Command")
        details_parts.append(f"```\n{recommended_drain_command}\n```")

        return {
            "node_name": node_name,
            "overall_safety": overall_safety,
            "summary": summary,
            "node_status": node_status,
            "other_nodes_available": len(other_ready),
            "capacity_check": capacity_check,
            "pods_on_node": {
                "total": len(pods_on_node),
                "daemonsets": daemonsets,
                "safely_reschedulable": safely_reschedulable,
                "pdb_constrained": pdb_constrained,
                "single_replica_risk": single_replica_risk,
                "standalone_will_be_lost": standalone,
                "local_storage_pods": local_storage,
            },
            "existing_cluster_issues": existing_issues,
            "risks": risks,
            "prerequisites": prerequisites,
            "recommended_drain_command": recommended_drain_command,
            "step_by_step_commands": steps,
            "assessment_details": "\n".join(details_parts),
        }

    def drain_node(self, node_name: str) -> dict:
        """
        ADVISORY ONLY — does NOT execute drain.
        Calls assess_maintenance_readiness() and returns the
        full assessment with exact commands.
        The engineer must run commands manually.

        Args:
            node_name: Node to assess for draining.

        Returns:
            Full assessment dict.
        """
        assessment = self.assess_maintenance_readiness(node_name)
        assessment["_advisory_notice"] = (
            "⚠️ THIS IS ADVISORY ONLY. The drain command has NOT been executed. "
            "Review the assessment above and run the commands manually if you "
            "are satisfied the risks are acceptable."
        )
        return assessment
