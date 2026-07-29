"""
server/guardrails.py
────────────────────────────────────────
All safety rules are hardcoded here.
The AI cannot override these.
Every write operation validates through this module before execution.
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.settings import (
    PROTECTED_NAMESPACES,
    MIN_REPLICAS,
    MAX_REPLICAS,
    ALLOWED_PATCH_FIELDS,
)


# ──────────────────────────────────────────────
# Tool classification sets
# ──────────────────────────────────────────────

WRITE_OPERATIONS: set[str] = {
    "scale_deployment",
    "restart_deployment",
    "patch_resource_labels",
    "cordon_node",
}

DESTRUCTIVE_OPERATIONS: set[str] = {
    "drain_node",
}


# ──────────────────────────────────────────────
# Validation helpers
# ──────────────────────────────────────────────

def validate_namespace(namespace: str) -> dict:
    """
    Block operations on protected Kubernetes system namespaces.

    Args:
        namespace: The target namespace to validate.

    Returns:
        dict with 'allowed' (bool) and 'reason' (str).
    """
    if namespace in PROTECTED_NAMESPACES:
        return {
            "allowed": False,
            "reason": (
                f"Namespace '{namespace}' is a protected system namespace. "
                f"Operations on {PROTECTED_NAMESPACES} are not permitted."
            ),
        }
    return {
        "allowed": True,
        "reason": f"Namespace '{namespace}' is allowed.",
    }


def validate_replica_count(replicas: int) -> dict:
    """
    Ensure replica count stays within safe bounds.

    Args:
        replicas: The desired replica count.

    Returns:
        dict with 'allowed' (bool) and 'reason' (str).
    """
    if replicas < MIN_REPLICAS:
        return {
            "allowed": False,
            "reason": (
                f"Cannot scale to {replicas} replicas. "
                f"Minimum allowed is {MIN_REPLICAS} (scaling to zero is never permitted)."
            ),
        }
    if replicas > MAX_REPLICAS:
        return {
            "allowed": False,
            "reason": (
                f"Cannot scale to {replicas} replicas. "
                f"Maximum allowed is {MAX_REPLICAS}."
            ),
        }
    return {
        "allowed": True,
        "reason": f"Replica count {replicas} is within allowed range ({MIN_REPLICAS}–{MAX_REPLICAS}).",
    }


def validate_scale_request(
    deployment: str, namespace: str, replicas: int
) -> dict:
    """
    Combined validation for a scale operation.
    Runs namespace + replica count checks.

    Args:
        deployment:  Name of the deployment to scale.
        namespace:   Target namespace.
        replicas:    Desired replica count.

    Returns:
        dict with 'allowed' (bool) and 'reason' (str).
    """
    ns_check = validate_namespace(namespace)
    if not ns_check["allowed"]:
        return ns_check

    replica_check = validate_replica_count(replicas)
    if not replica_check["allowed"]:
        return replica_check

    return {
        "allowed": True,
        "reason": (
            f"Scale request validated: deployment='{deployment}', "
            f"namespace='{namespace}', replicas={replicas}."
        ),
    }


def validate_patch_fields(fields: dict) -> dict:
    """
    Only allow patching labels and annotations — nothing else.

    Args:
        fields: Dict of field names being patched (keys are field names).

    Returns:
        dict with 'allowed' (bool) and 'reason' (str).
    """
    if not fields:
        return {
            "allowed": False,
            "reason": "No fields provided for patching.",
        }

    disallowed = [f for f in fields if f not in ALLOWED_PATCH_FIELDS]
    if disallowed:
        return {
            "allowed": False,
            "reason": (
                f"Cannot patch fields: {disallowed}. "
                f"Only {ALLOWED_PATCH_FIELDS} are allowed."
            ),
        }
    return {
        "allowed": True,
        "reason": f"All patch fields are allowed: {list(fields.keys())}.",
    }


def validate_restart_request(deployment: str, namespace: str) -> dict:
    """
    Validate a restart (rolling-restart) request.
    Runs namespace check.

    Args:
        deployment: Name of the deployment to restart.
        namespace:  Target namespace.

    Returns:
        dict with 'allowed' (bool) and 'reason' (str).
    """
    ns_check = validate_namespace(namespace)
    if not ns_check["allowed"]:
        return ns_check

    return {
        "allowed": True,
        "reason": (
            f"Restart request validated: deployment='{deployment}', "
            f"namespace='{namespace}'."
        ),
    }


def validate_node_operation(node_name: str) -> dict:
    """
    Basic node name validation.

    Args:
        node_name: Name of the node.

    Returns:
        dict with 'allowed' (bool) and 'reason' (str).
    """
    if not node_name or not node_name.strip():
        return {
            "allowed": False,
            "reason": "Node name cannot be empty.",
        }
    return {
        "allowed": True,
        "reason": f"Node name '{node_name}' is valid.",
    }


def validate_drain_request(node_name: str) -> dict:
    """
    Validate a drain request.
    Drain is NEVER auto-executed — always advisory only.

    Args:
        node_name: Name of the node to drain.

    Returns:
        dict with 'allowed' (bool), 'reason' (str),
        and 'advisory' flag set to True.
    """
    node_check = validate_node_operation(node_name)
    if not node_check["allowed"]:
        return node_check

    return {
        "allowed": True,
        "advisory": True,
        "reason": (
            f"Drain assessment for node '{node_name}' is permitted. "
            "NOTE: Drain is advisory only — the agent will generate a plan "
            "and exact commands, but will NEVER execute drain automatically. "
            "The engineer must run the commands manually."
        ),
    }


# ──────────────────────────────────────────────
# Tool classification functions
# ──────────────────────────────────────────────

def is_write_operation(tool_name: str) -> bool:
    """
    Returns True if the tool is a controlled write operation
    that requires the approval gate.

    Args:
        tool_name: Name of the MCP tool.

    Returns:
        True if tool modifies cluster state.
    """
    return tool_name in WRITE_OPERATIONS


def is_destructive_operation(tool_name: str) -> bool:
    """
    Returns True if the tool is a destructive operation.
    Destructive operations are advisory only — they generate
    a plan but never execute.

    Args:
        tool_name: Name of the MCP tool.

    Returns:
        True if tool is destructive (drain).
    """
    return tool_name in DESTRUCTIVE_OPERATIONS
