from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

from invest_agent.bond_report import BondPortfolioEnricher, render_bond_report_json
from invest_agent.domain import InstrumentType, PortfolioSnapshot, Position
from invest_agent.market.moex import (
    MoexApiError,
    MoexBondFacts,
    MoexBondMarketData,
    MoexBondSnapshot,
    MoexEmitter,
)

NOW = datetime(2026, 8, 8, 14, 40, tzinfo=UTC)


def position(ticker: str, value: str) -> Position:
    return Position(
        instrument_uid=f"BCS:TQCB:{ticker}",
        ticker=ticker,
        class_code="TQCB",
        instrument_type=InstrumentType.BOND,
        quantity=Decimal("10"),
        market_price=Decimal("1000"),
        market_value_rub=Decimal(value),
        tradable=True,
    )


def bond(ticker: str, emitter_id: int) -> MoexBondSnapshot:
    return MoexBondSnapshot(
        facts=MoexBondFacts(
            secid=ticker,
            isin=ticker,
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
            issue_size=Decimal("100000"),
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
        ),
        market=MoexBondMarketData(
            board_id="TQCB",
            bid_percent=Decimal("99"),
            offer_percent=Decimal("99.2"),
            last_percent=Decimal("99.1"),
            wap_percent=Decimal("99.1"),
            yield_at_wap_percent=Decimal("24"),
            effective_yield_percent=Decimal("24"),
            yield_date=date(2028, 1, 1),
            yield_date_type="MATDATE",
            duration_days=Decimal("500"),
            z_spread_bps=Decimal("900"),
            g_spread_bps=Decimal("910"),
            trades_today=10,
            volume_today=Decimal("100"),
            turnover_today_rub=Decimal("100000"),
            trading_status="T",
            trade_moment=NOW,
            system_moment=NOW,
            source_url=f"https://iss.moex.com/{ticker}/market",
        ),
        fetched_at=NOW,
    )


def emitter(emitter_id: int, name: str) -> MoexEmitter:
    return MoexEmitter(
        emitter_id=emitter_id,
        title=name,
        short_title=name,
        inn=None,
        ogrn=None,
        website=None,
        source_url=f"https://iss.moex.com/emitters/{emitter_id}",
    )


class FakeMoexClient:
    def __init__(self) -> None:
        self.bonds = {
            "BOND1": bond("BOND1", 1),
            "BOND2": bond("BOND2", 1),
            "BOND3": bond("BOND3", 2),
        }
        self.emitters = {1: emitter(1, "Эмитент 1"), 2: emitter(2, "Эмитент 2")}
        self.emitter_calls: list[int] = []

    def fetch_bond(self, secid: str) -> MoexBondSnapshot:
        if secid == "FAILED":
            raise MoexApiError("security:FAILED", 503)
        return self.bonds[secid]

    def fetch_emitter(self, emitter_id: int) -> MoexEmitter:
        self.emitter_calls.append(emitter_id)
        return self.emitters[emitter_id]


class BondPortfolioEnricherTests(unittest.TestCase):
    def test_aggregates_issues_by_emitter_and_caches_emitter(self) -> None:
        snapshot = PortfolioSnapshot(
            account_ref="bcs:test",
            is_iis=True,
            as_of=NOW,
            cash_rub=Decimal("50000"),
            positions=(
                position("BOND1", "60000"),
                position("BOND2", "30000"),
                position("BOND3", "10000"),
            ),
        )
        client = FakeMoexClient()

        report = BondPortfolioEnricher(client, now=lambda: NOW).enrich(snapshot)  # type: ignore[arg-type]

        self.assertEqual(report.market_coverage_share, Decimal("1"))
        self.assertEqual(report.issuer_coverage_share, Decimal("1"))
        self.assertEqual(report.issuer_exposures[0].value_rub, Decimal("90000"))
        self.assertEqual(report.issuer_exposures[0].issues, ("BOND1", "BOND2"))
        self.assertEqual(report.issuer_hhi_on_covered_bonds, Decimal("0.82"))
        self.assertEqual(client.emitter_calls, [1, 2])

        payload = json.loads(render_bond_report_json(report))
        self.assertEqual(payload["execution_state"], "ANALYSIS_ONLY")
        self.assertEqual(
            payload["positions"][0]["market"]["yield"][
                "comparable_effective_yield_percent"
            ],
            "24",
        )

    def test_keeps_partial_coverage_explicit(self) -> None:
        snapshot = PortfolioSnapshot(
            account_ref="bcs:test",
            is_iis=True,
            as_of=NOW,
            cash_rub=Decimal("0"),
            positions=(position("BOND1", "75000"), position("FAILED", "25000")),
        )
        client = FakeMoexClient()

        report = BondPortfolioEnricher(client, now=lambda: NOW).enrich(snapshot)  # type: ignore[arg-type]

        self.assertEqual(report.bond_position_count, 2)
        self.assertEqual(report.market_coverage_share, Decimal("0.75"))
        self.assertEqual(len(report.failures), 1)
        self.assertEqual(report.failures[0].stage, "bond")

    def test_does_not_treat_raw_floater_effective_yield_as_comparable(self) -> None:
        snapshot = PortfolioSnapshot(
            account_ref="bcs:test",
            is_iis=True,
            as_of=NOW,
            cash_rub=Decimal("0"),
            positions=(position("BOND1", "100000"),),
        )
        client = FakeMoexClient()
        fixed = client.bonds["BOND1"]
        assert fixed.market is not None
        client.bonds["BOND1"] = replace(
            fixed,
            facts=replace(
                fixed.facts,
                bond_type="Флоатер",
                coupon_percent=None,
                coupon_benchmark="RREFKEYR",
                coupon_benchmark_spread=Decimal("1.75"),
            ),
            market=replace(
                fixed.market,
                effective_yield_percent=Decimal("-0.1567"),
                yield_at_wap_percent=Decimal("16.13"),
            ),
        )

        report = BondPortfolioEnricher(client, now=lambda: NOW).enrich(snapshot)  # type: ignore[arg-type]
        record = report.positions[0]

        self.assertIsNone(record.comparable_effective_yield_percent)
        self.assertEqual(record.yield_interpretation, "FLOATER_REQUIRES_RATE_SCENARIO")
        payload = json.loads(render_bond_report_json(report))
        yield_data = payload["positions"][0]["market"]["yield"]
        self.assertIsNone(yield_data["comparable_effective_yield_percent"])
        self.assertEqual(yield_data["moex_effective_yield_raw_percent"], "-0.1567")
        self.assertIn("сценария ставки", record.missing_fields[0])


if __name__ == "__main__":
    unittest.main()
