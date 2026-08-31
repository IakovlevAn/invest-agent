from __future__ import annotations

import unittest
from datetime import UTC, date, datetime
from decimal import Decimal

from invest_agent.asset_dynamics import PortfolioAssetDynamicsAnalyzer
from invest_agent.credit import RatingBand
from invest_agent.domain import InstrumentType, PortfolioSnapshot, Position
from invest_agent.market.moex import MoexPriceHistory, MoexPricePoint

from test_manager import bond, inputs, rating


NOW = datetime(2026, 8, 8, 16, 0, tzinfo=UTC)


class FakeHistoryClient:
    def fetch_price_history(
        self,
        secid: str,
        *,
        board_id: str,
        market: str,
        from_date: date,
    ) -> MoexPriceHistory:
        reference = Decimal("105") if market == "bonds" else Decimal("40")
        return MoexPriceHistory(
            secid=secid,
            board_id=board_id,
            market=market,
            points=(
                MoexPricePoint(
                    trade_date=date(2026, 8, 1),
                    close_price=reference,
                    trades=10,
                    turnover_rub=Decimal("100000"),
                ),
            ),
            source_url=f"https://iss.moex.test/{secid}",
            fetched_at=NOW,
        )


class PortfolioAssetDynamicsAnalyzerTests(unittest.TestCase):
    def test_reports_each_supported_asset_and_keeps_unsupported_explicit(self) -> None:
        bond_record = bond(
            "RU000A000001",
            emitter_id=1,
            value="10000",
            yield_percent="20",
        )
        _, bonds, _ = inputs(
            (bond_record,),
            (rating("RU000A000001", emitter_id=1, band=RatingBand.HIGH),),
        )
        stock = Position(
            instrument_uid="BCS:TQBR:TEST",
            ticker="TEST",
            class_code="TQBR",
            instrument_type=InstrumentType.STOCK,
            quantity=Decimal("10"),
            market_price=Decimal("50"),
            market_value_rub=Decimal("500"),
            tradable=True,
        )
        foreign = Position(
            instrument_uid="BCS:OTC:LOCKED",
            ticker="LOCKED",
            class_code="OTC",
            instrument_type=InstrumentType.FOREIGN_STOCK,
            quantity=Decimal("1"),
            market_price=Decimal("100"),
            market_value_rub=Decimal("100"),
            tradable=False,
            blocked_reason="hold-only",
        )
        snapshot = PortfolioSnapshot(
            account_ref="bcs:test",
            is_iis=True,
            as_of=NOW,
            cash_rub=Decimal("2000"),
            positions=(bond_record.position, stock, foreign),
        )

        report = PortfolioAssetDynamicsAnalyzer(FakeHistoryClient()).analyze(
            snapshot,
            bonds,
        )
        payload = report.as_dict()

        self.assertEqual(len(payload["assets"]), 3)
        by_ticker = {item["ticker"]: item for item in payload["assets"]}
        self.assertEqual(by_ticker["RU000A000001"]["signal"], "REVIEW")
        self.assertEqual(by_ticker["TEST"]["signal"], "NO_MATERIAL_DECLINE")
        self.assertEqual(by_ticker["LOCKED"]["signal"], "UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
