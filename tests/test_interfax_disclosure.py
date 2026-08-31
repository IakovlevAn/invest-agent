from __future__ import annotations

import json
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from invest_agent.disclosure.interfax import (
    CLIENT_USER_AGENT,
    DisclosureContractError,
    EDisclosureClient,
)
from invest_agent.http import HttpResponse

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 8, 8, 15, 30, tzinfo=UTC)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def response(status: int, body: bytes | dict[str, Any]) -> HttpResponse:
    encoded = json.dumps(body).encode() if isinstance(body, dict) else body
    return HttpResponse(status=status, body=encoded)


class FakeTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def request(self, **kwargs: Any) -> HttpResponse:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected request")
        return self.responses.pop(0)


def success_responses() -> list[HttpResponse]:
    return [
        response(200, fixture("edisclosure_search.json")),
        response(200, fixture("edisclosure_company.html")),
        response(200, fixture("edisclosure_consolidated.html")),
        response(200, fixture("edisclosure_emission.html")),
    ]


class EDisclosureClientTests(unittest.TestCase):
    def test_indexes_exact_company_and_document_metadata(self) -> None:
        transport = FakeTransport(success_responses())
        client = EDisclosureClient(transport=transport, now=lambda: NOW)

        result = client.fetch_issuer_documents("9717068640")

        assert result is not None
        self.assertEqual(result.company.company_id, 38662)
        self.assertEqual(result.company.inn, "9717068640")
        self.assertEqual(result.consolidated[0].period, "2025")
        self.assertEqual(result.consolidated[0].published_at, date(2026, 3, 16))
        self.assertEqual(result.emission[1].registration_number, "4B02-04-00075-L-001P")
        self.assertEqual(len(transport.calls), 4)
        self.assertEqual(transport.calls[0]["method"], "POST")
        self.assertIn(b"9717068640", transport.calls[0]["body"])
        self.assertEqual(transport.calls[0]["headers"]["User-Agent"], CLIENT_USER_AGENT)

    def test_verified_empty_exact_search_returns_none(self) -> None:
        payload = {
            "foundCompaniesList": [],
            "pagingInfo": {"totalItems": 0},
            "allFoundCompanies": 0,
        }
        client = EDisclosureClient(transport=FakeTransport([response(200, payload)]))

        self.assertIsNone(client.fetch_issuer_documents("9717068640"))

    def test_rejects_company_card_inn_mismatch(self) -> None:
        bad_card = b"<table><tr><td>\xd0\x98\xd0\x9d\xd0\x9d</td><td>0000000000</td></tr></table>"
        transport = FakeTransport(
            [response(200, fixture("edisclosure_search.json")), response(200, bad_card)]
        )
        client = EDisclosureClient(transport=transport)

        with self.assertRaisesRegex(DisclosureContractError, "INN mismatch"):
            client.fetch_issuer_documents("9717068640")

    def test_rejects_untrusted_document_url(self) -> None:
        bad_files = fixture("edisclosure_consolidated.html").replace(
            b"https://www.e-disclosure.ru/portal/FileLoad.ashx",
            b"https://example.com/portal/FileLoad.ashx",
        )
        transport = FakeTransport(
            [
                response(200, fixture("edisclosure_search.json")),
                response(200, fixture("edisclosure_company.html")),
                response(200, bad_files),
            ]
        )
        client = EDisclosureClient(transport=transport)

        with self.assertRaisesRegex(DisclosureContractError, "document URL"):
            client.fetch_issuer_documents("9717068640")

    def test_retries_server_failure(self) -> None:
        empty = {
            "foundCompaniesList": [],
            "pagingInfo": {"totalItems": 0},
            "allFoundCompanies": 0,
        }
        transport = FakeTransport([response(503, b""), response(200, empty)])
        sleeps: list[float] = []
        client = EDisclosureClient(transport=transport, sleep=sleeps.append)

        self.assertIsNone(client.fetch_issuer_documents("9717068640"))
        self.assertEqual(sleeps, [0.25])


if __name__ == "__main__":
    unittest.main()
