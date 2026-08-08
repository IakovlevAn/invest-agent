"""Isolated BCS trading client with no portfolio decision logic."""

from __future__ import annotations

import json
import math
import time
import urllib.parse
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from invest_agent.brokers.bcs import (
    AUTH_URL,
    BcsAccessToken,
    BcsApiError,
    BcsTokenPair,
)
from invest_agent.domain import OrderIntent, OrderType, Side
from invest_agent.http import HttpResponse, HttpTransport, UrllibTransport

TRADE_CLIENT_ID = "trade-api-write"
CREATE_ORDER_URL = "https://be.broker.ru/trade-api-bff-operations/api/v1/orders"
ORDER_STATUS_URL = CREATE_ORDER_URL
CANCEL_ORDER_URL = f"{CREATE_ORDER_URL}/cancel"
ORDER_STATUSES = frozenset({"0", "1", "2", "4", "5", "6", "8", "9", "10"})
EXECUTION_TYPES = frozenset(
    {"0", "1", "2", "4", "5", "6", "8", "9", "10", "11", "12", "13"}
)


class BcsOrderNotFound(BcsApiError):
    """The broker has no order for the supplied client identifier."""


@dataclass(frozen=True, slots=True)
class BcsOperationAck:
    client_order_id: str
    status: str


@dataclass(frozen=True, slots=True)
class BcsOrderState:
    client_order_id: str
    order_status: str
    execution_type: str
    order_quantity: int
    executed_quantity: int
    remained_quantity: int
    ticker: str
    class_code: str
    side: str
    order_type: str
    price: Decimal
    currency: str
    order_id: str
    transaction_time: datetime
    reject_reason: str | None

    @property
    def is_active(self) -> bool:
        return self.order_status in {"0", "1", "6", "9", "10"}

    @property
    def is_terminal(self) -> bool:
        return self.order_status in {"2", "4", "5", "8"}

    def validate_against(self, expected: OrderIntent) -> None:
        expected_side = "1" if expected.side is Side.BUY else "2"
        if (
            self.ticker != expected.ticker
            or self.class_code != expected.class_code
            or self.side != expected_side
            or self.order_type != "2"
            or self.order_quantity != expected.quantity_units
            or self.price != expected.limit_price
            or self.currency != expected.currency
        ):
            raise BcsApiError("order-status-mismatch", 409)


class BcsTradeClient:
    """Trading authority is confined to this client and explicit order methods."""

    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._transport = transport or UrllibTransport()
        self._now = now or (lambda: datetime.now(tz=UTC))
        self._sleep = sleep
        self._max_attempts = max_attempts

    def exchange_trade_refresh_token(self, refresh_token: str) -> BcsTokenPair:
        if not refresh_token:
            raise ValueError("refresh_token cannot be empty")
        body = urllib.parse.urlencode(
            {
                "client_id": TRADE_CLIENT_ID,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
        ).encode("ascii")
        response = self._request_with_retry(
            operation="trade-authorization",
            method="POST",
            url=AUTH_URL,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body=body,
        )
        payload = self._json_object(response, "trade-authorization")
        access_token = payload.get("access_token")
        rotated_refresh_token = payload.get("refresh_token")
        token_type = payload.get("token_type", "Bearer")
        access_lifetime = self._positive_seconds(payload.get("expires_in"))
        refresh_lifetime = self._positive_seconds(payload.get("refresh_expires_in"))
        if (
            not isinstance(access_token, str)
            or not access_token
            or not isinstance(rotated_refresh_token, str)
            or not rotated_refresh_token
            or not isinstance(token_type, str)
            or access_lifetime is None
            or refresh_lifetime is None
        ):
            raise BcsApiError("trade-authorization-contract", response.status)
        issued_at = self._now()
        return BcsTokenPair(
            access_token=BcsAccessToken(
                value=access_token,
                expires_at=issued_at + timedelta(seconds=access_lifetime),
                token_type=token_type,
            ),
            refresh_token=rotated_refresh_token,
            refresh_expires_at=issued_at + timedelta(seconds=refresh_lifetime),
        )

    def create_limit_order(
        self,
        access_token: BcsAccessToken,
        *,
        order: OrderIntent,
        client_order_id: str,
    ) -> BcsOperationAck:
        self._require_token(access_token, "create-order-token-expired")
        _validated_uuid(client_order_id)
        if order.order_type is not OrderType.LIMIT or order.limit_price is None:
            raise ValueError("BCS trade client accepts limit orders only")
        wire_price = float(order.limit_price)
        if Decimal(str(wire_price)) != order.limit_price:
            raise ValueError("limit price cannot be represented safely for BCS JSON")
        payload = {
            "clientOrderId": client_order_id,
            "side": "1" if order.side is Side.BUY else "2",
            "orderType": "2",
            "orderQuantity": order.quantity_units,
            "ticker": order.ticker,
            "classCode": order.class_code,
            "price": wire_price,
        }
        response = self._request_with_retry(
            operation="create-order",
            method="POST",
            url=CREATE_ORDER_URL,
            headers=self._authorized_headers(access_token, json_body=True),
            body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        )
        return self._parse_ack(response, "create-order", client_order_id)

    def get_order_status(
        self,
        access_token: BcsAccessToken,
        *,
        client_order_id: str,
    ) -> BcsOrderState:
        self._require_token(access_token, "order-status-token-expired")
        _validated_uuid(client_order_id)
        query = urllib.parse.urlencode(
            {"orderIdType": "1", "orderId": client_order_id}
        )
        response = self._request_with_retry(
            operation="order-status",
            method="GET",
            url=f"{ORDER_STATUS_URL}?{query}",
            headers=self._authorized_headers(access_token),
            not_found_is_order=True,
        )
        payload = self._json_object(response, "order-status")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise BcsApiError("order-status-contract", response.status)
        outer_ids = {
            value
            for key in ("clientOrderId", "originalClientOrderId")
            if isinstance((value := payload.get(key)), str) and value
        }
        if client_order_id not in outer_ids:
            raise BcsApiError("order-status-id-mismatch", response.status)
        state = BcsOrderState(
            client_order_id=client_order_id,
            order_status=self._required_choice(
                data, "orderStatus", ORDER_STATUSES, response.status
            ),
            execution_type=self._required_choice(
                data, "executionType", EXECUTION_TYPES, response.status
            ),
            order_quantity=self._required_int(data, "orderQuantity", response.status),
            executed_quantity=self._required_int(
                data, "executedQuantity", response.status
            ),
            remained_quantity=self._required_int(
                data, "remainedQuantity", response.status
            ),
            ticker=self._required_text(data, "ticker", response.status),
            class_code=self._required_text(data, "classCode", response.status),
            side=self._required_choice(data, "side", frozenset({"1", "2"}), response.status),
            order_type=self._required_choice(
                data, "orderType", frozenset({"1", "2"}), response.status
            ),
            price=self._required_decimal(data, "price", response.status),
            currency=self._required_text(data, "currency", response.status).upper(),
            order_id=self._required_text(data, "orderId", response.status),
            transaction_time=self._required_datetime(
                data, "transactionTime", response.status
            ),
            reject_reason=self._optional_text(data, "rejectReason", response.status),
        )
        if state.executed_quantity + state.remained_quantity != state.order_quantity:
            raise BcsApiError("order-status-quantity-contract", response.status)
        return state

    def cancel_order(
        self,
        access_token: BcsAccessToken,
        *,
        order_client_id: str,
        cancel_client_id: str,
    ) -> BcsOperationAck:
        self._require_token(access_token, "cancel-order-token-expired")
        _validated_uuid(order_client_id)
        _validated_uuid(cancel_client_id)
        payload = {
            "orderIdType": "1",
            "orderId": order_client_id,
            "clientOrderId": cancel_client_id,
        }
        response = self._request_with_retry(
            operation="cancel-order",
            method="POST",
            url=CANCEL_ORDER_URL,
            headers=self._authorized_headers(access_token, json_body=True),
            body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        )
        return self._parse_ack(response, "cancel-order", cancel_client_id)

    def _request_with_retry(
        self,
        *,
        operation: str,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None = None,
        not_found_is_order: bool = False,
    ) -> HttpResponse:
        response: HttpResponse | None = None
        for attempt in range(self._max_attempts):
            try:
                response = self._transport.request(
                    method=method,
                    url=url,
                    headers=headers,
                    body=body,
                )
            except OSError as error:
                if attempt + 1 >= self._max_attempts:
                    raise BcsApiError(operation, None) from error
                self._sleep(0.25 * (2**attempt))
                continue
            if response.status != 429 and not 500 <= response.status < 600:
                break
            if attempt + 1 < self._max_attempts:
                self._sleep(0.25 * (2**attempt))
        assert response is not None
        if not 200 <= response.status < 300:
            error_type = self._error_type(response.body)
            trace_id = self._trace_id(response.body)
            if not_found_is_order and (
                response.status == 404 or error_type == "NOT_FOUND"
            ):
                raise BcsOrderNotFound(operation, response.status, trace_id=trace_id)
            raise BcsApiError(operation, response.status, trace_id=trace_id)
        return response

    def _parse_ack(
        self,
        response: HttpResponse,
        operation: str,
        expected_client_id: str,
    ) -> BcsOperationAck:
        payload = self._json_object(response, operation)
        client_order_id = payload.get("clientOrderId")
        status = payload.get("status")
        if client_order_id != expected_client_id or not isinstance(status, str):
            raise BcsApiError(f"{operation}-contract", response.status)
        return BcsOperationAck(client_order_id=client_order_id, status=status)

    def _require_token(self, access_token: BcsAccessToken, operation: str) -> None:
        if access_token.expires_at <= self._now():
            raise BcsApiError(operation, 401)

    @staticmethod
    def _authorized_headers(
        access_token: BcsAccessToken,
        *,
        json_body: bool = False,
    ) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"{access_token.token_type} {access_token.value}",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    @staticmethod
    def _json_object(response: HttpResponse, operation: str) -> Mapping[str, Any]:
        try:
            payload = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise BcsApiError(f"{operation}-invalid-json", response.status) from error
        if not isinstance(payload, dict):
            raise BcsApiError(f"{operation}-invalid-contract", response.status)
        return payload

    @staticmethod
    def _required_text(
        payload: Mapping[str, Any], field: str, status: int
    ) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise BcsApiError("order-status-contract", status)
        return value.strip()

    @staticmethod
    def _optional_text(
        payload: Mapping[str, Any], field: str, status: int
    ) -> str | None:
        value = payload.get(field)
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise BcsApiError("order-status-contract", status)
        return value

    @staticmethod
    def _required_enum(
        payload: Mapping[str, Any], field: str, status: int
    ) -> str:
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise BcsApiError("order-status-contract", status)
        return str(value)

    @classmethod
    def _required_choice(
        cls,
        payload: Mapping[str, Any],
        field: str,
        choices: frozenset[str],
        status: int,
    ) -> str:
        value = cls._required_enum(payload, field, status)
        if value not in choices:
            raise BcsApiError("order-status-contract", status)
        return value

    @staticmethod
    def _required_int(
        payload: Mapping[str, Any], field: str, status: int
    ) -> int:
        value = payload.get(field)
        if isinstance(value, bool):
            raise BcsApiError("order-status-contract", status)
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError) as error:
            raise BcsApiError("order-status-contract", status) from error
        if parsed < 0 or parsed != parsed.to_integral_value():
            raise BcsApiError("order-status-contract", status)
        return int(parsed)

    @staticmethod
    def _required_decimal(
        payload: Mapping[str, Any], field: str, status: int
    ) -> Decimal:
        value = payload.get(field)
        if isinstance(value, bool):
            raise BcsApiError("order-status-contract", status)
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError) as error:
            raise BcsApiError("order-status-contract", status) from error
        if not parsed.is_finite() or parsed < 0:
            raise BcsApiError("order-status-contract", status)
        return parsed

    @staticmethod
    def _required_datetime(
        payload: Mapping[str, Any], field: str, status: int
    ) -> datetime:
        value = payload.get(field)
        if not isinstance(value, str):
            raise BcsApiError("order-status-contract", status)
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise BcsApiError("order-status-contract", status) from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise BcsApiError("order-status-contract", status)
        return parsed

    @staticmethod
    def _positive_seconds(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return None
        return seconds if math.isfinite(seconds) and seconds > 0 else None

    @staticmethod
    def _trace_id(body: bytes) -> str | None:
        payload = BcsTradeClient._safe_error_payload(body)
        trace_id = payload.get("traceId")
        return trace_id if isinstance(trace_id, str) else None

    @staticmethod
    def _error_type(body: bytes) -> str | None:
        payload = BcsTradeClient._safe_error_payload(body)
        error_type = payload.get("type")
        return error_type if isinstance(error_type, str) else None

    @staticmethod
    def _safe_error_payload(body: bytes) -> Mapping[str, Any]:
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}


def _validated_uuid(value: str) -> uuid.UUID:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise ValueError("BCS client order identifier must be a UUID") from error
    if str(parsed) != value:
        raise ValueError("BCS client order identifier must be canonical lowercase UUID")
    return parsed
