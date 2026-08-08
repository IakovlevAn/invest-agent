"""Read-only client for the official Bank of Russia ratings repository.

The repository exposes a Bitrix web-component endpoint rather than a documented
public API. The client therefore validates the observed contract strictly and
fails closed when the site changes or requires a CAPTCHA.
"""

from __future__ import annotations

import http.cookiejar
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

from invest_agent.http import HttpResponse

CBR_RATINGS_URL = "https://ratings.cbr.ru/"
CBR_ACTION_URL = (
    "https://ratings.cbr.ru/bitrix/services/main/ajax.php"
    "?mode=ajax&c=prr.form&action={action}"
)
_CSRF_PATTERN = re.compile(r'bitrix_sessid"\s*:\s*"([a-fA-F0-9]{32})"')
_CAPTCHA_MARKERS = (
    'id="captcha"',
    "id='captcha'",
    'name="captchaCode"',
    "name='captchaCode'",
)


class CbrRatingsApiError(RuntimeError):
    """Sanitized HTTP or network failure from the CBR repository."""

    def __init__(self, operation: str, status: int | None) -> None:
        self.operation = operation
        self.status = status
        status_text = "network" if status is None else f"HTTP {status}"
        super().__init__(f"CBR ratings {operation} failed ({status_text})")


class CbrRatingsContractError(ValueError):
    """The repository returned data outside the verified web contract."""


class CbrRatingsCaptchaRequired(CbrRatingsApiError):
    """The official site requires a human CAPTCHA; automation must stop."""

    def __init__(self) -> None:
        super().__init__("captcha-required", None)


@dataclass(frozen=True, slots=True)
class CbrRatingAction:
    object_id: str
    object_name: str
    subject_name: str | None
    object_type: str
    inn: str | None
    isin: str | None
    rating_value: str | None
    outlook: str | None
    agency: str
    release_date: date
    rating_action: str
    release_url: str | None


class RatingsSession(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse: ...

    def post_form(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        fields: Mapping[str, str | int],
        timeout_seconds: float,
    ) -> HttpResponse: ...


class UrllibCookieSession:
    """Cookie-aware stdlib session; cookies are never exposed to callers."""

    def __init__(self) -> None:
        jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        return self._request(
            method="GET",
            url=url,
            headers=headers,
            body=None,
            timeout_seconds=timeout_seconds,
        )

    def post_form(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        fields: Mapping[str, str | int],
        timeout_seconds: float,
    ) -> HttpResponse:
        body = urllib.parse.urlencode(fields).encode("ascii")
        return self._request(
            method="POST",
            url=url,
            headers={
                **headers,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            body=body,
            timeout_seconds=timeout_seconds,
        )

    def _request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                return HttpResponse(
                    status=response.status,
                    body=response.read(),
                    headers=dict(response.headers.items()),
                )
        except urllib.error.HTTPError as error:
            return HttpResponse(
                status=error.code,
                body=error.read(),
                headers=dict(error.headers.items()),
            )


class CbrRatingsClient:
    """Search current official rating actions by issuer INN or exact ISIN."""

    def __init__(
        self,
        *,
        session: RatingsSession | None = None,
        timeout_seconds: float = 20.0,
        page_size: int = 100,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if page_size not in {10, 25, 50, 100}:
            raise ValueError("page_size must be one of 10, 25, 50 or 100")
        self._session = session or UrllibCookieSession()
        self._timeout_seconds = timeout_seconds
        self._page_size = page_size
        self._csrf_token: str | None = None

    def fetch_by_inn(self, inn: str) -> tuple[CbrRatingAction, ...]:
        normalized = inn.strip()
        if not re.fullmatch(r"\d{10}|\d{12}", normalized):
            raise ValueError("INN must contain 10 or 12 digits")
        return self._search(inn=normalized, isin="")

    def fetch_by_isin(self, isin: str) -> tuple[CbrRatingAction, ...]:
        normalized = isin.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{12}", normalized):
            raise ValueError("ISIN must contain 12 Latin letters or digits")
        return self._search(inn="", isin=normalized)

    def _search(self, *, inn: str, isin: str) -> tuple[CbrRatingAction, ...]:
        csrf = self._ensure_session()
        fields = {
            "fields[formSearh]": "quick",
            "fields[inn]": inn,
            "fields[ratingName]": "",
            "fields[isin]": isin,
            "fields[koNumber]": "",
            "fields[dateFrom]": "",
            "fields[dateTo]": "",
        }
        first = self._post_action("searchRating", fields, csrf=csrf)
        total = _required_nonnegative_integer(first.get("itemCount"), "itemCount")
        if total == 0:
            return ()

        page = self._post_action(
            "searchRatingNavigation",
            {
                "fields[pageSize]": self._page_size,
                "fields[pageNumber]": 1,
                "fields[sortingField]": "releaseDate",
                "fields[sortingDirection]": "descending",
            },
            csrf=csrf,
        )
        pages = _required_positive_integer(page.get("pageCount"), "pageCount")
        actions = list(_parse_items(page))
        for page_number in range(2, pages + 1):
            page = self._post_action(
                "searchRatingNavigation",
                {
                    "fields[pageSize]": self._page_size,
                    "fields[pageNumber]": page_number,
                    "fields[sortingField]": "releaseDate",
                    "fields[sortingDirection]": "descending",
                },
                csrf=csrf,
            )
            actions.extend(_parse_items(page))
        if len(actions) != total:
            raise CbrRatingsContractError("CBR ratings pagination item count mismatch")
        return tuple(actions)

    def _ensure_session(self) -> str:
        if self._csrf_token is not None:
            return self._csrf_token
        try:
            response = self._session.get(
                CBR_RATINGS_URL,
                headers={"Accept": "text/html"},
                timeout_seconds=self._timeout_seconds,
            )
        except OSError as error:
            raise CbrRatingsApiError("session", None) from error
        if not 200 <= response.status < 300:
            raise CbrRatingsApiError("session", response.status)
        try:
            html = response.body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CbrRatingsContractError("CBR ratings homepage is not UTF-8") from error
        lowered = html.casefold()
        if any(marker.casefold() in lowered for marker in _CAPTCHA_MARKERS):
            raise CbrRatingsCaptchaRequired
        match = _CSRF_PATTERN.search(html)
        if match is None:
            raise CbrRatingsContractError("CBR ratings CSRF contract is missing")
        self._csrf_token = match.group(1)
        return self._csrf_token

    def _post_action(
        self,
        action: str,
        fields: Mapping[str, str | int],
        *,
        csrf: str,
    ) -> Mapping[str, Any]:
        try:
            response = self._session.post_form(
                CBR_ACTION_URL.format(action=action),
                headers={
                    "Accept": "application/json",
                    "X-Bitrix-Csrf-Token": csrf,
                    "X-Bitrix-Site-Id": "s1",
                },
                fields=fields,
                timeout_seconds=self._timeout_seconds,
            )
        except OSError as error:
            raise CbrRatingsApiError(action, None) from error
        if not 200 <= response.status < 300:
            raise CbrRatingsApiError(action, response.status)
        try:
            payload = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise CbrRatingsContractError(f"CBR ratings {action} returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise CbrRatingsContractError(f"CBR ratings {action} must return an object")
        data = payload.get("data")
        if isinstance(data, dict) and data.get("captchaResult") is False:
            raise CbrRatingsCaptchaRequired
        if payload.get("status") != "success":
            if action == "searchRating" and _is_observed_no_results(payload):
                return {"itemCount": 0, "itemList": []}
            raise CbrRatingsApiError(action, response.status)
        if not isinstance(data, dict):
            raise CbrRatingsContractError(f"CBR ratings {action}.data must be an object")
        return data


def _parse_items(data: Mapping[str, Any]) -> tuple[CbrRatingAction, ...]:
    raw_items = data.get("itemList")
    if not isinstance(raw_items, list):
        raise CbrRatingsContractError("CBR ratings itemList must be a list")
    return tuple(_parse_action(item, index=index) for index, item in enumerate(raw_items))


def _parse_action(raw: Any, *, index: int) -> CbrRatingAction:
    if not isinstance(raw, dict):
        raise CbrRatingsContractError(f"CBR ratings itemList[{index}] must be an object")
    return CbrRatingAction(
        object_id=_required_text(raw.get("objectId"), "objectId"),
        object_name=_required_text(raw.get("objectName"), "objectName"),
        subject_name=_optional_text(raw.get("subjectName")),
        object_type=_required_text(raw.get("objectType"), "objectType"),
        inn=_optional_text(raw.get("inn")),
        isin=_optional_text(raw.get("isin")),
        rating_value=_optional_text(raw.get("ratingValue")),
        outlook=_optional_text(raw.get("prediction")),
        agency=_required_text(raw.get("kraName"), "kraName"),
        release_date=_release_date(raw.get("releaseDate")),
        rating_action=_required_text(raw.get("ratingAction"), "ratingAction"),
        release_url=_release_url(raw.get("releaseUrl")),
    )


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CbrRatingsContractError(f"CBR ratings {field} must be non-empty text")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _release_date(value: Any) -> date:
    if not isinstance(value, str):
        raise CbrRatingsContractError("CBR ratings releaseDate must be text")
    try:
        return datetime.strptime(value.strip(), "%d.%m.%Y").date()
    except ValueError as error:
        raise CbrRatingsContractError("CBR ratings releaseDate is invalid") from error


def _release_url(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CbrRatingsContractError("CBR ratings releaseUrl must be HTTP(S)")
    return text


def _required_nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CbrRatingsContractError(f"CBR ratings {field} must be a non-negative integer")
    return value


def _required_positive_integer(value: Any, field: str) -> int:
    result = _required_nonnegative_integer(value, field)
    if result == 0:
        raise CbrRatingsContractError(f"CBR ratings {field} must be positive")
    return result


def _is_observed_no_results(payload: Mapping[str, Any]) -> bool:
    """Match the repository's verified empty-search response exactly."""

    errors = payload.get("errors")
    if payload.get("data") is not None or not isinstance(errors, list) or len(errors) != 1:
        return False
    error = errors[0]
    return (
        isinstance(error, dict)
        and error.get("code") == 0
        and error.get("message") == "Array"
    )
