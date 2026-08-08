"""Portfolio index of issuer-filed consolidated and exact-issue documents."""

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
from invest_agent.disclosure.interfax import (
    DISCLOSURE_BASE_URL,
    DisclosureApiError,
    DisclosureContractError,
    DisclosureDocument,
    DisclosureIssuerDocuments,
)


class DisclosureSeverity(StrEnum):
    WARNING = "WARNING"
    INFO = "INFO"


@dataclass(frozen=True, slots=True)
class DisclosurePolicy:
    annual_consolidated_max_age_days: int

    @classmethod
    def from_toml(cls, path: str | Path) -> DisclosurePolicy:
        with Path(path).open("rb") as source:
            raw = tomllib.load(source)["disclosures"]
        value = raw["annual_consolidated_max_age_days"]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("disclosures.annual_consolidated_max_age_days must be positive")
        return cls(annual_consolidated_max_age_days=value)


@dataclass(frozen=True, slots=True)
class DisclosureSignal:
    code: str
    severity: DisclosureSeverity
    message: str


@dataclass(frozen=True, slots=True)
class DisclosurePassport:
    ticker: str
    isin: str
    emitter_name: str
    inn: str | None
    registration_number: str | None
    market_value_rub: Decimal
    issuer_documents: DisclosureIssuerDocuments | None
    latest_annual_consolidated: DisclosureDocument | None
    latest_interim_consolidated: DisclosureDocument | None
    exact_issue_documents: tuple[DisclosureDocument, ...]
    lookup_complete: bool
    signals: tuple[DisclosureSignal, ...]


@dataclass(frozen=True, slots=True)
class DisclosureFailure:
    ticker: str
    message: str


@dataclass(frozen=True, slots=True)
class DisclosurePortfolioReport:
    account_ref: str
    portfolio_as_of: datetime
    moex_fetched_at: datetime
    fetched_at: datetime
    total_bond_positions: int
    bond_value_rub: Decimal
    company_covered_value_rub: Decimal
    annual_consolidated_covered_value_rub: Decimal
    exact_issue_document_covered_value_rub: Decimal
    passports: tuple[DisclosurePassport, ...]
    failures: tuple[DisclosureFailure, ...]

    @property
    def company_coverage_share(self) -> Decimal:
        return _share(self.company_covered_value_rub, self.bond_value_rub)

    @property
    def annual_consolidated_coverage_share(self) -> Decimal:
        return _share(self.annual_consolidated_covered_value_rub, self.bond_value_rub)

    @property
    def exact_issue_document_coverage_share(self) -> Decimal:
        return _share(self.exact_issue_document_covered_value_rub, self.bond_value_rub)

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_ref": self.account_ref,
            "portfolio_as_of": self.portfolio_as_of.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
            "source": {
                "provider": "Interfax Corporate Information Disclosure Center",
                "url": DISCLOSURE_BASE_URL,
                "access": "public issuer-filed read-only disclosure; web contract may change",
                "moex_fetched_at": self.moex_fetched_at.isoformat(),
            },
            "coverage": {
                "bond_positions": self.total_bond_positions,
                "passports": len(self.passports),
                "bond_value_rub": _decimal_text(self.bond_value_rub),
                "company_covered_value_rub": _decimal_text(self.company_covered_value_rub),
                "company_share": _decimal_text(self.company_coverage_share),
                "annual_consolidated_covered_value_rub": _decimal_text(
                    self.annual_consolidated_covered_value_rub
                ),
                "annual_consolidated_share": _decimal_text(self.annual_consolidated_coverage_share),
                "exact_issue_document_covered_value_rub": _decimal_text(
                    self.exact_issue_document_covered_value_rub
                ),
                "exact_issue_document_share": _decimal_text(
                    self.exact_issue_document_coverage_share
                ),
            },
            "methodology": {
                "facts": "issuer-filed document metadata and direct source links",
                "matching": "exact issuer INN and exact MOEX issue registration number",
                "scope_warning": (
                    "document contents are not parsed; a located file does not establish "
                    "covenants, security, guarantees or group support"
                ),
                "judgment": "this command produces no buy, sell or hold recommendation",
            },
            "passports": [_passport_as_dict(passport) for passport in self.passports],
            "failures": [
                {"ticker": failure.ticker, "message": failure.message} for failure in self.failures
            ],
            "remaining_data_gaps": [
                "структурированные показатели и примечания консолидированной МСФО",
                "извлечённые ковенанты, обеспечение, гарантии и субординация",
                "доказанная связь эмитента выпуска с периметром консолидации группы",
                "поддержка группы и календарь обязательств вне текущего выпуска",
            ],
            "execution_state": "ANALYSIS_ONLY",
        }


class DisclosureClient(Protocol):
    def fetch_issuer_documents(self, inn: str) -> DisclosureIssuerDocuments | None: ...


@dataclass(frozen=True, slots=True)
class _DisclosureLookup:
    documents: DisclosureIssuerDocuments | None
    error: str | None = None


class DisclosurePortfolioAnalyzer:
    def __init__(
        self,
        client: DisclosureClient,
        policy: DisclosurePolicy,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._policy = policy
        self._now = now or (lambda: datetime.now(tz=UTC))

    def analyze(self, bond_report: BondMarketReport) -> DisclosurePortfolioReport:
        fetched_at = self._now()
        cache: dict[str, _DisclosureLookup] = {}
        passports: list[DisclosurePassport] = []
        failures = [
            DisclosureFailure(
                ticker=failure.ticker,
                message=f"MOEX {failure.stage}: {failure.message}",
            )
            for failure in bond_report.failures
        ]
        for record in bond_report.positions:
            emitter = record.emitter
            inn = None if emitter is None else emitter.inn
            if inn is None:
                lookup = _DisclosureLookup(None, "MOEX issuer INN is unavailable")
            elif inn in cache:
                lookup = cache[inn]
            else:
                try:
                    lookup = _DisclosureLookup(self._client.fetch_issuer_documents(inn))
                except (DisclosureApiError, DisclosureContractError, ValueError) as error:
                    lookup = _DisclosureLookup(None, str(error))
                cache[inn] = lookup
            if lookup.error is not None:
                failures.append(
                    DisclosureFailure(ticker=record.position.ticker, message=lookup.error)
                )
            passports.append(
                _passport(
                    record,
                    lookup=lookup,
                    as_of=fetched_at.date(),
                    policy=self._policy,
                )
            )

        company_value = _covered_value(
            passports,
            lambda passport: passport.issuer_documents is not None,
        )
        annual_value = _covered_value(
            passports,
            lambda passport: passport.latest_annual_consolidated is not None,
        )
        issue_value = _covered_value(
            passports,
            lambda passport: bool(passport.exact_issue_documents),
        )
        return DisclosurePortfolioReport(
            account_ref=bond_report.account_ref,
            portfolio_as_of=bond_report.portfolio_as_of,
            moex_fetched_at=bond_report.fetched_at,
            fetched_at=fetched_at,
            total_bond_positions=bond_report.total_bond_positions,
            bond_value_rub=bond_report.bond_value_rub,
            company_covered_value_rub=company_value,
            annual_consolidated_covered_value_rub=annual_value,
            exact_issue_document_covered_value_rub=issue_value,
            passports=tuple(passports),
            failures=tuple(_unique_failures(failures)),
        )


def render_disclosure_report_json(report: DisclosurePortfolioReport) -> str:
    return json.dumps(report.as_dict(), ensure_ascii=False, indent=2)


def render_disclosure_report_text(report: DisclosurePortfolioReport) -> str:
    lines = [
        f"Индекс раскрытий для {report.account_ref}",
        f"Портфель на: {report.portfolio_as_of.isoformat()}",
        f"Данные получены: {report.fetched_at.isoformat()}",
        (
            f"Карточки эмитентов: {report.company_coverage_share:.2%}; "
            f"годовая консолидированная отчётность: "
            f"{report.annual_consolidated_coverage_share:.2%}; "
            f"документы точного выпуска: {report.exact_issue_document_coverage_share:.2%}."
        ),
        "Документы только найдены и сопоставлены; их содержимое пока не разобрано.",
        "",
    ]
    for passport in report.passports:
        lines.append(
            f"- {passport.ticker} · {passport.emitter_name} · {passport.market_value_rub:,.2f} ₽"
        )
        annual = passport.latest_annual_consolidated
        if annual is not None:
            lines.append(
                f"  годовая консолидация: {annual.period}, раскрыта "
                f"{annual.published_at.isoformat()} — {annual.file_url}"
            )
        else:
            lines.append("  годовая консолидация: не найдена")
        lines.append(f"  документы точного выпуска: {len(passport.exact_issue_documents)}")
        for signal in passport.signals:
            lines.append(f"  {signal.severity.value} {signal.code}: {signal.message}")
    if report.failures:
        lines.extend(["", "Ошибки покрытия:"])
        lines.extend(f"- {failure.ticker}: {failure.message}" for failure in report.failures)
    lines.extend(
        [
            "",
            "Пока не извлечены: показатели МСФО, ковенанты, обеспечение и поддержка группы.",
            "Статус исполнения: только анализ, сделок и рекомендаций нет.",
        ]
    )
    return "\n".join(lines)


def _passport(
    record: EnrichedBondPosition,
    *,
    lookup: _DisclosureLookup,
    as_of: date,
    policy: DisclosurePolicy,
) -> DisclosurePassport:
    issuer_documents = lookup.documents
    consolidated = () if issuer_documents is None else issuer_documents.consolidated
    emission = () if issuer_documents is None else issuer_documents.emission
    annual = _latest_document(consolidated, _is_annual_consolidated)
    interim = _latest_document(consolidated, _is_interim_consolidated)
    registration_number = record.moex.facts.registration_number
    exact_issue = tuple(
        document
        for document in emission
        if registration_number is not None
        and document.registration_number is not None
        and _registration_key(document.registration_number)
        == _registration_key(registration_number)
    )
    signals: list[DisclosureSignal] = []
    if lookup.error is not None:
        signals.append(
            DisclosureSignal(
                "DISCLOSURE_DATA_UNAVAILABLE",
                DisclosureSeverity.WARNING,
                "поиск раскрытий выполнен не полностью",
            )
        )
    elif issuer_documents is None:
        signals.append(
            DisclosureSignal(
                "NO_DISCLOSURE_COMPANY",
                DisclosureSeverity.WARNING,
                "карточка эмитента по точному ИНН не найдена",
            )
        )
    if issuer_documents is not None and annual is None:
        signals.append(
            DisclosureSignal(
                "NO_ANNUAL_CONSOLIDATED_REPORT",
                DisclosureSeverity.WARNING,
                "годовая консолидированная отчётность в карточке не найдена",
            )
        )
    elif annual is not None and as_of - annual.published_at > timedelta(
        days=policy.annual_consolidated_max_age_days
    ):
        signals.append(
            DisclosureSignal(
                "ANNUAL_CONSOLIDATED_REPORT_STALE",
                DisclosureSeverity.WARNING,
                (
                    "последняя годовая консолидированная отчётность раскрыта более "
                    f"{policy.annual_consolidated_max_age_days} дней назад"
                ),
            )
        )
    if registration_number is None:
        signals.append(
            DisclosureSignal(
                "ISSUE_REGISTRATION_NUMBER_UNAVAILABLE",
                DisclosureSeverity.WARNING,
                "MOEX не вернула регистрационный номер для точного сопоставления",
            )
        )
    elif issuer_documents is not None and not exact_issue:
        signals.append(
            DisclosureSignal(
                "NO_EXACT_ISSUE_DOCUMENT",
                DisclosureSeverity.WARNING,
                "эмиссионный документ с точным регистрационным номером не найден",
            )
        )
    if annual is not None:
        signals.append(
            DisclosureSignal(
                "CONSOLIDATED_CONTENT_NOT_PARSED",
                DisclosureSeverity.INFO,
                "файл отчётности найден, но показатели и примечания ещё не извлечены",
            )
        )
    if exact_issue:
        signals.append(
            DisclosureSignal(
                "ISSUE_CONTENT_NOT_PARSED",
                DisclosureSeverity.INFO,
                "файлы выпуска найдены, но ковенанты и обеспечение ещё не извлечены",
            )
        )
    signals.sort(key=lambda signal: (_severity_rank(signal.severity), signal.code))
    emitter = record.emitter
    return DisclosurePassport(
        ticker=record.position.ticker,
        isin=record.moex.facts.isin,
        emitter_name=record.moex.facts.name if emitter is None else emitter.short_title,
        inn=None if emitter is None else emitter.inn,
        registration_number=registration_number,
        market_value_rub=record.position.market_value_rub,
        issuer_documents=issuer_documents,
        latest_annual_consolidated=annual,
        latest_interim_consolidated=interim,
        exact_issue_documents=exact_issue,
        lookup_complete=lookup.error is None,
        signals=tuple(signals),
    )


def _passport_as_dict(passport: DisclosurePassport) -> dict[str, Any]:
    documents = passport.issuer_documents
    return {
        "ticker": passport.ticker,
        "isin": passport.isin,
        "emitter": {"name": passport.emitter_name, "inn": passport.inn},
        "registration_number": passport.registration_number,
        "market_value_rub": _decimal_text(passport.market_value_rub),
        "lookup_complete": passport.lookup_complete,
        "company": None if documents is None else _company_as_dict(documents),
        "latest_annual_consolidated": _optional_document_as_dict(
            passport.latest_annual_consolidated
        ),
        "latest_interim_consolidated": _optional_document_as_dict(
            passport.latest_interim_consolidated
        ),
        "exact_issue_documents": [
            _document_as_dict(document) for document in passport.exact_issue_documents
        ],
        "signals": [
            {
                "code": signal.code,
                "severity": signal.severity.value,
                "message": signal.message,
            }
            for signal in passport.signals
        ],
    }


def _company_as_dict(documents: DisclosureIssuerDocuments) -> dict[str, Any]:
    company = documents.company
    return {
        "id": company.company_id,
        "name": company.name,
        "inn": company.inn,
        "district": company.district,
        "region": company.region,
        "branch": company.branch,
        "last_activity": (
            None if company.last_activity is None else company.last_activity.isoformat()
        ),
        "document_count": company.document_count,
        "source_url": company.source_url,
        "fetched_at": documents.fetched_at.isoformat(),
    }


def _optional_document_as_dict(document: DisclosureDocument | None) -> dict[str, Any] | None:
    return None if document is None else _document_as_dict(document)


def _document_as_dict(document: DisclosureDocument) -> dict[str, Any]:
    return {
        "category": document.category,
        "file_id": document.file_id,
        "document_type": document.document_type,
        "period": document.period,
        "registration_number": document.registration_number,
        "registration_date": _date_text(document.registration_date),
        "publication_basis_date": document.publication_basis_date.isoformat(),
        "published_at": document.published_at.isoformat(),
        "file_url": document.file_url,
        "index_url": document.index_url,
    }


def _latest_document(
    documents: tuple[DisclosureDocument, ...],
    predicate: Callable[[DisclosureDocument], bool],
) -> DisclosureDocument | None:
    return next((document for document in documents if predicate(document)), None)


def _is_annual_consolidated(document: DisclosureDocument) -> bool:
    normalized = document.document_type.casefold()
    return "годовая" in normalized and "консолидирован" in normalized


def _is_interim_consolidated(document: DisclosureDocument) -> bool:
    normalized = document.document_type.casefold()
    return "промежуточ" in normalized and "консолидирован" in normalized


def _registration_key(value: str) -> str:
    normalized_dashes = value.translate(str.maketrans("‐‑‒–—−", "------"))
    return "".join(normalized_dashes.upper().split())


def _covered_value(
    passports: list[DisclosurePassport],
    predicate: Callable[[DisclosurePassport], bool],
) -> Decimal:
    return sum(
        (passport.market_value_rub for passport in passports if predicate(passport)),
        Decimal("0"),
    )


def _unique_failures(failures: list[DisclosureFailure]) -> list[DisclosureFailure]:
    seen: set[tuple[str, str]] = set()
    result: list[DisclosureFailure] = []
    for failure in failures:
        key = (failure.ticker, failure.message)
        if key not in seen:
            seen.add(key)
            result.append(failure)
    return result


def _severity_rank(severity: DisclosureSeverity) -> int:
    return {
        DisclosureSeverity.WARNING: 0,
        DisclosureSeverity.INFO: 1,
    }[severity]


def _share(value: Decimal, denominator: Decimal) -> Decimal:
    return Decimal("0") if denominator == 0 else value / denominator


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _date_text(value: date | None) -> str | None:
    return None if value is None else value.isoformat()
