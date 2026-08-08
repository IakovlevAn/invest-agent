from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path

from invest_agent.domain import InstrumentType
from invest_agent.portfolio import (
    BcsPortfolioNormalizer,
    PortfolioContractError,
    portfolio_as_dict,
    render_portfolio_json,
    render_portfolio_text,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def fixture() -> dict[str, object]:
    return json.loads((ROOT / "tests/fixtures/bcs_portfolio.json").read_text())


class PortfolioNormalizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.normalizer = BcsPortfolioNormalizer(now=lambda: NOW)

    def test_normalizes_cash_positions_and_blocked_assets(self) -> None:
        snapshot = self.normalizer.normalize(fixture(), is_iis=True)

        self.assertTrue(snapshot.is_iis)
        self.assertEqual(str(snapshot.cash_rub), "50000")
        self.assertEqual(str(snapshot.total_value_rub), "250000")
        self.assertEqual(len(snapshot.positions), 3)
        self.assertTrue(snapshot.account_ref.startswith("bcs:"))
        self.assertNotIn("synthetic-agreement", snapshot.account_ref)

        bond = next(
            item for item in snapshot.positions if item.instrument_type is InstrumentType.BOND
        )
        self.assertEqual(str(bond.available_quantity), "90")
        self.assertTrue(bond.tradable)

        blocked = next(
            item
            for item in snapshot.positions
            if item.instrument_type is InstrumentType.FOREIGN_STOCK
        )
        self.assertFalse(blocked.tradable)
        self.assertIn("blocked", blocked.blocked_reason.lower())

    def test_unknown_instrument_is_preserved_but_not_tradable(self) -> None:
        payload = fixture()
        payload["positions"][1]["instrumentType"] = "NEW_UNKNOWN_TYPE"  # type: ignore[index]
        snapshot = self.normalizer.normalize(payload, is_iis=True)
        unknown = next(item for item in snapshot.positions if item.ticker == "RU000A0TEST1")
        self.assertEqual(unknown.instrument_type, InstrumentType.UNKNOWN)
        self.assertFalse(unknown.tradable)

    def test_rejects_multiple_agreements(self) -> None:
        payload = fixture()
        payload["positions"][1]["agreementId"] = "another-agreement"  # type: ignore[index]
        with self.assertRaisesRegex(PortfolioContractError, "exactly one agreement"):
            self.normalizer.normalize(payload, is_iis=True)

    def test_rejects_missing_block_flags(self) -> None:
        payload = fixture()
        del payload["positions"][1]["isBlocked"]  # type: ignore[index]
        with self.assertRaisesRegex(PortfolioContractError, "isBlocked must be boolean"):
            self.normalizer.normalize(payload, is_iis=True)

    def test_reports_do_not_expose_broker_agreement(self) -> None:
        snapshot = self.normalizer.normalize(fixture(), is_iis=True)
        json_report = render_portfolio_json(snapshot)
        text_report = render_portfolio_text(snapshot)
        self.assertNotIn("synthetic-agreement-123", json_report)
        self.assertNotIn("synthetic-agreement-123", text_report)
        self.assertIn("LOCKED", text_report)
        self.assertEqual(portfolio_as_dict(snapshot)["total_value_rub"], "250000")


if __name__ == "__main__":
    unittest.main()
