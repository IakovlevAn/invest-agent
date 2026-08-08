from __future__ import annotations

import json
import unittest
import urllib.parse
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from invest_agent.brokers.bcs import (
    AUTH_URL,
    PORTFOLIO_URL,
    BcsAccessToken,
    BcsApiError,
    BcsReadClient,
    HttpResponse,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


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
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.responses.pop(0)


def json_response(status: int, payload: object) -> HttpResponse:
    return HttpResponse(status=status, body=json.dumps(payload).encode("utf-8"))


class BcsReadClientTests(unittest.TestCase):
    def test_exchanges_only_read_token_and_redacts_it(self) -> None:
        transport = FakeTransport(
            [
                json_response(
                    200,
                    {
                        "access_token": "access-secret",
                        "expires_in": "86400",
                        "refresh_token": "rotated-refresh-secret",
                        "refresh_expires_in": "7776000",
                    },
                )
            ]
        )
        client = BcsReadClient(transport=transport, now=lambda: NOW)

        pair = client.exchange_read_only_refresh_token("refresh-secret")

        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], AUTH_URL)
        form = urllib.parse.parse_qs(call["body"].decode("ascii"))  # type: ignore[union-attr]
        self.assertEqual(form["client_id"], ["trade-api-read"])
        self.assertEqual(form["grant_type"], ["refresh_token"])
        self.assertEqual(form["refresh_token"], ["refresh-secret"])
        self.assertEqual(pair.access_token.expires_at, NOW + timedelta(days=1))
        self.assertEqual(pair.refresh_token, "rotated-refresh-secret")
        self.assertEqual(pair.refresh_expires_at, NOW + timedelta(days=90))
        self.assertNotIn("access-secret", repr(pair))
        self.assertNotIn("rotated-refresh-secret", repr(pair))

    def test_rejects_non_finite_token_lifetime(self) -> None:
        transport = FakeTransport(
            [
                json_response(
                    200,
                    {
                        "access_token": "access-secret",
                        "expires_in": "NaN",
                        "refresh_token": "rotated-refresh-secret",
                        "refresh_expires_in": "7776000",
                    },
                )
            ]
        )
        client = BcsReadClient(transport=transport, now=lambda: NOW)

        with self.assertRaisesRegex(BcsApiError, "authorization-contract"):
            client.exchange_read_only_refresh_token("refresh-secret")

    def test_fetches_portfolio_with_bearer_token(self) -> None:
        payload = {"agreementData": {"isIia": True}, "positions": []}
        transport = FakeTransport([json_response(200, payload)])
        client = BcsReadClient(transport=transport, now=lambda: NOW)
        token = BcsAccessToken("access-secret", NOW + timedelta(hours=1))

        result = client.fetch_raw_portfolio(token)

        self.assertEqual(result, payload)
        call = transport.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], PORTFOLIO_URL)
        self.assertEqual(call["headers"]["Authorization"], "Bearer access-secret")  # type: ignore[index]

    def test_wraps_live_top_level_position_array(self) -> None:
        transport = FakeTransport([json_response(200, [{"ticker": "TEST"}])])
        client = BcsReadClient(transport=transport, now=lambda: NOW)
        token = BcsAccessToken("access-secret", NOW + timedelta(hours=1))

        result = client.fetch_raw_portfolio(token)

        self.assertEqual(result, {"positions": [{"ticker": "TEST"}]})

    def test_retries_429_with_exponential_backoff(self) -> None:
        sleeps: list[float] = []
        transport = FakeTransport(
            [
                json_response(429, {"traceId": "one"}),
                json_response(429, {"traceId": "two"}),
                json_response(200, {"positions": []}),
            ]
        )
        client = BcsReadClient(
            transport=transport,
            now=lambda: NOW,
            sleep=sleeps.append,
            max_attempts=3,
        )
        token = BcsAccessToken("access-secret", NOW + timedelta(hours=1))

        self.assertEqual(client.fetch_raw_portfolio(token), {"positions": []})
        self.assertEqual(sleeps, [0.25, 0.5])
        self.assertEqual(len(transport.calls), 3)

    def test_error_is_sanitized_and_keeps_trace_id(self) -> None:
        transport = FakeTransport(
            [json_response(401, {"traceId": "safe-trace", "access_token": "must-not-leak"})]
        )
        client = BcsReadClient(transport=transport, now=lambda: NOW)
        token = BcsAccessToken("access-secret", NOW + timedelta(hours=1))

        with self.assertRaises(BcsApiError) as context:
            client.fetch_raw_portfolio(token)

        message = str(context.exception)
        self.assertIn("safe-trace", message)
        self.assertNotIn("must-not-leak", message)
        self.assertNotIn("access-secret", message)

    def test_expired_token_fails_before_network(self) -> None:
        transport = FakeTransport([])
        client = BcsReadClient(transport=transport, now=lambda: NOW)
        token = BcsAccessToken("access-secret", NOW - timedelta(seconds=1))

        with self.assertRaisesRegex(BcsApiError, "HTTP 401"):
            client.fetch_raw_portfolio(token)
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
