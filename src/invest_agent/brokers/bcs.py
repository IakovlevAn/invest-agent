"""Minimal read-only client for the official BCS Trade API.

This module deliberately contains no order endpoint and no trade-token client id.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

AUTH_URL = "https://be.broker.ru/trade-api-keycloak/realms/tradeapi/protocol/openid-connect/token"
PORTFOLIO_URL = "https://be.broker.ru/trade-api-bff-portfolio/api/v1/portfolio"
READ_ONLY_CLIENT_ID = "trade-api-read"


class BcsApiError(RuntimeError):
    """A sanitized BCS API error that never embeds tokens or response bodies."""

    def __init__(self, operation: str, status: int, *, trace_id: str | None = None) -> None:
        self.operation = operation
        self.status = status
        self.trace_id = trace_id
        suffix = "" if trace_id is None else f" (trace_id={trace_id})"
        super().__init__(f"BCS {operation} failed with HTTP {status}{suffix}")


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
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


class HttpTransport(Protocol):
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout_seconds: float = 15.0,
    ) -> HttpResponse: ...


class UrllibTransport:
    """Small stdlib transport; injected in tests to keep them offline."""

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout_seconds: float = 15.0,
    ) -> HttpResponse:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return HttpResponse(
                    status=response.status,
                    body=response.read(),
                    headers=dict(response.headers.items()),
                )
        except urllib.error.HTTPError as error:
            return HttpResponse(
                status=error.code,
                body=error.read(),
                headers=dict(error.headers.items()),
            )


class BcsReadClient:
    """BCS client limited to token exchange and portfolio reads."""

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
            response = self._transport.request(
                method=method,
                url=url,
                headers=headers,
                body=body,
            )
            if response.status != 429:
                break
            if attempt + 1 < self._max_attempts:
                self._sleep(0.25 * (2**attempt))
        assert response is not None
        if not 200 <= response.status < 300:
            raise BcsApiError(operation, response.status, trace_id=self._trace_id(response.body))
        return response

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
