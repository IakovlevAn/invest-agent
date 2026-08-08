from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from invest_agent.audit import FindingSeverity, PortfolioAuditor, render_audit_json
from invest_agent.policy import InvestmentPolicy
from invest_agent.portfolio import BcsPortfolioNormalizer

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def fixture() -> dict[str, object]:
    return json.loads((ROOT / "tests/fixtures/bcs_portfolio.json").read_text())


class PortfolioAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = InvestmentPolicy.from_toml(ROOT / "config/investment_policy.toml")
        self.normalizer = BcsPortfolioNormalizer(now=lambda: NOW)
        self.auditor = PortfolioAuditor(self.policy)

    def test_audits_managed_sleeve_and_soft_bond_gap(self) -> None:
        snapshot = self.normalizer.normalize(fixture(), is_iis=True)

        audit = self.auditor.audit(snapshot)

        self.assertEqual(audit.total_value_rub, Decimal("250000"))
        self.assertEqual(audit.managed_value_rub, Decimal("250000"))
        self.assertEqual(audit.managed_securities_value_rub, Decimal("200000"))
        self.assertEqual(audit.bond_share_managed, Decimal("0.41"))
        self.assertEqual(audit.bond_target_gap_rub, Decimal("122500.0"))
        self.assertEqual(audit.bond_share_after_investing_cash, Decimal("0.61"))
        self.assertEqual(audit.bond_only_contribution_needed_rub, Decimal("1225000"))
        self.assertEqual(audit.regular_contributions_needed, 25)
        self.assertEqual(audit.position_hhi, Decimal("0.50031250"))
        self.assertEqual(audit.top_positions[0].ticker, "RU000A0TEST1")
        self.assertEqual(audit.top_positions[0].share_of_managed_securities, Decimal("0.5125"))
        self.assertIn("BOND_TARGET_GAP", {finding.code for finding in audit.findings})
        concentration = next(
            finding for finding in audit.findings if finding.code == "POSITION_CONCENTRATION"
        )
        self.assertEqual(concentration.severity, FindingSeverity.WARNING)
        self.assertFalse(audit.target_bond_share_is_hard_limit)

    def test_excludes_blocked_assets_from_managed_target_weights(self) -> None:
        payload = fixture()
        blocked = payload["positions"][3]  # type: ignore[index]
        blocked["currentValueRub"] = 50000
        snapshot = self.normalizer.normalize(payload, is_iis=True)

        audit = self.auditor.audit(snapshot)

        self.assertEqual(audit.total_value_rub, Decimal("300000"))
        self.assertEqual(audit.managed_value_rub, Decimal("250000"))
        self.assertEqual(audit.blocked_value_rub, Decimal("50000"))
        self.assertEqual(audit.blocked_share_full, Decimal("0.1666666666666666666666666667"))
        self.assertIn("BLOCKED_ASSETS_MATERIAL", {finding.code for finding in audit.findings})

    def test_json_is_analysis_only_and_declares_data_gaps(self) -> None:
        snapshot = self.normalizer.normalize(fixture(), is_iis=True)

        rendered = json.loads(render_audit_json(self.auditor.audit(snapshot)))

        self.assertEqual(rendered["execution_state"], "ANALYSIS_ONLY")
        self.assertGreaterEqual(len(rendered["data_gaps"]), 4)
        self.assertIn("managed", rendered["allocation"])


if __name__ == "__main__":
    unittest.main()
