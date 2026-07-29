"""
server/audit_log.py
────────────────────────────────────────
Permanent audit logging for every write operation.
Each log line is JSON format with UTC ISO timestamps.
Writes to logs/audit.log (creates file if it does not exist).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.settings import AUDIT_LOG_PATH

# ──────────────────────────────────────────────
# Ensure log directory and file exist
# ──────────────────────────────────────────────

_log_path = Path(AUDIT_LOG_PATH)
_log_path.parent.mkdir(parents=True, exist_ok=True)

# Dedicated logger for audit trail
_audit_logger = logging.getLogger("kubeguardian.audit")
_audit_logger.setLevel(logging.INFO)
_audit_logger.propagate = False

# File handler — append mode, UTF-8
_file_handler = logging.FileHandler(str(_log_path), mode="a", encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(message)s"))
_audit_logger.addHandler(_file_handler)


def _utc_now() -> str:
    """Return the current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _write_entry(entry: dict) -> None:
    """
    Write a single JSON log entry to the audit log file.

    Args:
        entry: Dictionary to serialise as a JSON line.
    """
    _audit_logger.info(json.dumps(entry, default=str))


# ──────────────────────────────────────────────
# Public logging functions
# ──────────────────────────────────────────────

def log_write_operation(
    operation: str,
    target: str,
    namespace: str,
    before_state: dict | str | None,
    after_state: dict | str | None,
    result: str,
) -> None:
    """
    Log a completed write operation (scale, restart, patch, cordon).

    Args:
        operation:    Name of the operation (e.g. 'scale_deployment').
        target:       Target resource name.
        namespace:    Kubernetes namespace.
        before_state: State before the operation.
        after_state:  State after the operation.
        result:       Outcome description.
    """
    _write_entry({
        "timestamp": _utc_now(),
        "status": "EXECUTED",
        "operation": operation,
        "target": target,
        "namespace": namespace,
        "before_state": before_state,
        "after_state": after_state,
        "result": result,
    })


def log_approval_given(
    operation: str,
    target: str,
    namespace: str,
) -> None:
    """
    Log that a user approved a pending write operation.

    Args:
        operation: Name of the operation.
        target:    Target resource name.
        namespace: Kubernetes namespace.
    """
    _write_entry({
        "timestamp": _utc_now(),
        "status": "APPROVED",
        "operation": operation,
        "target": target,
        "namespace": namespace,
    })


def log_approval_denied(
    operation: str,
    target: str,
    namespace: str,
) -> None:
    """
    Log that a user denied / cancelled a pending write operation.

    Args:
        operation: Name of the operation.
        target:    Target resource name.
        namespace: Kubernetes namespace.
    """
    _write_entry({
        "timestamp": _utc_now(),
        "status": "DENIED",
        "operation": operation,
        "target": target,
        "namespace": namespace,
    })


def log_guardrail_block(
    operation: str,
    target: str,
    reason: str,
) -> None:
    """
    Log that a guardrail blocked an operation before it reached
    the approval gate.

    Args:
        operation: Name of the operation.
        target:    Target resource name.
        reason:    Why the guardrail blocked it.
    """
    _write_entry({
        "timestamp": _utc_now(),
        "status": "BLOCKED",
        "operation": operation,
        "target": target,
        "reason": reason,
    })


def log_error(
    operation: str,
    error_message: str,
) -> None:
    """
    Log an error that occurred during an operation.

    Args:
        operation:     Name of the operation.
        error_message: Error details.
    """
    _write_entry({
        "timestamp": _utc_now(),
        "status": "ERROR",
        "operation": operation,
        "error": error_message,
    })
