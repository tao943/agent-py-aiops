"""Compatibility imports for operational scripts."""

from super_ai.documents.index_client import (
    IndexPollingError,
    IndexPollingTimeout,
    IndexProtocolError,
    IndexTaskFailed,
    parse_created_task,
    wait_for_index_task,
)

__all__ = [
    "IndexPollingError",
    "IndexPollingTimeout",
    "IndexProtocolError",
    "IndexTaskFailed",
    "parse_created_task",
    "wait_for_index_task",
]
