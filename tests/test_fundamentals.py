from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

from invest_agent.bond_report import BondMarketReport, EnrichedBondPosition
from invest_agent.domain import InstrumentType, Position
from invest_agent.financial.fns import GirboApiError, RasFinancialStatement
from invest_agent.fundamentals import (
    FundamentalPolicy,
    FundamentalPortfolioAnalyzer,
    render_fundamental_report_json,
)
from invest_agent.market.moex import (
    MoexBondAmortization,
    MoexBondCoupon,
    MoexBondFacts,
    MoexBondOffer,
    MoexBondSchedule,
    MoexBondSnapshot,
    MoexEmitter,
)

NOW = datetime(2026, 8, 8, 15, 30, tzinfo=UTC)


def policy() -> FundamentalPolicy:
    return FundamentalPolicy(
        financial_max_age_days=550,
        revenue_decline_warning=Decimal("0.10"),
        interest_coverage_warning=Decimal("1"),
        current_ratio_warning=Decimal("1"),
        debt_to_equity_warning=Decimal("3"),
        near_term_event_days=365,
    )


def statement(*, equity: str = "410758") -> RasFinancialStatement:
    return RasFinancialStatement(
        organization_id=10703464,
        organization_name="ООО ВУШ",
        inn="9717068640",
        period=2025,
        previous_period=2024,
        reported_at=date(2026, 4, 30),
        has_audit_report=True,
        correction_version=1,
        revenue=Decimal("10694433"),
        previous_revenue=Decimal("13196362"),
        operating_profit=Decimal("917898"),
        previous_operating_profit=Decimal("1618967"),
        interest_income=Decimal("352627"),
        interest_expense=Decimal("2583863"),
        profit_before_tax=Decimal("-2549955"),
        net_income=Decimal("-2549955"),
        previous_net_income=Decimal("360446"),
        cash=Decimal("330829"),
        long_term_debt=Decimal("9287656"),
        short_term_debt=Decimal("4607881"),
        equity=Decimal(equity),
        total_assets=Decimal("15994976"),
        current_assets=Decimal("4971431"),
        current_liabilities=Decimal("5382399"),
        operating_cash_flow=Decimal("213787"),
        investing_cash_flow=Decimal("-3333797"),
        financing_cash_flow=Decimal("174890"),
        source_url="https://bo.nalog.gov.ru/organizations-card/10703464",
        fetched_at=NOW,
    )


def schedule(secid: str, isin: str) -> MoexBondSchedule:
    return MoexBondSchedule(
        secid=secid,
        isin=isin,
        coupons=(
            MoexBondCoupon(
                coupon_date=date(2026, 9, 1),
                record_date=None,
                start_date=date(2026, 8, 1),
                face_value=Decimal("1000"),
                currency="RUB",
                value=Decimal("16"),
                annual_percent=Decimal("20"),
                value_rub=Decimal("16"),
            ),
        ),
        amortizations=(
            MoexBondAmortization(
                amortization_date=date(2027, 1, 10),
                face_value=Decimal("1000"),
                initial_face_value=Decimal("1000"),
                currency="RUB",
                value_percent=Decimal("100"),
                value=Decimal("1000"),
                value_rub=Decimal("1000"),
                data_source="maturity",
            ),
        ),
        offers=(
            MoexBondOffer(
                offer_date=date(2026, 12, 1),
                offer_start_date=date(2026, 11, 25),
                offer_end_date=date(2026, 11, 30),
                face_value=Decimal("1000"),
                currency="RUB",
                price_percent=Decimal("100"),
                value=Decimal("1000"),
                agent="Агент",
                offer_type="put",
            ),
        ),
        source_url=f"https://iss.moex.com/bondization/{secid}",
        fetched_at=NOW,
    )


def enriched_bond(
    ticker: str = "RU000A10BS76",
    *,
    isin: str = "RU000A10BS76",
    value: str = "100000",
) -> EnrichedBondPosition:
    position = Position(
        instrument_uid=f"BCS:TQCB:{ticker}",
        ticker=ticker,
        class_code="TQCB",
        instrument_type=InstrumentType.BOND,
        quantity=Decimal("100"),
        market_price=Decimal("1000"),
        market_value_rub=Decimal(value),
        tradable=True,
    )
    facts = MoexBondFacts(
        secid=ticker,
        isin=isin,
        name=ticker,
        short_name=ticker,
        primary_board="TQCB",
        emitter_id=1,
        issue_name=ticker,
        registration_number=None,
        issue_date=date(2025, 1, 1),
        maturity_date=date(2027, 1, 10),
        face_value=Decimal("1000"),
        face_currency="RUB",
        issue_size=Decimal("1000000"),
        list_level=2,
        coupon_percent=Decimal("20"),
        coupon_value=Decimal("16"),
        coupon_frequency=12,
        coupon_benchmark=None,
        coupon_benchmark_spread=None,
        next_coupon_date=date(2026, 9, 1),
        offer_date=date(2026, 12, 1),
        bond_type="Фикс",
        bond_subtype="До погашения",
        qualified_only=False,
        has_default=False,
        has_technical_default=False,
        source_url=f"https://iss.moex.com/{ticker}",
    )
    emitter = MoexEmitter(
        emitter_id=1,
        title="ООО ВУШ",
        short_title="ООО ВУШ",
        inn="9717068640",
        ogrn=None,
        website=None,
        source_url="https://iss.moex.com/emitters/1",
    )
    return EnrichedBondPosition(
        position=position,
        moex=MoexBondSnapshot(facts=facts, market=None, fetched_at=NOW),
        emitter=emitter,
        missing_fields=(),
    )


def market_report(*records: EnrichedBondPosition) -> BondMarketReport:
    value = sum((record.position.market_value_rub for record in records), Decimal("0"))
    return BondMarketReport(
        account_ref="bcs:test",
        portfolio_as_of=NOW,
        fetched_at=NOW,
        total_portfolio_value_rub=value,
        total_bond_positions=len(records),
        bond_value_rub=value,
        covered_bond_value_rub=value,
        market_covered_bond_value_rub=Decimal("0"),
        issuer_covered_value_rub=value,
        positions=records,
        issuer_exposures=(),
        failures=(),
        issuer_hhi_on_covered_bonds=Decimal("1"),
        effective_issuer_count=Decimal("1"),
    )


class FakeFinancialClient:
    def __init__(
        self,
        result: RasFinancialStatement | None,
        *,
        fail: bool = False,
    ) -> None:
        self.result = result
        self.fail = fail
        self.calls: list[str] = []

    def fetch_latest_annual(self, inn: str) -> RasFinancialStatement | None:
        self.calls.append(inn)
        if self.fail:
            raise GirboApiError("search", 503)
        return self.result


class FakeScheduleClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch_bond_schedule(self, secid: str) -> MoexBondSchedule:
        self.calls.append(secid)
        return schedule(secid, secid)


class FundamentalPortfolioTests(unittest.TestCase):
    def analyze(
        self,
        financial: FakeFinancialClient,
        schedule_client: FakeScheduleClient,
        *records: EnrichedBondPosition,
    ):
        return FundamentalPortfolioAnalyzer(
            financial,
            schedule_client,
            policy(),
            now=lambda: NOW,
        ).analyze(market_report(*records))

    def test_calculates_transparent_ras_metrics_and_signals(self) -> None:
        report = self.analyze(
            FakeFinancialClient(statement()),
            FakeScheduleClient(),
            enriched_bond(),
        )
        passport = report.passports[0]
        assert passport.metrics is not None
        codes = {signal.code for signal in passport.signals}

        self.assertEqual(passport.metrics.gross_debt, Decimal("13895537"))
        self.assertEqual(passport.metrics.net_debt, Decimal("13564708"))
        self.assertLess(passport.metrics.interest_coverage or Decimal("0"), Decimal("1"))
        self.assertIn("NET_LOSS", codes)
        self.assertIn("REVENUE_DECLINE", codes)
        self.assertIn("LOW_INTEREST_COVERAGE", codes)
        self.assertIn("LOW_CURRENT_RATIO", codes)
        self.assertIn("HIGH_DEBT_TO_EQUITY", codes)
        self.assertIn("NEGATIVE_APPROX_FCF", codes)
        self.assertIn("NEAR_TERM_OFFER", codes)
        assert passport.schedule is not None
        self.assertEqual(passport.schedule.next_offer_type, "put")
        self.assertEqual(passport.schedule.next_offer_price_percent, Decimal("100"))

    def test_absent_public_statement_is_not_source_failure(self) -> None:
        report = self.analyze(
            FakeFinancialClient(None),
            FakeScheduleClient(),
            enriched_bond(),
        )
        codes = {signal.code for signal in report.passports[0].signals}

        self.assertIn("NO_PUBLIC_RAS_STATEMENT", codes)
        self.assertNotIn("FINANCIAL_DATA_UNAVAILABLE", codes)
        self.assertEqual(report.financial_coverage_share, Decimal("0"))
        self.assertEqual(report.failures, ())

    def test_source_failure_is_explicit(self) -> None:
        report = self.analyze(
            FakeFinancialClient(None, fail=True),
            FakeScheduleClient(),
            enriched_bond(),
        )
        codes = {signal.code for signal in report.passports[0].signals}

        self.assertIn("FINANCIAL_DATA_UNAVAILABLE", codes)
        self.assertEqual(report.failures[0].stage, "financials")

    def test_negative_equity_is_critical(self) -> None:
        report = self.analyze(
            FakeFinancialClient(replace(statement(), equity=Decimal("-1"))),
            FakeScheduleClient(),
            enriched_bond(),
        )
        severities = {signal.code: signal.severity.value for signal in report.passports[0].signals}

        self.assertEqual(severities["NEGATIVE_EQUITY"], "CRITICAL")

    def test_caches_issuer_financials_and_report_is_analysis_only(self) -> None:
        financial = FakeFinancialClient(statement())
        second = enriched_bond("RU000A999999", isin="RU000A999999", value="50000")
        report = self.analyze(
            financial,
            FakeScheduleClient(),
            enriched_bond(),
            second,
        )
        payload = json.loads(render_fundamental_report_json(report))

        self.assertEqual(financial.calls, ["9717068640"])
        self.assertEqual(report.financial_coverage_share, Decimal("1"))
        self.assertEqual(report.schedule_coverage_share, Decimal("1"))
        self.assertEqual(payload["execution_state"], "ANALYSIS_ONLY")
        self.assertEqual(
            payload["passports"][0]["financial_statement"]["scope"],
            "LEGAL_ENTITY_RAS",
        )
        self.assertEqual(
            payload["passports"][0]["metrics"]["ratios"]["interest_coverage"],
            "0.355243",
        )
        self.assertEqual(
            payload["passports"][0]["payment_schedule"]["next_offer_type"],
            "put",
        )
        self.assertIn("not consolidated IFRS", payload["methodology"]["scope_warning"])


if __name__ == "__main__":
    unittest.main()
