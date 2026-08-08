from __future__ import annotations

import json
import unittest
import urllib.parse
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from invest_agent.brokers.bcs import AUTH_URL, BcsAccessToken, BcsApiError
from invest_agent.brokers.bcs_trade import (
    CANCEL_ORDER_URL,
    CREATE_ORDER_URL,
    ORDER_STATUS_URL,
    TRADE_CLIENT_ID,
    BcsTradeClient,
)
from invest_agent.domain import InstrumentType, OrderIntent, OrderType, Side
from invest_agent.http import HttpResponse

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
ORDER_CLIENT_ID = "11111111-1111-4111-8111-111111111111"
CANCEL_CLIENT_ID = "22222222-2222-4222-8222-222222222222"


class FakeTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def request(self, **kwargs: object) -> HttpResponse:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def json_response(status: int, payload: object) -> HttpResponse:
    return HttpResponse(status=status, body=json.dumps(payload).encode("utf-8"))


def order() -> OrderIntent:
    return OrderIntent(
        instrument_uid="BCS:TQCB:RU000A000001",
        ticker="RU000A000001",
        class_code="TQCB",
        instrument_type=InstrumentType.BOND,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        lots=5,
        limit_price=Decimal("100.10"),
        currency="RUB",
        lot_size=2,
        price_step=Decimal("0.01"),
        estimated_cash_rub=Decimal("10010"),
        quote_observed_at=NOW - timedelta(seconds=5),
        order_valid_until=NOW + timedelta(hours=1),
    )


class BcsTradeClientTests(unittest.TestCase):
    def test_exchanges_only_trade_token_without_order_request(self) -> None:
        transport = FakeTransport(
            [
                json_response(
                    200,
                    {
                        "access_token": "trade-access-secret",
                        "expires_in": 3600,
                        "refresh_expires_in": 7200,
                        "refresh_token": "rotated-trade-refresh-secret",
                        "token_type": "Bearer",
                    },
                )
            ]
        )

        pair = BcsTradeClient(transport=transport, now=lambda: NOW).exchange_trade_refresh_token(
            "trade-refresh-secret"
        )

        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0]["url"], AUTH_URL)
        form = urllib.parse.parse_qs(transport.calls[0]["body"].decode("ascii"))
        self.assertEqual(form["client_id"], [TRADE_CLIENT_ID])
        self.assertNotIn("trade-access-secret", repr(pair))

    def test_creates_exact_limit_order_in_security_units(self) -> None:
        transport = FakeTransport(
            [json_response(200, {"clientOrderId": ORDER_CLIENT_ID, "status": "OK"})]
        )
        client = BcsTradeClient(transport=transport, now=lambda: NOW)

        ack = client.create_limit_order(
            BcsAccessToken("access-secret", NOW + timedelta(hours=1)),
            order=order(),
            client_order_id=ORDER_CLIENT_ID,
        )

        payload = json.loads(transport.calls[0]["body"])
        self.assertEqual(transport.calls[0]["url"], CREATE_ORDER_URL)
        self.assertEqual(payload["orderType"], "2")
        self.assertEqual(payload["side"], "1")
        self.assertEqual(payload["orderQuantity"], 10)
        self.assertEqual(payload["price"], 100.1)
        self.assertEqual(ack.client_order_id, ORDER_CLIENT_ID)

    def test_has_no_market_order_submission_path(self) -> None:
        client = BcsTradeClient(transport=FakeTransport([]), now=lambda: NOW)
        market_order = replace(order(), order_type=OrderType.MARKET, limit_price=None)

        with self.assertRaisesRegex(ValueError, "limit orders only"):
            client.create_limit_order(
                BcsAccessToken("access-secret", NOW + timedelta(hours=1)),
                order=market_order,
                client_order_id=ORDER_CLIENT_ID,
            )

    def test_reads_status_and_validates_exact_order(self) -> None:
        transport = FakeTransport(
            [
                json_response(
                    200,
                    {
                        "clientOrderId": ORDER_CLIENT_ID,
                        "originalClientOrderId": "",
                        "data": {
                            "orderStatus": "1",
                            "executionType": "12",
                            "orderQuantity": 10,
                            "executedQuantity": 4,
                            "remainedQuantity": 6,
                            "ticker": "RU000A000001",
                            "classCode": "TQCB",
                            "side": "1",
                            "orderType": "2",
                            "price": 100.1,
                            "currency": "RUB",
                            "orderId": "20260808-TQCB-1",
                            "transactionTime": "2026-08-08T12:00:01Z",
                            "rejectReason": "",
                        },
                    },
                )
            ]
        )
        client = BcsTradeClient(transport=transport, now=lambda: NOW)

        state = client.get_order_status(
            BcsAccessToken("access-secret", NOW + timedelta(hours=1)),
            client_order_id=ORDER_CLIENT_ID,
        )
        state.validate_against(order())

        self.assertTrue(state.is_active)
        self.assertEqual(state.executed_quantity, 4)
        self.assertTrue(str(transport.calls[0]["url"]).startswith(ORDER_STATUS_URL + "?"))

    def test_cancels_by_original_client_id_with_new_idempotency_id(self) -> None:
        transport = FakeTransport(
            [json_response(200, {"clientOrderId": CANCEL_CLIENT_ID, "status": "OK"})]
        )
        client = BcsTradeClient(transport=transport, now=lambda: NOW)

        client.cancel_order(
            BcsAccessToken("access-secret", NOW + timedelta(hours=1)),
            order_client_id=ORDER_CLIENT_ID,
            cancel_client_id=CANCEL_CLIENT_ID,
        )

        payload = json.loads(transport.calls[0]["body"])
        self.assertEqual(transport.calls[0]["url"], CANCEL_ORDER_URL)
        self.assertEqual(
            payload,
            {
                "orderIdType": "1",
                "orderId": ORDER_CLIENT_ID,
                "clientOrderId": CANCEL_CLIENT_ID,
            },
        )
        uuid.UUID(payload["clientOrderId"])

    def test_rejects_unknown_broker_order_status(self) -> None:
        transport = FakeTransport(
            [
                json_response(
                    200,
                    {
                        "clientOrderId": ORDER_CLIENT_ID,
                        "data": {
                            "orderStatus": "3",
                            "executionType": "12",
                            "orderQuantity": 10,
                            "executedQuantity": 0,
                            "remainedQuantity": 10,
                            "ticker": "RU000A000001",
                            "classCode": "TQCB",
                            "side": "1",
                            "orderType": "2",
                            "price": 100.1,
                            "currency": "RUB",
                            "orderId": "broker-order",
                            "transactionTime": "2026-08-08T12:00:01Z",
                            "rejectReason": "",
                        },
                    },
                )
            ]
        )

        with self.assertRaises(BcsApiError):
            BcsTradeClient(transport=transport, now=lambda: NOW).get_order_status(
                BcsAccessToken("access-secret", NOW + timedelta(hours=1)),
                client_order_id=ORDER_CLIENT_ID,
            )


if __name__ == "__main__":
    unittest.main()
