"""Fast deterministic portfolio-manager recommendation layer.

The hot path intentionally uses the live BCS snapshot, current MOEX bond facts
and the Bank of Russia rating repository. Annual statements remain background
evidence and do not block a routine recommendation.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from invest_agent.audit import PortfolioAudit
from invest_agent.bond_report import BondMarketReport, EnrichedBondPosition
from invest_agent.credit import (
    CreditPassport,
    CreditPortfolioReport,
    RatingBand,
    SignalSeverity,
)
from invest_agent.policy import InvestmentPolicy
from invest_agent.universe import BondUniverseReport


class ManagerAction(StrEnum):
    URGENT_REVIEW = "URGENT_REVIEW"
    REDUCE_RISK = "REDUCE_RISK"
    DO_NOT_ADD = "DO_NOT_ADD"
    ADD_CANDIDATE = "ADD_CANDIDATE"
    HOLD = "HOLD"


@dataclass(frozen=True, slots=True)
class ManagerPolicy:
    max_bond_issuer_share_after_add: Decimal
    rating_stress_loss: tuple[tuple[RatingBand, Decimal], ...]
    minimum_allocation_rub: Decimal
    allocation_rounding_rub: Decimal
    maximum_single_purchase_share_of_cash: Decimal
    minimum_cash_reserve_rub: Decimal = Decimal("0")
    maximum_purchase_count: int = 8
    maximum_list_level_for_add: int = 2
    minimum_add_yield_percent: Decimal = Decimal("20")
    maximum_add_yield_percent: Decimal = Decimal("30")
    minimum_add_duration_days: Decimal = Decimal("90")
    minimum_add_turnover_rub: Decimal = Decimal("0")
    third_level_minimum_rating_band: RatingBand = RatingBand.STRONG

    @classmethod
    def from_toml(cls, path: str | Path) -> ManagerPolicy:
        with Path(path).open("rb") as source:
            document = tomllib.load(source)
        raw = document["manager"]
        stress_loss = document["credit"]["stress_loss"]
        policy = cls(
            max_bond_issuer_share_after_add=_fraction(
                raw["max_bond_issuer_share_after_add"],
                "manager.max_bond_issuer_share_after_add",
            ),
            rating_stress_loss=tuple(
                (
                    band,
                    _fraction(
                        stress_loss[band.value],
                        f"credit.stress_loss.{band.value}",
                    ),
                )
                for band in RatingBand
            ),
            minimum_allocation_rub=_nonnegative_decimal(
                raw["minimum_allocation_rub"],
                "manager.minimum_allocation_rub",
            ),
            allocation_rounding_rub=_positive_decimal(
                raw["allocation_rounding_rub"],
                "manager.allocation_rounding_rub",
            ),
            maximum_single_purchase_share_of_cash=_fraction(
                raw["maximum_single_purchase_share_of_cash"],
                "manager.maximum_single_purchase_share_of_cash",
            ),
            minimum_cash_reserve_rub=_nonnegative_decimal(
                raw["minimum_cash_reserve_rub"],
                "manager.minimum_cash_reserve_rub",
            ),
            maximum_purchase_count=_positive_int(
                raw["maximum_purchase_count"],
                "manager.maximum_purchase_count",
            ),
            maximum_list_level_for_add=_positive_int(
                document["universe"]["maximum_list_level"],
                "universe.maximum_list_level",
            ),
            minimum_add_yield_percent=_positive_decimal(
                document["universe"]["minimum_yield_percent"],
                "universe.minimum_yield_percent",
            ),
            maximum_add_yield_percent=_positive_decimal(
                document["universe"]["maximum_yield_percent"],
                "universe.maximum_yield_percent",
            ),
            minimum_add_duration_days=_positive_decimal(
                document["universe"]["minimum_duration_days"],
                "universe.minimum_duration_days",
            ),
            minimum_add_turnover_rub=_positive_decimal(
                document["universe"]["minimum_turnover_rub"],
                "universe.minimum_turnover_rub",
            ),
            third_level_minimum_rating_band=_rating_band(
                document["universe"]["third_level_minimum_rating_band"],
                "universe.third_level_minimum_rating_band",
            ),
        )
        if policy.minimum_add_yield_percent >= policy.maximum_add_yield_percent:
            raise ValueError("universe yield range is invalid")
        return policy

    def stress_loss_fraction(self, band: RatingBand | None) -> Decimal:
        effective_band = RatingBand.UNRATED if band is None else band
        try:
            return dict(self.rating_stress_loss)[effective_band]
        except KeyError as error:
            raise ValueError(
                f"credit stress loss is missing for {effective_band.value}"
            ) from error


@dataclass(frozen=True, slots=True)
class BondManagerDecision:
    ticker: str
    isin: str
    emitter_id: int
    emitter_name: str
    market_value_rub: Decimal
    issuer_share_of_bonds: Decimal
    action: ManagerAction
    broad_rating_band: RatingBand | None
    stress_loss_fraction: Decimal
    comparable_yield_percent: Decimal | None
    duration_days: Decimal | None
    estimated_unit_value_rub: Decimal
    available_units: Decimal
    recommended_add_rub: Decimal
    recommended_reduce_rub: Decimal
    recommended_reduce_units: int
    reasons: tuple[str, ...]
    exit_triggers: tuple[str, ...]
    credit_warning: bool = False

    @property
    def risk_return_score(self) -> Decimal | None:
        if self.comparable_yield_percent is None:
            return None
        return _risk_return_score(
            self.comparable_yield_percent,
            self.stress_loss_fraction,
        )


@dataclass(frozen=True, slots=True)
class NewBondManagerDecision:
    ticker: str
    isin: str
    name: str
    emitter_id: int
    emitter_name: str
    current_issuer_share_of_bonds: Decimal
    broad_rating_band: RatingBand
    stress_loss_fraction: Decimal
    comparable_yield_percent: Decimal
    duration_days: Decimal
    estimated_lot_cost_rub: Decimal
    reference_buy_price_percent: Decimal
    turnover_today_rub: Decimal
    ranking_score: Decimal
    recommended_add_rub: Decimal
    reasons: tuple[str, ...]
    bcs_availability_verified: bool
    moex_security_url: str
    moex_market_url: str
    list_level: int = 2

    @property
    def risk_return_score(self) -> Decimal:
        return _risk_return_score(
            self.comparable_yield_percent,
            self.stress_loss_fraction,
        )


@dataclass(frozen=True, slots=True)
class ManagerScenario:
    code: str
    recommended: bool
    invested_cash_rub: Decimal
    estimated_sale_proceeds_rub: Decimal
    remaining_cash_rub: Decimal
    net_bond_change_rub: Decimal
    projected_bond_share_managed: Decimal
    comparable_bond_yield_percent: Decimal | None
    required_stock_sales_rub: Decimal
    explanation: str


@dataclass(frozen=True, slots=True)
class PortfolioManagerReport:
    account_ref: str
    portfolio_as_of: str
    generated_at: datetime
    snapshot_digest: str
    total_value_rub: Decimal
    managed_value_rub: Decimal
    cash_rub: Decimal
    bond_value_rub: Decimal
    target_bond_share: Decimal
    current_bond_share_managed: Decimal
    current_comparable_bond_yield_percent: Decimal | None
    comparable_yield_coverage_share: Decimal
    stressed_credit_share_of_bonds: Decimal
    primary_action: str
    decisions: tuple[BondManagerDecision, ...]
    new_bond_candidates: tuple[NewBondManagerDecision, ...]
    scenarios: tuple[ManagerScenario, ...]
    data_failures: tuple[str, ...]
    bcs_as_of: str
    moex_fetched_at: datetime
    ratings_fetched_at: datetime
    universe_scanned_count: int
    universe_coarse_eligible_count: int
    universe_detailed_count: int
    universe_rejection_counts: tuple[tuple[str, int], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_ref": self.account_ref,
            "portfolio_as_of": self.portfolio_as_of,
            "generated_at": self.generated_at.isoformat(),
            "snapshot_digest": self.snapshot_digest,
            "sources": {
                "bcs_as_of": self.bcs_as_of,
                "moex_fetched_at": self.moex_fetched_at.isoformat(),
                "ratings_fetched_at": self.ratings_fetched_at.isoformat(),
                "hot_path": "BCS portfolio + MOEX market facts + CBR ratings",
                "annual_financials": "optional lagging background; not a hot-path blocker",
            },
            "portfolio": {
                "total_value_rub": _decimal_text(self.total_value_rub),
                "managed_value_rub": _decimal_text(self.managed_value_rub),
                "cash_rub": _decimal_text(self.cash_rub),
                "bond_value_rub": _decimal_text(self.bond_value_rub),
                "target_bond_share": _decimal_text(self.target_bond_share),
                "current_bond_share_managed": _decimal_text(
                    self.current_bond_share_managed
                ),
                "current_comparable_bond_yield_percent": _optional_decimal_text(
                    self.current_comparable_bond_yield_percent
                ),
                "comparable_yield_coverage_share": _decimal_text(
                    self.comparable_yield_coverage_share
                ),
                "stressed_credit_share_of_bonds": _decimal_text(
                    self.stressed_credit_share_of_bonds
                ),
            },
            "primary_action": self.primary_action,
            "decisions": [_decision_as_dict(decision) for decision in self.decisions],
            "new_bond_candidates": [
                _new_candidate_as_dict(candidate)
                for candidate in self.new_bond_candidates
            ],
            "universe": {
                "scanned_count": self.universe_scanned_count,
                "coarse_eligible_count": self.universe_coarse_eligible_count,
                "detailed_count": self.universe_detailed_count,
                "selected_count": len(self.new_bond_candidates),
                "rejection_counts": dict(self.universe_rejection_counts),
            },
            "scenarios": [_scenario_as_dict(scenario) for scenario in self.scenarios],
            "data_failures": list(self.data_failures),
            "model_boundaries": [
                "MOEX yield is a market indication, not a guaranteed return",
                "floaters without a rate scenario have no comparable expected yield",
                "rating bands are diagnostic classes, not default probabilities",
                "credit haircuts are configurable stress scenarios, not expected losses",
                "issuer reductions are sized by drawdown and concentration "
                "budgets, not fixed sell fractions",
                "amounts are allocation targets, not executable orders or lot calculations",
                "new candidates require BCS availability and exact lot-price verification",
                "additions to existing bonds obey the configured maximum listing level",
                "stocks remain unchanged until a separate superior-alternative case is proven",
            ],
            "trade_gate": {
                "state": "RECOMMENDATION_ONLY",
                "orders_created": False,
                "explicit_confirmation_required": True,
                "limit_orders_only": True,
            },
        }


class PortfolioManager:
    def __init__(
        self,
        investment_policy: InvestmentPolicy,
        manager_policy: ManagerPolicy,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._investment_policy = investment_policy
        self._manager_policy = manager_policy
        self._now = now or (lambda: datetime.now(tz=UTC))

    def recommend(
        self,
        audit: PortfolioAudit,
        bonds: BondMarketReport,
        credit: CreditPortfolioReport,
        universe: BondUniverseReport | None = None,
    ) -> PortfolioManagerReport:
        _validate_inputs(audit, bonds, credit, universe)
        credit_by_ticker = {passport.ticker: passport for passport in credit.passports}
        issuer_shares = {
            exposure.emitter_id: exposure.share_of_covered_bonds
            for exposure in bonds.issuer_exposures
        }
        decisions = [
            self._decision(
                record,
                credit_by_ticker.get(record.position.ticker),
                issuer_shares.get(record.moex.facts.emitter_id, Decimal("0")),
            )
            for record in bonds.positions
        ]
        new_candidates = _new_candidate_decisions(universe, self._manager_policy)
        decisions = self._apply_risk_adjusted_issuer_limits(
            decisions,
            bonds,
            new_candidates,
        )
        planned_sale_proceeds = _planned_sale_proceeds(decisions)
        decisions, new_candidates = self._allocate_cash(
            decisions,
            new_candidates,
            bonds,
            audit.cash_rub + planned_sale_proceeds,
            audit.managed_value_rub,
        )
        current_yield, yield_coverage = _comparable_yield(bonds.positions)
        stressed_value = sum(
            (
                decision.market_value_rub
                for decision in decisions
                if decision.action in {ManagerAction.URGENT_REVIEW, ManagerAction.REDUCE_RISK}
            ),
            Decimal("0"),
        )
        stressed_share = _share(stressed_value, bonds.bond_value_rub)
        scenarios = self._scenarios(
            audit,
            bonds,
            decisions,
            new_candidates,
            current_yield=current_yield,
        )
        primary_action = _primary_action(decisions, new_candidates)
        failures = tuple(
            sorted(
                {
                    *(
                        f"MOEX {failure.ticker}/{failure.stage}: {failure.message}"
                        for failure in bonds.failures
                    ),
                    *(
                        f"CBR {failure.ticker}/{failure.stage}: {failure.message}"
                        for failure in credit.failures
                    ),
                }
            )
        )
        return PortfolioManagerReport(
            account_ref=audit.account_ref,
            portfolio_as_of=audit.as_of,
            generated_at=self._now(),
            snapshot_digest=audit.snapshot_digest,
            total_value_rub=audit.total_value_rub,
            managed_value_rub=audit.managed_value_rub,
            cash_rub=audit.cash_rub,
            bond_value_rub=bonds.bond_value_rub,
            target_bond_share=self._investment_policy.target_bond_share,
            current_bond_share_managed=audit.bond_share_managed,
            current_comparable_bond_yield_percent=current_yield,
            comparable_yield_coverage_share=yield_coverage,
            stressed_credit_share_of_bonds=stressed_share,
            primary_action=primary_action,
            decisions=tuple(sorted(decisions, key=_decision_sort_key)),
            new_bond_candidates=tuple(
                sorted(
                    new_candidates,
                    key=lambda item: (-item.risk_return_score, item.ticker),
                )
            ),
            scenarios=scenarios,
            data_failures=tuple(
                sorted(
                    {
                        *failures,
                        *(() if universe is None else universe.failures),
                    }
                )
            ),
            bcs_as_of=audit.as_of,
            moex_fetched_at=bonds.fetched_at,
            ratings_fetched_at=credit.fetched_at,
            universe_scanned_count=0 if universe is None else universe.scanned_count,
            universe_coarse_eligible_count=(
                0 if universe is None else universe.coarse_eligible_count
            ),
            universe_detailed_count=0 if universe is None else universe.detailed_count,
            universe_rejection_counts=(
                () if universe is None else universe.rejection_counts
            ),
        )

    def apply_bcs_buy_availability(
        self,
        report: PortfolioManagerReport,
        audit: PortfolioAudit,
        bonds: BondMarketReport,
        available_buy_isins: set[str],
        *,
        include_planned_sale_proceeds: bool = True,
        cash_override_rub: Decimal | None = None,
        buy_lot_cost_rub: dict[str, Decimal] | None = None,
    ) -> PortfolioManagerReport:
        decisions = [
            replace(
                decision,
                action=(
                    ManagerAction.DO_NOT_ADD
                    if decision.action is ManagerAction.ADD_CANDIDATE
                    and decision.isin not in available_buy_isins
                    else decision.action
                ),
                recommended_add_rub=Decimal("0"),
                reasons=(
                    tuple(
                        dict.fromkeys(
                            (
                                *decision.reasons,
                                "покупка выпуска недоступна в справочнике БКС",
                            )
                        )
                    )
                    if decision.action is ManagerAction.ADD_CANDIDATE
                    and decision.isin not in available_buy_isins
                    else decision.reasons
                ),
            )
            for decision in report.decisions
        ]
        candidates = [
            replace(
                candidate,
                recommended_add_rub=Decimal("0"),
                bcs_availability_verified=candidate.isin in available_buy_isins,
            )
            for candidate in report.new_bond_candidates
            if candidate.isin in available_buy_isins
        ]
        base_cash = report.cash_rub if cash_override_rub is None else cash_override_rub
        decisions, candidates = self._allocate_cash(
            decisions,
            candidates,
            bonds,
            base_cash
            + (
                _planned_sale_proceeds(decisions)
                if include_planned_sale_proceeds
                else Decimal("0")
            ),
            report.managed_value_rub,
            buy_lot_cost_rub=buy_lot_cost_rub,
        )
        scenarios = self._scenarios(
            audit,
            bonds,
            decisions,
            candidates,
            current_yield=report.current_comparable_bond_yield_percent,
        )
        return replace(
            report,
            cash_rub=base_cash,
            managed_value_rub=report.managed_value_rub + base_cash - report.cash_rub,
            total_value_rub=report.total_value_rub + base_cash - report.cash_rub,
            decisions=tuple(sorted(decisions, key=_decision_sort_key)),
            new_bond_candidates=tuple(
                sorted(
                    candidates,
                    key=lambda item: (-item.risk_return_score, item.ticker),
                )
            ),
            scenarios=scenarios,
            primary_action=_primary_action(decisions, candidates),
        )

    def _decision(
        self,
        record: EnrichedBondPosition,
        credit: CreditPassport | None,
        issuer_share: Decimal,
    ) -> BondManagerDecision:
        market = record.moex.market
        comparable_yield = record.comparable_effective_yield_percent
        duration = None if market is None else market.duration_days
        reasons: list[str] = []
        has_credit_warning = False
        exits = [
            "подтверждённый дефолт или пропуск платежа",
            "дальнейшее понижение, негативный прогноз или отзыв рейтинга",
            "одновременное подтверждённое ухудшение ликвидности и кредитных метрик",
        ]
        if credit is None:
            action = ManagerAction.DO_NOT_ADD
            band = None
            reasons.append("нет кредитного паспорта текущего выпуска")
        else:
            current = credit.current_ratings
            band = _worst_band(current)
            critical = [
                signal for signal in credit.signals if signal.severity is SignalSeverity.CRITICAL
            ]
            warnings = [
                signal for signal in credit.signals if signal.severity is SignalSeverity.WARNING
            ]
            has_credit_warning = bool(warnings)
            speculative = any(signal.code == "SPECULATIVE_RATING" for signal in warnings)
            if critical:
                action = ManagerAction.URGENT_REVIEW
                reasons.extend(signal.message for signal in critical)
                reasons.append("до проверки текущего статуса новые покупки запрещены")
            elif speculative:
                action = ManagerAction.DO_NOT_ADD
                reasons.extend(signal.message for signal in warnings)
                reasons.append(
                    "размер позиции определяется её доходностью на единицу "
                    "сценарного кредитного риска"
                )
            elif warnings:
                action = ManagerAction.DO_NOT_ADD
                reasons.extend(signal.message for signal in warnings)
            elif not current or band is None or band is RatingBand.UNRATED:
                action = ManagerAction.DO_NOT_ADD
                reasons.append("нет поддержанного текущего рейтингового класса")
            elif (
                record.moex.facts.list_level is None
                or record.moex.facts.list_level
                > self._manager_policy.maximum_list_level_for_add
            ):
                action = ManagerAction.DO_NOT_ADD
                reasons.append(
                    "уровень листинга выпуска выше допуска для новых покупок"
                )
            elif (
                record.moex.facts.list_level == 3
                and _band_rank(band)
                > _band_rank(self._manager_policy.third_level_minimum_rating_band)
            ):
                action = ManagerAction.DO_NOT_ADD
                reasons.append(
                    "для третьего уровня листинга кредитный рейтинг недостаточно высок"
                )
            elif comparable_yield is None:
                action = ManagerAction.HOLD
                reasons.append("доходность флоатера требует отдельного сценария ставки")
            elif duration is None or duration < self._manager_policy.minimum_add_duration_days:
                action = ManagerAction.DO_NOT_ADD
                reasons.append("дюрация ниже допуска для новой покупки")
            elif market is None or market.turnover_today_rub is None or (
                market.turnover_today_rub < self._manager_policy.minimum_add_turnover_rub
            ):
                action = ManagerAction.DO_NOT_ADD
                reasons.append("ликвидность выпуска ниже допуска для новой покупки")
            elif (
                self._manager_policy.minimum_add_yield_percent
                <= comparable_yield
                <= self._manager_policy.maximum_add_yield_percent
            ):
                action = ManagerAction.ADD_CANDIDATE
                reasons.append(
                    "доходность входит в допустимый диапазон портфельной стратегии"
                )
                reasons.append("текущий рейтинг не находится в спекулятивной зоне")
            else:
                action = ManagerAction.HOLD
                reasons.append("доходность ниже тактической цели, но позиция может снижать риск")

        if action is ManagerAction.URGENT_REVIEW:
            exits.insert(0, "подтверждение, что технический/default-флаг MOEX актуален")
        stress_loss_fraction = self._manager_policy.stress_loss_fraction(band)
        return BondManagerDecision(
            ticker=record.position.ticker,
            isin=record.moex.facts.isin,
            emitter_id=record.moex.facts.emitter_id,
            emitter_name=(
                record.moex.facts.name
                if record.emitter is None
                else record.emitter.short_title
            ),
            market_value_rub=record.position.market_value_rub,
            issuer_share_of_bonds=issuer_share,
            action=action,
            broad_rating_band=band,
            stress_loss_fraction=stress_loss_fraction,
            comparable_yield_percent=comparable_yield,
            duration_days=duration,
            estimated_unit_value_rub=(
                Decimal("0")
                if record.position.quantity <= 0
                else record.position.market_value_rub / record.position.quantity
            ),
            available_units=record.position.available_quantity,
            recommended_add_rub=Decimal("0"),
            recommended_reduce_rub=Decimal("0"),
            recommended_reduce_units=0,
            reasons=tuple(dict.fromkeys(reasons)),
            exit_triggers=tuple(exits),
            credit_warning=has_credit_warning,
        )

    def _apply_risk_adjusted_issuer_limits(
        self,
        decisions: list[BondManagerDecision],
        bonds: BondMarketReport,
        new_candidates: list[NewBondManagerDecision],
    ) -> list[BondManagerDecision]:
        updated = list(decisions)
        maximum_issuer_value = (
            bonds.bond_value_rub
            * self._manager_policy.max_bond_issuer_share_after_add
        )
        issuer_stress_budget = (
            maximum_issuer_value * self._investment_policy.maximum_drawdown
        )
        replacement_score = max(
            (candidate.risk_return_score for candidate in new_candidates),
            default=None,
        )
        emitter_ids = sorted({decision.emitter_id for decision in decisions})
        for emitter_id in emitter_ids:
            issuer_indexes = [
                index
                for index, decision in enumerate(updated)
                if decision.emitter_id == emitter_id
                and decision.action is not ManagerAction.URGENT_REVIEW
            ]
            indexes = [
                index
                for index in issuer_indexes
                if updated[index].credit_warning
            ]
            if not indexes:
                continue
            issuer_value = sum(
                (updated[index].market_value_rub for index in issuer_indexes),
                Decimal("0"),
            )
            issuer_stress_loss = sum(
                (
                    updated[index].market_value_rub
                    * updated[index].stress_loss_fraction
                    for index in issuer_indexes
                ),
                Decimal("0"),
            )
            concentration_remaining = max(
                Decimal("0"), issuer_value - maximum_issuer_value
            )
            stress_remaining = max(
                Decimal("0"), issuer_stress_loss - issuer_stress_budget
            )
            if concentration_remaining <= 0 and stress_remaining <= 0:
                continue
            indexes.sort(
                key=lambda index: (
                    updated[index].risk_return_score is not None,
                    updated[index].risk_return_score or Decimal("0"),
                    -updated[index].stress_loss_fraction,
                )
            )
            for index in indexes:
                if concentration_remaining <= 0 and stress_remaining <= 0:
                    break
                decision = updated[index]
                stress_driven = (
                    Decimal("0")
                    if decision.stress_loss_fraction <= 0
                    else stress_remaining / decision.stress_loss_fraction
                )
                required_reduction = max(concentration_remaining, stress_driven)
                available_units = int(
                    decision.available_units.to_integral_value(rounding=ROUND_FLOOR)
                )
                if decision.estimated_unit_value_rub <= 0 or available_units <= 0:
                    continue
                reduction_units = min(
                    available_units,
                    int(
                        (
                            required_reduction / decision.estimated_unit_value_rub
                        ).to_integral_value(rounding=ROUND_CEILING)
                    ),
                )
                if reduction_units <= 0:
                    continue
                reduction = min(
                    decision.market_value_rub,
                    decision.estimated_unit_value_rub * reduction_units,
                )
                if reduction < self._manager_policy.minimum_allocation_rub:
                    continue
                reasons = list(decision.reasons)
                if concentration_remaining > 0:
                    reasons.append(
                        "доля эмитента превышает портфельный лимит "
                        f"{self._manager_policy.max_bond_issuer_share_after_add:.0%}"
                    )
                if stress_remaining > 0:
                    reasons.append(
                        "сценарный убыток эмитента превышает выделенный ему "
                        "бюджет стресс-риска"
                    )
                if (
                    decision.risk_return_score is not None
                    and replacement_score is not None
                ):
                    reasons.append(
                        "доходность/стресс текущего выпуска "
                        f"{decision.risk_return_score:.2f}; лучшая отобранная "
                        f"альтернатива {replacement_score:.2f}"
                    )
                updated[index] = replace(
                    decision,
                    action=ManagerAction.REDUCE_RISK,
                    recommended_reduce_rub=reduction,
                    recommended_reduce_units=reduction_units,
                    reasons=tuple(dict.fromkeys(reasons)),
                )
                concentration_remaining = max(
                    Decimal("0"), concentration_remaining - reduction
                )
                stress_remaining = max(
                    Decimal("0"),
                    stress_remaining - reduction * decision.stress_loss_fraction,
                )
        return updated

    def _allocate_cash(
        self,
        decisions: list[BondManagerDecision],
        new_candidates: list[NewBondManagerDecision],
        bonds: BondMarketReport,
        cash_rub: Decimal,
        managed_value_rub: Decimal,
        *,
        buy_lot_cost_rub: dict[str, Decimal] | None = None,
    ) -> tuple[list[BondManagerDecision], list[NewBondManagerDecision]]:
        remaining = max(
            Decimal("0"),
            cash_rub - self._manager_policy.minimum_cash_reserve_rub,
        )
        updated_existing = list(decisions)
        updated_new = list(new_candidates)
        candidates: list[
            tuple[Decimal, str, BondManagerDecision | NewBondManagerDecision]
        ] = [
            (decision.comparable_yield_percent, "EXISTING", decision)
            for decision in decisions
            if decision.action is ManagerAction.ADD_CANDIDATE
            and decision.comparable_yield_percent is not None
        ]
        candidates.extend(
            (candidate.comparable_yield_percent, "NEW", candidate)
            for candidate in new_candidates
        )
        candidates.sort(
            key=lambda item: (
                -item[0],
                item[2].current_issuer_share_of_bonds
                if isinstance(item[2], NewBondManagerDecision)
                else item[2].issuer_share_of_bonds,
                item[2].ticker,
            )
        )
        issuer_values = {
            exposure.emitter_id: exposure.value_rub
            for exposure in bonds.issuer_exposures
        }
        maximum_purchase = (
            remaining * self._manager_policy.maximum_single_purchase_share_of_cash
        )
        existing_minimum_purchase = {
            record.moex.facts.isin: _round_up(
                record.position.market_value_rub / record.position.quantity,
                self._manager_policy.allocation_rounding_rub,
            )
            for record in bonds.positions
            if record.position.quantity > 0
        }
        maximum_stress_loss = (
            managed_value_rub * self._investment_policy.maximum_drawdown
        )
        projected_stress_loss = sum(
            (
                max(
                    Decimal("0"),
                    decision.market_value_rub - decision.recommended_reduce_rub,
                )
                * decision.stress_loss_fraction
                for decision in decisions
            ),
            Decimal("0"),
        )
        allocated_count = 0
        exact_lot_cost = {} if buy_lot_cost_rub is None else buy_lot_cost_rub
        for _, candidate_type, candidate in candidates:
            candidate_lot_cost = exact_lot_cost.get(candidate.isin) or (
                candidate.estimated_lot_cost_rub
                if isinstance(candidate, NewBondManagerDecision)
                else existing_minimum_purchase.get(
                    candidate.isin,
                    self._manager_policy.allocation_rounding_rub,
                )
            )
            minimum_executable = max(
                self._manager_policy.minimum_allocation_rub,
                candidate_lot_cost,
            )
            if minimum_executable > remaining:
                continue
            current_issuer_value = issuer_values.get(candidate.emitter_id, Decimal("0"))
            risk_adjusted_share_limit = _risk_adjusted_issuer_share_limit(
                self._manager_policy.max_bond_issuer_share_after_add,
                self._investment_policy.maximum_drawdown,
                candidate.stress_loss_fraction,
            )
            capacity = _issuer_add_capacity(
                current_value=current_issuer_value,
                sleeve_value=bonds.bond_value_rub,
                maximum_share=risk_adjusted_share_limit,
            )
            stress_capacity = max(
                Decimal("0"),
                maximum_stress_loss - projected_stress_loss,
            ) / candidate.stress_loss_fraction
            amount = _round_down(
                min(
                    remaining,
                    capacity,
                    stress_capacity,
                    max(maximum_purchase, minimum_executable),
                ),
                minimum_executable,
            )
            if amount < minimum_executable:
                continue
            if candidate_type == "EXISTING":
                existing = candidate
                assert isinstance(existing, BondManagerDecision)
                index = updated_existing.index(existing)
                updated_existing[index] = replace(existing, recommended_add_rub=amount)
            else:
                new = candidate
                assert isinstance(new, NewBondManagerDecision)
                index = updated_new.index(new)
                updated_new[index] = replace(new, recommended_add_rub=amount)
            issuer_values[candidate.emitter_id] = current_issuer_value + amount
            projected_stress_loss += amount * candidate.stress_loss_fraction
            remaining -= amount
            allocated_count += 1
            if (
                remaining < self._manager_policy.allocation_rounding_rub
                or allocated_count >= self._manager_policy.maximum_purchase_count
            ):
                break
        return updated_existing, updated_new

    def _scenarios(
        self,
        audit: PortfolioAudit,
        bonds: BondMarketReport,
        decisions: list[BondManagerDecision],
        new_candidates: list[NewBondManagerDecision],
        *,
        current_yield: Decimal | None,
    ) -> tuple[ManagerScenario, ...]:
        invested = sum(
            (decision.recommended_add_rub for decision in decisions),
            Decimal("0"),
        ) + sum(
            (candidate.recommended_add_rub for candidate in new_candidates),
            Decimal("0"),
        )
        sale_proceeds = _planned_sale_proceeds(decisions)
        invested_yield = _yield_after_allocations(
            bonds.positions,
            decisions,
            new_candidates,
        )
        net_bond_change = invested - sale_proceeds
        projected_share = _share(
            bonds.bond_value_rub + net_bond_change,
            audit.managed_value_rub,
        )
        stock_sales = max(Decimal("0"), audit.bond_target_gap_rub - audit.cash_rub)
        risk_reduction_first = any(
            decision.action in {ManagerAction.URGENT_REVIEW, ManagerAction.REDUCE_RISK}
            for decision in decisions
        )
        return (
            ManagerScenario(
                code="NO_ACTION",
                recommended=False,
                invested_cash_rub=Decimal("0"),
                estimated_sale_proceeds_rub=Decimal("0"),
                remaining_cash_rub=audit.cash_rub,
                net_bond_change_rub=Decimal("0"),
                projected_bond_share_managed=audit.bond_share_managed,
                comparable_bond_yield_percent=current_yield,
                required_stock_sales_rub=Decimal("0"),
                explanation="сохраняет текущую концентрацию и не использует свободные деньги",
            ),
            ManagerScenario(
                code="INVEST_CURRENT_CASH",
                recommended=(invested > 0 or sale_proceeds > 0)
                and not any(
                    decision.action is ManagerAction.URGENT_REVIEW
                    for decision in decisions
                ),
                invested_cash_rub=invested,
                estimated_sale_proceeds_rub=sale_proceeds,
                remaining_cash_rub=audit.cash_rub + sale_proceeds - invested,
                net_bond_change_rub=net_bond_change,
                projected_bond_share_managed=projected_share,
                comparable_bond_yield_percent=invested_yield,
                required_stock_sales_rub=Decimal("0"),
                explanation=(
                    "последовательная ребалансировка: сначала сокращение кредитного "
                    "риска, затем покупки отобранных выпусков"
                    if risk_reduction_first and invested > 0
                    else (
                        "снижает кредитный риск без немедленной замены слабых выпусков"
                        if risk_reduction_first
                        else (
                            "распределяет деньги только в выпуски без критических или "
                            "предупреждающих кредитных сигналов и не превышает лимит эмитента"
                        )
                    )
                ),
            ),
            ManagerScenario(
                code="REBALANCE_TO_BOND_TARGET",
                recommended=False,
                invested_cash_rub=min(audit.cash_rub, audit.bond_target_gap_rub),
                estimated_sale_proceeds_rub=Decimal("0"),
                remaining_cash_rub=max(
                    Decimal("0"),
                    audit.cash_rub - audit.bond_target_gap_rub,
                ),
                net_bond_change_rub=min(audit.cash_rub, audit.bond_target_gap_rub)
                + stock_sales,
                projected_bond_share_managed=self._investment_policy.target_bond_share,
                comparable_bond_yield_percent=None,
                required_stock_sales_rub=stock_sales,
                explanation=(
                    "требует отдельного доказательства превосходства облигаций перед "
                    "продажей существующих акций; автоматически не рекомендуется"
                ),
            ),
        )


def render_manager_report_json(report: PortfolioManagerReport) -> str:
    return json.dumps(report.as_dict(), ensure_ascii=False, indent=2)


def render_manager_report_text(report: PortfolioManagerReport) -> str:
    lines = [
        "Рекомендация портфельного менеджера",
        f"Портфель на: {report.portfolio_as_of}",
        f"Решение: {report.primary_action}",
        (
            f"Свободные деньги: {report.cash_rub:,.2f} ₽; облигации: "
            f"{report.current_bond_share_managed:.2%} управляемого контура; "
            f"сопоставимая доходность облигационной части: "
            f"{_percent_text(report.current_comparable_bond_yield_percent)}."
        ),
        "",
        "Действия по облигациям:",
    ]
    for decision in report.decisions:
        lines.append(
            f"- {decision.action.value} {decision.ticker} · {decision.emitter_name} · "
            f"доходность {_percent_text(decision.comparable_yield_percent)}"
        )
        if decision.recommended_add_rub > 0:
            lines.append(f"  добавить ориентировочно {decision.recommended_add_rub:,.0f} ₽")
        if decision.recommended_reduce_rub > 0:
            lines.append(
                f"  сократить ориентировочно {decision.recommended_reduce_rub:,.0f} ₽"
            )
        lines.append(f"  основание: {'; '.join(decision.reasons)}")
    lines.extend(["", "Новые выпуски рынка:"])
    if not report.new_bond_candidates:
        lines.append("- кандидатов, прошедших все фильтры, нет")
    for candidate in report.new_bond_candidates:
        lines.append(
            f"- {candidate.ticker} · {candidate.emitter_name} · доходность "
            f"{candidate.comparable_yield_percent:.2f}% · рейтинг "
            f"{candidate.broad_rating_band.value} · уровень листинга "
            f"{candidate.list_level}"
        )
        if candidate.recommended_add_rub > 0:
            availability = (
                "доступность в БКС подтверждена; точные лоты зависят от свежей котировки"
                if candidate.bcs_availability_verified
                else "доступность и лоты в БКС ещё не подтверждены"
            )
            lines.append(
                f"  ориентир распределения {candidate.recommended_add_rub:,.0f} ₽; "
                f"{availability}"
            )
    lines.extend(["", "Сценарии:"])
    for scenario in report.scenarios:
        marker = "РЕКОМЕНДОВАН" if scenario.recommended else "АЛЬТЕРНАТИВА"
        lines.append(
            f"- {marker} {scenario.code}: вложить {scenario.invested_cash_rub:,.0f} ₽; "
            f"ожидаемые продажи {scenario.estimated_sale_proceeds_rub:,.0f} ₽; "
            f"остаток денег {scenario.remaining_cash_rub:,.0f} ₽; "
            f"доля облигаций {scenario.projected_bond_share_managed:.2%}"
        )
        lines.append(f"  {scenario.explanation}")
    lines.extend(
        [
            "",
            "Исполнение: только рекомендация. Заявки не созданы.",
            "Для любой сделки нужны отдельные точные параметры и явное подтверждение.",
        ]
    )
    return "\n".join(lines)


def _validate_inputs(
    audit: PortfolioAudit,
    bonds: BondMarketReport,
    credit: CreditPortfolioReport,
    universe: BondUniverseReport | None,
) -> None:
    if len({audit.account_ref, bonds.account_ref, credit.account_ref}) != 1:
        raise ValueError("manager input account mismatch")
    if bonds.bond_value_rub != credit.bond_value_rub:
        raise ValueError("manager bond value mismatch")
    bond_tickers = {record.position.ticker for record in bonds.positions}
    credit_tickers = {passport.ticker for passport in credit.passports}
    if not credit_tickers.issubset(bond_tickers):
        raise ValueError("manager credit passport is outside the bond report")
    if universe is not None and universe.account_ref != audit.account_ref:
        raise ValueError("manager universe account mismatch")


def _worst_band(ratings: tuple[Any, ...]) -> RatingBand | None:
    bands = [rating.band for rating in ratings]
    return None if not bands else max(bands, key=_band_rank)


def _band_rank(band: RatingBand) -> int:
    return {
        RatingBand.HIGHEST: 0,
        RatingBand.HIGH: 1,
        RatingBand.STRONG: 2,
        RatingBand.ADEQUATE: 3,
        RatingBand.SPECULATIVE: 4,
        RatingBand.HIGH_RISK: 5,
        RatingBand.VERY_HIGH_RISK: 6,
        RatingBand.DEFAULT: 7,
        RatingBand.UNRATED: 8,
    }[band]


def _new_candidate_decisions(
    universe: BondUniverseReport | None,
    manager_policy: ManagerPolicy,
) -> list[NewBondManagerDecision]:
    if universe is None:
        return []
    return [
        NewBondManagerDecision(
            ticker=candidate.ticker,
            isin=candidate.isin,
            name=candidate.name,
            emitter_id=candidate.emitter_id,
            emitter_name=candidate.emitter_name,
            current_issuer_share_of_bonds=(
                candidate.current_issuer_share_of_bonds
            ),
            broad_rating_band=candidate.broad_rating_band,
            stress_loss_fraction=manager_policy.stress_loss_fraction(
                candidate.broad_rating_band
            ),
            comparable_yield_percent=candidate.effective_yield_percent,
            duration_days=candidate.duration_days,
            estimated_lot_cost_rub=candidate.estimated_lot_cost_rub,
            reference_buy_price_percent=candidate.reference_buy_price_percent,
            turnover_today_rub=candidate.turnover_today_rub,
            ranking_score=candidate.ranking_score,
            recommended_add_rub=Decimal("0"),
            reasons=candidate.reasons,
            bcs_availability_verified=False,
            moex_security_url=candidate.moex_security_url,
            moex_market_url=candidate.moex_market_url,
            list_level=candidate.list_level,
        )
        for candidate in universe.candidates
    ]


def _risk_return_score(
    comparable_yield_percent: Decimal,
    stress_loss_fraction: Decimal,
) -> Decimal:
    if stress_loss_fraction <= 0:
        raise ValueError("stress loss fraction must be positive")
    return comparable_yield_percent / (stress_loss_fraction * Decimal("100"))


def _risk_adjusted_issuer_share_limit(
    concentration_limit: Decimal,
    maximum_drawdown: Decimal,
    stress_loss_fraction: Decimal,
) -> Decimal:
    if stress_loss_fraction <= 0:
        raise ValueError("stress loss fraction must be positive")
    return min(
        concentration_limit,
        concentration_limit * maximum_drawdown / stress_loss_fraction,
    )


def _issuer_add_capacity(
    *,
    current_value: Decimal,
    sleeve_value: Decimal,
    maximum_share: Decimal,
) -> Decimal:
    numerator = maximum_share * sleeve_value - current_value
    if numerator <= 0:
        return Decimal("0")
    return numerator / (Decimal("1") - maximum_share)


def _comparable_yield(
    records: tuple[EnrichedBondPosition, ...],
) -> tuple[Decimal | None, Decimal]:
    covered = [
        (record.position.market_value_rub, record.comparable_effective_yield_percent)
        for record in records
        if record.comparable_effective_yield_percent is not None
    ]
    covered_value = sum((value for value, _ in covered), Decimal("0"))
    total_value = sum(
        (record.position.market_value_rub for record in records),
        Decimal("0"),
    )
    if covered_value == 0:
        return None, Decimal("0")
    weighted = sum(
        (value * yield_percent for value, yield_percent in covered if yield_percent is not None),
        Decimal("0"),
    ) / covered_value
    return weighted, _share(covered_value, total_value)


def _yield_after_allocations(
    records: tuple[EnrichedBondPosition, ...],
    decisions: list[BondManagerDecision],
    new_candidates: list[NewBondManagerDecision],
) -> Decimal | None:
    current, _ = _comparable_yield(records)
    covered_value = sum(
        (
            record.position.market_value_rub
            for record in records
            if record.comparable_effective_yield_percent is not None
        ),
        Decimal("0"),
    )
    numerator = Decimal("0") if current is None else current * covered_value
    allocated = Decimal("0")
    for decision in decisions:
        if (
            decision.recommended_reduce_rub > 0
            and decision.comparable_yield_percent is not None
        ):
            reduction = min(decision.recommended_reduce_rub, decision.market_value_rub)
            numerator -= reduction * decision.comparable_yield_percent
            covered_value -= reduction
        if decision.recommended_add_rub <= 0 or decision.comparable_yield_percent is None:
            continue
        numerator += decision.recommended_add_rub * decision.comparable_yield_percent
        allocated += decision.recommended_add_rub
    for candidate in new_candidates:
        if candidate.recommended_add_rub <= 0:
            continue
        numerator += candidate.recommended_add_rub * candidate.comparable_yield_percent
        allocated += candidate.recommended_add_rub
    denominator = covered_value + allocated
    return None if denominator == 0 else numerator / denominator


def _planned_sale_proceeds(decisions: list[BondManagerDecision]) -> Decimal:
    return sum(
        (decision.recommended_reduce_rub for decision in decisions),
        Decimal("0"),
    )


def _primary_action(
    decisions: list[BondManagerDecision],
    new_candidates: list[NewBondManagerDecision],
) -> str:
    if any(decision.action is ManagerAction.URGENT_REVIEW for decision in decisions):
        return "VERIFY_CRITICAL_FLAG_BEFORE_NEW_RISK"
    has_reduction = any(
        decision.action is ManagerAction.REDUCE_RISK for decision in decisions
    )
    has_purchase = any(
        decision.recommended_add_rub > 0 for decision in decisions
    ) or any(candidate.recommended_add_rub > 0 for candidate in new_candidates)
    if has_reduction and has_purchase:
        return "REBALANCE_CREDIT_RISK_AND_INVEST_CASH"
    if has_reduction:
        return "REDUCE_CREDIT_RISK_BEFORE_NEW_RISK"
    if has_purchase:
        return "INVEST_CASH_SELECTIVELY"
    return "HOLD_CASH_AND_SCAN_MARKET"


def _decision_sort_key(decision: BondManagerDecision) -> tuple[int, Decimal, str]:
    action_rank = {
        ManagerAction.URGENT_REVIEW: 0,
        ManagerAction.REDUCE_RISK: 1,
        ManagerAction.DO_NOT_ADD: 2,
        ManagerAction.ADD_CANDIDATE: 3,
        ManagerAction.HOLD: 4,
    }
    return (action_rank[decision.action], -decision.market_value_rub, decision.ticker)


def _decision_as_dict(decision: BondManagerDecision) -> dict[str, Any]:
    return {
        "ticker": decision.ticker,
        "isin": decision.isin,
        "emitter_id": decision.emitter_id,
        "emitter_name": decision.emitter_name,
        "market_value_rub": _decimal_text(decision.market_value_rub),
        "issuer_share_of_bonds": _decimal_text(decision.issuer_share_of_bonds),
        "action": decision.action.value,
        "broad_rating_band": (
            None if decision.broad_rating_band is None else decision.broad_rating_band.value
        ),
        "stress_loss_fraction": _decimal_text(decision.stress_loss_fraction),
        "risk_return_score": _optional_decimal_text(decision.risk_return_score),
        "comparable_yield_percent": _optional_decimal_text(
            decision.comparable_yield_percent
        ),
        "duration_days": _optional_decimal_text(decision.duration_days),
        "estimated_unit_value_rub": _decimal_text(
            decision.estimated_unit_value_rub
        ),
        "available_units": _decimal_text(decision.available_units),
        "recommended_add_rub": _decimal_text(decision.recommended_add_rub),
        "recommended_reduce_rub": _decimal_text(decision.recommended_reduce_rub),
        "recommended_reduce_units": decision.recommended_reduce_units,
        "credit_warning": decision.credit_warning,
        "reasons": list(decision.reasons),
        "exit_triggers": list(decision.exit_triggers),
    }


def _new_candidate_as_dict(candidate: NewBondManagerDecision) -> dict[str, Any]:
    return {
        "ticker": candidate.ticker,
        "isin": candidate.isin,
        "name": candidate.name,
        "emitter_id": candidate.emitter_id,
        "emitter_name": candidate.emitter_name,
        "current_issuer_share_of_bonds": _decimal_text(
            candidate.current_issuer_share_of_bonds
        ),
        "broad_rating_band": candidate.broad_rating_band.value,
        "stress_loss_fraction": _decimal_text(candidate.stress_loss_fraction),
        "risk_return_score": _decimal_text(candidate.risk_return_score),
        "comparable_yield_percent": _decimal_text(
            candidate.comparable_yield_percent
        ),
        "duration_days": _decimal_text(candidate.duration_days),
        "estimated_lot_cost_rub": _decimal_text(candidate.estimated_lot_cost_rub),
        "reference_buy_price_percent": _decimal_text(
            candidate.reference_buy_price_percent
        ),
        "turnover_today_rub": _decimal_text(candidate.turnover_today_rub),
        "list_level": candidate.list_level,
        "ranking_score": _decimal_text(candidate.ranking_score),
        "recommended_add_rub": _decimal_text(candidate.recommended_add_rub),
        "reasons": list(candidate.reasons),
        "bcs_availability_verified": candidate.bcs_availability_verified,
        "sources": {
            "security": candidate.moex_security_url,
            "market": candidate.moex_market_url,
        },
    }


def _scenario_as_dict(scenario: ManagerScenario) -> dict[str, Any]:
    return {
        "code": scenario.code,
        "recommended": scenario.recommended,
        "invested_cash_rub": _decimal_text(scenario.invested_cash_rub),
        "estimated_sale_proceeds_rub": _decimal_text(
            scenario.estimated_sale_proceeds_rub
        ),
        "remaining_cash_rub": _decimal_text(scenario.remaining_cash_rub),
        "net_bond_change_rub": _decimal_text(scenario.net_bond_change_rub),
        "projected_bond_share_managed": _decimal_text(
            scenario.projected_bond_share_managed
        ),
        "comparable_bond_yield_percent": _optional_decimal_text(
            scenario.comparable_bond_yield_percent
        ),
        "required_stock_sales_rub": _decimal_text(scenario.required_stock_sales_rub),
        "explanation": scenario.explanation,
    }


def _fraction(value: Any, field: str) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or not Decimal("0") < result <= Decimal("1"):
        raise ValueError(f"{field} must be in (0, 1]")
    return result


def _positive_decimal(value: Any, field: str) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{field} must be positive")
    return result


def _nonnegative_decimal(value: Any, field: str) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError(f"{field} must be nonnegative")
    return result


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _rating_band(value: Any, field: str) -> RatingBand:
    try:
        band = RatingBand(str(value).strip().upper())
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a supported rating band") from error
    if band not in {
        RatingBand.HIGHEST,
        RatingBand.HIGH,
        RatingBand.STRONG,
        RatingBand.ADEQUATE,
    }:
        raise ValueError(f"{field} must be investment grade")
    return band


def _round_down(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _round_up(value: Decimal, step: Decimal) -> Decimal:
    rounded_down = _round_down(value, step)
    return rounded_down if rounded_down == value else rounded_down + step


def _share(value: Decimal, denominator: Decimal) -> Decimal:
    return Decimal("0") if denominator == 0 else value / denominator


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else _decimal_text(value)


def _percent_text(value: Decimal | None) -> str:
    return "н/д" if value is None else f"{value:.2f}%"
