"""
server/main.py
────────────────────────────────────────
The MCP Server entry point.
Initializes FastMCP with streamable HTTP transport.
Registers all Kubernetes management tools.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.settings import MCP_SERVER_HOST, MCP_SERVER_PORT
from server.k8s_client import K8sClient
from server.guardrails import (
    validate_scale_request,
    validate_restart_request,
    validate_namespace,
    validate_patch_fields,
    validate_node_operation,
    is_write_operation,
    is_destructive_operation,
)
from server.audit_log import (
    log_write_operation,
    log_guardrail_block,
    log_error,
)

logger = logging.getLogger("kubeguardian.mcp")

# ──────────────────────────────────────────────
# Initialize MCP Server & K8s Client
# ──────────────────────────────────────────────

mcp = FastMCP("eks-guardian")

# K8s client is initialized lazily on first tool call
_k8s: K8sClient | None = None


def _get_k8s() -> K8sClient:
    """Get or initialize the Kubernetes client (lazy singleton)."""
    global _k8s
    if _k8s is None:
        _k8s = K8sClient()
    return _k8s


def _json(data) -> str:
    """Convert data to formatted JSON string."""
    return json.dumps(data, indent=2, default=str)


# ══════════════════════════════════════════════
# READ-ONLY TOOLS (no confirmation needed)
# ══════════════════════════════════════════════


@mcp.tool()
def get_cluster_overview() -> str:
    """
    Get overall EKS cluster health summary including node count,
    pod counts by status, and cluster version.
    Use this when user asks about cluster health, cluster status,
    or wants a general overview.
    """
    try:
        return _json(_get_k8s().get_cluster_info())
    except Exception as e:
        logger.error("get_cluster_overview failed: %s", e)
        return _json({"error": str(e), "tool": "get_cluster_overview"})


@mcp.tool()
def get_nodes_status() -> str:
    """
    Get all nodes in the cluster with their health status,
    instance type, capacity, and conditions. Use this when
    user asks about nodes, worker nodes, or node health.
    """
    try:
        return _json(_get_k8s().get_nodes())
    except Exception as e:
        logger.error("get_nodes_status failed: %s", e)
        return _json({"error": str(e), "tool": "get_nodes_status"})


@mcp.tool()
def get_pods(namespace: str = "all", label_selector: str = None) -> str:
    """
    Get pods in a namespace or all namespaces. Use namespace
    'all' for cluster-wide view. Use this when user asks
    about pods, running containers, or workload status.
    """
    try:
        return _json(_get_k8s().get_pods(
            namespace=namespace,
            label_selector=label_selector,
        ))
    except Exception as e:
        logger.error("get_pods failed: %s", e)
        return _json({"error": str(e), "tool": "get_pods"})


@mcp.tool()
def get_pod_logs(
    pod_name: str,
    namespace: str,
    tail_lines: int = 100,
    previous: bool = False,
    container: str = None,
) -> str:
    """
    Get logs from a specific pod. Use previous=True for
    logs from a crashed container. Use this when user asks
    about logs, errors, or what a pod is outputting.
    """
    try:
        logs = _get_k8s().get_pod_logs(
            pod_name=pod_name,
            namespace=namespace,
            container=container,
            tail_lines=tail_lines,
            previous=previous,
        )
        return logs
    except Exception as e:
        logger.error("get_pod_logs failed: %s", e)
        return _json({"error": str(e), "tool": "get_pod_logs"})


@mcp.tool()
def describe_pod(pod_name: str, namespace: str) -> str:
    """
    Get detailed information about a specific pod including
    events, conditions, and resource usage. Use this when
    user asks to describe or inspect a specific pod.
    """
    try:
        return _json(_get_k8s().describe_pod(pod_name, namespace))
    except Exception as e:
        logger.error("describe_pod failed: %s", e)
        return _json({"error": str(e), "tool": "describe_pod"})


@mcp.tool()
def get_deployments(namespace: str = "all") -> str:
    """
    Get all deployments with replica counts and health status.
    Use this when user asks about deployments, applications,
    or replica counts.
    """
    try:
        return _json(_get_k8s().get_deployments(namespace))
    except Exception as e:
        logger.error("get_deployments failed: %s", e)
        return _json({"error": str(e), "tool": "get_deployments"})


@mcp.tool()
def get_services(namespace: str = "all") -> str:
    """
    Get all services including load balancers and their
    endpoints. Use this when user asks about services,
    endpoints, or how applications are exposed.
    """
    try:
        return _json(_get_k8s().get_services(namespace))
    except Exception as e:
        logger.error("get_services failed: %s", e)
        return _json({"error": str(e), "tool": "get_services"})


@mcp.tool()
def get_namespaces() -> str:
    """
    Get all namespaces in the cluster with their status.
    Use this when user asks about namespaces or wants
    to see cluster organization.
    """
    try:
        return _json(_get_k8s().get_namespaces())
    except Exception as e:
        logger.error("get_namespaces failed: %s", e)
        return _json({"error": str(e), "tool": "get_namespaces"})


@mcp.tool()
def get_cluster_events(
    namespace: str = "all",
    event_type: str = None,
    limit: int = 30,
) -> str:
    """
    Get recent cluster events. Set event_type to 'Warning'
    for problems only. Use this when user asks about events,
    warnings, or recent cluster activity.
    """
    try:
        return _json(_get_k8s().get_events(
            namespace=namespace,
            event_type=event_type,
            limit=limit,
        ))
    except Exception as e:
        logger.error("get_cluster_events failed: %s", e)
        return _json({"error": str(e), "tool": "get_cluster_events"})


@mcp.tool()
def check_pod_disruption_budgets(namespace: str = "all") -> str:
    """
    Get all Pod Disruption Budgets and their current status.
    Use this when user asks about PDBs or before maintenance.
    """
    try:
        return _json(_get_k8s().get_pdb(namespace))
    except Exception as e:
        logger.error("check_pod_disruption_budgets failed: %s", e)
        return _json({"error": str(e), "tool": "check_pod_disruption_budgets"})


@mcp.tool()
def check_pending_failed_pods() -> str:
    """
    Find all pods that are Pending or Failed across the
    entire cluster with reasons. Use this when user asks
    about unhealthy pods or cluster problems.
    """
    try:
        return _json(_get_k8s().check_pending_failed_pods())
    except Exception as e:
        logger.error("check_pending_failed_pods failed: %s", e)
        return _json({"error": str(e), "tool": "check_pending_failed_pods"})


@mcp.tool()
def get_node_details(node_name: str) -> str:
    """
    Get deep details about a specific node including
    allocated resources and recent events. Use this when
    user asks to inspect or describe a specific node.
    """
    try:
        return _json(_get_k8s().get_node_details(node_name))
    except Exception as e:
        logger.error("get_node_details failed: %s", e)
        return _json({"error": str(e), "tool": "get_node_details"})


@mcp.tool()
def assess_node_maintenance(node_name: str) -> str:
    """
    PRIMARY MAINTENANCE TOOL. Perform complete safety
    assessment before draining a node. Checks node health,
    cluster capacity, PDB constraints, pod risks, and
    generates exact drain commands. Use this when user
    asks about draining a node, node maintenance,
    or whether it is safe to take a node offline.
    NEVER executes drain — analysis and commands only.
    """
    try:
        return _json(_get_k8s().drain_node(node_name))
    except Exception as e:
        logger.error("assess_node_maintenance failed: %s", e)
        return _json({"error": str(e), "tool": "assess_node_maintenance"})


# ══════════════════════════════════════════════
# CONTROLLED WRITE TOOLS (pre-check + approval gate)
# ══════════════════════════════════════════════


@mcp.tool()
def scale_deployment(
    deployment_name: str,
    namespace: str,
    replicas: int,
) -> str:
    """
    Scale a deployment to specified replica count.
    Guardrails: cannot scale to 0, cannot exceed 20
    replicas, cannot touch protected namespaces.
    Use this when user asks to scale a deployment.
    """
    try:
        # Guardrail validation
        check = validate_scale_request(deployment_name, namespace, replicas)
        if not check["allowed"]:
            log_guardrail_block("scale_deployment", deployment_name, check["reason"])
            return _json({
                "blocked": True,
                "reason": check["reason"],
                "tool": "scale_deployment",
            })

        # Pre-checks: deployment exists, current state, capacity
        k8s = _get_k8s()
        deps = k8s.get_deployments(namespace)
        dep = next((d for d in deps if d["name"] == deployment_name), None)
        if dep is None:
            return _json({
                "error": f"Deployment '{deployment_name}' not found in namespace '{namespace}'.",
                "tool": "scale_deployment",
            })

        capacity = k8s.check_scheduling_capacity(
            additional_cpu_millicores=100 * max(0, replicas - dep["desired_replicas"]),
            additional_memory_mi=128 * max(0, replicas - dep["desired_replicas"]),
        )

        risks = []
        if replicas < dep["desired_replicas"]:
            risks.append(f"Scaling DOWN from {dep['desired_replicas']} to {replicas}")
        if replicas == 1:
            risks.append("Single replica — no redundancy, downtime during failures")
        if not capacity["can_schedule"] and replicas > dep["desired_replicas"]:
            risks.append("Cluster may not have capacity for additional replicas")

        risk_level = "LOW"
        if replicas == 1 or not capacity["can_schedule"]:
            risk_level = "MEDIUM"
        if replicas < dep["desired_replicas"] and dep["desired_replicas"] > 2:
            risk_level = "MEDIUM"

        return _json({
            "pre_check": True,
            "operation": "scale_deployment",
            "deployment": deployment_name,
            "namespace": namespace,
            "current_replicas": dep["desired_replicas"],
            "ready_replicas": dep["ready_replicas"],
            "target_replicas": replicas,
            "capacity_check": capacity,
            "risks": risks,
            "risk_level": risk_level,
            "impact": (
                f"Scale deployment '{deployment_name}' in '{namespace}' "
                f"from {dep['desired_replicas']} to {replicas} replicas."
            ),
            "action_plan": (
                f"Will scale '{deployment_name}' from "
                f"{dep['desired_replicas']} → {replicas} replicas."
            ),
        })

    except Exception as e:
        logger.error("scale_deployment pre-check failed: %s", e)
        log_error("scale_deployment", str(e))
        return _json({"error": str(e), "tool": "scale_deployment"})


@mcp.tool()
def execute_scale_deployment(
    deployment_name: str,
    namespace: str,
    replicas: int,
) -> str:
    """
    INTERNAL TOOL — Execute scaling after approval confirmed.
    Only called by backend after user clicks YES.
    """
    try:
        k8s = _get_k8s()
        # Get before state
        deps = k8s.get_deployments(namespace)
        dep = next((d for d in deps if d["name"] == deployment_name), None)
        before_replicas = dep["desired_replicas"] if dep else "Unknown"

        result = k8s.scale_deployment(deployment_name, namespace, replicas)
        log_write_operation(
            "scale_deployment",
            deployment_name,
            namespace,
            {"replicas": before_replicas},
            {"replicas": replicas},
            "success",
        )
        return _json(result)
    except Exception as e:
        logger.error("execute_scale_deployment failed: %s", e)
        log_error("execute_scale_deployment", str(e))
        return _json({"error": str(e), "tool": "execute_scale_deployment"})


@mcp.tool()
def restart_deployment(deployment_name: str, namespace: str) -> str:
    """
    Perform rolling restart of a deployment.
    Use this when user asks to restart a deployment
    or bounce pods.
    """
    try:
        check = validate_restart_request(deployment_name, namespace)
        if not check["allowed"]:
            log_guardrail_block("restart_deployment", deployment_name, check["reason"])
            return _json({
                "blocked": True,
                "reason": check["reason"],
                "tool": "restart_deployment",
            })

        k8s = _get_k8s()
        deps = k8s.get_deployments(namespace)
        dep = next((d for d in deps if d["name"] == deployment_name), None)
        if dep is None:
            return _json({
                "error": f"Deployment '{deployment_name}' not found in namespace '{namespace}'.",
                "tool": "restart_deployment",
            })

        risks = []
        risk_level = "LOW"
        if dep["desired_replicas"] <= 1:
            risks.append("Single replica deployment — restart will cause brief downtime")
            risk_level = "MEDIUM"

        return _json({
            "pre_check": True,
            "operation": "restart_deployment",
            "deployment": deployment_name,
            "namespace": namespace,
            "current_replicas": dep["desired_replicas"],
            "ready_replicas": dep["ready_replicas"],
            "risks": risks,
            "risk_level": risk_level,
            "impact": (
                f"Rolling restart of deployment '{deployment_name}' in '{namespace}'. "
                f"Currently {dep['ready_replicas']}/{dep['desired_replicas']} pods ready."
            ),
            "action_plan": (
                f"Will perform rolling restart of '{deployment_name}' "
                f"({dep['desired_replicas']} replicas)."
            ),
        })
    except Exception as e:
        logger.error("restart_deployment pre-check failed: %s", e)
        log_error("restart_deployment", str(e))
        return _json({"error": str(e), "tool": "restart_deployment"})


@mcp.tool()
def execute_restart_deployment(deployment_name: str, namespace: str) -> str:
    """
    INTERNAL TOOL — Execute restart after approval confirmed.
    Only called by backend after user clicks YES.
    """
    try:
        result = _get_k8s().restart_deployment(deployment_name, namespace)
        log_write_operation(
            "restart_deployment",
            deployment_name,
            namespace,
            None,
            None,
            "success",
        )
        return _json(result)
    except Exception as e:
        logger.error("execute_restart_deployment failed: %s", e)
        log_error("execute_restart_deployment", str(e))
        return _json({"error": str(e), "tool": "execute_restart_deployment"})


@mcp.tool()
def patch_resource_labels(
    resource_type: str,
    resource_name: str,
    namespace: str,
    labels: dict = None,
    annotations: dict = None,
) -> str:
    """
    Patch labels or annotations on a Kubernetes resource.
    Only labels and annotations can be patched — nothing else.
    Use when user asks to label or annotate a resource.
    """
    try:
        # Build fields dict for validation
        fields = {}
        if labels:
            fields["labels"] = labels
        if annotations:
            fields["annotations"] = annotations

        field_check = validate_patch_fields(fields)
        if not field_check["allowed"]:
            log_guardrail_block("patch_resource_labels", resource_name, field_check["reason"])
            return _json({
                "blocked": True,
                "reason": field_check["reason"],
                "tool": "patch_resource_labels",
            })

        ns_check = validate_namespace(namespace)
        if not ns_check["allowed"]:
            log_guardrail_block("patch_resource_labels", resource_name, ns_check["reason"])
            return _json({
                "blocked": True,
                "reason": ns_check["reason"],
                "tool": "patch_resource_labels",
            })

        return _json({
            "pre_check": True,
            "operation": "patch_resource_labels",
            "resource_type": resource_type,
            "resource_name": resource_name,
            "namespace": namespace,
            "labels_to_set": labels,
            "annotations_to_set": annotations,
            "risks": [],
            "risk_level": "LOW",
            "impact": (
                f"Patch {resource_type} '{resource_name}' in '{namespace}': "
                f"labels={labels}, annotations={annotations}."
            ),
            "action_plan": (
                f"Will patch labels/annotations on {resource_type} '{resource_name}'."
            ),
        })
    except Exception as e:
        logger.error("patch_resource_labels pre-check failed: %s", e)
        log_error("patch_resource_labels", str(e))
        return _json({"error": str(e), "tool": "patch_resource_labels"})


@mcp.tool()
def execute_patch_resource(
    resource_type: str,
    resource_name: str,
    namespace: str,
    labels: dict = None,
    annotations: dict = None,
) -> str:
    """
    INTERNAL TOOL — Execute patch after approval confirmed.
    Only called by backend after user clicks YES.
    """
    try:
        result = _get_k8s().patch_labels_annotations(
            resource_type, resource_name, namespace,
            labels=labels, annotations=annotations,
        )
        log_write_operation(
            "patch_resource_labels",
            resource_name,
            namespace,
            None,
            {"labels": labels, "annotations": annotations},
            "success",
        )
        return _json(result)
    except Exception as e:
        logger.error("execute_patch_resource failed: %s", e)
        log_error("execute_patch_resource", str(e))
        return _json({"error": str(e), "tool": "execute_patch_resource"})


@mcp.tool()
def cordon_node(node_name: str) -> str:
    """
    Mark a node as unschedulable (cordon).
    No new pods will be scheduled on it.
    Use when user explicitly asks to cordon a node.
    """
    try:
        check = validate_node_operation(node_name)
        if not check["allowed"]:
            log_guardrail_block("cordon_node", node_name, check["reason"])
            return _json({
                "blocked": True,
                "reason": check["reason"],
                "tool": "cordon_node",
            })

        k8s = _get_k8s()
        node_detail = k8s.get_node_details(node_name)
        pods_on_node = k8s.get_pods_on_node(node_name)

        return _json({
            "pre_check": True,
            "operation": "cordon_node",
            "node_name": node_name,
            "current_unschedulable": node_detail.get("unschedulable", False),
            "pods_on_node": len(pods_on_node),
            "risks": ["Existing pods will continue running but no new pods will be scheduled"],
            "risk_level": "MEDIUM",
            "impact": (
                f"Cordon node '{node_name}': mark as unschedulable. "
                f"Currently has {len(pods_on_node)} pods."
            ),
            "action_plan": f"Will cordon node '{node_name}' (mark unschedulable).",
        })
    except Exception as e:
        logger.error("cordon_node pre-check failed: %s", e)
        log_error("cordon_node", str(e))
        return _json({"error": str(e), "tool": "cordon_node"})


@mcp.tool()
def execute_cordon_node(node_name: str) -> str:
    """
    INTERNAL TOOL — Execute cordon after approval confirmed.
    Only called by backend after user clicks YES.
    """
    try:
        result = _get_k8s().cordon_node(node_name)
        log_write_operation(
            "cordon_node",
            node_name,
            "cluster",
            {"unschedulable": False},
            {"unschedulable": True},
            "success",
        )
        return _json(result)
    except Exception as e:
        logger.error("execute_cordon_node failed: %s", e)
        log_error("execute_cordon_node", str(e))
        return _json({"error": str(e), "tool": "execute_cordon_node"})


# ══════════════════════════════════════════════
# Server Entry Point
# ══════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    logger.info(
        "Starting EKS Guardian MCP Server on %s:%s",
        MCP_SERVER_HOST, MCP_SERVER_PORT,
    )
    mcp.run(
        transport="streamable-http",
        host=MCP_SERVER_HOST,
        port=MCP_SERVER_PORT,
    )
