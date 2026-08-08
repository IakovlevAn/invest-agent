from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from invest_agent.approval import ApprovalViolation, validate_approval
from invest_agent.domain import (
    Approval,
    InstrumentType,
    OrderIntent,
    OrderType,
    ProposalBundle,
    Side,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def proposal() -> ProposalBundle:
    order = OrderIntent(
        instrument_uid="RU000A000001",
        ticker="TEST-BOND",
        class_code="TQCB",
        instrument_type=InstrumentType.BOND,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        lots=5,
        limit_price=Decimal("100.10"),
        currency="RUB",
        lot_size=1,
        price_step=Decimal("0.01"),
        estimated_cash_rub=Decimal("500.50"),
        quote_observed_at=NOW - timedelta(seconds=10),
        order_valid_until=NOW + timedelta(hours=6),
    )
    return ProposalBundle(
        proposal_id="proposal-approval-test",
        portfolio_snapshot_digest="portfolio-snapshot",
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        orders=(order,),
        rationale=("Test rationale",),
        projected_annual_return=Decimal("0.20"),
        projected_stress_loss=Decimal("0.10"),
    )


def approval(item: ProposalBundle) -> Approval:
    return Approval(
        approval_id="approval-1",
        proposal_digest=item.digest,
        approved_by="portfolio-owner",
        approved_at=NOW + timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=5),
    )


class ApprovalTests(unittest.TestCase):
    def test_accepts_exact_live_approval(self) -> None:
        item = proposal()
        validate_approval(item, approval(item), now=NOW + timedelta(minutes=2))

    def test_price_change_invalidates_approval(self) -> None:
        original = proposal()
        changed_order = replace(original.orders[0], limit_price=Decimal("100.11"))
        changed = replace(original, orders=(changed_order,))
        with self.assertRaisesRegex(ApprovalViolation, "exact proposal"):
            validate_approval(changed, approval(original), now=NOW + timedelta(minutes=2))

    def test_expired_approval_is_rejected(self) -> None:
        item = proposal()
        with self.assertRaisesRegex(ApprovalViolation, "approval has expired"):
            validate_approval(item, approval(item), now=NOW + timedelta(minutes=6))

    def test_consumed_approval_is_rejected(self) -> None:
        item = proposal()
        with self.assertRaisesRegex(ApprovalViolation, "already been consumed"):
            validate_approval(
                item,
                approval(item),
                now=NOW + timedelta(minutes=2),
                consumed_proposal_digests={item.digest},
            )


if __name__ == "__main__":
    unittest.main()
