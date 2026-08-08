from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from invest_agent.financial.fns import GirboClient, GirboContractError
from invest_agent.http import HttpResponse

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 8, 15, 30, tzinfo=UTC)


def fixture(name: str) -> object:
    return json.loads((ROOT / "tests/fixtures" / name).read_text())


def response(status: int, payload: object) -> HttpResponse:
    return HttpResponse(status=status, body=json.dumps(payload).encode())


class FakeTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = responses
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout_seconds: float = 15.0,
    ) -> HttpResponse:
        self.urls.append(url)
        self.headers.append(dict(headers))
        return self.responses.pop(0)


class GirboClientTests(unittest.TestCase):
    def test_reads_latest_exact_annual_ras_statement(self) -> None:
        transport = FakeTransport(
            [
                response(200, fixture("fns_girbo_search.json")),
                response(200, fixture("fns_girbo_bfo.json")),
            ]
        )
        client = GirboClient(transport=transport, now=lambda: NOW)

        statement = client.fetch_latest_annual("9717068640")

        assert statement is not None
        self.assertEqual(statement.period, 2025)
        self.assertEqual(statement.scope, "LEGAL_ENTITY_RAS")
        self.assertEqual(statement.unit, "THOUSAND_RUB")
        self.assertEqual(statement.revenue, Decimal("10694433"))
        self.assertEqual(statement.interest_expense, Decimal("2583863"))
        self.assertEqual(statement.net_income, Decimal("-2549955"))
        self.assertTrue(statement.has_audit_report)
        self.assertIn("organizations-card/10703464", statement.source_url)
        self.assertIn("advanced-search/organizations/search", transport.urls[0])
        self.assertTrue(transport.headers[0]["User-Agent"].startswith("InvestAgent/"))

    def test_returns_none_for_verified_absent_exact_inn(self) -> None:
        transport = FakeTransport([response(200, {"content": [], "totalElements": 0})])
        client = GirboClient(transport=transport, now=lambda: NOW)

        self.assertIsNone(client.fetch_latest_annual("7703104630"))
        self.assertEqual(len(transport.urls), 1)

    def test_rejects_report_inn_mismatch(self) -> None:
        report = fixture("fns_girbo_bfo.json")
        report[0]["typeCorrections"][0]["correction"]["bfoOrganizationInfo"][  # type: ignore[index]
            "inn"
        ] = "0000000000"
        client = GirboClient(
            transport=FakeTransport(
                [
                    response(200, fixture("fns_girbo_search.json")),
                    response(200, report),
                ]
            ),
            now=lambda: NOW,
        )

        with self.assertRaisesRegex(GirboContractError, "INN mismatch"):
            client.fetch_latest_annual("9717068640")

    def test_retries_server_failure(self) -> None:
        sleeps: list[float] = []
        client = GirboClient(
            transport=FakeTransport(
                [
                    response(503, {}),
                    response(200, {"content": [], "totalElements": 0}),
                ]
            ),
            now=lambda: NOW,
            sleep=sleeps.append,
        )

        self.assertIsNone(client.fetch_latest_annual("7703104630"))
        self.assertEqual(sleeps, [0.25])


if __name__ == "__main__":
    unittest.main()
