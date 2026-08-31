"""Normalize and render the official BCS portfolio response."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from invest_agent.domain import InstrumentType, PortfolioSnapshot, Position


class PortfolioContractError(ValueError):
    """The broker payload does not match the verified portfolio contract."""


_INSTRUMENT_TYPES = {
    "BOND": InstrumentType.BOND,
    "BONDS": InstrumentType.BOND,
    "STOCK": InstrumentType.STOCK,
    "SHARE": InstrumentType.STOCK,
    "FOREIGN_STOCK": InstrumentType.FOREIGN_STOCK,
    "BLOCKED": InstrumentType.FOREIGN_STOCK,
    "OTC": InstrumentType.FOREIGN_STOCK,
    "OTC_EQUITIES": InstrumentType.FOREIGN_STOCK,
    "MUTUAL_FUNDS": InstrumentType.FUND,
    "ETF": InstrumentType.FUND,
    "FUND": InstrumentType.FUND,
    "MONEY": InstrumentType.CASH,
    "CASH": InstrumentType.CASH,
    "CURRENCY": InstrumentType.CURRENCY,
    "FUTURES": InstrumentType.FUTURE,
    "FUTURE": InstrumentType.FUTURE,
    "OPTIONS": InstrumentType.OPTION,
    "OPTION": InstrumentType.OPTION,
    "GOODS": InstrumentType.METAL,
    "METAL": InstrumentType.METAL,
}


class BcsPortfolioNormalizer:
    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(tz=UTC))

    def normalize(
        self,
        payload: Mapping[str, Any],
        *,
        is_iis: bool,
    ) -> PortfolioSnapshot:
        raw_positions = payload.get("positions")
        if not isinstance(raw_positions, list):
            raise PortfolioContractError("BCS portfolio field 'positions' must be an array")
        if not raw_positions:
            raise PortfolioContractError(
                "BCS portfolio contains no positions, including no cash position"
            )
        selected_positions = self._select_current_term(raw_positions)

        agreement_ids: set[str] = set()
        positions: list[Position] = []
        cash_rub = Decimal("0")
        for index, raw in enumerate(selected_positions):
            if not isinstance(raw, dict):
                raise PortfolioContractError(f"positions[{index}] must be an object")
            ticker = self._required_text(raw, "ticker", index)
            agreement_ids.add(self._required_text(raw, "agreementId", index))
            instrument_type = self._instrument_type(raw)
            quantity = self._decimal(raw, "quantity", index)
            current_value_rub = self._decimal(raw, "currentValueRub", index)
            if quantity < 0 or current_value_rub < 0:
                raise PortfolioContractError(
                    f"positions[{index}] contains a short or negative-value position"
                )
            if self._is_ruble_cash(raw, instrument_type):
                cash_rub += current_value_rub
                continue

            current_price = self._decimal(raw, "currentPrice", index)
            locked = self._decimal(raw, "locked", index)
            if locked < 0 or locked > quantity:
                raise PortfolioContractError(
                    f"positions[{index}].locked must be between zero and quantity"
                )
            board_value = raw.get("board")
            if not isinstance(board_value, str):
                raise PortfolioContractError(f"positions[{index}].board must be text")
            board = board_value.strip()
            exchange = self._required_text(raw, "exchange", index)
            class_code = board or exchange
            blocked_reasons: list[str] = []
            if self._boolean(raw, "isBlockedTradeAccount", index):
                blocked_reasons.append("BCS trade account is blocked")
            if self._boolean(raw, "isBlocked", index):
                blocked_reasons.append("BCS marks the position as blocked")
            if quantity > 0 and locked == quantity:
                blocked_reasons.append("all units are locked")
            if instrument_type is InstrumentType.UNKNOWN:
                blocked_reasons.append("unknown BCS instrument type")
            if instrument_type is InstrumentType.FOREIGN_STOCK:
                blocked_reasons.append("foreign assets are hold-only by mandate")
            if not board:
                blocked_reasons.append("BCS board is empty")

            positions.append(
                Position(
                    instrument_uid=f"BCS:{class_code}:{ticker}",
                    ticker=ticker,
                    class_code=class_code,
                    instrument_type=instrument_type,
                    quantity=quantity,
                    market_price=current_price,
                    market_value_rub=current_value_rub,
                    tradable=not blocked_reasons,
                    blocked_reason="; ".join(blocked_reasons) or None,
                    display_name=str(raw.get("displayName") or ticker),
                    currency=str(raw.get("currency") or "RUB").upper(),
                    locked_quantity=locked,
                )
            )

        if len(agreement_ids) != 1:
            raise PortfolioContractError("BCS response must contain exactly one agreement")
        agreement_id = next(iter(agreement_ids))
        account_ref = "bcs:" + hashlib.sha256(agreement_id.encode("utf-8")).hexdigest()[:12]
        positions.sort(key=lambda item: (-item.market_value_rub, item.ticker))
        return PortfolioSnapshot(
            account_ref=account_ref,
            is_iis=is_iis,
            as_of=self._now(),
            cash_rub=cash_rub,
            positions=tuple(positions),
        )

    def cash_by_settlement_term(
        self,
        payload: Mapping[str, Any],
    ) -> dict[str, Decimal]:
        raw_positions = payload.get("positions")
        if not isinstance(raw_positions, list):
            raise PortfolioContractError("BCS portfolio field 'positions' must be an array")
        cash: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for index, raw in enumerate(raw_positions):
            if not isinstance(raw, dict):
                raise PortfolioContractError(f"positions[{index}] must be an object")
            instrument_type = self._instrument_type(raw)
            if not self._is_ruble_cash(raw, instrument_type):
                continue
            term = self._required_text(raw, "term", index).upper()
            current_value_rub = self._decimal(raw, "currentValueRub", index)
            if current_value_rub < 0:
                raise PortfolioContractError(
                    f"positions[{index}] contains negative cash"
                )
            cash[term] += current_value_rub
        return dict(sorted(cash.items()))

    def bond_income_summary(
        self,
        payload: Mapping[str, Any],
        *,
        term: str = "T0",
    ) -> dict[str, Any]:
        raw_positions = payload.get("positions")
        if not isinstance(raw_positions, list):
            raise PortfolioContractError("BCS portfolio field 'positions' must be an array")
        normalized_term = term.strip().upper()
        positions: list[dict[str, Any]] = []
        current_total = Decimal("0")
        cost_total = Decimal("0")
        daily_total = Decimal("0")
        accrued_total = Decimal("0")
        for index, raw in enumerate(raw_positions):
            if not isinstance(raw, dict):
                raise PortfolioContractError(f"positions[{index}] must be an object")
            if self._required_text(raw, "term", index).upper() != normalized_term:
                continue
            if self._instrument_type(raw) is not InstrumentType.BOND:
                continue
            if str(raw.get("currency") or "").upper() != "RUB":
                continue
            quantity = self._decimal(raw, "quantity", index)
            current_value = self._decimal(raw, "currentValueRub", index)
            cost_value = self._decimal(raw, "balanceValueRub", index)
            daily_pl = self._decimal(raw, "dailyPL", index)
            accrued_per_unit = self._decimal(raw, "accruedIncome", index)
            unrealized = current_value - cost_value
            accrued = accrued_per_unit * quantity
            positions.append(
                {
                    "ticker": self._required_text(raw, "ticker", index),
                    "name": str(raw.get("displayName") or raw.get("ticker") or ""),
                    "quantity": quantity,
                    "current_value_rub": current_value,
                    "cost_basis_rub": cost_value,
                    "unrealized_pl_rub": unrealized,
                    "unrealized_return_percent": (
                        None
                        if cost_value == 0
                        else unrealized / cost_value * Decimal("100")
                    ),
                    "daily_pl_rub": daily_pl,
                    "accrued_income_rub": accrued,
                }
            )
            current_total += current_value
            cost_total += cost_value
            daily_total += daily_pl
            accrued_total += accrued
        unrealized_total = current_total - cost_total
        return {
            "settlement_term": normalized_term,
            "current_value_rub": current_total,
            "cost_basis_rub": cost_total,
            "unrealized_pl_rub": unrealized_total,
            "unrealized_return_percent": (
                None
                if cost_total == 0
                else unrealized_total / cost_total * Decimal("100")
            ),
            "daily_pl_rub": daily_total,
            "accrued_income_rub": accrued_total,
            "positions": tuple(
                sorted(
                    positions,
                    key=lambda item: (-item["current_value_rub"], item["ticker"]),
                )
            ),
            "limitations": (
                "Unrealized P&L is current BCS market value minus BCS cost basis.",
                "Accrued income may already be reflected in dirty market value.",
                "Historical coupons already paid, taxes and broker fees are not in this endpoint.",
            ),
        }

    def _select_current_term(self, raw_positions: list[Any]) -> list[dict[str, Any]]:
        by_term: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for index, raw in enumerate(raw_positions):
            if not isinstance(raw, dict):
                raise PortfolioContractError(f"positions[{index}] must be an object")
            term = self._required_text(raw, "term", index).upper()
            by_term[term].append(raw)
        if "T0" in by_term:
            return by_term["T0"]
        if len(by_term) == 1:
            return next(iter(by_term.values()))
        available = ", ".join(sorted(by_term))
        raise PortfolioContractError(f"BCS portfolio has no T0 term; available terms: {available}")

    @staticmethod
    def _instrument_type(raw: Mapping[str, Any]) -> InstrumentType:
        instrument_type = str(raw.get("instrumentType") or "").upper()
        upper_type = str(raw.get("upperType") or "").upper()
        return _INSTRUMENT_TYPES.get(
            instrument_type,
            _INSTRUMENT_TYPES.get(upper_type, InstrumentType.UNKNOWN),
        )

    @staticmethod
    def _is_ruble_cash(raw: Mapping[str, Any], instrument_type: InstrumentType) -> bool:
        ticker = str(raw.get("ticker") or "").upper()
        currency = str(raw.get("currency") or "").upper()
        board = str(raw.get("board") or "")
        return instrument_type is InstrumentType.CASH or (
            instrument_type is InstrumentType.CURRENCY
            and ticker == "RUB"
            and currency == "RUB"
            and not board
        )

    @staticmethod
    def _required_text(raw: Mapping[str, Any], field: str, index: int) -> str:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise PortfolioContractError(f"positions[{index}].{field} must be non-empty text")
        return value

    @staticmethod
    def _decimal(raw: Mapping[str, Any], field: str, index: int) -> Decimal:
        value = raw.get(field)
        if isinstance(value, bool) or value is None:
            raise PortfolioContractError(f"positions[{index}].{field} must be numeric")
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError) as error:
            raise PortfolioContractError(f"positions[{index}].{field} must be numeric") from error
        if not result.is_finite():
            raise PortfolioContractError(f"positions[{index}].{field} must be finite")
        return result

    @staticmethod
    def _boolean(raw: Mapping[str, Any], field: str, index: int) -> bool:
        value = raw.get(field)
        if not isinstance(value, bool):
            raise PortfolioContractError(f"positions[{index}].{field} must be boolean")
        return value


def portfolio_as_dict(snapshot: PortfolioSnapshot) -> dict[str, Any]:
    total = snapshot.total_value_rub
    allocation: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    allocation[InstrumentType.CASH.value] += snapshot.cash_rub
    for position in snapshot.positions:
        allocation[position.instrument_type.value] += position.market_value_rub
    return {
        "account_ref": snapshot.account_ref,
        "is_iis": snapshot.is_iis,
        "as_of": snapshot.as_of.isoformat(),
        "cash_rub": str(snapshot.cash_rub),
        "total_value_rub": str(total),
        "allocation": {
            key: {
                "value_rub": str(value),
                "share": "0" if total == 0 else str(value / total),
            }
            for key, value in sorted(allocation.items())
        },
        "positions": [
            {
                "ticker": item.ticker,
                "name": item.display_name,
                "class_code": item.class_code,
                "instrument_type": item.instrument_type.value,
                "quantity": str(item.quantity),
                "locked_quantity": str(item.locked_quantity),
                "available_quantity": str(item.available_quantity),
                "market_price": str(item.market_price),
                "market_value_rub": str(item.market_value_rub),
                "portfolio_share": "0" if total == 0 else str(item.market_value_rub / total),
                "tradable": item.tradable,
                "blocked_reason": item.blocked_reason,
            }
            for item in snapshot.positions
        ],
    }


def bond_income_as_dict(summary: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "settlement_term": summary["settlement_term"],
        "current_value_rub": _optional_decimal_text(summary["current_value_rub"]),
        "cost_basis_rub": _optional_decimal_text(summary["cost_basis_rub"]),
        "unrealized_pl_rub": _optional_decimal_text(summary["unrealized_pl_rub"]),
        "unrealized_return_percent": _optional_decimal_text(
            summary["unrealized_return_percent"]
        ),
        "daily_pl_rub": _optional_decimal_text(summary["daily_pl_rub"]),
        "accrued_income_rub": _optional_decimal_text(summary["accrued_income_rub"]),
        "positions": [
            {
                key: (_optional_decimal_text(value) if isinstance(value, Decimal) else value)
                for key, value in position.items()
            }
            for position in summary["positions"]
        ],
        "limitations": list(summary["limitations"]),
    }


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def render_portfolio_json(snapshot: PortfolioSnapshot) -> str:
    return json.dumps(portfolio_as_dict(snapshot), ensure_ascii=False, indent=2)


def render_portfolio_text(snapshot: PortfolioSnapshot) -> str:
    data = portfolio_as_dict(snapshot)
    lines = [
        f"Портфель {data['account_ref']} ({'ИИС' if snapshot.is_iis else 'брокерский счёт'})",
        f"Снимок: {snapshot.as_of.isoformat()}",
        f"Стоимость: {snapshot.total_value_rub:,.2f} ₽",
        f"Свободные рубли: {snapshot.cash_rub:,.2f} ₽",
        "",
        "Позиции:",
    ]
    for item in snapshot.positions:
        status = "доступна" if item.tradable else f"заблокирована: {item.blocked_reason}"
        lines.append(
            f"- {item.ticker} · {item.instrument_type.value} · "
            f"{item.market_value_rub:,.2f} ₽ · {status}"
        )
    return "\n".join(lines)
