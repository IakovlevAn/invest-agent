"""Deterministic investment-policy checks.

The LLM cannot override any violation raised here.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from invest_agent.domain import InstrumentType, OrderIntent, OrderType, ProposalBundle


class PolicyViolation(ValueError):
    """A proposal violates the owner's machine-readable mandate."""


@dataclass(frozen=True, slots=True)
class InvestmentPolicy:
    base_currency: str
    target_annual_return: Decimal
    target_is_guarantee: bool
    warning_drawdown: Decimal
    maximum_drawdown: Decimal
    target_bond_share: Decimal
    target_bond_share_is_hard_limit: bool
    regular_contribution_rub: Decimal
    allow_ofz: bool
    position_concentration_warning_share: Decimal
    material_blocked_share: Decimal
    allowed_instrument_types: frozenset[InstrumentType]
    limit_orders_only: bool
    explicit_external_approval_required: bool
    approval_ttl_seconds: int
    max_price_drift_bps: int
    allow_margin: bool
    allow_derivatives: bool
    allow_foreign_purchases: bool
    allow_withdrawals: bool
    allow_asset_transfers: bool

    @classmethod
    def from_toml(cls, path: str | Path) -> InvestmentPolicy:
        with Path(path).open("rb") as source:
            raw = tomllib.load(source)

        objective = raw["objective"]
        portfolio = raw["portfolio"]
        risk = raw["risk"]
        trading = raw["trading"]
        analytics = raw["analytics"]
        return cls(
            base_currency=objective["base_currency"],
            target_annual_return=Decimal(str(objective["current_target_annual_return"])),
            target_is_guarantee=objective["target_is_guarantee"],
            warning_drawdown=Decimal(str(risk["warning_drawdown"])),
            maximum_drawdown=Decimal(str(risk["maximum_drawdown"])),
            target_bond_share=Decimal(str(portfolio["target_bond_share"])),
            target_bond_share_is_hard_limit=portfolio["target_bond_share_is_hard_limit"],
            regular_contribution_rub=Decimal(str(portfolio["regular_contribution_rub"])),
            allow_ofz=portfolio["allow_ofz"],
            position_concentration_warning_share=Decimal(
                str(analytics["position_concentration_warning_share"])
            ),
            material_blocked_share=Decimal(str(analytics["material_blocked_share"])),
            allowed_instrument_types=frozenset(
                InstrumentType(value) for value in trading["allowed_instrument_types"]
            ),
            limit_orders_only=trading["limit_orders_only"],
            explicit_external_approval_required=trading["explicit_external_approval_required"],
            approval_ttl_seconds=trading["approval_ttl_seconds"],
            max_price_drift_bps=trading["max_price_drift_bps"],
            allow_margin=trading["allow_margin"],
            allow_derivatives=trading["allow_derivatives"],
            allow_foreign_purchases=trading["allow_foreign_purchases"],
            allow_withdrawals=trading["allow_withdrawals"],
            allow_asset_transfers=trading["allow_asset_transfers"],
        )

    def validate_order(self, order: OrderIntent) -> None:
        if not order.tradable:
            raise PolicyViolation(f"{order.ticker} is not tradable: {order.blocked_reason}")
        if order.instrument_type not in self.allowed_instrument_types:
            raise PolicyViolation(f"instrument type {order.instrument_type.value} is forbidden")
        if order.instrument_type in {InstrumentType.FUTURE, InstrumentType.OPTION}:
            raise PolicyViolation("derivatives are forbidden")
        if order.instrument_type is InstrumentType.FOREIGN_STOCK:
            raise PolicyViolation("foreign purchases and sales are outside the trading mandate")
        if order.currency != self.base_currency:
            raise PolicyViolation(f"only {self.base_currency} orders are allowed")
        if self.limit_orders_only and order.order_type is not OrderType.LIMIT:
            raise PolicyViolation("only limit orders are allowed")

    def validate_proposal(self, proposal: ProposalBundle) -> None:
        if proposal.projected_stress_loss > self.maximum_drawdown:
            raise PolicyViolation(
                "projected stress loss exceeds maximum drawdown: "
                f"{proposal.projected_stress_loss} > {self.maximum_drawdown}"
            )
        maximum_ttl = timedelta(seconds=self.approval_ttl_seconds)
        if proposal.expires_at - proposal.created_at > maximum_ttl:
            raise PolicyViolation("proposal lifetime exceeds the approval time-to-live")
        for order in proposal.orders:
            self.validate_order(order)
