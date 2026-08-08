"""Deterministic credit passports for the current Russian bond sleeve."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from invest_agent.bond_report import BondMarketReport, EnrichedBondPosition
from invest_agent.ratings.cbr import (
    CBR_RATINGS_URL,
    CbrRatingAction,
    CbrRatingsApiError,
    CbrRatingsCaptchaRequired,
)


class RatingBand(StrEnum):
    HIGHEST = "HIGHEST"
    HIGH = "HIGH"
    STRONG = "STRONG"
    ADEQUATE = "ADEQUATE"
    SPECULATIVE = "SPECULATIVE"
    HIGH_RISK = "HIGH_RISK"
    VERY_HIGH_RISK = "VERY_HIGH_RISK"
    DEFAULT = "DEFAULT"
    UNRATED = "UNRATED"


class SignalSeverity(StrEnum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    INFO = "INFO"


@dataclass(frozen=True, slots=True)
class CreditAnalysisPolicy:
    rating_max_age_days: int

    @classmethod
    def from_toml(cls, path: str | Path) -> CreditAnalysisPolicy:
        with Path(path).open("rb") as source:
            raw = tomllib.load(source)
        days = raw["credit"]["rating_max_age_days"]
        if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
            raise ValueError("credit.rating_max_age_days must be a positive integer")
        return cls(rating_max_age_days=days)


@dataclass(frozen=True, slots=True)
class CreditSignal:
    code: str
    severity: SignalSeverity
    message: str


@dataclass(frozen=True, slots=True)
class CreditRatingEvidence:
    scope: str
    action: CbrRatingAction
    band: RatingBand
    status: str


@dataclass(frozen=True, slots=True)
class CreditPassport:
    ticker: str
    isin: str
    emitter_id: int
    emitter_name: str
    inn: str | None
    market_value_rub: Decimal
    ratings: tuple[CreditRatingEvidence, ...]
    signals: tuple[CreditSignal, ...]
    issuer_lookup_complete: bool
    issue_lookup_complete: bool

    @property
    def current_ratings(self) -> tuple[CreditRatingEvidence, ...]:
        return tuple(rating for rating in self.ratings if rating.status == "CURRENT")


@dataclass(frozen=True, slots=True)
class CreditAnalysisFailure:
    ticker: str
    stage: str
    message: str


@dataclass(frozen=True, slots=True)
class CreditPortfolioReport:
    account_ref: str
    portfolio_as_of: datetime
    moex_fetched_at: datetime
    fetched_at: datetime
    rating_max_age_days: int
    total_bond_positions: int
    bond_value_rub: Decimal
    rating_covered_value_rub: Decimal
    passports: tuple[CreditPassport, ...]
    failures: tuple[CreditAnalysisFailure, ...]

    @property
    def rating_coverage_share(self) -> Decimal:
        if self.bond_value_rub == 0:
            return Decimal("0")
        return self.rating_covered_value_rub / self.bond_value_rub

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_ref": self.account_ref,
            "portfolio_as_of": self.portfolio_as_of.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
            "sources": {
                "ratings": {
                    "provider": "Bank of Russia central ratings repository",
                    "url": CBR_RATINGS_URL,
                    "access": (
                        "official public read-only web endpoint; contract may change; "
                        "agency release links are preserved"
                    ),
                },
                "moex_fetched_at": self.moex_fetched_at.isoformat(),
            },
            "coverage": {
                "bond_positions": self.total_bond_positions,
                "passports": len(self.passports),
                "bond_value_rub": _decimal_text(self.bond_value_rub),
                "rating_covered_value_rub": _decimal_text(self.rating_covered_value_rub),
                "rating_share": _decimal_text(self.rating_coverage_share),
            },
            "methodology": {
                "facts": "raw rating actions, dates and links from CBR; default flags from MOEX",
                "model": (
                    "agency-specific symbols are mapped only to broad diagnostic bands; "
                    "the mapping is not a probability of default or a cross-agency score"
                ),
                "judgment": "no buy, sell or hold recommendation is produced by this command",
                "rating_max_age_days": self.rating_max_age_days,
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
                "финансовая отчётность и нормализованная динамика показателей",
                "чистый долг/EBITDA, покрытие процентов и свободный денежный поток",
                "ковенанты, оферты, амортизация и календарь погашений",
                "поддержка группы, субординация и качество обеспечения",
                "модель вероятности дефолта и потерь при дефолте (PD/LGD)",
            ],
            "execution_state": "ANALYSIS_ONLY",
        }


class RatingsClient(Protocol):
    def fetch_by_inn(self, inn: str) -> tuple[CbrRatingAction, ...]: ...

    def fetch_by_isin(self, isin: str) -> tuple[CbrRatingAction, ...]: ...


@dataclass(frozen=True, slots=True)
class _LookupResult:
    actions: tuple[CbrRatingAction, ...]
    error: str | None = None


class CreditPortfolioAnalyzer:
    def __init__(
        self,
        client: RatingsClient,
        policy: CreditAnalysisPolicy,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._policy = policy
        self._now = now or (lambda: datetime.now(tz=UTC))

    def analyze(self, bond_report: BondMarketReport) -> CreditPortfolioReport:
        fetched_at = self._now()
        inn_cache: dict[str, _LookupResult] = {}
        isin_cache: dict[str, _LookupResult] = {}
        failures: list[CreditAnalysisFailure] = [
            CreditAnalysisFailure(
                ticker=failure.ticker,
                stage=f"moex:{failure.stage}",
                message=failure.message,
            )
            for failure in bond_report.failures
        ]
        cbr_blocked: str | None = None

        def lookup_inn(inn: str) -> _LookupResult:
            nonlocal cbr_blocked
            if inn in inn_cache:
                return inn_cache[inn]
            if cbr_blocked is not None:
                result = _LookupResult((), cbr_blocked)
            else:
                try:
                    result = _LookupResult(self._client.fetch_by_inn(inn))
                except CbrRatingsCaptchaRequired as error:
                    cbr_blocked = str(error)
                    result = _LookupResult((), cbr_blocked)
                except (CbrRatingsApiError, ValueError) as error:
                    result = _LookupResult((), str(error))
            inn_cache[inn] = result
            return result

        def lookup_isin(isin: str) -> _LookupResult:
            nonlocal cbr_blocked
            if isin in isin_cache:
                return isin_cache[isin]
            if cbr_blocked is not None:
                result = _LookupResult((), cbr_blocked)
            else:
                try:
                    result = _LookupResult(self._client.fetch_by_isin(isin))
                except CbrRatingsCaptchaRequired as error:
                    cbr_blocked = str(error)
                    result = _LookupResult((), cbr_blocked)
                except (CbrRatingsApiError, ValueError) as error:
                    result = _LookupResult((), str(error))
            isin_cache[isin] = result
            return result

        passports: list[CreditPassport] = []
        for record in bond_report.positions:
            emitter = record.emitter
            isin = record.moex.facts.isin.upper()
            inn = None if emitter is None else emitter.inn
            if inn is None:
                issuer_result = _LookupResult((), "MOEX issuer INN is unavailable")
            else:
                issuer_result = lookup_inn(inn)
            issue_result = lookup_isin(isin)

            if issuer_result.error is not None:
                failures.append(
                    CreditAnalysisFailure(
                        ticker=record.position.ticker,
                        stage="issuer-rating",
                        message=issuer_result.error,
                    )
                )
            if issue_result.error is not None:
                failures.append(
                    CreditAnalysisFailure(
                        ticker=record.position.ticker,
                        stage="issue-rating",
                        message=issue_result.error,
                    )
                )

            issuer_actions = _issuer_actions(issuer_result.actions, inn)
            issue_actions = _issue_actions(issue_result.actions, isin)
            ratings = _rating_evidence(issuer_actions, issue_actions)
            passport = _passport(
                record,
                ratings=ratings,
                issuer_lookup_complete=issuer_result.error is None,
                issue_lookup_complete=issue_result.error is None,
                as_of=fetched_at.date(),
                max_age_days=self._policy.rating_max_age_days,
            )
            passports.append(passport)

        covered_value = sum(
            (
                passport.market_value_rub
                for passport in passports
                if passport.current_ratings
            ),
            start=Decimal("0"),
        )
        return CreditPortfolioReport(
            account_ref=bond_report.account_ref,
            portfolio_as_of=bond_report.portfolio_as_of,
            moex_fetched_at=bond_report.fetched_at,
            fetched_at=fetched_at,
            rating_max_age_days=self._policy.rating_max_age_days,
            total_bond_positions=bond_report.total_bond_positions,
            bond_value_rub=bond_report.bond_value_rub,
            rating_covered_value_rub=covered_value,
            passports=tuple(passports),
            failures=tuple(_unique_failures(failures)),
        )


def normalize_rating_band(raw_rating: str | None) -> RatingBand:
    if raw_rating is None:
        return RatingBand.UNRATED
    normalized = re.sub(r"\s+", "", raw_rating.upper())
    match = re.match(
        r"^(?:RU)?(SD|RD|AAA|BBB|CCC|AA|BB|CC|A|B|C|D)(?:[+-])?",
        normalized,
    )
    if match is None:
        return RatingBand.UNRATED
    grade = match.group(1)
    if grade == "AAA":
        return RatingBand.HIGHEST
    if grade == "AA":
        return RatingBand.HIGH
    if grade == "A":
        return RatingBand.STRONG
    if grade == "BBB":
        return RatingBand.ADEQUATE
    if grade == "BB":
        return RatingBand.SPECULATIVE
    if grade == "B":
        return RatingBand.HIGH_RISK
    if grade in {"CCC", "CC", "C"}:
        return RatingBand.VERY_HIGH_RISK
    return RatingBand.DEFAULT


def render_credit_report_json(report: CreditPortfolioReport) -> str:
    return json.dumps(report.as_dict(), ensure_ascii=False, indent=2)


def render_credit_report_text(report: CreditPortfolioReport) -> str:
    lines = [
        f"Кредитные паспорта для {report.account_ref}",
        f"Портфель на: {report.portfolio_as_of.isoformat()}",
        f"Данные получены: {report.fetched_at.isoformat()}",
        "Источник рейтингов: официальный центральный репозиторий Банка России.",
        (
            f"Покрытие текущими рейтингами: {report.rating_coverage_share:.2%} "
            f"стоимости облигаций; паспортов {len(report.passports)}/"
            f"{report.total_bond_positions}."
        ),
        "Широкие классы рейтинга — диагностика, не PD и не единый скоринг агентств.",
        "",
    ]
    for passport in report.passports:
        lines.append(
            f"- {passport.ticker} · {passport.emitter_name} · "
            f"{passport.market_value_rub:,.2f} ₽"
        )
        if passport.ratings:
            for rating in passport.ratings:
                raw = rating.action.rating_value or "без значения"
                outlook = "" if rating.action.outlook is None else f"; {rating.action.outlook}"
                lines.append(
                    f"  {rating.scope}: {rating.action.agency} {raw} "
                    f"[{rating.band.value}], {rating.action.release_date.isoformat()}, "
                    f"{rating.status}{outlook}"
                )
        else:
            lines.append("  рейтинги: не найдены или источник недоступен")
        for signal in passport.signals:
            lines.append(f"  {signal.severity.value} {signal.code}: {signal.message}")

    if report.failures:
        lines.extend(["", "Пробелы покрытия:"])
        lines.extend(
            f"- {failure.ticker} ({failure.stage}): {failure.message}"
            for failure in report.failures
        )
    lines.extend(
        [
            "",
            (
                "Пока не покрыто: отчётность, долговые метрики, FCF, ковенанты, "
                "оферты, поддержка группы и модель PD/LGD."
            ),
            "Статус исполнения: только анализ, сделок и рекомендаций нет.",
        ]
    )
    return "\n".join(lines)


def _issuer_actions(
    actions: tuple[CbrRatingAction, ...],
    inn: str | None,
) -> tuple[CbrRatingAction, ...]:
    if inn is None:
        return ()
    return _latest_actions(
        action
        for action in actions
        if action.inn == inn and action.isin is None
    )


def _issue_actions(
    actions: tuple[CbrRatingAction, ...],
    isin: str,
) -> tuple[CbrRatingAction, ...]:
    return _latest_actions(
        action
        for action in actions
        if action.isin is not None and action.isin.upper() == isin
    )


def _latest_actions(actions: Any) -> tuple[CbrRatingAction, ...]:
    latest: dict[tuple[str, str], CbrRatingAction] = {}
    for action in actions:
        key = (action.object_id, action.agency)
        previous = latest.get(key)
        if previous is None or action.release_date > previous.release_date:
            latest[key] = action
    return tuple(
        sorted(
            latest.values(),
            key=lambda item: (-item.release_date.toordinal(), item.agency, item.object_id),
        )
    )


def _rating_evidence(
    issuer_actions: tuple[CbrRatingAction, ...],
    issue_actions: tuple[CbrRatingAction, ...],
) -> tuple[CreditRatingEvidence, ...]:
    evidence = [
        CreditRatingEvidence(
            scope=scope,
            action=action,
            band=normalize_rating_band(action.rating_value),
            status="WITHDRAWN" if _is_withdrawn(action) else "CURRENT",
        )
        for scope, actions in (("ISSUER", issuer_actions), ("ISSUE", issue_actions))
        for action in actions
    ]
    return tuple(
        sorted(
            evidence,
            key=lambda item: (
                -item.action.release_date.toordinal(),
                item.scope,
                item.action.agency,
            ),
        )
    )


def _passport(
    record: EnrichedBondPosition,
    *,
    ratings: tuple[CreditRatingEvidence, ...],
    issuer_lookup_complete: bool,
    issue_lookup_complete: bool,
    as_of: date,
    max_age_days: int,
) -> CreditPassport:
    emitter = record.emitter
    current = tuple(rating for rating in ratings if rating.status == "CURRENT")
    signals: list[CreditSignal] = []
    facts = record.moex.facts
    if facts.has_default is True:
        signals.append(
            CreditSignal(
                code="MOEX_DEFAULT_FLAG",
                severity=SignalSeverity.CRITICAL,
                message=(
                    "в карточке MOEX установлен флаг default; "
                    "нужно проверить дату и текущий статус в раскрытии эмитента"
                ),
            )
        )
    elif facts.has_technical_default is True:
        signals.append(
            CreditSignal(
                code="MOEX_TECHNICAL_DEFAULT_HISTORY",
                severity=SignalSeverity.WARNING,
                message=(
                    "в карточке MOEX отмечен технический дефолт эмитента; "
                    "флаг может относиться к другому выпуску или закрытому событию, "
                    "поэтому он запрещает докупку, но сам по себе не доказывает "
                    "текущий дефолт этого выпуска"
                ),
            )
        )
    if not issuer_lookup_complete or not issue_lookup_complete:
        signals.append(
            CreditSignal(
                code="RATING_DATA_UNAVAILABLE",
                severity=SignalSeverity.WARNING,
                message="поиск рейтинга эмитента или выпуска выполнен не полностью",
            )
        )
    elif not current:
        signals.append(
            CreditSignal(
                code="NO_CURRENT_RATING",
                severity=SignalSeverity.WARNING,
                message="текущий рейтинг эмитента или точного выпуска не найден",
            )
        )
    if current and all(
        as_of - rating.action.release_date > timedelta(days=max_age_days)
        for rating in current
    ):
        signals.append(
            CreditSignal(
                code="RATING_STALE",
                severity=SignalSeverity.WARNING,
                message=f"все текущие рейтинговые действия старше {max_age_days} дней",
            )
        )
    watched = [rating for rating in current if _is_watch_or_adverse_outlook(rating.action)]
    if watched:
        signals.append(
            CreditSignal(
                code="RATING_WATCH",
                severity=SignalSeverity.WARNING,
                message="прогноз негативный/развивающийся либо рейтинг помещён на пересмотр",
            )
        )
    downgraded = [rating for rating in current if _is_downgrade(rating.action)]
    if downgraded:
        signals.append(
            CreditSignal(
                code="RATING_DOWNGRADE",
                severity=SignalSeverity.WARNING,
                message="хотя бы одно последнее рейтинговое действие сообщает о понижении",
            )
        )
    risky = [
        rating
        for rating in current
        if rating.band
        in {
            RatingBand.SPECULATIVE,
            RatingBand.HIGH_RISK,
            RatingBand.VERY_HIGH_RISK,
            RatingBand.DEFAULT,
        }
    ]
    if risky:
        signals.append(
            CreditSignal(
                code="SPECULATIVE_RATING",
                severity=SignalSeverity.WARNING,
                message="хотя бы один текущий рейтинг соответствует широкому классу BB или ниже",
            )
        )
    if any(rating.band is RatingBand.UNRATED for rating in current):
        signals.append(
            CreditSignal(
                code="UNRECOGNIZED_RATING_SCALE",
                severity=SignalSeverity.INFO,
                message=(
                    "текущий рейтинг есть, но его шкала не поддержана "
                    "диагностическим маппингом"
                ),
            )
        )
    if any(rating.status == "WITHDRAWN" for rating in ratings):
        signals.append(
            CreditSignal(
                code="RATING_WITHDRAWN",
                severity=SignalSeverity.INFO,
                message="одно из агентств отозвало рейтинг эмитента или точного текущего выпуска",
            )
        )
    signals.sort(key=lambda item: (_severity_rank(item.severity), item.code))
    return CreditPassport(
        ticker=record.position.ticker,
        isin=facts.isin,
        emitter_id=facts.emitter_id,
        emitter_name=facts.name if emitter is None else emitter.short_title,
        inn=None if emitter is None else emitter.inn,
        market_value_rub=record.position.market_value_rub,
        ratings=ratings,
        signals=tuple(signals),
        issuer_lookup_complete=issuer_lookup_complete,
        issue_lookup_complete=issue_lookup_complete,
    )


def _is_withdrawn(action: CbrRatingAction) -> bool:
    text = f"{action.rating_action} {action.rating_value or ''}".casefold()
    return any(marker in text for marker in ("отозв", "аннулир", "withdraw", "прекращ"))


def _is_watch_or_adverse_outlook(action: CbrRatingAction) -> bool:
    outlook = (action.outlook or "").casefold()
    if any(
        marker in outlook
        for marker in (
            "негатив",
            "развива",
            "develop",
            "negative",
        )
    ):
        return True
    rating_action = action.rating_action.casefold()
    removed = any(marker in rating_action for marker in ("снят", "выведен", "removed"))
    active_watch = any(
        marker in rating_action
        for marker in (
            "помещен под наблюден",
            "помещён под наблюден",
            "помещен на пересмотр",
            "помещён на пересмотр",
            "under review",
            "placed on watch",
        )
    )
    return active_watch and not removed


def _is_downgrade(action: CbrRatingAction) -> bool:
    text = action.rating_action.casefold()
    return "пониж" in text or "downgrad" in text


def _passport_as_dict(passport: CreditPassport) -> dict[str, Any]:
    return {
        "ticker": passport.ticker,
        "isin": passport.isin,
        "emitter": {
            "id": passport.emitter_id,
            "name": passport.emitter_name,
            "inn": passport.inn,
        },
        "market_value_rub": _decimal_text(passport.market_value_rub),
        "lookup": {
            "issuer_complete": passport.issuer_lookup_complete,
            "issue_complete": passport.issue_lookup_complete,
        },
        "ratings": [_rating_as_dict(rating) for rating in passport.ratings],
        "signals": [
            {
                "code": signal.code,
                "severity": signal.severity.value,
                "message": signal.message,
            }
            for signal in passport.signals
        ],
    }


def _rating_as_dict(rating: CreditRatingEvidence) -> dict[str, Any]:
    action = rating.action
    return {
        "scope": rating.scope,
        "status": rating.status,
        "agency": action.agency,
        "rating_value": action.rating_value,
        "broad_band": rating.band.value,
        "outlook": action.outlook,
        "release_date": action.release_date.isoformat(),
        "rating_action": action.rating_action,
        "object_id": action.object_id,
        "object_name": action.object_name,
        "object_type": action.object_type,
        "inn": action.inn,
        "isin": action.isin,
        "release_url": action.release_url,
    }


def _severity_rank(severity: SignalSeverity) -> int:
    return {
        SignalSeverity.CRITICAL: 0,
        SignalSeverity.WARNING: 1,
        SignalSeverity.INFO: 2,
    }[severity]


def _unique_failures(
    failures: list[CreditAnalysisFailure],
) -> tuple[CreditAnalysisFailure, ...]:
    seen: set[tuple[str, str, str]] = set()
    result: list[CreditAnalysisFailure] = []
    for failure in failures:
        key = (failure.ticker, failure.stage, failure.message)
        if key not in seen:
            seen.add(key)
            result.append(failure)
    return tuple(result)


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")
