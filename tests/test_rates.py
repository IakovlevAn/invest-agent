from __future__ import annotations

import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from invest_agent.http import HttpResponse
from invest_agent.rates import (
    BondRateModel,
    CBR_KEY_RATE_URL,
    CbrKeyRateClient,
    KeyRateObservation,
    RateScenario,
    RateScenarioPolicy,
    is_key_rate_benchmark,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
POLICY_PATH = Path(__file__).parents[1] / "config" / "investment_policy.toml"


class FakeTransport:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def request(self, **kwargs: object) -> HttpResponse:
        self.calls.append(kwargs)
        return self.response


class RateModelTests(unittest.TestCase):
    def test_recognizes_moex_key_rate_benchmark_code(self) -> None:
        self.assertTrue(is_key_rate_benchmark("RREFKEYR"))
        self.assertTrue(
            is_key_rate_benchmark("Ключевая ставка Банка России")
        )
        self.assertFalse(is_key_rate_benchmark("RUONIA"))

    def test_reads_latest_key_rate_from_official_table_contract(self) -> None:
        html = b"""
        <html><table>
          <tr><td>24.07.2026</td><td>14,25</td></tr>
          <tr><td>28.07.2026</td><td>14,00</td></tr>
        </table></html>
        """
        transport = FakeTransport(HttpResponse(status=200, body=html))

        observation = CbrKeyRateClient(
            transport=transport,
            now=lambda: NOW,
        ).fetch()

        self.assertEqual(observation.value_percent, Decimal("14.00"))
        self.assertEqual(observation.effective_date, date(2026, 7, 28))
        self.assertEqual(transport.calls[0]["url"], CBR_KEY_RATE_URL)

    def test_floater_and_long_bond_scenarios_are_transparent(self) -> None:
        model = BondRateModel(RateScenarioPolicy.from_toml(POLICY_PATH))
        observation = KeyRateObservation(
            value_percent=Decimal("14"),
            effective_date=date(2026, 7, 28),
            fetched_at=NOW,
            source_url=CBR_KEY_RATE_URL,
        )
        base = RateScenario(
            code="BASE",
            label="Base",
            key_rate_change_percent=Decimal("-1.5"),
        )

        floater = model._floater_scenario(base, observation, spread=Decimal("2.5"))
        fixed = model._fixed_scenario(
            base,
            observation,
            duration_days=Decimal("730"),
            current_yield_percent=Decimal("20"),
        )

        self.assertEqual(floater.projected_key_rate_percent, Decimal("12.5"))
        self.assertEqual(floater.approximate_total_return_percent, Decimal("15.0"))
        self.assertEqual(fixed.approximate_price_return_percent, Decimal("2.250"))
        self.assertEqual(fixed.approximate_total_return_percent, Decimal("22.250"))


if __name__ == "__main__":
    unittest.main()
