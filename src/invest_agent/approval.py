"""Validation for externally created, one-time approvals."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime

from invest_agent.domain import Approval, ProposalBundle


class ApprovalViolation(ValueError):
    """An approval is absent, stale, mismatched or already consumed."""


def validate_approval(
    proposal: ProposalBundle,
    approval: Approval,
    *,
    now: datetime,
    consumed_proposal_digests: Collection[str] = (),
) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    proposal_digest = proposal.digest
    if approval.proposal_digest != proposal_digest:
        raise ApprovalViolation("approval does not match the exact proposal")
    if now > proposal.expires_at:
        raise ApprovalViolation("proposal has expired")
    if now > approval.expires_at:
        raise ApprovalViolation("approval has expired")
    if approval.approved_at < proposal.created_at:
        raise ApprovalViolation("approval predates the proposal")
    if proposal_digest in consumed_proposal_digests:
        raise ApprovalViolation("proposal approval has already been consumed")
