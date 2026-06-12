"""Commit construction: message builders (with secret masking) and the
user/agent commit helpers."""

from agit.commits.actions import AgitActions
from agit.commits.message import (
    METADATA_HEADER,
    apply_summary_to_message,
    build_agent_commit_message,
    build_agent_merge_message,
    build_backend_amend_message,
    build_user_commit_message,
    summary_metadata_lines,
)

__all__ = [
    "AgitActions",
    "METADATA_HEADER",
    "apply_summary_to_message",
    "build_agent_commit_message",
    "build_agent_merge_message",
    "build_backend_amend_message",
    "build_user_commit_message",
    "summary_metadata_lines",
]
