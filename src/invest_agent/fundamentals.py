"""Fundamental and payment-schedule passports for the current bond sleeve."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from invest_agent.bond_report import BondMarketReport, EnrichedBondPosition
from invest_agent.financial.fns import (
    GIRBO_BASE_URL,
    GirboApiError,
    GirboContractError,
    RasFinancialStatement,
)
from invest_agent.market.moex import (
    BONDIZATION_BASE_URL,
    MoexApiError,
    MoexBondSchedule,
    MoexContractError,
)


class FundamentalSeverity(StrEnum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    INFO = "INFO"


@dataclass(frozen=True, slots=True)
class FundamentalPolicy:
    financial_max_age_days: int
    revenue_decline_warning: Decimal
    interest_coverage_warning: Decimal
    current_ratio_warning: Decimal
    debt_to_equity_warning: Decimal
    near_term_event_days: int

    @classmethod
    def from_toml(cls, path: str | Path) -> FundamentalPolicy:
        with Path(path).open("rb") as source:
            raw = tomllib.load(source)["fundamentals"]
        policy = cls(
            financial_max_age_days=_positive_int(raw["financial_max_age_days"]),
            revenue_decline_warning=_positive_decimal(raw["revenue_decline_warning"]),
            interest_coverage_warning=_positive_decimal(raw["interest_coverage_warning"]),
            current_ratio_warning=_positive_decimal(raw["current_ratio_warning"]),
            debt_to_equity_warning=_positive_decimal(raw["debt_to_equity_warning"]),
            near_term_event_days=_positive_int(raw["near_term_event_days"]),
        )
        if policy.revenue_decline_warning >= 1:
            raise ValueError("fundamentals.revenue_decline_warning must be below 1")
        return policy


@dataclass(frozen=True, slots=True)
class FundamentalSignal:
    code: str
    severity: FundamentalSeverity
    message: str


@dataclass(frozen=True, slots=True)
class FinancialMetrics:
    gross_debt: Decimal | None
    net_debt: Decimal | None
    approximate_free_cash_flow: Decimal | None
    revenue_growth: Decimal | None
    operating_margin: Decimal | None
    net_margin: Decimal | None
    interest_coverage: Decimal | None
    current_ratio: Decimal | None
    debt_to_equity: Decimal | None


@dataclass(frozen=True, slots=True)
class ScheduleSummary:
    next_coupon_date: date | None
    coupon_known_until: date | None
    next_amortization_date: date | None
    next_amortization_percent: Decimal | None
    next_offer_date: date | None
    next_offer_window_start: date | None
    next_offer_window_end: date | None
    next_offer_type: str | None
    next_offer_price_percent: Decimal | None
    future_coupon_count: int
    future_amortization_count: int
    future_offer_count: int
    source_url: str
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class FundamentalPassport:
    ticker: str
    isin: str
    emitter_name: str
    inn: str | None
    market_value_rub: Decimal
    statement: RasFinancialStatement | None
    metrics: FinancialMetrics | None
    schedule: ScheduleSummary | None
    signals: tuple[FundamentalSignal, ...]
    financial_lookup_complete: bool
    schedule_lookup_complete: bool


@dataclass(frozen=True, slots=True)
class FundamentalFailure:
    ticker: str
    stage: str
    message: str


@dataclass(frozen=True, slots=True)
class FundamentalPortfolioReport:
    account_ref: str
    portfolio_as_of: datetime
    moex_fetched_at: datetime
    fetched_at: datetime
    total_bond_positions: int
    bond_value_rub: Decimal
    financial_covered_value_rub: Decimal
    schedule_covered_value_rub: Decimal
    passports: tuple[FundamentalPassport, ...]
    failures: tuple[FundamentalFailure, ...]

    @property
    def financial_coverage_share(self) -> Decimal:
        return _share(self.financial_covered_value_rub, self.bond_value_rub)

    @property
    def schedule_coverage_share(self) -> Decimal:
        return _share(self.schedule_covered_value_rub, self.bond_value_rub)

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_ref": self.account_ref,
            "portfolio_as_of": self.portfolio_as_of.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
            "sources": {
                "financials": {
                    "provider": "FNS GIR BO",
                    "url": GIRBO_BASE_URL,
                    "scope": "annual standalone RAS of the exact legal entity",
                    "access": "official public read-only website endpoint; contract may change",
                },
                "payment_schedule": {
                    "provider": "Moscow Exchange ISS bondization",
                    "url": BONDIZATION_BASE_URL,
                    "access": "official public read-only data; may be delayed",
                },
                "moex_fetched_at": self.moex_fetched_at.isoformat(),
            },
            "coverage": {
                "bond_positions": self.total_bond_positions,
                "passports": len(self.passports),
                "bond_value_rub": _decimal_text(self.bond_value_rub),
                "financial_covered_value_rub": _decimal_text(self.financial_covered_value_rub),
                "financial_share": _decimal_text(self.financial_coverage_share),
                "schedule_covered_value_rub": _decimal_text(self.schedule_covered_value_rub),
                "schedule_share": _decimal_text(self.schedule_coverage_share),
            },
            "methodology": {
                "facts": (
                    "reported RAS lines for the exact issuer legal entity and MOEX payment dates"
                ),
                "model": (
                    "transparent arithmetic ratios; approximate FCF equals operating plus "
                    "investing cash flow"
                ),
                "scope_warning": (
                    "standalone RAS is not consolidated IFRS and must not be treated as group data"
                ),
                "judgment": "this command produces no buy, sell or hold recommendation",
            },
            "passports": [_passport_as_dict(passport) for passport in self.passports],
            "failures": [
                {
                    "ticker": failure.ticker,
                    "stage": failure.stage,
                    "message": failure.message,
                }
                for failure in self.failures
            ],
            "remaining_data_gaps": [
                "консолидированная МСФО и внутригрупповые корректировки",
                "EBITDA и чистый долг/EBITDA по методологии кредитного анализа",
                "ковенанты, обеспечение, субординация и гарантии по выпуску",
                "поддержка группы и календарь всех обязательств, не только текущего выпуска",
                "модель вероятности дефолта и потерь при дефолте (PD/LGD)",
            ],
            "execution_state": "ANALYSIS_ONLY",
        }


class FinancialClient(Protocol):
    def fetch_latest_annual(self, inn: str) -> RasFinancialStatement | None: ...


class ScheduleClient(Protocol):
    def fetch_bond_schedule(self, secid: str) -> MoexBondSchedule: ...


@dataclass(frozen=True, slots=True)
class _FinancialLookup:
    statement: RasFinancialStatement | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _ScheduleLookup:
    schedule: MoexBondSchedule | None
    error: str | None = None


class FundamentalPortfolioAnalyzer:
    def __init__(
        self,
        financial_client: FinancialClient,
        schedule_client: ScheduleClient,
        policy: FundamentalPolicy,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._financial_client = financial_client
        self._schedule_client = schedule_client
        self._policy = policy
        self._now = now or (lambda: datetime.now(tz=UTC))

    def analyze(self, bond_report: BondMarketReport) -> FundamentalPortfolioReport:
        fetched_at = self._now()
        financial_cache: dict[str, _FinancialLookup] = {}
        schedule_cache: dict[str, _ScheduleLookup] = {}
        failures = [
            FundamentalFailure(
                ticker=failure.ticker,
                stage=f"moex:{failure.stage}",
                message=failure.message,
            )
            for failure in bond_report.failures
        ]
        passports: list[FundamentalPassport] = []
        for record in bond_report.positions:
            emitter = record.emitter
            inn = None if emitter is None else emitter.inn
            if inn is None:
                financial = _FinancialLookup(None, "MOEX issuer INN is unavailable")
            elif inn in financial_cache:
                financial = financial_cache[inn]
            else:
                try:
                    financial = _FinancialLookup(self._financial_client.fetch_latest_annual(inn))
                except (GirboApiError, GirboContractError, ValueError) as error:
                    financial = _FinancialLookup(None, str(error))
                financial_cache[inn] = financial

            isin = record.moex.facts.isin.upper()
            if isin in schedule_cache:
                schedule_lookup = schedule_cache[isin]
            else:
                try:
                    raw_schedule = self._schedule_client.fetch_bond_schedule(record.position.ticker)
                    if raw_schedule.isin.upper() != isin:
                        raise MoexContractError("MOEX bondization ISIN mismatch")
                    schedule_lookup = _ScheduleLookup(raw_schedule)
                except (MoexApiError, MoexContractError, ValueError) as error:
                    schedule_lookup = _ScheduleLookup(None, str(error))
                schedule_cache[isin] = schedule_lookup

            if financial.error is not None:
                failures.append(
                    FundamentalFailure(
                        ticker=record.position.ticker,
                        stage="financials",
                        message=financial.error,
                    )
                )
            if schedule_lookup.error is not None:
                failures.append(
                    FundamentalFailure(
                        ticker=record.position.ticker,
                        stage="payment-schedule",
                        message=schedule_lookup.error,
                    )
                )
            statement = financial.statement
            metrics = None if statement is None else _financial_metrics(statement)
            schedule = (
                None
                if schedule_lookup.schedule is None
                else _schedule_summary(schedule_lookup.schedule, fetched_at.date())
            )
            passports.append(
                _passport(
                    record,
                    statement=statement,
                    metrics=metrics,
                    schedule=schedule,
                    financial_lookup_complete=financial.error is None,
                    schedule_lookup_complete=schedule_lookup.error is None,
                    as_of=fetched_at.date(),
                    policy=self._policy,
                )
            )

        financial_value = sum(
            (passport.market_value_rub for passport in passports if passport.statement is not None),
            Decimal("0"),
        )
        schedule_value = sum(
            (passport.market_value_rub for passport in passports if passport.schedule is not None),
            Decimal("0"),
        )
        return FundamentalPortfolioReport(
            account_ref=bond_report.account_ref,
            portfolio_as_of=bond_report.portfolio_as_of,
            moex_fetched_at=bond_report.fetched_at,
            fetched_at=fetched_at,
            total_bond_positions=bond_report.total_bond_positions,
            bond_value_rub=bond_report.bond_value_rub,
            financial_covered_value_rub=financial_value,
            schedule_covered_value_rub=schedule_value,
            passports=tuple(passports),
            failures=tuple(_unique_failures(failures)),
        )


def render_fundamental_report_json(report: FundamentalPortfolioReport) -> str:
    return json.dumps(report.as_dict(), ensure_ascii=False, indent=2)


def render_fundamental_report_text(report: FundamentalPortfolioReport) -> str:
    lines = [
        f"Фундаментальные паспорта для {report.account_ref}",
        f"Портфель на: {report.portfolio_as_of.isoformat()}",
        f"Данные получены: {report.fetched_at.isoformat()}",
        (
            f"Покрытие РСБУ: {report.financial_coverage_share:.2%}; "
            f"графиков выплат: {report.schedule_coverage_share:.2%}."
        ),
        "РСБУ относится только к юридическому лицу и не заменяет консолидированную МСФО.",
        "",
    ]
    for passport in report.passports:
        lines.append(
            f"- {passport.ticker} · {passport.emitter_name} · {passport.market_value_rub:,.2f} ₽"
        )
        if passport.statement is None:
            lines.append("  РСБУ: нет публичного годового отчёта или источник недоступен")
        else:
            statement = passport.statement
            metrics = passport.metrics
            assert metrics is not None
            lines.append(
                f"  РСБУ {statement.period}: выручка {_amount(statement.revenue)}, "
                f"чистая прибыль {_amount(statement.net_income)}, "
                f"чистый долг {_amount(metrics.net_debt)}"
            )
            lines.append(
                f"  покрытие процентов {_ratio_text(metrics.interest_coverage)}; "
                f"current ratio {_ratio_text(metrics.current_ratio)}; "
                f"долг/капитал {_ratio_text(metrics.debt_to_equity)}"
            )
        if passport.schedule is not None:
            schedule = passport.schedule
            lines.append(
                f"  выплаты: купон {_date_text(schedule.next_coupon_date)}, "
                f"амортизация {_date_text(schedule.next_amortization_date)}, "
                f"оферта {_date_text(schedule.next_offer_date)}"
            )
        for signal in passport.signals:
            lines.append(f"  {signal.severity.value} {signal.code}: {signal.message}")
    if report.failures:
        lines.extend(["", "Ошибки покрытия:"])
        lines.extend(
            f"- {failure.ticker} ({failure.stage}): {failure.message}"
            for failure in report.failures
        )
    lines.extend(
        [
            "",
            "Пока не покрыто: МСФО, ковенанты, обеспечение, поддержка группы и PD/LGD.",
            "Статус исполнения: только анализ, сделок и рекомендаций нет.",
        ]
    )
    return "\n".join(lines)


def _financial_metrics(statement: RasFinancialStatement) -> FinancialMetrics:
    gross_debt = _sum_known(statement.long_term_debt, statement.short_term_debt)
    net_debt = None if gross_debt is None or statement.cash is None else gross_debt - statement.cash
    approximate_fcf = _sum_required(
        statement.operating_cash_flow,
        statement.investing_cash_flow,
    )
    return FinancialMetrics(
        gross_debt=gross_debt,
        net_debt=net_debt,
        approximate_free_cash_flow=approximate_fcf,
        revenue_growth=_growth(statement.revenue, statement.previous_revenue),
        operating_margin=_ratio(statement.operating_profit, statement.revenue),
        net_margin=_ratio(statement.net_income, statement.revenue),
        interest_coverage=_ratio(statement.operating_profit, statement.interest_expense),
        current_ratio=_ratio(statement.current_assets, statement.current_liabilities),
        debt_to_equity=(
            None
            if gross_debt is None or statement.equity is None or statement.equity <= 0
            else gross_debt / statement.equity
        ),
    )


def _schedule_summary(schedule: MoexBondSchedule, as_of: date) -> ScheduleSummary:
    future_coupons = sorted(
        (coupon for coupon in schedule.coupons if coupon.coupon_date >= as_of),
        key=lambda coupon: coupon.coupon_date,
    )
    future_amortizations = sorted(
        (
            amortization
            for amortization in schedule.amortizations
            if amortization.amortization_date >= as_of
        ),
        key=lambda amortization: amortization.amortization_date,
    )
    future_offers = sorted(
        (
            offer
            for offer in schedule.offers
            if _offer_reference_date(offer) is not None and _offer_reference_date(offer) >= as_of
        ),
        key=lambda offer: _offer_reference_date(offer) or date.max,
    )
    next_amortization = future_amortizations[0] if future_amortizations else None
    next_offer = future_offers[0] if future_offers else None
    return ScheduleSummary(
        next_coupon_date=None if not future_coupons else future_coupons[0].coupon_date,
        coupon_known_until=None if not future_coupons else future_coupons[-1].coupon_date,
        next_amortization_date=(
            None if next_amortization is None else next_amortization.amortization_date
        ),
        next_amortization_percent=(
            None if next_amortization is None else next_amortization.value_percent
        ),
        next_offer_date=None if next_offer is None else _offer_reference_date(next_offer),
        next_offer_window_start=(None if next_offer is None else next_offer.offer_start_date),
        next_offer_window_end=None if next_offer is None else next_offer.offer_end_date,
        next_offer_type=None if next_offer is None else next_offer.offer_type,
        next_offer_price_percent=(None if next_offer is None else next_offer.price_percent),
        future_coupon_count=len(future_coupons),
        future_amortization_count=len(future_amortizations),
        future_offer_count=len(future_offers),
        source_url=schedule.source_url,
        fetched_at=schedule.fetched_at,
    )


def _passport(
    record: EnrichedBondPosition,
    *,
    statement: RasFinancialStatement | None,
    metrics: FinancialMetrics | None,
    schedule: ScheduleSummary | None,
    financial_lookup_complete: bool,
    schedule_lookup_complete: bool,
    as_of: date,
    policy: FundamentalPolicy,
) -> FundamentalPassport:
    signals: list[FundamentalSignal] = []
    if not financial_lookup_complete:
        signals.append(
            FundamentalSignal(
                "FINANCIAL_DATA_UNAVAILABLE",
                FundamentalSeverity.WARNING,
                "поиск отчётности юридического лица выполнен не полностью",
            )
        )
    elif statement is None:
        signals.append(
            FundamentalSignal(
                "NO_PUBLIC_RAS_STATEMENT",
                FundamentalSeverity.WARNING,
                "публичный годовой отчёт РСБУ по точному ИНН не найден",
            )
        )
    else:
        assert metrics is not None
        _financial_signals(signals, statement, metrics, as_of=as_of, policy=policy)
    if not schedule_lookup_complete or schedule is None:
        signals.append(
            FundamentalSignal(
                "PAYMENT_SCHEDULE_UNAVAILABLE",
                FundamentalSeverity.WARNING,
                "график выплат текущего выпуска MOEX получен не полностью",
            )
        )
    else:
        _schedule_signals(signals, schedule, as_of=as_of, policy=policy)
    signals.sort(key=lambda signal: (_severity_rank(signal.severity), signal.code))
    emitter = record.emitter
    return FundamentalPassport(
        ticker=record.position.ticker,
        isin=record.moex.facts.isin,
        emitter_name=record.moex.facts.name if emitter is None else emitter.short_title,
        inn=None if emitter is None else emitter.inn,
        market_value_rub=record.position.market_value_rub,
        statement=statement,
        metrics=metrics,
        schedule=schedule,
        signals=tuple(signals),
        financial_lookup_complete=financial_lookup_complete,
        schedule_lookup_complete=schedule_lookup_complete,
    )


def _financial_signals(
    signals: list[FundamentalSignal],
    statement: RasFinancialStatement,
    metrics: FinancialMetrics,
    *,
    as_of: date,
    policy: FundamentalPolicy,
) -> None:
    if as_of - statement.reported_at > timedelta(days=policy.financial_max_age_days):
        signals.append(
            FundamentalSignal(
                "FINANCIALS_STALE",
                FundamentalSeverity.WARNING,
                f"отчётность опубликована более {policy.financial_max_age_days} дней назад",
            )
        )
    if statement.equity is not None and statement.equity <= 0:
        signals.append(
            FundamentalSignal(
                "NEGATIVE_EQUITY",
                FundamentalSeverity.CRITICAL,
                "капитал юридического лица по РСБУ неположительный",
            )
        )
    if statement.operating_profit is not None and statement.operating_profit < 0:
        signals.append(
            FundamentalSignal(
                "OPERATING_LOSS",
                FundamentalSeverity.WARNING,
                "по РСБУ зафиксирован операционный убыток",
            )
        )
    if statement.net_income is not None and statement.net_income < 0:
        signals.append(
            FundamentalSignal(
                "NET_LOSS",
                FundamentalSeverity.WARNING,
                "по РСБУ зафиксирован чистый убыток",
            )
        )
    if (
        metrics.revenue_growth is not None
        and metrics.revenue_growth < -policy.revenue_decline_warning
    ):
        signals.append(
            FundamentalSignal(
                "REVENUE_DECLINE",
                FundamentalSeverity.WARNING,
                "выручка юридического лица снизилась выше диагностического порога",
            )
        )
    if (
        metrics.interest_coverage is not None
        and metrics.interest_coverage < policy.interest_coverage_warning
    ):
        signals.append(
            FundamentalSignal(
                "LOW_INTEREST_COVERAGE",
                FundamentalSeverity.WARNING,
                "операционная прибыль не покрывает процентные расходы по РСБУ",
            )
        )
    if metrics.current_ratio is not None and metrics.current_ratio < policy.current_ratio_warning:
        signals.append(
            FundamentalSignal(
                "LOW_CURRENT_RATIO",
                FundamentalSeverity.WARNING,
                "оборотные активы ниже краткосрочных обязательств по РСБУ",
            )
        )
    if (
        metrics.debt_to_equity is not None
        and metrics.debt_to_equity > policy.debt_to_equity_warning
    ):
        signals.append(
            FundamentalSignal(
                "HIGH_DEBT_TO_EQUITY",
                FundamentalSeverity.WARNING,
                "заёмные средства к капиталу выше диагностического порога",
            )
        )
    if metrics.approximate_free_cash_flow is not None and metrics.approximate_free_cash_flow < 0:
        signals.append(
            FundamentalSignal(
                "NEGATIVE_APPROX_FCF",
                FundamentalSeverity.INFO,
                "операционный плюс инвестиционный денежный поток отрицателен",
            )
        )


def _schedule_signals(
    signals: list[FundamentalSignal],
    schedule: ScheduleSummary,
    *,
    as_of: date,
    policy: FundamentalPolicy,
) -> None:
    horizon = as_of + timedelta(days=policy.near_term_event_days)
    if schedule.next_offer_date is not None and schedule.next_offer_date <= horizon:
        signals.append(
            FundamentalSignal(
                "NEAR_TERM_OFFER",
                FundamentalSeverity.INFO,
                f"оферта текущего выпуска ожидается {schedule.next_offer_date.isoformat()}",
            )
        )
    if schedule.next_amortization_date is not None and schedule.next_amortization_date <= horizon:
        signals.append(
            FundamentalSignal(
                "NEAR_TERM_AMORTIZATION",
                FundamentalSeverity.INFO,
                (
                    "амортизация или погашение текущего выпуска ожидается "
                    f"{schedule.next_amortization_date.isoformat()}"
                ),
            )
        )


def _passport_as_dict(passport: FundamentalPassport) -> dict[str, Any]:
    return {
        "ticker": passport.ticker,
        "isin": passport.isin,
        "emitter": {"name": passport.emitter_name, "inn": passport.inn},
        "market_value_rub": _decimal_text(passport.market_value_rub),
        "lookup": {
            "financial_complete": passport.financial_lookup_complete,
            "schedule_complete": passport.schedule_lookup_complete,
        },
        "financial_statement": (
            None if passport.statement is None else _statement_as_dict(passport.statement)
        ),
        "metrics": None if passport.metrics is None else _metrics_as_dict(passport.metrics),
        "payment_schedule": (
            None if passport.schedule is None else _schedule_as_dict(passport.schedule)
        ),
        "signals": [
            {
                "code": signal.code,
                "severity": signal.severity.value,
                "message": signal.message,
            }
            for signal in passport.signals
        ],
    }


def _statement_as_dict(statement: RasFinancialStatement) -> dict[str, Any]:
    return {
        "scope": statement.scope,
        "unit": statement.unit,
        "period": statement.period,
        "previous_period": statement.previous_period,
        "reported_at": statement.reported_at.isoformat(),
        "has_audit_report": statement.has_audit_report,
        "correction_version": statement.correction_version,
        "organization_name": statement.organization_name,
        "organization_id": statement.organization_id,
        "values": {
            field: _optional_decimal_text(getattr(statement, field))
            for field in (
                "revenue",
                "previous_revenue",
                "operating_profit",
                "previous_operating_profit",
                "interest_income",
                "interest_expense",
                "profit_before_tax",
                "net_income",
                "previous_net_income",
                "cash",
                "long_term_debt",
                "short_term_debt",
                "equity",
                "total_assets",
                "current_assets",
                "current_liabilities",
                "operating_cash_flow",
                "investing_cash_flow",
                "financing_cash_flow",
            )
        },
        "source_url": statement.source_url,
        "fetched_at": statement.fetched_at.isoformat(),
    }


def _metrics_as_dict(metrics: FinancialMetrics) -> dict[str, Any]:
    return {
        "amounts_thousand_rub": {
            "gross_debt": _optional_decimal_text(metrics.gross_debt),
            "net_debt": _optional_decimal_text(metrics.net_debt),
            "approximate_free_cash_flow": _optional_decimal_text(
                metrics.approximate_free_cash_flow
            ),
        },
        "ratios": {
            "revenue_growth": _optional_ratio_text(metrics.revenue_growth),
            "operating_margin": _optional_ratio_text(metrics.operating_margin),
            "net_margin": _optional_ratio_text(metrics.net_margin),
            "interest_coverage": _optional_ratio_text(metrics.interest_coverage),
            "current_ratio": _optional_ratio_text(metrics.current_ratio),
            "debt_to_equity": _optional_ratio_text(metrics.debt_to_equity),
        },
    }


def _schedule_as_dict(schedule: ScheduleSummary) -> dict[str, Any]:
    return {
        "next_coupon_date": _date_text(schedule.next_coupon_date),
        "coupon_known_until": _date_text(schedule.coupon_known_until),
        "next_amortization_date": _date_text(schedule.next_amortization_date),
        "next_amortization_percent": _optional_decimal_text(schedule.next_amortization_percent),
        "next_offer_date": _date_text(schedule.next_offer_date),
        "next_offer_window_start": _date_text(schedule.next_offer_window_start),
        "next_offer_window_end": _date_text(schedule.next_offer_window_end),
        "next_offer_type": schedule.next_offer_type,
        "next_offer_price_percent": _optional_decimal_text(schedule.next_offer_price_percent),
        "future_coupon_count": schedule.future_coupon_count,
        "future_amortization_count": schedule.future_amortization_count,
        "future_offer_count": schedule.future_offer_count,
        "source_url": schedule.source_url,
        "fetched_at": schedule.fetched_at.isoformat(),
    }


def _offer_reference_date(offer: Any) -> date | None:
    return offer.offer_start_date or offer.offer_date or offer.offer_end_date


def _sum_known(*values: Decimal | None) -> Decimal | None:
    known = [value for value in values if value is not None]
    return None if not known else sum(known, Decimal("0"))


def _sum_required(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    return None if left is None or right is None else left + right


def _growth(current: Decimal | None, previous: Decimal | None) -> Decimal | None:
    if current is None or previous is None or previous == 0:
        return None
    return current / previous - Decimal("1")


def _ratio(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _severity_rank(severity: FundamentalSeverity) -> int:
    return {
        FundamentalSeverity.CRITICAL: 0,
        FundamentalSeverity.WARNING: 1,
        FundamentalSeverity.INFO: 2,
    }[severity]


def _unique_failures(failures: list[FundamentalFailure]) -> list[FundamentalFailure]:
    seen: set[tuple[str, str, str]] = set()
    result: list[FundamentalFailure] = []
    for failure in failures:
        key = (failure.ticker, failure.stage, failure.message)
        if key not in seen:
            seen.add(key)
            result.append(failure)
    return result


def _positive_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("fundamental integer threshold must be positive")
    return value


def _positive_decimal(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError("fundamental decimal threshold must be positive and finite")
    return result


def _share(value: Decimal, denominator: Decimal) -> Decimal:
    return Decimal("0") if denominator == 0 else value / denominator


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else _decimal_text(value)


def _optional_ratio_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value, ".6f").rstrip("0").rstrip(".")
    return text or "0"


def _date_text(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _amount(value: Decimal | None) -> str:
    return "н/д" if value is None else f"{value:,.0f} тыс. ₽"


def _ratio_text(value: Decimal | None) -> str:
    return "н/д" if value is None else f"{value:.2f}x"
