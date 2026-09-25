"""Deterministic Worklink orchestration primitives.

Worklink is the Mimir-native Chainlink worker rail: model-backed backends
may edit a per-issue checkout, but claiming, checkout lifecycle, and evidence
validation live here as plain Python.
"""

from .backends import (
    BackendRegistry,
    Caps,
    ComputeBackend,
    ComputeCaps,
    ComputeLaunchError,
    ComputeResult,
    LaunchHandle,
    LocalSubprocessComputeBackend,
    RawResult,
    ToolBackend,
    ToolPin,
    WorkOrder,
    WorkSpec,
    WorklinkConfig,
)
from .claims import ClaimRecord, ClaimResult, ChainlinkClaims
from .evidence import CommandResult, EvidenceValidation, TestResult, WorklinkEvidence
from .tool_pins import (
    DEFAULT_TOOL_PINS,
    default_tool_pins,
)
from .checkout import CheckoutLease

__all__ = [
    "BackendRegistry",
    "Caps",
    "ChainlinkClaims",
    "ClaimRecord",
    "ClaimResult",
    "CommandResult",
    "LocalSubprocessComputeBackend",
    "LaunchHandle",
    "ComputeResult",
    "ComputeCaps",
    "ComputeBackend",
    "ComputeLaunchError",
    "EvidenceValidation",
    "TestResult",
    "RawResult",
    "DEFAULT_TOOL_PINS",
    "ToolBackend",
    "ToolPin",
    "WorkOrder",
    "WorkSpec",
    "WorklinkConfig",
    "WorklinkEvidence",
    "CheckoutLease",
    "default_tool_pins",
]
