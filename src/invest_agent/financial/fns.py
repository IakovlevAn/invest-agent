"""Read-only client for public annual RAS statements in the FNS GIR BO portal.

GIR BO is the official Russian state resource for accounting statements. The
public website endpoints are not a documented stable API, so this client keeps
the contract narrow, validates exact INN matches and fails closed on changes.
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from invest_agent.http import HttpResponse, HttpTransport, UrllibTransport

GIRBO_BASE_URL = "https://bo.nalog.gov.ru"
ANNUAL_PERIOD_TYPE = 12
CLIENT_USER_AGENT = "InvestAgent/0.1 (local read-only portfolio analysis)"


class GirboApiError(RuntimeError):
    """Sanitized HTTP or network failure from the FNS public portal."""

    def __init__(self, operation: str, status: int | None) -> None:
        self.operation = operation
        self.status = status
        status_text = "network" if status is None else f"HTTP {status}"
        super().__init__(f"FNS GIR BO {operation} failed ({status_text})")


class GirboContractError(ValueError):
    """The FNS portal returned data outside the verified public contract."""


@dataclass(frozen=True, slots=True)
class RasFinancialStatement:
    organization_id: int
    organization_name: str
    inn: str
    period: int
    previous_period: int
    reported_at: date
    has_audit_report: bool
    correction_version: int | None
    revenue: Decimal | None
    previous_revenue: Decimal | None
    operating_profit: Decimal | None
    previous_operating_profit: Decimal | None
    interest_income: Decimal | None
    interest_expense: Decimal | None
    profit_before_tax: Decimal | None
    net_income: Decimal | None
    previous_net_income: Decimal | None
    cash: Decimal | None
    long_term_debt: Decimal | None
    short_term_debt: Decimal | None
    equity: Decimal | None
    total_assets: Decimal | None
    current_assets: Decimal | None
    current_liabilities: Decimal | None
    operating_cash_flow: Decimal | None
    investing_cash_flow: Decimal | None
    financing_cash_flow: Decimal | None
    source_url: str
    fetched_at: datetime
    unit: str = "THOUSAND_RUB"
    scope: str = "LEGAL_ENTITY_RAS"


class GirboClient:
    """Fetch the latest published annual RAS statement for an exact INN."""

    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._transport = transport or UrllibTransport()
        self._now = now or (lambda: datetime.now(tz=UTC))
        self._sleep = sleep
        self._max_attempts = max_attempts

    def fetch_latest_annual(self, inn: str) -> RasFinancialStatement | None:
        normalized_inn = inn.strip()
        if not re.fullmatch(r"\d{10}|\d{12}", normalized_inn):
            raise ValueError("INN must contain 10 or 12 digits")
        search_url = self._search_url(normalized_inn)
        search = self._get_json(search_url, operation=f"search:{normalized_inn}")
        organization = _exact_organization(search, normalized_inn)
        if organization is None:
            return None
        organization_id = _positive_integer(organization.get("id"), "organization.id")
        reports_url = f"{GIRBO_BASE_URL}/nbo/organizations/{organization_id}/bfo/"
        reports = self._get_json(reports_url, operation=f"reports:{normalized_inn}")
        report = _latest_annual_report(reports)
        if report is None:
            return None
        return _statement(
            report,
            organization_id=organization_id,
            expected_inn=normalized_inn,
            source_url=(
                f"{GIRBO_BASE_URL}/organizations-card/{organization_id}"
                f"?period={report['period']}&periodType={ANNUAL_PERIOD_TYPE}"
            ),
            fetched_at=self._now(),
        )

    def _get_json(self, url: str, *, operation: str) -> Any:
        response: HttpResponse | None = None
        try:
            for attempt in range(self._max_attempts):
                response = self._transport.request(
                    method="GET",
                    url=url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": CLIENT_USER_AGENT,
                    },
                )
                if response.status != 429 and not 500 <= response.status < 600:
                    break
                if attempt + 1 < self._max_attempts:
                    self._sleep(0.25 * (2**attempt))
        except OSError as error:
            raise GirboApiError(operation, None) from error
        assert response is not None
        if not 200 <= response.status < 300:
            raise GirboApiError(operation, response.status)
        try:
            return json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise GirboContractError(f"FNS GIR BO {operation} returned invalid JSON") from error

    @staticmethod
    def _search_url(inn: str) -> str:
        query = urllib.parse.urlencode({"query": inn, "page": 0})
        return f"{GIRBO_BASE_URL}/advanced-search/organizations/search?{query}"


def _exact_organization(payload: Any, inn: str) -> Mapping[str, Any] | None:
    if not isinstance(payload, dict):
        raise GirboContractError("FNS GIR BO search must return an object")
    content = payload.get("content")
    total = payload.get("totalElements")
    if not isinstance(content, list) or isinstance(total, bool) or not isinstance(total, int):
        raise GirboContractError("FNS GIR BO search pagination is invalid")
    matches: list[Mapping[str, Any]] = []
    for index, item in enumerate(content):
        if not isinstance(item, dict):
            raise GirboContractError(f"FNS GIR BO search content[{index}] is invalid")
        returned_inn = item.get("inn")
        if isinstance(returned_inn, str) and "".join(re.findall(r"\d", returned_inn)) == inn:
            matches.append(item)
    if not matches:
        if total > len(content):
            raise GirboContractError("FNS GIR BO exact INN may be outside the first page")
        return None
    if len(matches) != 1:
        raise GirboContractError("FNS GIR BO exact INN must match one organization")
    return matches[0]


def _latest_annual_report(payload: Any) -> Mapping[str, Any] | None:
    if not isinstance(payload, list):
        raise GirboContractError("FNS GIR BO reports must return a list")
    candidates: list[tuple[int, Mapping[str, Any]]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise GirboContractError(f"FNS GIR BO reports[{index}] is invalid")
        if item.get("published") is not True:
            continue
        period = _period(item.get("period"))
        corrections = item.get("typeCorrections")
        if not isinstance(corrections, list):
            raise GirboContractError("FNS GIR BO typeCorrections must be a list")
        annual = [
            correction
            for correction in corrections
            if isinstance(correction, dict) and correction.get("type") == ANNUAL_PERIOD_TYPE
        ]
        if annual:
            if len(annual) != 1:
                raise GirboContractError("FNS GIR BO annual correction must be unique")
            candidates.append((period, item))
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]


def _statement(
    report: Mapping[str, Any],
    *,
    organization_id: int,
    expected_inn: str,
    source_url: str,
    fetched_at: datetime,
) -> RasFinancialStatement:
    period = _period(report.get("period"))
    correction = _annual_correction(report)
    organization = correction.get("bfoOrganizationInfo")
    if not isinstance(organization, dict):
        raise GirboContractError("FNS GIR BO bfoOrganizationInfo is missing")
    returned_inn = _required_text(organization.get("inn"), "organization.inn")
    if returned_inn != expected_inn:
        raise GirboContractError("FNS GIR BO report INN mismatch")
    balance = _required_mapping(correction.get("balance"), "balance")
    result = _required_mapping(correction.get("financialResult"), "financialResult")
    cash_flow = _optional_mapping(correction.get("fundsMovement"), "fundsMovement")
    return RasFinancialStatement(
        organization_id=organization_id,
        organization_name=_required_text(organization.get("fullName"), "organization.fullName"),
        inn=expected_inn,
        period=period,
        previous_period=period - 1,
        reported_at=_iso_date(report.get("actualBfoDate"), "actualBfoDate"),
        has_audit_report=(
            report.get("hasAz") is True or isinstance(correction.get("auditReport"), dict)
        ),
        correction_version=_optional_nonnegative_integer(correction.get("correctionVersion")),
        revenue=_line(result, "current", 2110),
        previous_revenue=_line(result, "previous", 2110),
        operating_profit=_line(result, "current", 2200),
        previous_operating_profit=_line(result, "previous", 2200),
        interest_income=_line(result, "current", 2320),
        interest_expense=_line(result, "current", 2330),
        profit_before_tax=_line(result, "current", 2300),
        net_income=_line(result, "current", 2400),
        previous_net_income=_line(result, "previous", 2400),
        cash=_line(balance, "current", 1250),
        long_term_debt=_line(balance, "current", 1410),
        short_term_debt=_line(balance, "current", 1510),
        equity=_line(balance, "current", 1300),
        total_assets=_line(balance, "current", 1600),
        current_assets=_line(balance, "current", 1200),
        current_liabilities=_line(balance, "current", 1500),
        operating_cash_flow=(None if cash_flow is None else _line(cash_flow, "current", 4100)),
        investing_cash_flow=(None if cash_flow is None else _line(cash_flow, "current", 4200)),
        financing_cash_flow=(None if cash_flow is None else _line(cash_flow, "current", 4300)),
        source_url=source_url,
        fetched_at=fetched_at,
    )


def _annual_correction(report: Mapping[str, Any]) -> Mapping[str, Any]:
    corrections = report.get("typeCorrections")
    assert isinstance(corrections, list)
    annual = [
        item.get("correction")
        for item in corrections
        if isinstance(item, dict) and item.get("type") == ANNUAL_PERIOD_TYPE
    ]
    if len(annual) != 1 or not isinstance(annual[0], dict):
        raise GirboContractError("FNS GIR BO annual correction is invalid")
    return annual[0]


def _required_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise GirboContractError(f"FNS GIR BO {field} must be an object")
    return value


def _optional_mapping(value: Any, field: str) -> Mapping[str, Any] | None:
    if value is None:
        return None
    return _required_mapping(value, field)


def _line(form: Mapping[str, Any], prefix: str, code: int) -> Decimal | None:
    value = form.get(f"{prefix}{code}")
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise GirboContractError(f"FNS GIR BO line {code} must be numeric")
    if isinstance(value, float) and not math.isfinite(value):
        raise GirboContractError(f"FNS GIR BO line {code} must be finite")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise GirboContractError(f"FNS GIR BO line {code} must be numeric") from error
    if not result.is_finite():
        raise GirboContractError(f"FNS GIR BO line {code} must be finite")
    return result


def _period(value: Any) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"20\d{2}", value):
        raise GirboContractError("FNS GIR BO period must be YYYY")
    return int(value)


def _iso_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise GirboContractError(f"FNS GIR BO {field} must be text")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise GirboContractError(f"FNS GIR BO {field} is invalid") from error


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GirboContractError(f"FNS GIR BO {field} must be non-empty text")
    return value.strip()


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GirboContractError(f"FNS GIR BO {field} must be a positive integer")
    return value


def _optional_nonnegative_integer(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GirboContractError("FNS GIR BO correctionVersion must be non-negative")
    return value
