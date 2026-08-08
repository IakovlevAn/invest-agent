"""Minimal read-only client for the official BCS Trade API.

This module deliberately contains no order endpoint and no trade-token client id.
"""

from __future__ import annotations

import json
import math
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from invest_agent.http import HttpResponse, HttpTransport, UrllibTransport

AUTH_URL = "https://be.broker.ru/trade-api-keycloak/realms/tradeapi/protocol/openid-connect/token"
PORTFOLIO_URL = "https://be.broker.ru/trade-api-bff-portfolio/api/v1/portfolio"
INSTRUMENTS_BY_ISINS_URL = (
    "https://be.broker.ru/trade-api-information-service/api/v1/instruments/by-isins"
)
QUOTES_URL = "https://be.broker.ru/trade-api-market-data-connector/api/v1/quotes"
ORDER_BOOK_URL = "https://be.broker.ru/trade-api-market-data-connector/api/v1/order-book"
READ_ONLY_CLIENT_ID = "trade-api-read"


class BcsApiError(RuntimeError):
    """A sanitized BCS API error that never embeds tokens or response bodies."""

    def __init__(
        self,
        operation: str,
        status: int | None,
        *,
        trace_id: str | None = None,
    ) -> None:
        self.operation = operation
        self.status = status
        self.trace_id = trace_id
        suffix = "" if trace_id is None else f" (trace_id={trace_id})"
        status_text = "network error" if status is None else f"HTTP {status}"
        super().__init__(f"BCS {operation} failed with {status_text}{suffix}")


@dataclass(frozen=True, slots=True, repr=False)
class BcsAccessToken:
    value: str
    expires_at: datetime
    token_type: str = "Bearer"

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("access token cannot be empty")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")

    def __repr__(self) -> str:
        return (
            "BcsAccessToken(value=<redacted>, "
            f"expires_at={self.expires_at.isoformat()}, token_type={self.token_type!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class BcsTokenPair:
    access_token: BcsAccessToken
    refresh_token: str
    refresh_expires_at: datetime

    def __post_init__(self) -> None:
        if not self.refresh_token:
            raise ValueError("refresh token cannot be empty")
        if self.refresh_expires_at.tzinfo is None or self.refresh_expires_at.utcoffset() is None:
            raise ValueError("refresh_expires_at must be timezone-aware")

    def __repr__(self) -> str:
        return (
            "BcsTokenPair(access_token=<redacted>, refresh_token=<redacted>, "
            f"refresh_expires_at={self.refresh_expires_at.isoformat()})"
        )


@dataclass(frozen=True, slots=True)
class BcsBoard:
    class_code: str
    exchange: str


@dataclass(frozen=True, slots=True)
class BcsInstrument:
    ticker: str
    isin: str
    display_name: str
    instrument_type: str
    boards: tuple[BcsBoard, ...]
    primary_board: str
    trading_currency: str
    settlement_currency: str
    face_value: Decimal
    lot_size: int
    minimum_step: Decimal
    accrued_interest: Decimal
    scale: int
    is_blocked: bool
    is_qualified_only: bool
    available_for_unqualified: bool
    coupon_type_name: str | None

    @property
    def is_ruble_bond(self) -> bool:
        return (
            self.instrument_type == "BONDS"
            and self.trading_currency == "RUB"
            and self.settlement_currency == "RUB"
        )


@dataclass(frozen=True, slots=True)
class BcsQuote:
    ticker: str
    class_code: str
    observed_at: datetime
    security_trading_status: int
    currency: str
    bid: Decimal | None
    offer: Decimal | None
    last: Decimal | None
    bid_yield: Decimal | None
    offer_yield: Decimal | None

    @property
    def trading_is_open(self) -> bool:
        return self.security_trading_status == 17


@dataclass(frozen=True, slots=True)
class BcsOrderBookLevel:
    price: Decimal
    quantity: int


@dataclass(frozen=True, slots=True)
class BcsOrderBook:
    ticker: str
    class_code: str
    observed_at: datetime
    bids: tuple[BcsOrderBookLevel, ...]
    asks: tuple[BcsOrderBookLevel, ...]

    @property
    def best_bid(self) -> Decimal | None:
        return None if not self.bids else max(level.price for level in self.bids)

    @property
    def best_offer(self) -> Decimal | None:
        return None if not self.asks else min(level.price for level in self.asks)


class BcsReadClient:
    """BCS client limited to authenticated read-only portfolio and market data."""

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

    def exchange_read_only_refresh_token(self, refresh_token: str) -> BcsTokenPair:
        """Exchange a read-only refresh token for a short-lived access token."""
        if not refresh_token:
            raise ValueError("refresh_token cannot be empty")
        body = urllib.parse.urlencode(
            {
                "client_id": READ_ONLY_CLIENT_ID,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
        ).encode("ascii")
        response = self._request_with_retry(
            operation="authorization",
            method="POST",
            url=AUTH_URL,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body=body,
        )
        payload = self._json_object(response, operation="authorization")
        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        rotated_refresh_token = payload.get("refresh_token")
        refresh_expires_in = payload.get("refresh_expires_in")
        token_type = payload.get("token_type", "Bearer")
        if not isinstance(access_token, str) or not access_token:
            raise BcsApiError("authorization-contract", response.status)
        access_lifetime = self._positive_seconds(expires_in)
        if access_lifetime is None:
            raise BcsApiError("authorization-contract", response.status)
        if not isinstance(rotated_refresh_token, str) or not rotated_refresh_token:
            raise BcsApiError("authorization-contract", response.status)
        refresh_lifetime = self._positive_seconds(refresh_expires_in)
        if refresh_lifetime is None:
            raise BcsApiError("authorization-contract", response.status)
        if not isinstance(token_type, str):
            raise BcsApiError("authorization-contract", response.status)
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

    def fetch_raw_portfolio(self, access_token: BcsAccessToken) -> Mapping[str, Any]:
        """Fetch the official portfolio payload without guessing its schema."""
        if access_token.expires_at <= self._now():
            raise BcsApiError("portfolio-token-expired", 401)
        response = self._request_with_retry(
            operation="portfolio",
            method="GET",
            url=PORTFOLIO_URL,
            headers={
                "Accept": "application/json",
                "Authorization": f"{access_token.token_type} {access_token.value}",
            },
        )
        payload = self._json_value(response, operation="portfolio")
        if isinstance(payload, list):
            return {"positions": payload}
        if isinstance(payload, dict):
            return payload
        raise BcsApiError("portfolio-invalid-contract", response.status)

    def fetch_instruments_by_isins(
        self,
        access_token: BcsAccessToken,
        isins: tuple[str, ...],
    ) -> tuple[BcsInstrument, ...]:
        """Resolve exact instruments through the authenticated BCS catalogue."""
        self._require_live_token(access_token, "instruments-token-expired")
        normalized = tuple(dict.fromkeys(isin.strip().upper() for isin in isins if isin.strip()))
        if not normalized or len(normalized) > 100:
            raise ValueError("between one and 100 ISINs are required")
        response = self._request_with_retry(
            operation="instruments-by-isins",
            method="POST",
            url=INSTRUMENTS_BY_ISINS_URL,
            headers=self._authorized_headers(access_token, json_body=True),
            body=json.dumps({"isins": list(normalized)}, separators=(",", ":")).encode("utf-8"),
        )
        payload = self._json_value(response, operation="instruments-by-isins")
        if not isinstance(payload, list):
            raise BcsApiError("instruments-by-isins-invalid-contract", response.status)
        instruments = tuple(
            self._parse_instrument(item, response.status, index)
            for index, item in enumerate(payload)
        )
        if len({item.isin for item in instruments}) != len(instruments):
            raise BcsApiError("instruments-by-isins-duplicate-contract", response.status)
        return instruments

    def fetch_quotes(
        self,
        access_token: BcsAccessToken,
        instruments: tuple[tuple[str, str], ...],
    ) -> tuple[BcsQuote, ...]:
        """Fetch authenticated BCS quotes for exact ticker/class-code pairs."""
        self._require_live_token(access_token, "quotes-token-expired")
        normalized = tuple(
            dict.fromkeys(
                (ticker.strip(), class_code.strip())
                for ticker, class_code in instruments
                if ticker.strip() and class_code.strip()
            )
        )
        if not normalized or len(normalized) > 100:
            raise ValueError("between one and 100 BCS instruments are required")
        body = {
            "instruments": [
                {"ticker": ticker, "classCode": class_code}
                for ticker, class_code in normalized
            ]
        }
        response = self._request_with_retry(
            operation="quotes",
            method="POST",
            url=QUOTES_URL,
            headers=self._authorized_headers(access_token, json_body=True),
            body=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        )
        payload = self._json_object(response, operation="quotes")
        records = payload.get("records")
        if not isinstance(records, list):
            raise BcsApiError("quotes-invalid-contract", response.status)
        return tuple(
            self._parse_quote(item, response.status, index)
            for index, item in enumerate(records)
        )

    def fetch_order_book(
        self,
        access_token: BcsAccessToken,
        *,
        ticker: str,
        class_code: str,
        depth: int = 20,
    ) -> BcsOrderBook:
        """Fetch the current authenticated BCS order book for one instrument."""
        self._require_live_token(access_token, "order-book-token-expired")
        if not ticker.strip() or not class_code.strip():
            raise ValueError("ticker and class_code are required")
        if not 1 <= depth <= 20:
            raise ValueError("depth must be between one and 20")
        query = urllib.parse.urlencode(
            {"ticker": ticker.strip(), "classCode": class_code.strip(), "depth": depth}
        )
        response = self._request_with_retry(
            operation="order-book",
            method="GET",
            url=f"{ORDER_BOOK_URL}?{query}",
            headers=self._authorized_headers(access_token),
        )
        payload = self._json_object(response, operation="order-book")
        return self._parse_order_book(payload, response.status)

    def _request_with_retry(
        self,
        *,
        operation: str,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None = None,
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
            raise BcsApiError(operation, response.status, trace_id=self._trace_id(response.body))
        return response

    def _require_live_token(self, access_token: BcsAccessToken, operation: str) -> None:
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

    @classmethod
    def _parse_instrument(
        cls,
        payload: Any,
        status: int,
        index: int,
    ) -> BcsInstrument:
        operation = f"instruments-by-isins[{index}]-contract"
        if not isinstance(payload, dict):
            raise BcsApiError(operation, status)
        boards_payload = payload.get("boards")
        if not isinstance(boards_payload, list):
            raise BcsApiError(operation, status)
        boards: list[BcsBoard] = []
        for board in boards_payload:
            if not isinstance(board, dict):
                raise BcsApiError(operation, status)
            boards.append(
                BcsBoard(
                    class_code=cls._required_text(board, "classCode", operation, status),
                    exchange=cls._required_text(board, "exchange", operation, status),
                )
            )
        lot_size_decimal = cls._required_decimal(payload, "lotSize", operation, status)
        if lot_size_decimal != lot_size_decimal.to_integral_value() or lot_size_decimal <= 0:
            raise BcsApiError(operation, status)
        scale = payload.get("scale")
        if isinstance(scale, bool) or not isinstance(scale, int) or scale < 0:
            raise BcsApiError(operation, status)
        primary_board = cls._required_text(payload, "primaryBoard", operation, status)
        if primary_board not in {board.class_code for board in boards}:
            raise BcsApiError(operation, status)
        coupon_type = payload.get("couponTypeName")
        if coupon_type is not None and not isinstance(coupon_type, str):
            raise BcsApiError(operation, status)
        return BcsInstrument(
            ticker=cls._required_text(payload, "ticker", operation, status),
            isin=cls._required_text(payload, "isin", operation, status).upper(),
            display_name=cls._required_text(payload, "displayName", operation, status),
            instrument_type=cls._required_text(
                payload, "instrumentType", operation, status
            ).upper(),
            boards=tuple(boards),
            primary_board=primary_board,
            trading_currency=cls._required_text(
                payload, "tradingCurrency", operation, status
            ).upper(),
            settlement_currency=cls._required_text(
                payload, "settlementCurrency", operation, status
            ).upper(),
            face_value=cls._required_decimal(payload, "faceValue", operation, status),
            lot_size=int(lot_size_decimal),
            minimum_step=cls._required_decimal(payload, "minimumStep", operation, status),
            accrued_interest=cls._required_decimal(payload, "accruedInt", operation, status),
            scale=scale,
            is_blocked=cls._required_bool(payload, "isBlocked", operation, status),
            is_qualified_only=cls._required_bool(
                payload, "isQualifiedOnly", operation, status
            ),
            available_for_unqualified=cls._required_bool(
                payload, "availableForUnqualified", operation, status
            ),
            coupon_type_name=coupon_type.strip() if coupon_type else None,
        )

    @classmethod
    def _parse_quote(cls, payload: Any, status: int, index: int) -> BcsQuote:
        operation = f"quotes[{index}]-contract"
        if not isinstance(payload, dict):
            raise BcsApiError(operation, status)
        trading_status = payload.get("securityTradingStatus")
        if isinstance(trading_status, bool) or not isinstance(trading_status, int):
            raise BcsApiError(operation, status)
        return BcsQuote(
            ticker=cls._required_text(payload, "ticker", operation, status),
            class_code=cls._required_text(payload, "classCode", operation, status),
            observed_at=cls._required_datetime(payload, "dateTime", operation, status),
            security_trading_status=trading_status,
            currency=cls._required_text(payload, "currency", operation, status).upper(),
            bid=cls._optional_decimal(payload, "bid", operation, status),
            offer=cls._optional_decimal(payload, "offer", operation, status),
            last=cls._optional_decimal(payload, "last", operation, status),
            bid_yield=cls._optional_decimal(payload, "bidYield", operation, status),
            offer_yield=cls._optional_decimal(payload, "offerYield", operation, status),
        )

    @classmethod
    def _parse_order_book(cls, payload: Mapping[str, Any], status: int) -> BcsOrderBook:
        operation = "order-book-contract"
        bids = cls._parse_levels(payload.get("bids"), "bids", operation, status)
        asks = cls._parse_levels(payload.get("asks"), "asks", operation, status)
        return BcsOrderBook(
            ticker=cls._required_text(payload, "ticker", operation, status),
            class_code=cls._required_text(payload, "classCode", operation, status),
            observed_at=cls._required_datetime(payload, "dateTime", operation, status),
            bids=bids,
            asks=asks,
        )

    @classmethod
    def _parse_levels(
        cls,
        payload: Any,
        field: str,
        operation: str,
        status: int,
    ) -> tuple[BcsOrderBookLevel, ...]:
        if not isinstance(payload, list):
            raise BcsApiError(operation, status)
        levels: list[BcsOrderBookLevel] = []
        for index, item in enumerate(payload):
            if not isinstance(item, dict):
                raise BcsApiError(operation, status)
            price = cls._required_decimal(item, "price", operation, status)
            quantity = item.get("quantity")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
                raise BcsApiError(f"{operation}-{field}[{index}]", status)
            if price <= 0:
                raise BcsApiError(f"{operation}-{field}[{index}]", status)
            levels.append(BcsOrderBookLevel(price=price, quantity=quantity))
        return tuple(levels)

    @staticmethod
    def _required_text(
        payload: Mapping[str, Any],
        field: str,
        operation: str,
        status: int,
    ) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise BcsApiError(operation, status)
        return value.strip()

    @staticmethod
    def _required_bool(
        payload: Mapping[str, Any],
        field: str,
        operation: str,
        status: int,
    ) -> bool:
        value = payload.get(field)
        if not isinstance(value, bool):
            raise BcsApiError(operation, status)
        return value

    @classmethod
    def _required_decimal(
        cls,
        payload: Mapping[str, Any],
        field: str,
        operation: str,
        status: int,
    ) -> Decimal:
        value = cls._optional_decimal(payload, field, operation, status)
        if value is None or value < 0:
            raise BcsApiError(operation, status)
        return value

    @staticmethod
    def _optional_decimal(
        payload: Mapping[str, Any],
        field: str,
        operation: str,
        status: int,
    ) -> Decimal | None:
        value = payload.get(field)
        if value is None:
            return None
        if isinstance(value, bool):
            raise BcsApiError(operation, status)
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError) as error:
            raise BcsApiError(operation, status) from error
        if not result.is_finite():
            raise BcsApiError(operation, status)
        return result

    @staticmethod
    def _required_datetime(
        payload: Mapping[str, Any],
        field: str,
        operation: str,
        status: int,
    ) -> datetime:
        value = payload.get(field)
        if not isinstance(value, str):
            raise BcsApiError(operation, status)
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise BcsApiError(operation, status) from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise BcsApiError(operation, status)
        return parsed

    @staticmethod
    def _json_object(response: HttpResponse, *, operation: str) -> Mapping[str, Any]:
        payload = BcsReadClient._json_value(response, operation=operation)
        if not isinstance(payload, dict):
            raise BcsApiError(f"{operation}-invalid-contract", response.status)
        return payload

    @staticmethod
    def _json_value(response: HttpResponse, *, operation: str) -> Any:
        try:
            payload = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise BcsApiError(f"{operation}-invalid-json", response.status) from error
        return payload

    @staticmethod
    def _trace_id(body: bytes) -> str | None:
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if isinstance(payload, dict) and isinstance(payload.get("traceId"), str):
            return payload["traceId"]
        return None

    @staticmethod
    def _positive_seconds(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(seconds) or seconds <= 0:
            return None
        return seconds
