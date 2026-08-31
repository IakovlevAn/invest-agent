"""Per-asset price dynamics for the live portfolio."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from invest_agent.bond_report import BondMarketReport
from invest_agent.domain import InstrumentType, PortfolioSnapshot, Position
from invest_agent.market.moex import (
    MOSCOW_TZ,
    MoexApiError,
    MoexContractError,
    MoexIssClient,
    MoexPriceHistory,
)


@dataclass(frozen=True, slots=True)
class PriceWindowChange:
    days: int
    reference_date: str
    reference_price: Decimal
    change_percent: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "reference_date": self.reference_date,
            "reference_price": _decimal_text(self.reference_price),
            "change_percent": _decimal_text(self.change_percent),
        }


@dataclass(frozen=True, slots=True)
class AssetPriceDynamics:
    ticker: str
    name: str
    instrument_type: InstrumentType
    board: str
    quantity: Decimal
    market_value_rub: Decimal
    current_price: Decimal
    current_price_unit: str
    changes: tuple[PriceWindowChange, ...]
    signal: str
    source_url: str | None
    limitation: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "name": self.name,
            "instrument_type": self.instrument_type.value,
            "board": self.board,
            "quantity": _decimal_text(self.quantity),
            "market_value_rub": _decimal_text(self.market_value_rub),
            "current_price": _decimal_text(self.current_price),
            "current_price_unit": self.current_price_unit,
            "changes": {
                f"{change.days}d": change.as_dict() for change in self.changes
            },
            "signal": self.signal,
            "source_url": self.source_url,
            "limitation": self.limitation,
        }


@dataclass(frozen=True, slots=True)
class PortfolioAssetDynamicsReport:
    portfolio_as_of: str
    fetched_at: str
    assets: tuple[AssetPriceDynamics, ...]
    failures: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        alerts = [
            asset
            for asset in self.assets
            if asset.signal in {"WATCH", "REVIEW", "SEVERE"}
        ]
        return {
            "portfolio_as_of": self.portfolio_as_of,
            "fetched_at": self.fetched_at,
            "provider": "Moscow Exchange ISS",
            "price_definition": (
                "Stocks and funds use RUB market price; bonds use clean price as "
                "percent of face value. Changes exclude coupons, accrued interest, "
                "taxes and broker fees."
            ),
            "assets": [asset.as_dict() for asset in self.assets],
            "alerts": [asset.as_dict() for asset in alerts],
            "failures": list(self.failures),
        }


class PortfolioAssetDynamicsAnalyzer:
    def __init__(self, client: MoexIssClient) -> None:
        self._client = client

    def analyze(
        self,
        snapshot: PortfolioSnapshot,
        bonds: BondMarketReport,
    ) -> PortfolioAssetDynamicsReport:
        bond_records = {
            record.position.instrument_uid: record for record in bonds.positions
        }
        assets: list[AssetPriceDynamics] = []
        failures: list[str] = []
        observed_date = snapshot.as_of.astimezone(MOSCOW_TZ).date()
        fetched_at = snapshot.as_of
        for position in snapshot.positions:
            prepared = _prepare_asset(position, bond_records)
            if prepared is None:
                assets.append(
                    _unavailable_asset(
                        position,
                        "MOEX history is unsupported for this instrument type or board",
                    )
                )
                continue
            secid, board, market, current_price, unit = prepared
            try:
                history = self._client.fetch_price_history(
                    secid,
                    board_id=board,
                    market=market,
                    from_date=observed_date - timedelta(days=100),
                )
                fetched_at = max(fetched_at, history.fetched_at)
                changes = tuple(
                    change
                    for days in (1, 7, 30, 90)
                    if (
                        change := _window_change(
                            history,
                            current_price=current_price,
                            observed_date=observed_date,
                            days=days,
                        )
                    )
                    is not None
                )
                assets.append(
                    AssetPriceDynamics(
                        ticker=position.ticker,
                        name=position.display_name or position.ticker,
                        instrument_type=position.instrument_type,
                        board=board,
                        quantity=position.quantity,
                        market_value_rub=position.market_value_rub,
                        current_price=current_price,
                        current_price_unit=unit,
                        changes=changes,
                        signal=_signal(changes),
                        source_url=history.source_url,
                        limitation=(
                            None
                            if changes
                            else "MOEX returned no comparable historical close"
                        ),
                    )
                )
            except (MoexApiError, MoexContractError, ValueError) as error:
                failures.append(f"{position.ticker}: {error}")
                assets.append(_unavailable_asset(position, str(error)))
        return PortfolioAssetDynamicsReport(
            portfolio_as_of=snapshot.as_of.isoformat(),
            fetched_at=fetched_at.isoformat(),
            assets=tuple(
                sorted(assets, key=lambda item: (-item.market_value_rub, item.ticker))
            ),
            failures=tuple(failures),
        )


def _prepare_asset(
    position: Position,
    bond_records: dict[str, Any],
) -> tuple[str, str, str, Decimal, str] | None:
    if position.instrument_type is InstrumentType.BOND:
        record = bond_records.get(position.instrument_uid)
        if record is None:
            return None
        face_value = record.moex.facts.face_value
        if face_value is None or face_value <= 0 or position.market_price <= 0:
            return None
        return (
            record.moex.facts.secid,
            record.moex.facts.primary_board,
            "bonds",
            position.market_price / face_value * Decimal("100"),
            "PERCENT_OF_FACE",
        )
    if position.instrument_type in {InstrumentType.STOCK, InstrumentType.FUND}:
        if position.market_price <= 0 or not position.class_code.strip():
            return None
        return (
            position.ticker,
            position.class_code,
            "shares",
            position.market_price,
            "RUB",
        )
    return None


def _window_change(
    history: MoexPriceHistory,
    *,
    current_price: Decimal,
    observed_date: date,
    days: int,
) -> PriceWindowChange | None:
    target = observed_date - timedelta(days=days)
    candidates = [point for point in history.points if point.trade_date <= target]
    if not candidates:
        return None
    reference = max(candidates, key=lambda item: item.trade_date)
    if reference.close_price <= 0:
        return None
    return PriceWindowChange(
        days=days,
        reference_date=reference.trade_date.isoformat(),
        reference_price=reference.close_price,
        change_percent=(current_price / reference.close_price - Decimal("1"))
        * Decimal("100"),
    )


def _signal(changes: tuple[PriceWindowChange, ...]) -> str:
    by_days = {change.days: change.change_percent for change in changes}
    if by_days.get(7, Decimal("0")) <= Decimal("-5") or by_days.get(
        30, Decimal("0")
    ) <= Decimal("-10"):
        return "SEVERE"
    if by_days.get(7, Decimal("0")) <= Decimal("-2") or by_days.get(
        30, Decimal("0")
    ) <= Decimal("-5"):
        return "REVIEW"
    if (
        by_days.get(7, Decimal("0")) <= Decimal("-1")
        or by_days.get(30, Decimal("0")) <= Decimal("-3")
        or by_days.get(90, Decimal("0")) <= Decimal("-7")
    ):
        return "WATCH"
    return "NO_MATERIAL_DECLINE"


def _unavailable_asset(position: Position, limitation: str) -> AssetPriceDynamics:
    return AssetPriceDynamics(
        ticker=position.ticker,
        name=position.display_name or position.ticker,
        instrument_type=position.instrument_type,
        board=position.class_code,
        quantity=position.quantity,
        market_value_rub=position.market_value_rub,
        current_price=position.market_price,
        current_price_unit="BCS_NATIVE",
        changes=(),
        signal="UNAVAILABLE",
        source_url=None,
        limitation=limitation,
    )


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")
