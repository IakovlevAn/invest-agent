from __future__ import annotations

import json
import unittest
import urllib.parse
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from invest_agent.http import HttpResponse
from invest_agent.market.moex import MoexContractError, MoexIssClient

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 8, 14, 40, tzinfo=UTC)


def fixture(name: str) -> object:
    return json.loads((ROOT / "tests/fixtures" / name).read_text())


def response(status: int, payload: object) -> HttpResponse:
    return HttpResponse(status=status, body=json.dumps(payload).encode("utf-8"))


class FakeTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout_seconds: float = 15.0,
    ) -> HttpResponse:
        self.calls.append({"method": method, "url": url, "headers": dict(headers)})
        return self.responses.pop(0)


class MoexIssClientTests(unittest.TestCase):
    def test_reads_primary_board_bond_market_and_emitter(self) -> None:
        transport = FakeTransport(
            [
                response(200, fixture("moex_bond_security.json")),
                response(200, fixture("moex_bond_market.json")),
                response(200, fixture("moex_emitter.json")),
            ]
        )
        client = MoexIssClient(transport=transport, now=lambda: NOW)

        bond = client.fetch_bond("ru000a10bs76")
        emitter = client.fetch_emitter(13592)

        self.assertEqual(bond.facts.primary_board, "TQCB")
        self.assertEqual(bond.facts.coupon_percent, Decimal("20.25"))
        self.assertFalse(bond.facts.has_default)
        self.assertIsNotNone(bond.market)
        assert bond.market is not None
        self.assertEqual(bond.market.effective_yield_percent, Decimal("24.2836"))
        self.assertEqual(bond.market.yield_at_wap_percent, Decimal("24.29"))
        self.assertEqual(bond.market.duration_days, Decimal("551"))
        self.assertEqual(bond.market.z_spread_bps, Decimal("1001"))
        self.assertEqual(
            bond.market.bid_offer_spread_percent_of_face,
            Decimal("0.19"),
        )
        assert bond.market.trade_moment is not None
        self.assertEqual(bond.market.trade_moment.utcoffset().total_seconds(), 10800)
        self.assertEqual(emitter.short_title, "ООО ВУШ")
        self.assertEqual(emitter.inn, "9717068640")

        market_url = urllib.parse.urlparse(transport.calls[1]["url"])  # type: ignore[arg-type]
        self.assertIn("/boards/TQCB/securities/RU000A10BS76.json", market_url.path)
        query = urllib.parse.parse_qs(market_url.query)
        self.assertEqual(query["iss.only"], ["marketdata,marketdata_yields"])
        self.assertIn("EFFECTIVEYIELDWAPRICE", query["marketdata_yields.columns"][0])

    def test_retries_server_failures(self) -> None:
        sleeps: list[float] = []
        transport = FakeTransport(
            [
                response(503, {}),
                response(200, fixture("moex_bond_security.json")),
                response(200, fixture("moex_bond_market.json")),
            ]
        )
        client = MoexIssClient(transport=transport, now=lambda: NOW, sleep=sleeps.append)

        client.fetch_bond("RU000A10BS76")

        self.assertEqual(sleeps, [0.25])

    def test_rejects_non_bond_contract(self) -> None:
        payload = fixture("moex_bond_security.json")
        payload["description"]["data"][-2][1] = "stock_shares"  # type: ignore[index]
        client = MoexIssClient(
            transport=FakeTransport([response(200, payload)]),
            now=lambda: NOW,
        )

        with self.assertRaisesRegex(MoexContractError, "is not a bond"):
            client.fetch_bond("RU000A10BS76")

    def test_rejects_malformed_table_row(self) -> None:
        payload = fixture("moex_bond_security.json")
        payload["boards"]["data"][0].pop()  # type: ignore[index]
        client = MoexIssClient(
            transport=FakeTransport([response(200, payload)]),
            now=lambda: NOW,
        )

        with self.assertRaisesRegex(MoexContractError, r"boards.data\[0\]"):
            client.fetch_bond("RU000A10BS76")

    def test_reads_paginated_bond_payment_schedule(self) -> None:
        transport = FakeTransport(
            [
                response(200, fixture("moex_bondization_0.json")),
                response(200, fixture("moex_bondization_2.json")),
            ]
        )
        client = MoexIssClient(transport=transport, now=lambda: NOW)

        schedule = client.fetch_bond_schedule("ru000a10bs76")

        self.assertEqual(schedule.isin, "RU000A10BS76")
        self.assertEqual(len(schedule.coupons), 3)
        self.assertEqual(schedule.coupons[-1].coupon_date.isoformat(), "2026-11-01")
        self.assertEqual(schedule.amortizations[0].value_percent, Decimal("100"))
        self.assertEqual(schedule.offers[0].offer_type, "put")
        self.assertIn("coupons.start=2", transport.calls[1]["url"])


if __name__ == "__main__":
    unittest.main()
