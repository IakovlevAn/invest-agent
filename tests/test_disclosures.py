from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

from invest_agent.bond_report import BondMarketReport, EnrichedBondPosition
from invest_agent.disclosure.interfax import (
    DisclosureApiError,
    DisclosureCompany,
    DisclosureDocument,
    DisclosureIssuerDocuments,
)
from invest_agent.disclosures import (
    DisclosurePolicy,
    DisclosurePortfolioAnalyzer,
    render_disclosure_report_json,
)
from invest_agent.domain import InstrumentType, Position
from invest_agent.market.moex import MoexBondFacts, MoexBondSnapshot, MoexEmitter

NOW = datetime(2026, 8, 8, 15, 30, tzinfo=UTC)
REGISTRATION = "4B02-04-00075-L-001P"


def document(
    *,
    category: str,
    file_id: int,
    document_type: str,
    published_at: date,
    period: str | None = None,
    registration_number: str | None = None,
) -> DisclosureDocument:
    return DisclosureDocument(
        category=category,
        file_id=file_id,
        document_type=document_type,
        period=period,
        registration_number=registration_number,
        registration_date=None if registration_number is None else published_at,
        publication_basis_date=published_at,
        published_at=published_at,
        file_url=f"https://www.e-disclosure.ru/portal/FileLoad.ashx?Fileid={file_id}",
        index_url="https://www.e-disclosure.ru/portal/files.aspx?id=38662&type=4",
    )


def issuer_documents() -> DisclosureIssuerDocuments:
    return DisclosureIssuerDocuments(
        company=DisclosureCompany(
            company_id=38662,
            name='ООО "ВУШ"',
            inn="9717068640",
            district="Центральный",
            region="Москва",
            branch="Иное",
            last_activity=datetime(2026, 8, 7, 11, 5),
            document_count=65,
            source_url="https://www.e-disclosure.ru/portal/company.aspx?id=38662",
        ),
        consolidated=(
            document(
                category="CONSOLIDATED",
                file_id=1914696,
                document_type="Годовая консолидированная финансовая отчетность по МСФО",
                period="2025",
                published_at=date(2026, 3, 16),
            ),
            document(
                category="CONSOLIDATED",
                file_id=1896434,
                document_type="Промежуточная консолидированная финансовая отчетность по МСФО",
                period="2025, 6 месяцев",
                published_at=date(2025, 8, 27),
            ),
        ),
        emission=(
            document(
                category="EMISSION",
                file_id=1932761,
                document_type="Проспект ценных бумаг",
                registration_number="4-00075-L-001P-02E",
                published_at=date(2026, 6, 11),
            ),
            document(
                category="EMISSION",
                file_id=1870860,
                document_type="Решение о выпуске ценных бумаг",
                registration_number=REGISTRATION,
                published_at=date(2025, 3, 27),
            ),
        ),
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
        issue_name="биржевые облигации серии 001P-04",
        registration_number=REGISTRATION,
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
        offer_date=None,
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


class FakeDisclosureClient:
    def __init__(
        self,
        result: DisclosureIssuerDocuments | None,
        *,
        fail: bool = False,
    ) -> None:
        self.result = result
        self.fail = fail
        self.calls: list[str] = []

    def fetch_issuer_documents(self, inn: str) -> DisclosureIssuerDocuments | None:
        self.calls.append(inn)
        if self.fail:
            raise DisclosureApiError("search", 503)
        return self.result


class DisclosurePortfolioTests(unittest.TestCase):
    def analyze(
        self,
        client: FakeDisclosureClient,
        *records: EnrichedBondPosition,
    ):
        return DisclosurePortfolioAnalyzer(
            client,
            DisclosurePolicy(annual_consolidated_max_age_days=550),
            now=lambda: NOW,
        ).analyze(market_report(*records))

    def test_indexes_latest_consolidated_and_exact_issue_only(self) -> None:
        report = self.analyze(FakeDisclosureClient(issuer_documents()), enriched_bond())
        passport = report.passports[0]
        payload = json.loads(render_disclosure_report_json(report))

        assert passport.latest_annual_consolidated is not None
        assert passport.latest_interim_consolidated is not None
        self.assertEqual(passport.latest_annual_consolidated.period, "2025")
        self.assertEqual(len(passport.exact_issue_documents), 1)
        self.assertEqual(passport.exact_issue_documents[0].file_id, 1870860)
        self.assertEqual(report.company_coverage_share, Decimal("1"))
        self.assertEqual(report.annual_consolidated_coverage_share, Decimal("1"))
        self.assertEqual(report.exact_issue_document_coverage_share, Decimal("1"))
        self.assertEqual(payload["execution_state"], "ANALYSIS_ONLY")
        self.assertIn("does not establish", payload["methodology"]["scope_warning"])

    def test_caches_issuer_lookup_across_issues(self) -> None:
        client = FakeDisclosureClient(issuer_documents())
        second = enriched_bond("RU000A999999", isin="RU000A999999", value="50000")

        report = self.analyze(client, enriched_bond(), second)

        self.assertEqual(client.calls, ["9717068640"])
        self.assertEqual(len(report.passports), 2)

    def test_verified_absent_company_is_not_source_failure(self) -> None:
        report = self.analyze(FakeDisclosureClient(None), enriched_bond())
        codes = {signal.code for signal in report.passports[0].signals}

        self.assertIn("NO_DISCLOSURE_COMPANY", codes)
        self.assertNotIn("DISCLOSURE_DATA_UNAVAILABLE", codes)
        self.assertEqual(report.failures, ())

    def test_source_failure_is_explicit(self) -> None:
        report = self.analyze(FakeDisclosureClient(None, fail=True), enriched_bond())
        codes = {signal.code for signal in report.passports[0].signals}

        self.assertIn("DISCLOSURE_DATA_UNAVAILABLE", codes)
        self.assertEqual(len(report.failures), 1)

    def test_stale_annual_and_missing_exact_issue_are_warnings(self) -> None:
        source = issuer_documents()
        stale_annual = replace(
            source.consolidated[0],
            published_at=date(2024, 1, 1),
        )
        mismatched = replace(
            source,
            consolidated=(stale_annual,),
            emission=(source.emission[0],),
        )

        report = self.analyze(FakeDisclosureClient(mismatched), enriched_bond())
        codes = {signal.code for signal in report.passports[0].signals}

        self.assertIn("ANNUAL_CONSOLIDATED_REPORT_STALE", codes)
        self.assertIn("NO_EXACT_ISSUE_DOCUMENT", codes)


if __name__ == "__main__":
    unittest.main()
