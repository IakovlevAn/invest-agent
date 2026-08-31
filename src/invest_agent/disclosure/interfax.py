"""Strict read-only index of issuer-filed documents on e-disclosure.ru.

The accredited disclosure portal exposes a public company search and HTML
document tables rather than a documented stable API. This client only indexes
metadata and direct issuer-filed source links. It does not infer terms from a
document title and does not parse the document contents.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from typing import Any

from invest_agent.http import HttpResponse, HttpTransport, UrllibTransport

DISCLOSURE_BASE_URL = "https://www.e-disclosure.ru"
DISCLOSURE_SEARCH_URL = f"{DISCLOSURE_BASE_URL}/api/search/companies"
CLIENT_USER_AGENT = "InvestAgent/0.1 (local read-only portfolio analysis)"
CONSOLIDATED_DOCUMENT_TYPE = 4
EMISSION_DOCUMENT_TYPE = 7


class DisclosureApiError(RuntimeError):
    """Sanitized HTTP or network failure from the disclosure portal."""

    def __init__(self, operation: str, status: int | None) -> None:
        self.operation = operation
        self.status = status
        status_text = "network" if status is None else f"HTTP {status}"
        super().__init__(f"e-disclosure {operation} failed ({status_text})")


class DisclosureContractError(ValueError):
    """The portal returned data outside the verified public contract."""


@dataclass(frozen=True, slots=True)
class DisclosureCompany:
    company_id: int
    name: str
    inn: str
    district: str | None
    region: str | None
    branch: str | None
    last_activity: datetime | None
    document_count: int
    source_url: str


@dataclass(frozen=True, slots=True)
class DisclosureDocument:
    category: str
    file_id: int
    document_type: str
    period: str | None
    registration_number: str | None
    registration_date: date | None
    publication_basis_date: date
    published_at: date
    file_url: str
    index_url: str


@dataclass(frozen=True, slots=True)
class DisclosureIssuerDocuments:
    company: DisclosureCompany
    consolidated: tuple[DisclosureDocument, ...]
    emission: tuple[DisclosureDocument, ...]
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class _HtmlCell:
    classes: frozenset[str]
    text: str
    file_url: str | None
    file_id: str | None


class EDisclosureClient:
    """Find exact issuer pages and index consolidated and emission documents."""

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

    def fetch_issuer_documents(self, inn: str) -> DisclosureIssuerDocuments | None:
        normalized_inn = inn.strip()
        if not re.fullmatch(r"\d{10}|\d{12}", normalized_inn):
            raise ValueError("INN must contain 10 or 12 digits")
        search = self._post_search(normalized_inn)
        company = _search_company(search, normalized_inn)
        if company is None:
            return None
        card_body = self._get_html(company.source_url, operation=f"company:{normalized_inn}")
        returned_inn = _company_card_inn(card_body)
        if returned_inn != normalized_inn:
            raise DisclosureContractError("e-disclosure company INN mismatch")
        company = DisclosureCompany(
            company_id=company.company_id,
            name=company.name,
            inn=returned_inn,
            district=company.district,
            region=company.region,
            branch=company.branch,
            last_activity=company.last_activity,
            document_count=company.document_count,
            source_url=company.source_url,
        )
        consolidated_url = _files_url(company.company_id, CONSOLIDATED_DOCUMENT_TYPE)
        emission_url = _files_url(company.company_id, EMISSION_DOCUMENT_TYPE)
        consolidated = _documents(
            self._get_html(
                consolidated_url,
                operation=f"consolidated:{normalized_inn}",
            ),
            category="CONSOLIDATED",
            index_url=consolidated_url,
        )
        emission = _documents(
            self._get_html(emission_url, operation=f"emission:{normalized_inn}"),
            category="EMISSION",
            index_url=emission_url,
        )
        return DisclosureIssuerDocuments(
            company=company,
            consolidated=consolidated,
            emission=emission,
            fetched_at=self._now(),
        )

    def _post_search(self, inn: str) -> Any:
        filter_payload = json.dumps(
            {
                "area": {"type": "districts", "values": ["-1"]},
                "branch": {"values": ["-1"]},
                "text": inn,
                "pageNumber": "1",
                "rowsCount": "10",
            },
            separators=(",", ":"),
        )
        body = urllib.parse.urlencode(
            {
                "went2Card": "",
                "textfield": inn,
                "radReg": "FederalDistricts",
                "districtsCheckboxGroup": "-1",
                "regionsCheckboxGroup": "-1",
                "branchesCheckboxGroup": "-1",
                "lastPageSize": "10",
                "lastPageNumber": "1",
                "query": inn,
                "filter": filter_payload,
            }
        ).encode("ascii")
        response = self._request(
            method="POST",
            url=DISCLOSURE_SEARCH_URL,
            operation=f"search:{inn}",
            body=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        try:
            return json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise DisclosureContractError("e-disclosure search returned invalid JSON") from error

    def _get_html(self, url: str, *, operation: str) -> str:
        response = self._request(
            method="GET",
            url=url,
            operation=operation,
            body=None,
            headers={"Accept": "text/html"},
        )
        try:
            return response.body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DisclosureContractError(f"e-disclosure {operation} is not UTF-8") from error

    def _request(
        self,
        *,
        method: str,
        url: str,
        operation: str,
        body: bytes | None,
        headers: Mapping[str, str],
    ) -> HttpResponse:
        response: HttpResponse | None = None
        try:
            for attempt in range(self._max_attempts):
                response = self._transport.request(
                    method=method,
                    url=url,
                    headers={**headers, "User-Agent": CLIENT_USER_AGENT},
                    body=body,
                )
                if response.status != 429 and not 500 <= response.status < 600:
                    break
                if attempt + 1 < self._max_attempts:
                    self._sleep(0.25 * (2**attempt))
        except OSError as error:
            raise DisclosureApiError(operation, None) from error
        assert response is not None
        if not 200 <= response.status < 300:
            raise DisclosureApiError(operation, response.status)
        return response


def _search_company(payload: Any, expected_inn: str) -> DisclosureCompany | None:
    if not isinstance(payload, dict) or payload.get("errors") is not None:
        raise DisclosureContractError("e-disclosure search response is invalid")
    companies = payload.get("foundCompaniesList")
    paging = payload.get("pagingInfo")
    total = payload.get("allFoundCompanies")
    if not isinstance(companies, list) or not isinstance(paging, dict):
        raise DisclosureContractError("e-disclosure search results are invalid")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise DisclosureContractError("e-disclosure search total is invalid")
    paging_total = _nonnegative_integer(paging.get("totalItems"), "pagingInfo.totalItems")
    if total != paging_total or total != len(companies):
        raise DisclosureContractError("e-disclosure exact search pagination mismatch")
    if total == 0:
        return None
    if total != 1:
        raise DisclosureContractError("e-disclosure exact INN must match one company")
    item = companies[0]
    if not isinstance(item, dict):
        raise DisclosureContractError("e-disclosure company result is invalid")
    company_id = _positive_integer(item.get("id"), "company.id")
    return DisclosureCompany(
        company_id=company_id,
        name=_required_text(item.get("name"), "company.name"),
        inn=expected_inn,
        district=_optional_text(item.get("district")),
        region=_optional_text(item.get("region")),
        branch=_optional_text(item.get("branch")),
        last_activity=_optional_datetime(item.get("lastActivity"), "company.lastActivity"),
        document_count=_nonnegative_integer(item.get("docCount"), "company.docCount"),
        source_url=f"{DISCLOSURE_BASE_URL}/portal/company.aspx?id={company_id}",
    )


def _company_card_inn(body: str) -> str:
    parser = _TableParser()
    parser.feed(body)
    parser.close()
    values: list[str] = []
    for row in parser.rows:
        for index, cell in enumerate(row[:-1]):
            if cell.text == "ИНН":
                values.append(_digits(row[index + 1].text))
    if len(values) != 1 or not re.fullmatch(r"\d{10}|\d{12}", values[0]):
        raise DisclosureContractError("e-disclosure company card INN is missing or ambiguous")
    return values[0]


def _documents(body: str, *, category: str, index_url: str) -> tuple[DisclosureDocument, ...]:
    parser = _TableParser()
    parser.feed(body)
    parser.close()
    documents: list[DisclosureDocument] = []
    for row in parser.rows:
        file_cells = [cell for cell in row if cell.file_url is not None]
        if not file_cells:
            continue
        if len(file_cells) != 1:
            raise DisclosureContractError("e-disclosure file row must contain one link")
        file_cell = file_cells[0]
        file_id = _positive_integer_text(file_cell.file_id, "document.file_id")
        file_url = _validated_file_url(file_cell.file_url, file_id)
        type_cell = _one_class_cell(row, "type-cell")
        date_cells = [cell for cell in row if "date-cell" in cell.classes]
        if category == "CONSOLIDATED":
            plain = [cell for cell in row if not cell.classes and cell.text]
            if len(plain) != 1 or len(date_cells) != 2:
                raise DisclosureContractError("e-disclosure consolidated row is invalid")
            period = plain[0].text
            registration_number = None
            registration_date = None
            publication_basis = _russian_date(date_cells[0].text, "publication basis")
            published_at = _russian_date(date_cells[1].text, "published at")
        elif category == "EMISSION":
            registration_cell = _one_class_cell(row, "num-reg-cell")
            if len(date_cells) != 3:
                raise DisclosureContractError("e-disclosure emission row is invalid")
            period = None
            registration_number = registration_cell.text
            registration_date = _russian_date(date_cells[0].text, "registration date")
            publication_basis = _russian_date(date_cells[1].text, "publication basis")
            published_at = _russian_date(date_cells[2].text, "published at")
        else:
            raise AssertionError("unsupported disclosure category")
        documents.append(
            DisclosureDocument(
                category=category,
                file_id=file_id,
                document_type=type_cell.text,
                period=period,
                registration_number=registration_number,
                registration_date=registration_date,
                publication_basis_date=publication_basis,
                published_at=published_at,
                file_url=file_url,
                index_url=index_url,
            )
        )
    documents.sort(key=lambda document: (document.published_at, document.file_id), reverse=True)
    return tuple(documents)


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[_HtmlCell, ...]] = []
        self._row: list[_HtmlCell] | None = None
        self._cell_classes: frozenset[str] | None = None
        self._cell_text: list[str] = []
        self._file_url: str | None = None
        self._file_id: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell_classes = frozenset((attributes.get("class") or "").split())
            self._cell_text = []
            self._file_url = None
            self._file_id = None
        elif tag == "a" and self._cell_classes is not None:
            classes = (attributes.get("class") or "").split()
            if "file-link" in classes:
                self._file_url = attributes.get("href")
                self._file_id = attributes.get("data-fileid")

    def handle_data(self, data: str) -> None:
        if self._cell_classes is not None:
            self._cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell_classes is not None and self._row is not None:
            self._row.append(
                _HtmlCell(
                    classes=self._cell_classes,
                    text=_normalized_text("".join(self._cell_text)),
                    file_url=self._file_url,
                    file_id=self._file_id,
                )
            )
            self._cell_classes = None
            self._cell_text = []
            self._file_url = None
            self._file_id = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(tuple(self._row))
            self._row = None


def _one_class_cell(row: tuple[_HtmlCell, ...], class_name: str) -> _HtmlCell:
    cells = [cell for cell in row if class_name in cell.classes]
    if len(cells) != 1 or not cells[0].text:
        raise DisclosureContractError(f"e-disclosure row {class_name} is missing or ambiguous")
    return cells[0]


def _validated_file_url(value: str | None, file_id: int) -> str:
    if value is None:
        raise DisclosureContractError("e-disclosure document URL is missing")
    parsed = urllib.parse.urlparse(value)
    query = urllib.parse.parse_qs(parsed.query)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.e-disclosure.ru"
        or parsed.path.lower() != "/portal/fileload.ashx"
        or query != {"Fileid": [str(file_id)]}
    ):
        raise DisclosureContractError("e-disclosure document URL is invalid")
    return value


def _files_url(company_id: int, document_type: int) -> str:
    return f"{DISCLOSURE_BASE_URL}/portal/files.aspx?id={company_id}&type={document_type}"


def _russian_date(value: str, field: str) -> date:
    try:
        return datetime.strptime(value, "%d.%m.%Y").date()
    except ValueError as error:
        raise DisclosureContractError(f"e-disclosure {field} is invalid") from error


def _optional_datetime(value: Any, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise DisclosureContractError(f"e-disclosure {field} must be text")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise DisclosureContractError(f"e-disclosure {field} is invalid") from error


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DisclosureContractError(f"e-disclosure {field} must be a positive integer")
    return value


def _positive_integer_text(value: str | None, field: str) -> int:
    if value is None or not value.isdigit():
        raise DisclosureContractError(f"e-disclosure {field} must be a positive integer")
    return _positive_integer(int(value), field)


def _nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DisclosureContractError(f"e-disclosure {field} must be non-negative")
    return value


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DisclosureContractError(f"e-disclosure {field} must be non-empty text")
    return _normalized_text(value)


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise DisclosureContractError("e-disclosure optional text field is invalid")
    return _normalized_text(value)


def _normalized_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def _digits(value: str) -> str:
    return "".join(re.findall(r"\d", value))
