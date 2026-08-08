"""Immutable domain objects shared by analytics and the future executor."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class InstrumentType(StrEnum):
    BOND = "BOND"
    STOCK = "STOCK"
    FUND = "FUND"
    CASH = "CASH"
    FOREIGN_STOCK = "FOREIGN_STOCK"
    FUTURE = "FUTURE"
    OPTION = "OPTION"
    CURRENCY = "CURRENCY"
    METAL = "METAL"
    UNKNOWN = "UNKNOWN"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Position:
    instrument_uid: str
    ticker: str
    class_code: str
    instrument_type: InstrumentType
    quantity: Decimal
    market_price: Decimal
    market_value_rub: Decimal
    tradable: bool
    blocked_reason: str | None = None
    display_name: str = ""
    currency: str = "RUB"
    locked_quantity: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if not self.instrument_uid.strip():
            raise ValueError("instrument_uid is required")
        if self.quantity < 0:
            raise ValueError("quantity cannot be negative")
        if self.market_price < 0 or self.market_value_rub < 0:
            raise ValueError("market values cannot be negative")
        if self.locked_quantity < 0:
            raise ValueError("locked_quantity cannot be negative")
        if self.locked_quantity > self.quantity:
            raise ValueError("locked_quantity cannot exceed quantity")
        if not self.tradable and not self.blocked_reason:
            raise ValueError("blocked_reason is required for a non-tradable position")

    @property
    def available_quantity(self) -> Decimal:
        return self.quantity - self.locked_quantity

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "instrument_uid": self.instrument_uid,
            "ticker": self.ticker,
            "class_code": self.class_code,
            "instrument_type": self.instrument_type.value,
            "quantity": _decimal_text(self.quantity),
            "market_price": _decimal_text(self.market_price),
            "market_value_rub": _decimal_text(self.market_value_rub),
            "tradable": self.tradable,
            "blocked_reason": self.blocked_reason,
            "display_name": self.display_name,
            "currency": self.currency,
            "locked_quantity": _decimal_text(self.locked_quantity),
        }


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    account_ref: str
    is_iis: bool
    as_of: datetime
    cash_rub: Decimal
    positions: tuple[Position, ...]

    def __post_init__(self) -> None:
        _require_aware(self.as_of, "as_of")
        if self.cash_rub < 0:
            raise ValueError("cash_rub cannot be negative")
        if not self.account_ref.strip():
            raise ValueError("account_ref is required")

    @property
    def total_value_rub(self) -> Decimal:
        return self.cash_rub + sum(
            (position.market_value_rub for position in self.positions), start=Decimal("0")
        )

    @property
    def digest(self) -> str:
        return _digest(
            {
                "account_ref": self.account_ref,
                "is_iis": self.is_iis,
                "as_of": self.as_of.isoformat(),
                "cash_rub": _decimal_text(self.cash_rub),
                "positions": [position.canonical_payload() for position in self.positions],
            }
        )


@dataclass(frozen=True, slots=True)
class OrderIntent:
    instrument_uid: str
    ticker: str
    class_code: str
    instrument_type: InstrumentType
    side: Side
    order_type: OrderType
    lots: int
    limit_price: Decimal | None
    currency: str
    lot_size: int
    price_step: Decimal
    estimated_cash_rub: Decimal
    quote_observed_at: datetime
    order_valid_until: datetime
    tradable: bool = True
    blocked_reason: str | None = None

    def __post_init__(self) -> None:
        if (
            not self.instrument_uid.strip()
            or not self.ticker.strip()
            or not self.class_code.strip()
        ):
            raise ValueError("instrument identifiers are required")
        if self.lots <= 0:
            raise ValueError("lots must be positive")
        if self.lot_size <= 0:
            raise ValueError("lot_size must be positive")
        if self.price_step <= 0:
            raise ValueError("price_step must be positive")
        if self.estimated_cash_rub <= 0:
            raise ValueError("estimated_cash_rub must be positive")
        _require_aware(self.quote_observed_at, "quote_observed_at")
        _require_aware(self.order_valid_until, "order_valid_until")
        if self.order_valid_until <= self.quote_observed_at:
            raise ValueError("order_valid_until must be later than the quote")
        if self.order_type is OrderType.LIMIT:
            if self.limit_price is None or self.limit_price <= 0:
                raise ValueError("positive limit_price is required for a limit order")
            if self.limit_price % self.price_step != 0:
                raise ValueError("limit_price must align with price_step")
        elif self.limit_price is not None:
            raise ValueError("market order cannot have limit_price")
        if not self.tradable and not self.blocked_reason:
            raise ValueError("blocked_reason is required for a non-tradable instrument")

    @property
    def quantity_units(self) -> int:
        return self.lots * self.lot_size

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "instrument_uid": self.instrument_uid,
            "ticker": self.ticker,
            "class_code": self.class_code,
            "instrument_type": self.instrument_type.value,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "lots": self.lots,
            "lot_size": self.lot_size,
            "quantity_units": self.quantity_units,
            "limit_price": None if self.limit_price is None else _decimal_text(self.limit_price),
            "currency": self.currency,
            "price_step": _decimal_text(self.price_step),
            "estimated_cash_rub": _decimal_text(self.estimated_cash_rub),
            "quote_observed_at": self.quote_observed_at.isoformat(),
            "order_valid_until": self.order_valid_until.isoformat(),
            "tradable": self.tradable,
            "blocked_reason": self.blocked_reason,
        }


@dataclass(frozen=True, slots=True)
class ProposalBundle:
    proposal_id: str
    portfolio_snapshot_digest: str
    created_at: datetime
    expires_at: datetime
    orders: tuple[OrderIntent, ...]
    rationale: tuple[str, ...]
    projected_annual_return: Decimal
    projected_stress_loss: Decimal

    def __post_init__(self) -> None:
        _require_aware(self.created_at, "created_at")
        _require_aware(self.expires_at, "expires_at")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        if not self.proposal_id.strip() or not self.portfolio_snapshot_digest.strip():
            raise ValueError("proposal identifiers are required")
        if not self.orders:
            raise ValueError("proposal must contain at least one order")
        if not self.rationale:
            raise ValueError("proposal must contain a rationale")
        if self.projected_stress_loss < 0:
            raise ValueError("projected_stress_loss cannot be negative")
        if any(order.quote_observed_at > self.created_at for order in self.orders):
            raise ValueError("proposal cannot predate its market quote")
        if any(self.expires_at > order.order_valid_until for order in self.orders):
            raise ValueError("proposal cannot outlive an order validity window")

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "portfolio_snapshot_digest": self.portfolio_snapshot_digest,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "orders": [order.canonical_payload() for order in self.orders],
            "rationale": list(self.rationale),
            "projected_annual_return": _decimal_text(self.projected_annual_return),
            "projected_stress_loss": _decimal_text(self.projected_stress_loss),
        }

    @property
    def digest(self) -> str:
        return _digest(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class Approval:
    approval_id: str
    proposal_digest: str
    approved_by: str
    approved_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.approved_at, "approved_at")
        _require_aware(self.expires_at, "expires_at")
        if self.expires_at <= self.approved_at:
            raise ValueError("approval expires_at must be later than approved_at")
        if not self.approval_id.strip() or not self.proposal_digest.strip():
            raise ValueError("approval identifiers are required")
        if not self.approved_by.strip():
            raise ValueError("approved_by is required")
