from __future__ import annotations

import json
import unittest
from datetime import UTC, date, datetime
from decimal import Decimal

from invest_agent.bond_report import BondMarketReport, EnrichedBondPosition
from invest_agent.credit import (
    CreditAnalysisPolicy,
    CreditPortfolioAnalyzer,
    RatingBand,
    normalize_rating_band,
    render_credit_report_json,
)
from invest_agent.domain import InstrumentType, Position
from invest_agent.market.moex import MoexBondFacts, MoexBondSnapshot, MoexEmitter
from invest_agent.ratings.cbr import CbrRatingAction, CbrRatingsApiError

NOW = datetime(2026, 8, 8, 14, 40, tzinfo=UTC)


def action(
    *,
    object_id: str = "issuer",
    inn: str = "1234567890",
    isin: str | None = None,
    value: str = "BBB+(RU)",
    outlook: str = "Стабильный",
    released: date = date(2026, 4, 1),
    rating_action: str = "Рейтинг подтвержден",
    agency: str = "АКРА (АО)",
) -> CbrRatingAction:
    return CbrRatingAction(
        object_id=object_id,
        object_name="Эмитент",
        subject_name=None,
        object_type="BNFC" if isin is None else "BND",
        inn=inn,
        isin=isin,
        rating_value=value,
        outlook=outlook,
        agency=agency,
        release_date=released,
        rating_action=rating_action,
        release_url="https://agency.example/release",
    )


def enriched_bond(
    ticker: str = "BOND1",
    *,
    isin: str = "RU000A123456",
    value: str = "100000",
    emitter_id: int = 1,
    has_default: bool = False,
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
        emitter_id=emitter_id,
        issue_name=ticker,
        registration_number=None,
        issue_date=date(2025, 1, 1),
        maturity_date=date(2028, 1, 1),
        face_value=Decimal("1000"),
        face_currency="SUR",
        issue_size=Decimal("1000000"),
        list_level=2,
        coupon_percent=Decimal("20"),
        coupon_value=Decimal("16"),
        coupon_frequency=12,
        coupon_benchmark=None,
        coupon_benchmark_spread=None,
        next_coupon_date=date(2026, 9, 1),
        offer_date=None,
        bond_type="Фикс",
        bond_subtype="До погашения",
        qualified_only=False,
        has_default=has_default,
        has_technical_default=False,
        source_url=f"https://iss.moex.com/{ticker}",
    )
    emitter = MoexEmitter(
        emitter_id=emitter_id,
        title="ООО Эмитент",
        short_title="ООО Эмитент",
        inn="1234567890",
        ogrn=None,
        website=None,
        source_url=f"https://iss.moex.com/emitters/{emitter_id}",
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


class FakeRatingsClient:
    def __init__(
        self,
        *,
        issuer: tuple[CbrRatingAction, ...] = (),
        issues: dict[str, tuple[CbrRatingAction, ...]] | None = None,
        fail: bool = False,
    ) -> None:
        self.issuer = issuer
        self.issues = issues or {}
        self.fail = fail
        self.inn_calls: list[str] = []

    def fetch_by_inn(self, inn: str) -> tuple[CbrRatingAction, ...]:
        self.inn_calls.append(inn)
        if self.fail:
            raise CbrRatingsApiError("searchRating", 503)
        return self.issuer

    def fetch_by_isin(self, isin: str) -> tuple[CbrRatingAction, ...]:
        if self.fail:
            raise CbrRatingsApiError("searchRating", 503)
        return self.issues.get(isin, ())


class CreditPortfolioTests(unittest.TestCase):
    def analyze(
        self,
        client: FakeRatingsClient,
        *records: EnrichedBondPosition,
        max_age: int = 400,
    ):
        return CreditPortfolioAnalyzer(
            client,
            CreditAnalysisPolicy(max_age),
            now=lambda: NOW,
        ).analyze(market_report(*records))

    def test_normalizes_agency_notations_only_to_broad_bands(self) -> None:
        self.assertEqual(normalize_rating_band("AAA(RU)"), RatingBand.HIGHEST)
        self.assertEqual(normalize_rating_band("ruAA-"), RatingBand.HIGH)
        self.assertEqual(normalize_rating_band("A+.ru"), RatingBand.STRONG)
        self.assertEqual(normalize_rating_band("BBB+|ru|"), RatingBand.ADEQUATE)
        self.assertEqual(normalize_rating_band("BB.ru"), RatingBand.SPECULATIVE)
        self.assertEqual(normalize_rating_band("D(RU)"), RatingBand.DEFAULT)
        self.assertEqual(normalize_rating_band("особая шкала"), RatingBand.UNRATED)

    def test_excludes_withdrawn_rating_of_an_unrelated_old_issue(self) -> None:
        current = action(outlook="DEV – развивающийся")
        old_issue = action(
            object_id="old",
            isin="RU000A654321",
            rating_action="Кредитный рейтинг отозван",
        )
        report = self.analyze(FakeRatingsClient(issuer=(current, old_issue)), enriched_bond())
        passport = report.passports[0]

        self.assertEqual(len(passport.ratings), 1)
        self.assertEqual(passport.ratings[0].scope, "ISSUER")
        self.assertEqual(passport.ratings[0].band, RatingBand.ADEQUATE)
        self.assertIn("RATING_WATCH", {signal.code for signal in passport.signals})
        self.assertNotIn("RATING_DOWNGRADE", {signal.code for signal in passport.signals})
        self.assertNotIn("RATING_WITHDRAWN", {signal.code for signal in passport.signals})

    def test_does_not_treat_previous_developing_outlook_as_current_watch(self) -> None:
        stabilized = action(
            outlook="STA – стабильный",
            rating_action="Прогноз изменен с развивающегося на стабильный",
        )
        report = self.analyze(FakeRatingsClient(issuer=(stabilized,)), enriched_bond())

        self.assertNotIn(
            "RATING_WATCH",
            {signal.code for signal in report.passports[0].signals},
        )

    def test_reports_latest_downgrade_separately(self) -> None:
        downgraded = action(
            outlook="STA – стабильный",
            rating_action="Кредитный рейтинг понижен",
        )
        report = self.analyze(FakeRatingsClient(issuer=(downgraded,)), enriched_bond())

        self.assertIn(
            "RATING_DOWNGRADE",
            {signal.code for signal in report.passports[0].signals},
        )

    def test_applies_withdrawal_only_to_exact_current_issue(self) -> None:
        isin = "RU000A123456"
        withdrawn = action(
            object_id="current-issue",
            isin=isin,
            rating_action="Кредитный рейтинг отозван",
        )
        report = self.analyze(
            FakeRatingsClient(issues={isin: (withdrawn,)}),
            enriched_bond(isin=isin),
        )
        codes = {signal.code for signal in report.passports[0].signals}

        self.assertEqual(report.passports[0].ratings[0].status, "WITHDRAWN")
        self.assertIn("RATING_WITHDRAWN", codes)
        self.assertIn("NO_CURRENT_RATING", codes)

    def test_emits_default_speculative_and_stale_signals(self) -> None:
        risky = action(value="ruBB-", released=date(2024, 1, 1))
        report = self.analyze(
            FakeRatingsClient(issuer=(risky,)),
            enriched_bond(has_default=True),
            max_age=365,
        )
        signals = {signal.code: signal.severity.value for signal in report.passports[0].signals}

        self.assertEqual(signals["MOEX_DEFAULT_FLAG"], "CRITICAL")
        self.assertEqual(signals["SPECULATIVE_RATING"], "WARNING")
        self.assertEqual(signals["RATING_STALE"], "WARNING")

    def test_source_failure_is_not_misreported_as_no_rating(self) -> None:
        report = self.analyze(FakeRatingsClient(fail=True), enriched_bond())
        codes = {signal.code for signal in report.passports[0].signals}

        self.assertIn("RATING_DATA_UNAVAILABLE", codes)
        self.assertNotIn("NO_CURRENT_RATING", codes)
        self.assertEqual(report.rating_coverage_share, Decimal("0"))

    def test_caches_issuer_search_and_keeps_report_analysis_only(self) -> None:
        client = FakeRatingsClient(issuer=(action(),))
        second = enriched_bond(
            "BOND2",
            isin="RU000A999999",
            value="50000",
            emitter_id=1,
        )
        report = self.analyze(client, enriched_bond(), second)
        payload = json.loads(render_credit_report_json(report))

        self.assertEqual(client.inn_calls, ["1234567890"])
        self.assertEqual(report.rating_coverage_share, Decimal("1"))
        self.assertEqual(payload["execution_state"], "ANALYSIS_ONLY")
        self.assertNotIn('"score":', json.dumps(payload).casefold())
        self.assertNotIn("recommendation", payload)


if __name__ == "__main__":
    unittest.main()
