from __future__ import annotations

import unittest
from datetime import UTC, date, datetime
from decimal import Decimal

from invest_agent.bond_report import BondMarketReport
from invest_agent.credit import CreditAnalysisPolicy, RatingBand
from invest_agent.domain import PortfolioSnapshot
from invest_agent.market.moex import (
    MoexBondFacts,
    MoexBondMarketData,
    MoexBondSnapshot,
    MoexBondUniverseQuote,
    MoexEmitter,
)
from invest_agent.ratings.cbr import CbrRatingAction
from invest_agent.universe import BondCandidateScreener, BondUniversePolicy

NOW = datetime(2026, 8, 8, 16, 0, tzinfo=UTC)


def policy() -> BondUniversePolicy:
    return BondUniversePolicy(
        board_id="TQCB",
        pre_credit_limit=8,
        result_limit=5,
        minimum_yield_percent=Decimal("20"),
        maximum_yield_percent=Decimal("30"),
        minimum_duration_days=Decimal("90"),
        maximum_duration_days=Decimal("1200"),
        minimum_days_to_maturity=180,
        minimum_issue_notional_rub=Decimal("1000000000"),
        minimum_turnover_rub=Decimal("500000"),
        maximum_lot_share_of_cash=Decimal("0.25"),
        maximum_list_level=2,
    )


def quote(ticker: str, *, currency: str = "SUR") -> MoexBondUniverseQuote:
    return MoexBondUniverseQuote(
        secid=ticker,
        board_id="TQCB",
        short_name=ticker,
        isin=ticker,
        lot_size=1,
        face_value=Decimal("1000"),
        face_currency=currency,
        status="A",
        list_level=2,
        maturity_date=date(2029, 5, 18),
        issue_size=Decimal("2000000"),
        accrued_interest_rub=Decimal("10"),
        previous_price_percent=Decimal("100"),
        effective_yield_percent=Decimal("24"),
        duration_days=Decimal("500"),
        bid_percent=Decimal("99.9"),
        offer_percent=Decimal("100.1"),
        last_percent=Decimal("100"),
        wap_percent=Decimal("100"),
        trades_today=100,
        turnover_today_rub=Decimal("2000000"),
        trading_status="T",
        trade_moment=NOW,
        source_url="https://iss.moex.test/universe",
    )


def bond(ticker: str, emitter_id: int) -> MoexBondSnapshot:
    facts = MoexBondFacts(
        secid=ticker,
        isin=ticker,
        name=ticker,
        short_name=ticker,
        primary_board="TQCB",
        emitter_id=emitter_id,
        issue_name=ticker,
        registration_number=None,
        issue_date=date(2026, 1, 1),
        maturity_date=date(2029, 5, 18),
        face_value=Decimal("1000"),
        face_currency="RUB",
        issue_size=Decimal("2000000"),
        list_level=2,
        coupon_percent=Decimal("20"),
        coupon_value=Decimal("20"),
        coupon_frequency=12,
        coupon_benchmark=None,
        coupon_benchmark_spread=None,
        next_coupon_date=date(2026, 9, 1),
        offer_date=None,
        bond_type="Фиксированный купон",
        bond_subtype="Корпоративный",
        qualified_only=False,
        has_default=False,
        has_technical_default=False,
        source_url=f"https://iss.moex.test/{ticker}",
    )
    market = MoexBondMarketData(
        board_id="TQCB",
        bid_percent=Decimal("99.9"),
        offer_percent=Decimal("100.1"),
        last_percent=Decimal("100"),
        wap_percent=Decimal("100"),
        yield_at_wap_percent=Decimal("24"),
        effective_yield_percent=Decimal("24"),
        yield_date=date(2029, 5, 18),
        yield_date_type="MATDATE",
        duration_days=Decimal("500"),
        z_spread_bps=Decimal("800"),
        g_spread_bps=Decimal("820"),
        trades_today=100,
        volume_today=Decimal("1000"),
        turnover_today_rub=Decimal("2000000"),
        trading_status="T",
        trade_moment=NOW,
        system_moment=NOW,
        source_url=f"https://iss.moex.test/market/{ticker}",
    )
    return MoexBondSnapshot(facts=facts, market=market, fetched_at=NOW)


class FakeMoex:
    def __init__(self, quotes: tuple[MoexBondUniverseQuote, ...]) -> None:
        self.quotes = quotes

    def fetch_bond_universe(self, board_id: str) -> tuple[MoexBondUniverseQuote, ...]:
        self.board_id = board_id
        return self.quotes

    def fetch_bond(self, secid: str) -> MoexBondSnapshot:
        return bond(secid, int(secid[-1]))

    def fetch_emitter(self, emitter_id: int) -> MoexEmitter:
        return MoexEmitter(
            emitter_id=emitter_id,
            title=f"Эмитент {emitter_id}",
            short_title=f"Эмитент {emitter_id}",
            inn=f"{emitter_id:010d}",
            ogrn=None,
            website=None,
            source_url=f"https://iss.moex.test/emitter/{emitter_id}",
        )


class FakeRatings:
    def __init__(self, *, outlook: str = "Стабильный") -> None:
        self.outlook = outlook

    def fetch_by_inn(self, inn: str) -> tuple[CbrRatingAction, ...]:
        return (
            CbrRatingAction(
                object_id=f"issuer-{inn}",
                object_name=f"Эмитент {inn}",
                subject_name=f"Эмитент {inn}",
                object_type="issuer",
                inn=inn,
                isin=None,
                rating_value="A(RU)",
                outlook=self.outlook,
                agency="АКРА",
                release_date=date(2026, 7, 1),
                rating_action="Подтверждение",
                release_url="https://rating.test/release",
            ),
        )

    def fetch_by_isin(self, isin: str) -> tuple[CbrRatingAction, ...]:
        return ()


def current_bonds() -> BondMarketReport:
    return BondMarketReport(
        account_ref="bcs:test",
        portfolio_as_of=NOW,
        fetched_at=NOW,
        total_portfolio_value_rub=Decimal("50000"),
        total_bond_positions=0,
        bond_value_rub=Decimal("0"),
        covered_bond_value_rub=Decimal("0"),
        market_covered_bond_value_rub=Decimal("0"),
        issuer_covered_value_rub=Decimal("0"),
        positions=(),
        issuer_exposures=(),
        failures=(),
        issuer_hhi_on_covered_bonds=Decimal("0"),
        effective_issuer_count=None,
    )


class BondCandidateScreenerTests(unittest.TestCase):
    def snapshot(self) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            account_ref="bcs:test",
            is_iis=True,
            as_of=NOW,
            cash_rub=Decimal("50000"),
            positions=(),
        )

    def test_keeps_only_ruble_liquid_credit_clean_candidate(self) -> None:
        eligible = quote("RU000A000001")
        foreign = quote("RU000A000002", currency="USD")
        screener = BondCandidateScreener(
            FakeMoex((eligible, foreign)),  # type: ignore[arg-type]
            FakeRatings(),
            CreditAnalysisPolicy(rating_max_age_days=400),
            policy(),
            now=lambda: NOW,
        )

        report = screener.screen(self.snapshot(), current_bonds())

        self.assertEqual(report.scanned_count, 2)
        self.assertEqual(report.coarse_eligible_count, 1)
        self.assertEqual(len(report.candidates), 1)
        self.assertEqual(report.candidates[0].broad_rating_band, RatingBand.STRONG)
        self.assertEqual(report.candidates[0].estimated_lot_cost_rub, Decimal("1011.0"))

    def test_rejects_negative_rating_outlook(self) -> None:
        screener = BondCandidateScreener(
            FakeMoex((quote("RU000A000001"),)),  # type: ignore[arg-type]
            FakeRatings(outlook="Негативный"),
            CreditAnalysisPolicy(rating_max_age_days=400),
            policy(),
            now=lambda: NOW,
        )

        report = screener.screen(self.snapshot(), current_bonds())

        self.assertEqual(report.detailed_count, 1)
        self.assertEqual(report.candidates, ())

    def test_filters_coarse_universe_by_bcs_buy_availability_before_credit_work(self) -> None:
        allowed = quote("RU000A000001")
        unavailable = quote("RU000A000002")
        requested: list[tuple[str, ...]] = []

        def buy_availability(isins: tuple[str, ...]) -> set[str]:
            requested.append(isins)
            return {allowed.isin}

        screener = BondCandidateScreener(
            FakeMoex((allowed, unavailable)),  # type: ignore[arg-type]
            FakeRatings(),
            CreditAnalysisPolicy(rating_max_age_days=400),
            policy(),
            buy_availability=buy_availability,
            now=lambda: NOW,
        )

        report = screener.screen(self.snapshot(), current_bonds())

        self.assertEqual(requested, [(allowed.isin, unavailable.isin)])
        self.assertEqual(report.coarse_eligible_count, 2)
        self.assertEqual(report.detailed_count, 1)
        self.assertEqual([candidate.isin for candidate in report.candidates], [allowed.isin])
        self.assertEqual(dict(report.rejection_counts)["bcs_buy_unavailable"], 1)


if __name__ == "__main__":
    unittest.main()
