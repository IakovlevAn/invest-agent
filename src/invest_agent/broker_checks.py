"""Authenticated BCS catalogue and quote checks for manager actions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from invest_agent.brokers.bcs import BcsAccessToken, BcsInstrument, BcsQuote, BcsReadClient
from invest_agent.domain import Side
from invest_agent.manager import ManagerAction, PortfolioManagerReport


@dataclass(frozen=True, slots=True)
class BcsTradeCheck:
    isin: str
    recommendation_ticker: str
    side: Side
    target_amount_rub: Decimal
    catalog_found: bool
    bcs_ticker: str | None
    class_code: str | None
    lot_size: int | None
    minimum_price_step: Decimal | None
    face_value: Decimal | None
    accrued_interest: Decimal | None
    instrument_blocked: bool | None
    available_for_unqualified: bool | None
    quote_observed_at: str | None
    trading_status: int | None
    bid: Decimal | None
    offer: Decimal | None
    last: Decimal | None
    broker_catalog_available: bool
    trading_is_open: bool
    blockers: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "isin": self.isin,
            "recommendation_ticker": self.recommendation_ticker,
            "side": self.side.value,
            "target_amount_rub": _decimal_text(self.target_amount_rub),
            "catalog_found": self.catalog_found,
            "bcs_ticker": self.bcs_ticker,
            "class_code": self.class_code,
            "lot_size": self.lot_size,
            "minimum_price_step": _optional_decimal_text(self.minimum_price_step),
            "face_value": _optional_decimal_text(self.face_value),
            "accrued_interest": _optional_decimal_text(self.accrued_interest),
            "instrument_blocked": self.instrument_blocked,
            "available_for_unqualified": self.available_for_unqualified,
            "quote_observed_at": self.quote_observed_at,
            "trading_status": self.trading_status,
            "bid": _optional_decimal_text(self.bid),
            "offer": _optional_decimal_text(self.offer),
            "last": _optional_decimal_text(self.last),
            "broker_catalog_available": self.broker_catalog_available,
            "trading_is_open": self.trading_is_open,
            "blockers": list(self.blockers),
        }


class BcsTradeVerifier:
    """Verify recommended instruments in the authenticated read-only BCS contour."""

    def __init__(self, client: BcsReadClient) -> None:
        self._client = client

    def verify_manager_actions(
        self,
        report: PortfolioManagerReport,
        access_token: BcsAccessToken,
    ) -> tuple[BcsTradeCheck, ...]:
        intents: list[tuple[str, str, Side, Decimal]] = []
        for decision in report.decisions:
            if decision.recommended_reduce_rub > 0:
                intents.append(
                    (
                        decision.isin,
                        decision.ticker,
                        Side.SELL,
                        decision.recommended_reduce_rub,
                    )
                )
            if (
                decision.action is ManagerAction.ADD_CANDIDATE
                and decision.recommended_add_rub > 0
            ):
                intents.append(
                    (
                        decision.isin,
                        decision.ticker,
                        Side.BUY,
                        decision.recommended_add_rub,
                    )
                )
        for candidate in report.new_bond_candidates:
            if candidate.recommended_add_rub > 0:
                intents.append(
                    (
                        candidate.isin,
                        candidate.ticker,
                        Side.BUY,
                        candidate.recommended_add_rub,
                    )
                )
        if not intents:
            return ()

        unique_isins = tuple(dict.fromkeys(isin for isin, _, _, _ in intents))
        instruments = self._client.fetch_instruments_by_isins(access_token, unique_isins)
        by_isin = {instrument.isin: instrument for instrument in instruments}
        quote_pairs = tuple(
            dict.fromkeys(
                (instrument.ticker, instrument.primary_board)
                for instrument in instruments
            )
        )
        quotes = self._client.fetch_quotes(access_token, quote_pairs) if quote_pairs else ()
        by_pair = {(quote.ticker, quote.class_code): quote for quote in quotes}
        checks = [
            _build_check(
                isin=isin,
                recommendation_ticker=ticker,
                side=side,
                target_amount_rub=amount,
                instrument=by_isin.get(isin),
                quotes=by_pair,
            )
            for isin, ticker, side, amount in intents
        ]
        return tuple(sorted(checks, key=lambda item: (item.side.value, item.isin)))


def _build_check(
    *,
    isin: str,
    recommendation_ticker: str,
    side: Side,
    target_amount_rub: Decimal,
    instrument: BcsInstrument | None,
    quotes: dict[tuple[str, str], BcsQuote],
) -> BcsTradeCheck:
    if instrument is None:
        return BcsTradeCheck(
            isin=isin,
            recommendation_ticker=recommendation_ticker,
            side=side,
            target_amount_rub=target_amount_rub,
            catalog_found=False,
            bcs_ticker=None,
            class_code=None,
            lot_size=None,
            minimum_price_step=None,
            face_value=None,
            accrued_interest=None,
            instrument_blocked=None,
            available_for_unqualified=None,
            quote_observed_at=None,
            trading_status=None,
            bid=None,
            offer=None,
            last=None,
            broker_catalog_available=False,
            trading_is_open=False,
            blockers=(
                "выпуск не найден в аутентифицированном справочнике БКС",
            ),
        )

    quote = quotes.get((instrument.ticker, instrument.primary_board))
    blockers: list[str] = []
    if not instrument.is_ruble_bond:
        blockers.append(
            "инструмент БКС не является рублёвой облигацией"
        )
    if instrument.is_blocked:
        blockers.append(
            "класс инструмента заблокирован в справочнике БКС"
        )
    if side is Side.BUY and (
        instrument.is_qualified_only or not instrument.available_for_unqualified
    ):
        blockers.append(
            "покупка недоступна неквалифицированному инвестору"
        )
    if instrument.lot_size <= 0:
        blockers.append("БКС вернул некорректный размер лота")
    if instrument.minimum_step <= 0:
        blockers.append("БКС вернул некорректный шаг цены")
    if quote is None:
        blockers.append(
            "БКС не вернул котировку для основного режима"
        )
    elif quote.currency != "RUB":
        blockers.append("котировка БКС не в рублях")

    catalog_available = not blockers
    trading_is_open = bool(quote and quote.trading_is_open)
    if quote is not None and not trading_is_open:
        blockers.append(
            "торговая сессия по выпуску сейчас закрыта"
        )
    return BcsTradeCheck(
        isin=isin,
        recommendation_ticker=recommendation_ticker,
        side=side,
        target_amount_rub=target_amount_rub,
        catalog_found=True,
        bcs_ticker=instrument.ticker,
        class_code=instrument.primary_board,
        lot_size=instrument.lot_size,
        minimum_price_step=instrument.minimum_step,
        face_value=instrument.face_value,
        accrued_interest=instrument.accrued_interest,
        instrument_blocked=instrument.is_blocked,
        available_for_unqualified=instrument.available_for_unqualified,
        quote_observed_at=None if quote is None else quote.observed_at.isoformat(),
        trading_status=None if quote is None else quote.security_trading_status,
        bid=None if quote is None else quote.bid,
        offer=None if quote is None else quote.offer,
        last=None if quote is None else quote.last,
        broker_catalog_available=catalog_available,
        trading_is_open=trading_is_open,
        blockers=tuple(blockers),
    )


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else _decimal_text(value)
