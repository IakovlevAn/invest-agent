"""Portfolio-level MOEX enrichment for Russian bond positions."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from invest_agent.domain import InstrumentType, PortfolioSnapshot, Position
from invest_agent.market.moex import (
    MoexApiError,
    MoexBondMarketData,
    MoexBondSnapshot,
    MoexContractError,
    MoexEmitter,
    MoexIssClient,
)


@dataclass(frozen=True, slots=True)
class BondEnrichmentFailure:
    ticker: str
    stage: str
    message: str


@dataclass(frozen=True, slots=True)
class EnrichedBondPosition:
    position: Position
    moex: MoexBondSnapshot
    emitter: MoexEmitter | None
    missing_fields: tuple[str, ...]

    @property
    def comparable_effective_yield_percent(self) -> Decimal | None:
        market = self.moex.market
        if market is None or _is_floater(self.moex.facts.bond_type):
            return None
        return market.effective_yield_percent

    @property
    def yield_interpretation(self) -> str:
        if _is_floater(self.moex.facts.bond_type):
            return "FLOATER_REQUIRES_RATE_SCENARIO"
        if self.comparable_effective_yield_percent is None:
            return "MISSING"
        return "MOEX_EFFECTIVE_YIELD"


@dataclass(frozen=True, slots=True)
class IssuerExposure:
    emitter_id: int
    emitter_name: str
    value_rub: Decimal
    share_of_covered_bonds: Decimal
    issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BondMarketReport:
    account_ref: str
    portfolio_as_of: datetime
    fetched_at: datetime
    total_portfolio_value_rub: Decimal
    total_bond_positions: int
    bond_value_rub: Decimal
    covered_bond_value_rub: Decimal
    market_covered_bond_value_rub: Decimal
    issuer_covered_value_rub: Decimal
    positions: tuple[EnrichedBondPosition, ...]
    issuer_exposures: tuple[IssuerExposure, ...]
    failures: tuple[BondEnrichmentFailure, ...]
    issuer_hhi_on_covered_bonds: Decimal
    effective_issuer_count: Decimal | None

    @property
    def bond_position_count(self) -> int:
        return self.total_bond_positions

    @property
    def market_coverage_share(self) -> Decimal:
        return _share(self.market_covered_bond_value_rub, self.bond_value_rub)

    @property
    def issue_coverage_share(self) -> Decimal:
        return _share(self.covered_bond_value_rub, self.bond_value_rub)

    @property
    def issuer_coverage_share(self) -> Decimal:
        return _share(self.issuer_covered_value_rub, self.bond_value_rub)

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_ref": self.account_ref,
            "portfolio_as_of": self.portfolio_as_of.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
            "source": {
                "provider": "Moscow Exchange ISS",
                "access": "public read-only data; may be delayed",
            },
            "coverage": {
                "bond_positions": self.bond_position_count,
                "enriched_positions": len(self.positions),
                "bond_value_rub": _decimal_text(self.bond_value_rub),
                "covered_bond_value_rub": _decimal_text(self.covered_bond_value_rub),
                "market_covered_bond_value_rub": _decimal_text(
                    self.market_covered_bond_value_rub
                ),
                "issue_share": _decimal_text(self.issue_coverage_share),
                "market_share": _decimal_text(self.market_coverage_share),
                "issuer_share": _decimal_text(self.issuer_coverage_share),
            },
            "issuer_concentration": {
                "hhi_on_covered_bonds": _decimal_text(self.issuer_hhi_on_covered_bonds),
                "effective_issuer_count": (
                    None
                    if self.effective_issuer_count is None
                    else _decimal_text(self.effective_issuer_count)
                ),
                "exposures": [
                    {
                        "emitter_id": exposure.emitter_id,
                        "emitter_name": exposure.emitter_name,
                        "value_rub": _decimal_text(exposure.value_rub),
                        "share_of_covered_bonds": _decimal_text(
                            exposure.share_of_covered_bonds
                        ),
                        "issues": list(exposure.issues),
                    }
                    for exposure in self.issuer_exposures
                ],
            },
            "positions": [_position_as_dict(item) for item in self.positions],
            "failures": [
                {
                    "ticker": failure.ticker,
                    "stage": failure.stage,
                    "message": failure.message,
                }
                for failure in self.failures
            ],
            "remaining_data_gaps": [
                "кредитные рейтинги и пресс-релизы рейтинговых агентств",
                "финансовая отчётность, оферты, ковенанты и корпоративные события",
                "история цен и оборотов для устойчивой оценки ликвидности",
                (
                    "ожидаемая вероятность дефолта и потери при дефолте "
                    "являются моделью, а не фактом MOEX"
                ),
            ],
            "execution_state": "ANALYSIS_ONLY",
        }


class BondPortfolioEnricher:
    def __init__(
        self,
        client: MoexIssClient,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._now = now or (lambda: datetime.now(tz=UTC))

    def enrich(self, snapshot: PortfolioSnapshot) -> BondMarketReport:
        bond_positions = tuple(
            position
            for position in snapshot.positions
            if position.instrument_type is InstrumentType.BOND
        )
        bond_value = sum(
            (position.market_value_rub for position in bond_positions),
            start=Decimal("0"),
        )
        emitter_cache: dict[int, MoexEmitter | None] = {}
        records: list[EnrichedBondPosition] = []
        failures: list[BondEnrichmentFailure] = []

        for position in bond_positions:
            try:
                moex = self._client.fetch_bond(position.ticker)
            except (MoexApiError, MoexContractError, ValueError) as error:
                failures.append(
                    BondEnrichmentFailure(
                        ticker=position.ticker,
                        stage="bond",
                        message=str(error),
                    )
                )
                continue

            emitter_id = moex.facts.emitter_id
            if emitter_id not in emitter_cache:
                try:
                    emitter_cache[emitter_id] = self._client.fetch_emitter(emitter_id)
                except (MoexApiError, MoexContractError, ValueError) as error:
                    emitter_cache[emitter_id] = None
                    failures.append(
                        BondEnrichmentFailure(
                            ticker=position.ticker,
                            stage="emitter",
                            message=str(error),
                        )
                    )
            emitter = emitter_cache[emitter_id]
            records.append(
                EnrichedBondPosition(
                    position=position,
                    moex=moex,
                    emitter=emitter,
                    missing_fields=_missing_fields(moex, emitter),
                )
            )

        covered_value = sum(
            (record.position.market_value_rub for record in records),
            start=Decimal("0"),
        )
        market_covered_value = sum(
            (
                record.position.market_value_rub
                for record in records
                if record.moex.market is not None
            ),
            start=Decimal("0"),
        )
        issuer_records = [record for record in records if record.emitter is not None]
        issuer_covered_value = sum(
            (record.position.market_value_rub for record in issuer_records),
            start=Decimal("0"),
        )
        issuer_exposures, issuer_hhi = _issuer_concentration(
            issuer_records,
            issuer_covered_value,
        )
        effective_count = None if issuer_hhi == 0 else Decimal("1") / issuer_hhi
        return BondMarketReport(
            account_ref=snapshot.account_ref,
            portfolio_as_of=snapshot.as_of,
            fetched_at=self._now(),
            total_portfolio_value_rub=snapshot.total_value_rub,
            total_bond_positions=len(bond_positions),
            bond_value_rub=bond_value,
            covered_bond_value_rub=covered_value,
            market_covered_bond_value_rub=market_covered_value,
            issuer_covered_value_rub=issuer_covered_value,
            positions=tuple(records),
            issuer_exposures=issuer_exposures,
            failures=tuple(failures),
            issuer_hhi_on_covered_bonds=issuer_hhi,
            effective_issuer_count=effective_count,
        )


def render_bond_report_json(report: BondMarketReport) -> str:
    return json.dumps(report.as_dict(), ensure_ascii=False, indent=2)


def render_bond_report_text(report: BondMarketReport) -> str:
    effective_count = (
        "н/д"
        if report.effective_issuer_count is None
        else f"{report.effective_issuer_count:.2f}"
    )
    lines = [
        f"Облигации MOEX для {report.account_ref}",
        f"Портфель на: {report.portfolio_as_of.isoformat()}",
        f"Данные получены: {report.fetched_at.isoformat()}",
        "Источник: Moscow Exchange ISS, публичные read-only данные могут быть задержаны.",
        f"Покрытие выпусков: {len(report.positions)}/{report.bond_position_count} позиций, "
        f"{report.issue_coverage_share:.2%} стоимости облигаций",
        f"Покрытие торговых данных: {report.market_coverage_share:.2%}",
        f"Покрытие эмитентов: {report.issuer_coverage_share:.2%}",
        f"HHI эмитентов на покрытой части: {report.issuer_hhi_on_covered_bonds:.4f}; "
        f"эффективное число эмитентов: {effective_count}",
        "",
        "Эмитенты:",
    ]
    if report.issuer_exposures:
        for exposure in report.issuer_exposures:
            issues = ", ".join(exposure.issues)
            lines.append(
                f"- {exposure.emitter_name}: {exposure.value_rub:,.2f} ₽ "
                f"({exposure.share_of_covered_bonds:.2%}); выпуски: {issues}"
            )
    else:
        lines.append("- нет покрытых данных")

    lines.extend(["", "Выпуски:"])
    for record in report.positions:
        market = record.moex.market
        yield_text = _yield_text(record)
        duration_text = (
            "н/д"
            if market is None or market.duration_days is None
            else f"{market.duration_days} дн."
        )
        turnover_text = (
            "н/д"
            if market is None or market.turnover_today_rub is None
            else f"{market.turnover_today_rub:,.0f} ₽"
        )
        emitter_name = "эмитент не покрыт" if record.emitter is None else record.emitter.short_title
        lines.append(
            f"- {record.position.ticker} · {emitter_name} · "
            f"{record.position.market_value_rub:,.2f} ₽ · доходность {yield_text} · "
            f"дюрация {duration_text} · оборот сегодня {turnover_text}"
        )
        if record.missing_fields:
            lines.append(f"  нет данных: {', '.join(record.missing_fields)}")

    if report.failures:
        lines.extend(["", "Ошибки покрытия:"])
        lines.extend(
            f"- {failure.ticker} ({failure.stage}): {failure.message}"
            for failure in report.failures
        )
    lines.extend(
        [
            "",
            (
                "Пока не покрыто: рейтинги, отчётность, ковенанты, "
                "история ликвидности и модель PD/LGD."
            ),
            "Статус исполнения: только анализ, сделок нет.",
        ]
    )
    return "\n".join(lines)


def _issuer_concentration(
    records: list[EnrichedBondPosition],
    covered_value: Decimal,
) -> tuple[tuple[IssuerExposure, ...], Decimal]:
    if covered_value == 0:
        return (), Decimal("0")
    values: defaultdict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    names: dict[int, str] = {}
    issues: defaultdict[int, list[str]] = defaultdict(list)
    for record in records:
        assert record.emitter is not None
        emitter_id = record.emitter.emitter_id
        values[emitter_id] += record.position.market_value_rub
        names[emitter_id] = record.emitter.short_title
        issues[emitter_id].append(record.position.ticker)
    exposures = tuple(
        IssuerExposure(
            emitter_id=emitter_id,
            emitter_name=names[emitter_id],
            value_rub=value,
            share_of_covered_bonds=value / covered_value,
            issues=tuple(sorted(issues[emitter_id])),
        )
        for emitter_id, value in sorted(values.items(), key=lambda item: (-item[1], item[0]))
    )
    hhi = sum(
        (exposure.share_of_covered_bonds**2 for exposure in exposures),
        start=Decimal("0"),
    )
    return exposures, hhi


def _missing_fields(moex: MoexBondSnapshot, emitter: MoexEmitter | None) -> tuple[str, ...]:
    missing: list[str] = []
    if emitter is None:
        missing.append("эмитент")
    facts = moex.facts
    market = moex.market
    if facts.maturity_date is None:
        missing.append("дата погашения")
    if facts.coupon_percent is None and facts.coupon_benchmark is None:
        missing.append("купон")
    if market is None:
        missing.extend(("котировки", "доходность", "дюрация", "ликвидность"))
        return tuple(missing)
    if _is_floater(facts.bond_type):
        missing.append("сопоставимая доходность: флоатер требует сценария ставки")
    elif market.effective_yield_percent is None:
        missing.append("эффективная доходность")
    if market.duration_days is None:
        missing.append("дюрация")
    if market.bid_percent is None or market.offer_percent is None:
        missing.append("bid/offer")
    if market.turnover_today_rub is None:
        missing.append("оборот")
    if market.z_spread_bps is None:
        missing.append("Z-spread")
    if market.g_spread_bps is None:
        missing.append("G-spread")
    return tuple(missing)


def _position_as_dict(record: EnrichedBondPosition) -> dict[str, Any]:
    position = record.position
    facts = record.moex.facts
    market = record.moex.market
    return {
        "portfolio": {
            "ticker": position.ticker,
            "quantity": _decimal_text(position.quantity),
            "market_value_rub": _decimal_text(position.market_value_rub),
            "tradable": position.tradable,
        },
        "security": {
            "secid": facts.secid,
            "isin": facts.isin,
            "name": facts.name,
            "short_name": facts.short_name,
            "primary_board": facts.primary_board,
            "issue_name": facts.issue_name,
            "registration_number": facts.registration_number,
            "issue_date": _date_text(facts.issue_date),
            "maturity_date": _date_text(facts.maturity_date),
            "face_value": _optional_decimal_text(facts.face_value),
            "face_currency": facts.face_currency,
            "issue_size": _optional_decimal_text(facts.issue_size),
            "list_level": facts.list_level,
            "coupon_percent": _optional_decimal_text(facts.coupon_percent),
            "coupon_value": _optional_decimal_text(facts.coupon_value),
            "coupon_frequency": facts.coupon_frequency,
            "coupon_benchmark": facts.coupon_benchmark,
            "coupon_benchmark_spread": _optional_decimal_text(
                facts.coupon_benchmark_spread
            ),
            "next_coupon_date": _date_text(facts.next_coupon_date),
            "offer_date": _date_text(facts.offer_date),
            "bond_type": facts.bond_type,
            "bond_subtype": facts.bond_subtype,
            "qualified_only": facts.qualified_only,
            "has_default": facts.has_default,
            "has_technical_default": facts.has_technical_default,
        },
        "emitter": (
            None
            if record.emitter is None
            else {
                "id": record.emitter.emitter_id,
                "title": record.emitter.title,
                "short_title": record.emitter.short_title,
                "inn": record.emitter.inn,
                "ogrn": record.emitter.ogrn,
                "website": record.emitter.website,
            }
        ),
        "market": None if market is None else _market_as_dict(market, record),
        "missing_fields": list(record.missing_fields),
        "source": {
            "security_url": facts.source_url,
            "market_url": None if market is None else market.source_url,
            "emitter_url": None if record.emitter is None else record.emitter.source_url,
            "fetched_at": record.moex.fetched_at.isoformat(),
        },
    }


def _market_as_dict(
    market: MoexBondMarketData,
    record: EnrichedBondPosition,
) -> dict[str, Any]:
    return {
        "board_id": market.board_id,
        "bid_percent_of_face": _optional_decimal_text(market.bid_percent),
        "offer_percent_of_face": _optional_decimal_text(market.offer_percent),
        "bid_offer_spread_percent_of_face": _optional_decimal_text(
            market.bid_offer_spread_percent_of_face
        ),
        "last_percent_of_face": _optional_decimal_text(market.last_percent),
        "wap_percent_of_face": _optional_decimal_text(market.wap_percent),
        "yield": {
            "comparable_effective_yield_percent": _optional_decimal_text(
                record.comparable_effective_yield_percent
            ),
            "interpretation": record.yield_interpretation,
            "moex_effective_yield_raw_percent": _optional_decimal_text(
                market.effective_yield_percent
            ),
            "moex_yield_at_wap_raw_percent": _optional_decimal_text(
                market.yield_at_wap_percent
            ),
        },
        "yield_date": _date_text(market.yield_date),
        "yield_date_type": market.yield_date_type,
        "duration_days": _optional_decimal_text(market.duration_days),
        "z_spread_bps": _optional_decimal_text(market.z_spread_bps),
        "g_spread_bps": _optional_decimal_text(market.g_spread_bps),
        "trades_today": market.trades_today,
        "volume_today": _optional_decimal_text(market.volume_today),
        "turnover_today_rub": _optional_decimal_text(market.turnover_today_rub),
        "trading_status": market.trading_status,
        "trade_moment": _datetime_text(market.trade_moment),
        "system_moment": _datetime_text(market.system_moment),
    }


def _share(value: Decimal, denominator: Decimal) -> Decimal:
    return Decimal("0") if denominator == 0 else value / denominator


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else _decimal_text(value)


def _date_text(value: Any) -> str | None:
    return None if value is None else value.isoformat()


def _datetime_text(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _percent_or_na(value: Decimal | None) -> str:
    return "н/д" if value is None else f"{value:.2f}%"


def _yield_text(record: EnrichedBondPosition) -> str:
    comparable = record.comparable_effective_yield_percent
    if comparable is not None:
        return _percent_or_na(comparable)
    facts = record.moex.facts
    if _is_floater(facts.bond_type):
        benchmark = facts.coupon_benchmark or "плавающий бенчмарк"
        spread = facts.coupon_benchmark_spread
        spread_text = "" if spread is None else f" + {spread}%"
        return f"н/д (флоатер: {benchmark}{spread_text})"
    return "н/д"


def _is_floater(bond_type: str | None) -> bool:
    return bond_type is not None and "флоат" in bond_type.casefold()
