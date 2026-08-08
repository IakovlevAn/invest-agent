"""Deterministic point-in-time portfolio audit."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from typing import Any

from invest_agent.domain import InstrumentType, PortfolioSnapshot, Position
from invest_agent.policy import InvestmentPolicy


class FindingSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"


@dataclass(frozen=True, slots=True)
class AuditFinding:
    code: str
    severity: FindingSeverity
    summary: str


@dataclass(frozen=True, slots=True)
class PositionExposure:
    ticker: str
    name: str
    instrument_type: InstrumentType
    value_rub: Decimal
    share_of_managed_securities: Decimal


@dataclass(frozen=True, slots=True)
class PortfolioAudit:
    account_ref: str
    as_of: str
    snapshot_digest: str
    total_value_rub: Decimal
    managed_value_rub: Decimal
    managed_securities_value_rub: Decimal
    blocked_value_rub: Decimal
    cash_rub: Decimal
    full_allocation_rub: dict[InstrumentType, Decimal]
    managed_allocation_rub: dict[InstrumentType, Decimal]
    bond_share_full: Decimal
    bond_share_managed: Decimal
    cash_share_managed: Decimal
    blocked_share_full: Decimal
    target_bond_share: Decimal
    target_bond_share_is_hard_limit: bool
    bond_target_gap_rub: Decimal
    bond_share_after_investing_cash: Decimal
    bond_only_contribution_needed_rub: Decimal | None
    regular_contributions_needed: int | None
    position_hhi: Decimal
    effective_position_count: Decimal | None
    top_positions: tuple[PositionExposure, ...]
    findings: tuple[AuditFinding, ...]
    data_gaps: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_ref": self.account_ref,
            "as_of": self.as_of,
            "snapshot_digest": self.snapshot_digest,
            "values": {
                "total_value_rub": _decimal_text(self.total_value_rub),
                "managed_value_rub": _decimal_text(self.managed_value_rub),
                "managed_securities_value_rub": _decimal_text(
                    self.managed_securities_value_rub
                ),
                "blocked_value_rub": _decimal_text(self.blocked_value_rub),
                "cash_rub": _decimal_text(self.cash_rub),
            },
            "allocation": {
                "full": _allocation_as_dict(self.full_allocation_rub, self.total_value_rub),
                "managed": _allocation_as_dict(
                    self.managed_allocation_rub,
                    self.managed_value_rub,
                ),
            },
            "key_shares": {
                "bond_full": _decimal_text(self.bond_share_full),
                "bond_managed": _decimal_text(self.bond_share_managed),
                "cash_managed": _decimal_text(self.cash_share_managed),
                "blocked_full": _decimal_text(self.blocked_share_full),
            },
            "bond_target": {
                "target_share": _decimal_text(self.target_bond_share),
                "is_hard_limit": self.target_bond_share_is_hard_limit,
                "gap_rub_with_rebalancing": _decimal_text(self.bond_target_gap_rub),
                "share_after_investing_current_cash": _decimal_text(
                    self.bond_share_after_investing_cash
                ),
                "bond_only_contribution_needed_without_sales_rub": (
                    None
                    if self.bond_only_contribution_needed_rub is None
                    else _decimal_text(self.bond_only_contribution_needed_rub)
                ),
                "regular_contributions_needed": self.regular_contributions_needed,
            },
            "concentration": {
                "position_hhi": _decimal_text(self.position_hhi),
                "effective_position_count": (
                    None
                    if self.effective_position_count is None
                    else _decimal_text(self.effective_position_count)
                ),
                "top_positions": [
                    {
                        "ticker": position.ticker,
                        "name": position.name,
                        "instrument_type": position.instrument_type.value,
                        "value_rub": _decimal_text(position.value_rub),
                        "share_of_managed_securities": _decimal_text(
                            position.share_of_managed_securities
                        ),
                    }
                    for position in self.top_positions
                ],
            },
            "findings": [
                {
                    "code": finding.code,
                    "severity": finding.severity.value,
                    "summary": finding.summary,
                }
                for finding in self.findings
            ],
            "data_gaps": list(self.data_gaps),
            "execution_state": "ANALYSIS_ONLY",
        }


class PortfolioAuditor:
    def __init__(self, policy: InvestmentPolicy, *, top_positions: int = 5) -> None:
        if top_positions < 1:
            raise ValueError("top_positions must be positive")
        self._policy = policy
        self._top_positions = top_positions

    def audit(self, snapshot: PortfolioSnapshot) -> PortfolioAudit:
        full_allocation: defaultdict[InstrumentType, Decimal] = defaultdict(
            lambda: Decimal("0")
        )
        full_allocation[InstrumentType.CASH] = snapshot.cash_rub
        managed_allocation: defaultdict[InstrumentType, Decimal] = defaultdict(
            lambda: Decimal("0")
        )
        managed_allocation[InstrumentType.CASH] = snapshot.cash_rub

        managed_positions: list[Position] = []
        blocked_value = Decimal("0")
        for position in snapshot.positions:
            full_allocation[position.instrument_type] += position.market_value_rub
            if position.tradable:
                managed_positions.append(position)
                managed_allocation[position.instrument_type] += position.market_value_rub
            else:
                blocked_value += position.market_value_rub

        total = snapshot.total_value_rub
        managed_securities = sum(
            (position.market_value_rub for position in managed_positions),
            start=Decimal("0"),
        )
        managed = snapshot.cash_rub + managed_securities
        bond_value = managed_allocation[InstrumentType.BOND]

        bond_share_full = _share(bond_value, total)
        bond_share_managed = _share(bond_value, managed)
        cash_share_managed = _share(snapshot.cash_rub, managed)
        blocked_share_full = _share(blocked_value, total)
        target_gap = max(
            Decimal("0"),
            self._policy.target_bond_share * managed - bond_value,
        )
        share_after_cash = _share(bond_value + snapshot.cash_rub, managed)
        contribution_needed = _bond_only_contribution_needed(
            managed_value=managed,
            bond_value=bond_value,
            target_share=self._policy.target_bond_share,
        )
        contributions_needed = _contribution_count(
            contribution_needed,
            self._policy.regular_contribution_rub,
        )

        top_positions, position_hhi = self._concentration(
            managed_positions,
            managed_securities,
        )
        effective_count = None if position_hhi == 0 else Decimal("1") / position_hhi
        findings = self._findings(
            bond_share_managed=bond_share_managed,
            blocked_share_full=blocked_share_full,
            top_positions=top_positions,
        )
        return PortfolioAudit(
            account_ref=snapshot.account_ref,
            as_of=snapshot.as_of.isoformat(),
            snapshot_digest=snapshot.digest,
            total_value_rub=total,
            managed_value_rub=managed,
            managed_securities_value_rub=managed_securities,
            blocked_value_rub=blocked_value,
            cash_rub=snapshot.cash_rub,
            full_allocation_rub=dict(full_allocation),
            managed_allocation_rub=dict(managed_allocation),
            bond_share_full=bond_share_full,
            bond_share_managed=bond_share_managed,
            cash_share_managed=cash_share_managed,
            blocked_share_full=blocked_share_full,
            target_bond_share=self._policy.target_bond_share,
            target_bond_share_is_hard_limit=self._policy.target_bond_share_is_hard_limit,
            bond_target_gap_rub=target_gap,
            bond_share_after_investing_cash=share_after_cash,
            bond_only_contribution_needed_rub=contribution_needed,
            regular_contributions_needed=contributions_needed,
            position_hhi=position_hhi,
            effective_position_count=effective_count,
            top_positions=top_positions,
            findings=findings,
            data_gaps=(
                "карта эмитентов и отраслей для агрегированной концентрации",
                "доходность, дюрация, купоны, рейтинг и ликвидность облигаций",
                "история цен для волатильности и фактической просадки",
                "ряды банковских ставок и индекса облигаций для бенчмарка",
            ),
        )

    def _concentration(
        self,
        positions: list[Position],
        managed_securities_value: Decimal,
    ) -> tuple[tuple[PositionExposure, ...], Decimal]:
        if managed_securities_value == 0:
            return (), Decimal("0")
        exposures = tuple(
            PositionExposure(
                ticker=position.ticker,
                name=position.display_name or position.ticker,
                instrument_type=position.instrument_type,
                value_rub=position.market_value_rub,
                share_of_managed_securities=position.market_value_rub
                / managed_securities_value,
            )
            for position in sorted(
                positions,
                key=lambda item: (-item.market_value_rub, item.ticker),
            )
        )
        hhi = sum(
            (position.share_of_managed_securities**2 for position in exposures),
            start=Decimal("0"),
        )
        return exposures[: self._top_positions], hhi

    def _findings(
        self,
        *,
        bond_share_managed: Decimal,
        blocked_share_full: Decimal,
        top_positions: tuple[PositionExposure, ...],
    ) -> tuple[AuditFinding, ...]:
        findings: list[AuditFinding] = []
        if bond_share_managed < self._policy.target_bond_share:
            findings.append(
                AuditFinding(
                    code="BOND_TARGET_GAP",
                    severity=FindingSeverity.INFO,
                    summary=(
                        "Доля облигаций в управляемом контуре ниже мягкого ориентира; "
                        "это диагностика, а не указание совершить сделку."
                    ),
                )
            )
        if (
            top_positions
            and top_positions[0].share_of_managed_securities
            > self._policy.position_concentration_warning_share
        ):
            findings.append(
                AuditFinding(
                    code="POSITION_CONCENTRATION",
                    severity=FindingSeverity.WARNING,
                    summary=(
                        "Крупнейшая торгуемая бумага превышает диагностический "
                        "порог позиционной концентрации."
                    ),
                )
            )
        if blocked_share_full > self._policy.material_blocked_share:
            findings.append(
                AuditFinding(
                    code="BLOCKED_ASSETS_MATERIAL",
                    severity=FindingSeverity.INFO,
                    summary=(
                        "Существенная доля портфеля находится вне управляемого "
                        "контура агента."
                    ),
                )
            )
        return tuple(findings)


def render_audit_json(audit: PortfolioAudit) -> str:
    return json.dumps(audit.as_dict(), ensure_ascii=False, indent=2)


def render_audit_text(audit: PortfolioAudit) -> str:
    effective_position_count = (
        "н/д"
        if audit.effective_position_count is None
        else f"{audit.effective_position_count:.2f}"
    )
    lines = [
        f"Аудит {audit.account_ref}",
        f"Снимок: {audit.as_of}",
        f"Стоимость: {audit.total_value_rub:,.2f} ₽",
        f"Управляемый контур: {audit.managed_value_rub:,.2f} ₽",
        f"Hold-only и заблокировано: {audit.blocked_value_rub:,.2f} ₽ "
        f"({audit.blocked_share_full:.2%})",
        f"Облигации в управляемом контуре: {audit.bond_share_managed:.2%} "
        f"при мягком ориентире {audit.target_bond_share:.2%}",
        f"После вложения текущих рублей только в облигации: "
        f"{audit.bond_share_after_investing_cash:.2%}",
    ]
    if audit.bond_only_contribution_needed_rub is not None:
        lines.append(
            "До ориентира без продаж, только новыми покупками облигаций: "
            f"{audit.bond_only_contribution_needed_rub:,.2f} ₽ "
            f"({audit.regular_contributions_needed} пополнений)"
        )
    lines.extend(
        [
            f"Позиционный HHI: {audit.position_hhi:.4f}; "
            f"эффективное число позиций: {effective_position_count}",
            "",
            "Крупнейшие позиции среди управляемых бумаг (без рублей):",
        ]
    )
    for position in audit.top_positions:
        lines.append(
            f"- {position.ticker}: {position.value_rub:,.2f} ₽ "
            f"({position.share_of_managed_securities:.2%})"
        )
    lines.extend(["", "Диагностические сигналы:"])
    for finding in audit.findings:
        lines.append(f"- [{finding.severity.value}] {finding.code}: {finding.summary}")
    lines.extend(["", "Пока не рассчитано:"])
    lines.extend(f"- {gap}" for gap in audit.data_gaps)
    lines.append("")
    lines.append("Статус исполнения: только анализ, сделок нет.")
    return "\n".join(lines)


def _allocation_as_dict(
    allocation: dict[InstrumentType, Decimal],
    denominator: Decimal,
) -> dict[str, dict[str, str]]:
    return {
        instrument_type.value: {
            "value_rub": _decimal_text(value),
            "share": _decimal_text(_share(value, denominator)),
        }
        for instrument_type, value in sorted(allocation.items(), key=lambda item: item[0].value)
    }


def _share(value: Decimal, denominator: Decimal) -> Decimal:
    return Decimal("0") if denominator == 0 else value / denominator


def _bond_only_contribution_needed(
    *,
    managed_value: Decimal,
    bond_value: Decimal,
    target_share: Decimal,
) -> Decimal | None:
    if not Decimal("0") <= target_share <= Decimal("1"):
        raise ValueError("target_share must be between zero and one")
    if _share(bond_value, managed_value) >= target_share:
        return Decimal("0")
    if target_share == Decimal("1"):
        return None
    return max(
        Decimal("0"),
        (target_share * managed_value - bond_value) / (Decimal("1") - target_share),
    )


def _contribution_count(amount: Decimal | None, contribution: Decimal) -> int | None:
    if amount is None or contribution <= 0:
        return None
    return int((amount / contribution).to_integral_value(rounding=ROUND_CEILING))


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")
