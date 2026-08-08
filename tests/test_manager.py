from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from invest_agent.audit import PortfolioAudit
from invest_agent.bond_report import BondMarketReport, EnrichedBondPosition, IssuerExposure
from invest_agent.credit import (
    CreditPassport,
    CreditPortfolioReport,
    CreditRatingEvidence,
    CreditSignal,
    RatingBand,
    SignalSeverity,
)
from invest_agent.domain import InstrumentType, Position
from invest_agent.manager import (
    ManagerAction,
    ManagerPolicy,
    PortfolioManager,
    render_manager_report_json,
)
from invest_agent.market.moex import (
    MoexBondFacts,
    MoexBondMarketData,
    MoexBondSnapshot,
    MoexEmitter,
)
from invest_agent.policy import InvestmentPolicy
from invest_agent.ratings.cbr import CbrRatingAction
from invest_agent.universe import BondUniverseCandidate, BondUniverseReport

NOW = datetime(2026, 8, 8, 16, 0, tzinfo=UTC)
POLICY_PATH = Path(__file__).parents[1] / "config" / "investment_policy.toml"


def manager_policy() -> ManagerPolicy:
    return ManagerPolicy(
        max_bond_issuer_share_after_add=Decimal("0.15"),
        speculative_reduce_fraction=Decimal("0.50"),
        minimum_allocation_rub=Decimal("5000"),
        allocation_rounding_rub=Decimal("100"),
        maximum_single_purchase_share_of_cash=Decimal("0.40"),
    )


def bond(
    ticker: str,
    *,
    emitter_id: int,
    value: str,
    yield_percent: str | None,
) -> EnrichedBondPosition:
    position = Position(
        instrument_uid=f"BCS:TQCB:{ticker}",
        ticker=ticker,
        class_code="TQCB",
        instrument_type=InstrumentType.BOND,
        quantity=Decimal("10"),
        market_price=Decimal("1000"),
        market_value_rub=Decimal(value),
        tradable=True,
    )
    facts = MoexBondFacts(
        secid=ticker,
        isin=ticker,
        name=ticker,
        short_name=ticker,
        primary_board="TQCB",
        emitter_id=emitter_id,
        issue_name=ticker,
        registration_number=None,
        issue_date=date(2025, 1, 1),
        maturity_date=date(2029, 1, 1),
        face_value=Decimal("1000"),
        face_currency="RUB",
        issue_size=Decimal("1000000"),
        list_level=2,
        coupon_percent=Decimal("20"),
        coupon_value=Decimal("20"),
        coupon_frequency=12,
        coupon_benchmark=None,
        coupon_benchmark_spread=None,
        next_coupon_date=date(2026, 9, 1),
        offer_date=None,
        bond_type="Фикс с известным купоном",
        bond_subtype="До погашения",
        qualified_only=False,
        has_default=False,
        has_technical_default=False,
        source_url=f"https://iss.moex.com/{ticker}",
    )
    market = MoexBondMarketData(
        board_id="TQCB",
        bid_percent=None,
        offer_percent=None,
        last_percent=Decimal("100"),
        wap_percent=Decimal("100"),
        yield_at_wap_percent=(
            None if yield_percent is None else Decimal(yield_percent)
        ),
        effective_yield_percent=(
            None if yield_percent is None else Decimal(yield_percent)
        ),
        yield_date=date(2029, 1, 1),
        yield_date_type="MATURITY",
        duration_days=Decimal("500"),
        z_spread_bps=None,
        g_spread_bps=None,
        trades_today=10,
        volume_today=Decimal("100"),
        turnover_today_rub=Decimal("100000"),
        trading_status="T",
        trade_moment=NOW,
        system_moment=NOW,
        source_url=f"https://iss.moex.com/{ticker}",
    )
    emitter = MoexEmitter(
        emitter_id=emitter_id,
        title=f"Эмитент {emitter_id}",
        short_title=f"Эмитент {emitter_id}",
        inn=f"{emitter_id:010d}",
        ogrn=None,
        website=None,
        source_url=f"https://iss.moex.com/emitters/{emitter_id}",
    )
    return EnrichedBondPosition(
        position=position,
        moex=MoexBondSnapshot(facts=facts, market=market, fetched_at=NOW),
        emitter=emitter,
        missing_fields=(),
    )


def rating(
    ticker: str,
    *,
    emitter_id: int,
    band: RatingBand,
    signal: CreditSignal | None = None,
) -> CreditPassport:
    action = CbrRatingAction(
        object_id=f"rating-{ticker}",
        object_name=ticker,
        subject_name=f"Эмитент {emitter_id}",
        object_type="issuer",
        inn=f"{emitter_id:010d}",
        isin=None,
        rating_value="BBB(RU)",
        outlook="Стабильный",
        agency="АКРА",
        release_date=date(2026, 7, 1),
        rating_action="Подтверждение",
        release_url="https://example.test/rating",
    )
    evidence = CreditRatingEvidence(
        scope="ISSUER",
        action=action,
        band=band,
        status="CURRENT",
    )
    return CreditPassport(
        ticker=ticker,
        isin=ticker,
        emitter_id=emitter_id,
        emitter_name=f"Эмитент {emitter_id}",
        inn=f"{emitter_id:010d}",
        market_value_rub=Decimal("0"),
        ratings=(evidence,),
        signals=() if signal is None else (signal,),
        issuer_lookup_complete=True,
        issue_lookup_complete=True,
    )


def inputs(
    records: tuple[EnrichedBondPosition, ...],
    ratings: tuple[CreditPassport, ...],
    *,
    cash: str = "50000",
) -> tuple[PortfolioAudit, BondMarketReport, CreditPortfolioReport]:
    bond_value = sum((record.position.market_value_rub for record in records), Decimal("0"))
    cash_value = Decimal(cash)
    managed = bond_value + cash_value
    exposures = tuple(
        IssuerExposure(
            emitter_id=record.moex.facts.emitter_id,
            emitter_name=record.emitter.short_title if record.emitter else record.position.ticker,
            value_rub=record.position.market_value_rub,
            share_of_covered_bonds=record.position.market_value_rub / bond_value,
            issues=(record.position.ticker,),
        )
        for record in records
    )
    audit = PortfolioAudit(
        account_ref="bcs:test",
        as_of=NOW.isoformat(),
        snapshot_digest="digest",
        total_value_rub=managed,
        managed_value_rub=managed,
        managed_securities_value_rub=bond_value,
        blocked_value_rub=Decimal("0"),
        cash_rub=cash_value,
        full_allocation_rub={InstrumentType.BOND: bond_value},
        managed_allocation_rub={InstrumentType.BOND: bond_value},
        bond_share_full=bond_value / managed,
        bond_share_managed=bond_value / managed,
        cash_share_managed=cash_value / managed,
        blocked_share_full=Decimal("0"),
        target_bond_share=Decimal("0.9"),
        target_bond_share_is_hard_limit=False,
        bond_target_gap_rub=Decimal("0.9") * managed - bond_value,
        bond_share_after_investing_cash=Decimal("1"),
        bond_only_contribution_needed_rub=None,
        regular_contributions_needed=None,
        position_hhi=Decimal("0.5"),
        effective_position_count=Decimal("2"),
        top_positions=(),
        findings=(),
        data_gaps=(),
    )
    bonds = BondMarketReport(
        account_ref="bcs:test",
        portfolio_as_of=NOW,
        fetched_at=NOW,
        total_portfolio_value_rub=managed,
        total_bond_positions=len(records),
        bond_value_rub=bond_value,
        covered_bond_value_rub=bond_value,
        market_covered_bond_value_rub=bond_value,
        issuer_covered_value_rub=bond_value,
        positions=records,
        issuer_exposures=exposures,
        failures=(),
        issuer_hhi_on_covered_bonds=Decimal("0.5"),
        effective_issuer_count=Decimal("2"),
    )
    credit = CreditPortfolioReport(
        account_ref="bcs:test",
        portfolio_as_of=NOW,
        moex_fetched_at=NOW,
        fetched_at=NOW,
        rating_max_age_days=400,
        total_bond_positions=len(records),
        bond_value_rub=bond_value,
        rating_covered_value_rub=bond_value,
        passports=ratings,
        failures=(),
    )
    return audit, bonds, credit


class PortfolioManagerTests(unittest.TestCase):
    def manager(self) -> PortfolioManager:
        return PortfolioManager(
            InvestmentPolicy.from_toml(POLICY_PATH),
            manager_policy(),
            now=lambda: NOW,
        )

    def test_allocates_only_to_clean_target_yield_candidate_with_issuer_cap(self) -> None:
        candidate = bond("RU000A000001", emitter_id=1, value="10000", yield_percent="30")
        stabilizer = bond("RU000A000002", emitter_id=2, value="90000", yield_percent="18")
        audit, bonds, credit = inputs(
            (candidate, stabilizer),
            (
                rating("RU000A000001", emitter_id=1, band=RatingBand.ADEQUATE),
                rating("RU000A000002", emitter_id=2, band=RatingBand.HIGHEST),
            ),
        )

        report = self.manager().recommend(audit, bonds, credit)
        decisions = {decision.ticker: decision for decision in report.decisions}

        self.assertEqual(decisions["RU000A000001"].action, ManagerAction.ADD_CANDIDATE)
        self.assertEqual(decisions["RU000A000001"].recommended_add_rub, Decimal("5800"))
        self.assertEqual(decisions["RU000A000002"].action, ManagerAction.HOLD)
        scenario = next(item for item in report.scenarios if item.code == "INVEST_CURRENT_CASH")
        self.assertTrue(scenario.recommended)
        self.assertEqual(scenario.invested_cash_rub, Decimal("5800"))

    def test_third_level_existing_bond_cannot_be_add_candidate(self) -> None:
        candidate = bond("RU000A000001", emitter_id=1, value="10000", yield_percent="29")
        candidate = replace(
            candidate,
            moex=replace(
                candidate.moex,
                facts=replace(candidate.moex.facts, list_level=3),
            ),
        )
        stabilizer = bond("RU000A000002", emitter_id=2, value="90000", yield_percent="18")
        audit, bonds, credit = inputs(
            (candidate, stabilizer),
            (
                rating("RU000A000001", emitter_id=1, band=RatingBand.ADEQUATE),
                rating("RU000A000002", emitter_id=2, band=RatingBand.HIGHEST),
            ),
        )

        report = self.manager().recommend(audit, bonds, credit)
        decision = next(item for item in report.decisions if item.ticker == "RU000A000001")

        self.assertEqual(decision.action, ManagerAction.DO_NOT_ADD)
        self.assertEqual(decision.recommended_add_rub, Decimal("0"))
        self.assertIn(
            "уровень листинга выпуска выше допуска для новых покупок",
            decision.reasons,
        )

    def test_critical_flag_blocks_invest_scenario_and_speculative_is_reduce(self) -> None:
        candidate = bond("RU000A000001", emitter_id=1, value="10000", yield_percent="30")
        critical = bond("RU000A000002", emitter_id=2, value="30000", yield_percent="26")
        speculative = bond("RU000A000003", emitter_id=3, value="20000", yield_percent="28")
        audit, bonds, credit = inputs(
            (candidate, critical, speculative),
            (
                rating("RU000A000001", emitter_id=1, band=RatingBand.ADEQUATE),
                rating(
                    "RU000A000002",
                    emitter_id=2,
                    band=RatingBand.STRONG,
                    signal=CreditSignal(
                        "MOEX_DEFAULT_FLAG",
                        SignalSeverity.CRITICAL,
                        "требуется проверка default-флага",
                    ),
                ),
                rating(
                    "RU000A000003",
                    emitter_id=3,
                    band=RatingBand.SPECULATIVE,
                    signal=CreditSignal(
                        "SPECULATIVE_RATING",
                        SignalSeverity.WARNING,
                        "рейтинг BB или ниже",
                    ),
                ),
            ),
        )

        report = self.manager().recommend(audit, bonds, credit)
        decisions = {decision.ticker: decision for decision in report.decisions}

        self.assertEqual(decisions["RU000A000002"].action, ManagerAction.URGENT_REVIEW)
        self.assertEqual(decisions["RU000A000003"].action, ManagerAction.REDUCE_RISK)
        speculative_reduction = decisions["RU000A000003"].recommended_reduce_rub
        self.assertEqual(speculative_reduction, Decimal("10000"))
        scenario = next(item for item in report.scenarios if item.code == "INVEST_CURRENT_CASH")
        self.assertFalse(scenario.recommended)
        self.assertEqual(report.primary_action, "VERIFY_CRITICAL_FLAG_BEFORE_NEW_RISK")

    def test_noncritical_speculative_and_concentration_cannot_sell_over_half(
        self,
    ) -> None:
        speculative = bond(
            "RU000A000001", emitter_id=1, value="100000", yield_percent="28"
        )
        audit, bonds, credit = inputs(
            (speculative,),
            (
                rating(
                    "RU000A000001",
                    emitter_id=1,
                    band=RatingBand.SPECULATIVE,
                    signal=CreditSignal(
                        "SPECULATIVE_RATING",
                        SignalSeverity.WARNING,
                        "рейтинг BB или ниже",
                    ),
                ),
            ),
        )
        manager = PortfolioManager(
            InvestmentPolicy.from_toml(POLICY_PATH),
            replace(
                manager_policy(),
                concentration_trim_fraction=Decimal("0.50"),
                maximum_noncritical_reduce_fraction=Decimal("0.50"),
            ),
            now=lambda: NOW,
        )

        report = manager.recommend(audit, bonds, credit)

        self.assertEqual(
            report.decisions[0].recommended_reduce_rub,
            Decimal("50000"),
        )
        self.assertIn(
            "один некритический рейтинговый сигнал означает пошаговое "
            "сокращение, а не автоматический полный выход",
            report.decisions[0].reasons,
        )

    def test_issuer_limit_uses_all_issues_of_the_same_emitter(self) -> None:
        candidate = bond("RU000A000001", emitter_id=1, value="10000", yield_percent="30")
        sibling = bond("RU000A000002", emitter_id=1, value="30000", yield_percent="18")
        other = bond("RU000A000003", emitter_id=2, value="60000", yield_percent="18")
        audit, bonds, credit = inputs(
            (candidate, sibling, other),
            (
                rating("RU000A000001", emitter_id=1, band=RatingBand.ADEQUATE),
                rating("RU000A000002", emitter_id=1, band=RatingBand.ADEQUATE),
                rating("RU000A000003", emitter_id=2, band=RatingBand.HIGHEST),
            ),
        )
        bonds = replace(
            bonds,
            issuer_exposures=(
                IssuerExposure(
                    emitter_id=1,
                    emitter_name="Эмитент 1",
                    value_rub=Decimal("40000"),
                    share_of_covered_bonds=Decimal("0.4"),
                    issues=("RU000A000001", "RU000A000002"),
                ),
                IssuerExposure(
                    emitter_id=2,
                    emitter_name="Эмитент 2",
                    value_rub=Decimal("60000"),
                    share_of_covered_bonds=Decimal("0.6"),
                    issues=("RU000A000003",),
                ),
            ),
        )

        report = self.manager().recommend(audit, bonds, credit)
        decisions = {decision.ticker: decision for decision in report.decisions}

        self.assertEqual(decisions["RU000A000001"].action, ManagerAction.ADD_CANDIDATE)
        self.assertEqual(decisions["RU000A000001"].recommended_add_rub, Decimal("0"))

    def test_credit_reduction_must_precede_new_purchase(self) -> None:
        candidate = bond("RU000A000001", emitter_id=1, value="1000", yield_percent="30")
        speculative = bond("RU000A000002", emitter_id=2, value="99000", yield_percent="28")
        audit, bonds, credit = inputs(
            (candidate, speculative),
            (
                rating("RU000A000001", emitter_id=1, band=RatingBand.ADEQUATE),
                rating(
                    "RU000A000002",
                    emitter_id=2,
                    band=RatingBand.SPECULATIVE,
                    signal=CreditSignal(
                        "SPECULATIVE_RATING",
                        SignalSeverity.WARNING,
                        "рейтинг BB или ниже",
                    ),
                ),
            ),
        )

        report = self.manager().recommend(audit, bonds, credit)
        scenario = next(item for item in report.scenarios if item.code == "INVEST_CURRENT_CASH")

        self.assertGreater(scenario.invested_cash_rub, Decimal("0"))
        self.assertTrue(scenario.recommended)
        self.assertEqual(
            report.primary_action,
            "REBALANCE_CREDIT_RISK_AND_INVEST_CASH",
        )

    def test_rebalance_scenario_includes_sale_proceeds_and_net_bond_change(self) -> None:
        speculative = bond("RU000A000001", emitter_id=1, value="20000", yield_percent="28")
        stabilizer = bond("RU000A000002", emitter_id=2, value="80000", yield_percent="18")
        audit, bonds, credit = inputs(
            (speculative, stabilizer),
            (
                rating(
                    "RU000A000001",
                    emitter_id=1,
                    band=RatingBand.SPECULATIVE,
                    signal=CreditSignal(
                        "SPECULATIVE_RATING",
                        SignalSeverity.WARNING,
                        "рейтинг BB или ниже",
                    ),
                ),
                rating("RU000A000002", emitter_id=2, band=RatingBand.HIGHEST),
            ),
        )

        report = self.manager().recommend(audit, bonds, credit)
        scenario = next(item for item in report.scenarios if item.code == "INVEST_CURRENT_CASH")

        self.assertTrue(scenario.recommended)
        self.assertEqual(scenario.invested_cash_rub, Decimal("0"))
        self.assertEqual(scenario.estimated_sale_proceeds_rub, Decimal("10000"))
        self.assertEqual(scenario.remaining_cash_rub, Decimal("60000"))
        self.assertEqual(scenario.net_bond_change_rub, Decimal("-10000"))
        self.assertEqual(scenario.projected_bond_share_managed, Decimal("0.6"))

    def test_warning_freezes_addition_and_output_never_creates_orders(self) -> None:
        record = bond("RU000A000001", emitter_id=1, value="10000", yield_percent="30")
        warning = CreditSignal(
            "RATING_DOWNGRADE",
            SignalSeverity.WARNING,
            "рейтинг понижен",
        )
        audit, bonds, credit = inputs(
            (record,),
            (rating("RU000A000001", emitter_id=1, band=RatingBand.ADEQUATE, signal=warning),),
        )

        report = self.manager().recommend(audit, bonds, credit)
        payload = json.loads(render_manager_report_json(report))

        self.assertEqual(report.decisions[0].action, ManagerAction.REDUCE_RISK)
        self.assertGreater(report.decisions[0].recommended_reduce_rub, Decimal("0"))
        self.assertEqual(payload["trade_gate"]["state"], "RECOMMENDATION_ONLY")
        self.assertFalse(payload["trade_gate"]["orders_created"])
        self.assertTrue(payload["trade_gate"]["explicit_confirmation_required"])
        self.assertEqual(len(payload["scenarios"]), 3)

        gradual_manager = PortfolioManager(
            InvestmentPolicy.from_toml(POLICY_PATH),
            replace(manager_policy(), concentration_trim_fraction=Decimal("0.50")),
            now=lambda: NOW,
        )
        gradual = gradual_manager.recommend(audit, bonds, credit)
        self.assertEqual(
            gradual.decisions[0].recommended_reduce_rub,
            Decimal("5000"),
        )

    def test_allocates_cash_to_ranked_new_issuers_without_creating_orders(self) -> None:
        current = bond("RU000A000001", emitter_id=1, value="100000", yield_percent="18")
        audit, bonds, credit = inputs(
            (current,),
            (rating("RU000A000001", emitter_id=1, band=RatingBand.HIGHEST),),
        )
        candidates = tuple(
            BondUniverseCandidate(
                ticker=f"RU000A00000{index}",
                isin=f"RU000A00000{index}",
                name=f"Новый выпуск {index}",
                emitter_id=index,
                emitter_name=f"Новый эмитент {index}",
                current_issuer_share_of_bonds=Decimal("0"),
                broad_rating_band=RatingBand.STRONG,
                effective_yield_percent=Decimal(yield_percent),
                duration_days=Decimal("500"),
                lot_size=1,
                estimated_lot_cost_rub=Decimal("1010"),
                reference_buy_price_percent=Decimal("100"),
                turnover_today_rub=Decimal("1000000"),
                ranking_score=Decimal(score),
                reasons=("чистый кредитный профиль",),
                moex_security_url="https://iss.moex.test/security",
                moex_market_url="https://iss.moex.test/market",
            )
            for index, yield_percent, score in ((2, "24", "22"), (3, "23", "21"))
        )
        universe = BondUniverseReport(
            account_ref=audit.account_ref,
            portfolio_as_of=NOW,
            fetched_at=NOW,
            board_id="TQCB",
            scanned_count=1000,
            coarse_eligible_count=20,
            detailed_count=8,
            candidates=candidates,
            rejection_counts=(),
            failures=(),
        )

        report = self.manager().recommend(audit, bonds, credit, universe)
        payload = json.loads(render_manager_report_json(report))

        self.assertEqual(
            [item.recommended_add_rub for item in report.new_bond_candidates],
            [Decimal("17600"), Decimal("17600")],
        )
        scenario = next(item for item in report.scenarios if item.code == "INVEST_CURRENT_CASH")
        self.assertTrue(scenario.recommended)
        self.assertEqual(scenario.invested_cash_rub, Decimal("35200"))
        self.assertFalse(payload["trade_gate"]["orders_created"])
        self.assertFalse(payload["new_bond_candidates"][0]["bcs_availability_verified"])

        filtered = self.manager().apply_bcs_buy_availability(
            report,
            audit,
            bonds,
            {"RU000A000003"},
        )
        self.assertEqual(len(filtered.new_bond_candidates), 1)
        self.assertEqual(filtered.new_bond_candidates[0].isin, "RU000A000003")
        self.assertEqual(
            filtered.new_bond_candidates[0].recommended_add_rub,
            Decimal("17600"),
        )
        self.assertTrue(filtered.new_bond_candidates[0].bcs_availability_verified)

        guarded_manager = PortfolioManager(
            InvestmentPolicy.from_toml(POLICY_PATH),
            replace(manager_policy(), minimum_cash_reserve_rub=Decimal("20000")),
            now=lambda: NOW,
        )
        guarded = guarded_manager.recommend(audit, bonds, credit, universe)
        guarded_scenario = next(
            item for item in guarded.scenarios if item.code == "INVEST_CURRENT_CASH"
        )
        self.assertEqual(guarded_scenario.invested_cash_rub, Decimal("24000"))
        self.assertEqual(guarded_scenario.remaining_cash_rub, Decimal("26000"))

        limited_manager = PortfolioManager(
            InvestmentPolicy.from_toml(POLICY_PATH),
            replace(manager_policy(), maximum_purchase_count=1),
            now=lambda: NOW,
        )
        limited = limited_manager.recommend(audit, bonds, credit, universe)
        self.assertEqual(
            sum(
                candidate.recommended_add_rub > 0
                for candidate in limited.new_bond_candidates
            ),
            1,
        )

    def test_rejects_cross_account_inputs(self) -> None:
        record = bond("RU000A000001", emitter_id=1, value="10000", yield_percent="30")
        audit, bonds, credit = inputs(
            (record,),
            (rating("RU000A000001", emitter_id=1, band=RatingBand.ADEQUATE),),
        )
        mismatched = replace(audit, account_ref="bcs:other")

        with self.assertRaisesRegex(ValueError, "account mismatch"):
            self.manager().recommend(mismatched, bonds, credit)


if __name__ == "__main__":
    unittest.main()
