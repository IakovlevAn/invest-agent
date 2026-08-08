from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from invest_agent.domain import InstrumentType, OrderIntent, OrderType, ProposalBundle, Side
from invest_agent.policy import InvestmentPolicy, PolicyViolation

ROOT = Path(__file__).resolve().parents[1]


def bond_order(**overrides: object) -> OrderIntent:
    values: dict[str, object] = {
        "instrument_uid": "RU000A000001",
        "ticker": "TEST-BOND",
        "class_code": "TQCB",
        "instrument_type": InstrumentType.BOND,
        "side": Side.BUY,
        "order_type": OrderType.LIMIT,
        "lots": 10,
        "limit_price": Decimal("101.25"),
        "currency": "RUB",
    }
    values.update(overrides)
    return OrderIntent(**values)  # type: ignore[arg-type]


def proposal(order: OrderIntent, *, stress_loss: str = "0.10") -> ProposalBundle:
    now = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    return ProposalBundle(
        proposal_id="proposal-1",
        portfolio_snapshot_digest="snapshot-digest",
        created_at=now,
        expires_at=now + timedelta(minutes=10),
        orders=(order,),
        rationale=("Risk-adjusted improvement supported by current evidence",),
        projected_annual_return=Decimal("0.20"),
        projected_stress_loss=Decimal(stress_loss),
    )


class InvestmentPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = InvestmentPolicy.from_toml(ROOT / "config/investment_policy.toml")

    def test_loads_agreed_mandate(self) -> None:
        self.assertEqual(self.policy.target_annual_return, Decimal("0.2"))
        self.assertEqual(self.policy.maximum_drawdown, Decimal("0.15"))
        self.assertEqual(self.policy.target_bond_share, Decimal("0.9"))
        self.assertFalse(self.policy.target_bond_share_is_hard_limit)
        self.assertEqual(self.policy.regular_contribution_rub, Decimal("50000"))
        self.assertEqual(
            self.policy.position_concentration_warning_share,
            Decimal("0.2"),
        )
        self.assertFalse(self.policy.target_is_guarantee)
        self.assertTrue(self.policy.explicit_external_approval_required)

    def test_accepts_ruble_limit_bond_order(self) -> None:
        self.policy.validate_proposal(proposal(bond_order()))

    def test_rejects_market_order(self) -> None:
        order = bond_order(order_type=OrderType.MARKET, limit_price=None)
        with self.assertRaisesRegex(PolicyViolation, "only limit orders"):
            self.policy.validate_proposal(proposal(order))

    def test_rejects_derivative(self) -> None:
        order = bond_order(instrument_type=InstrumentType.FUTURE)
        with self.assertRaisesRegex(PolicyViolation, "forbidden"):
            self.policy.validate_proposal(proposal(order))

    def test_rejects_blocked_instrument(self) -> None:
        order = bond_order(tradable=False, blocked_reason="blocked by infrastructure")
        with self.assertRaisesRegex(PolicyViolation, "not tradable"):
            self.policy.validate_proposal(proposal(order))

    def test_rejects_stress_loss_above_mandate(self) -> None:
        with self.assertRaisesRegex(PolicyViolation, "exceeds maximum drawdown"):
            self.policy.validate_proposal(proposal(bond_order(), stress_loss="0.151"))

    def test_rejects_overlong_proposal(self) -> None:
        item = proposal(bond_order())
        item = replace(item, expires_at=item.created_at + timedelta(minutes=11))
        with self.assertRaisesRegex(PolicyViolation, "lifetime"):
            self.policy.validate_proposal(item)


if __name__ == "__main__":
    unittest.main()
