"""Read-only client for public, possibly delayed MOEX ISS bond data."""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from invest_agent.http import HttpResponse, HttpTransport, UrllibTransport

ISS_BASE_URL = "https://iss.moex.com/iss"
BONDIZATION_BASE_URL = f"{ISS_BASE_URL}/statistics/engines/stock/markets/bonds/bondization"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

DESCRIPTION_COLUMNS = (
    "name",
    "value",
)
BOARD_COLUMNS = (
    "secid",
    "boardid",
    "market",
    "engine",
    "is_primary",
)
MARKETDATA_COLUMNS = (
    "SECID",
    "BOARDID",
    "BID",
    "OFFER",
    "LAST",
    "WAPRICE",
    "YIELD",
    "YIELDATWAPRICE",
    "YIELDTOOFFER",
    "DURATION",
    "NUMTRADES",
    "VOLTODAY",
    "VALTODAY_RUR",
    "TRADINGSTATUS",
    "UPDATETIME",
    "SYSTIME",
)
YIELD_COLUMNS = (
    "SECID",
    "BOARDID",
    "PRICE",
    "YIELDDATE",
    "YIELDDATETYPE",
    "EFFECTIVEYIELD",
    "DURATION",
    "ZSPREADBP",
    "GSPREADBP",
    "WAPRICE",
    "EFFECTIVEYIELDWAPRICE",
    "DURATIONWAPRICE",
    "TRADEMOMENT",
    "SYSTIME",
)


class MoexApiError(RuntimeError):
    """Sanitized HTTP or network failure from MOEX ISS."""

    def __init__(self, operation: str, status: int | None) -> None:
        self.operation = operation
        self.status = status
        status_text = "network" if status is None else f"HTTP {status}"
        super().__init__(f"MOEX {operation} failed ({status_text})")


class MoexContractError(ValueError):
    """MOEX returned a payload outside the verified ISS table contract."""


@dataclass(frozen=True, slots=True)
class MoexEmitter:
    emitter_id: int
    title: str
    short_title: str
    inn: str | None
    ogrn: str | None
    website: str | None
    source_url: str


@dataclass(frozen=True, slots=True)
class MoexBondFacts:
    secid: str
    isin: str
    name: str
    short_name: str
    primary_board: str
    emitter_id: int
    issue_name: str
    registration_number: str | None
    issue_date: date | None
    maturity_date: date | None
    face_value: Decimal | None
    face_currency: str | None
    issue_size: Decimal | None
    list_level: int | None
    coupon_percent: Decimal | None
    coupon_value: Decimal | None
    coupon_frequency: int | None
    coupon_benchmark: str | None
    coupon_benchmark_spread: Decimal | None
    next_coupon_date: date | None
    offer_date: date | None
    bond_type: str | None
    bond_subtype: str | None
    qualified_only: bool | None
    has_default: bool | None
    has_technical_default: bool | None
    source_url: str


@dataclass(frozen=True, slots=True)
class MoexBondMarketData:
    board_id: str
    bid_percent: Decimal | None
    offer_percent: Decimal | None
    last_percent: Decimal | None
    wap_percent: Decimal | None
    yield_at_wap_percent: Decimal | None
    effective_yield_percent: Decimal | None
    yield_date: date | None
    yield_date_type: str | None
    duration_days: Decimal | None
    z_spread_bps: Decimal | None
    g_spread_bps: Decimal | None
    trades_today: int | None
    volume_today: Decimal | None
    turnover_today_rub: Decimal | None
    trading_status: str | None
    trade_moment: datetime | None
    system_moment: datetime | None
    source_url: str

    @property
    def bid_offer_spread_percent_of_face(self) -> Decimal | None:
        if self.bid_percent is None or self.offer_percent is None:
            return None
        return self.offer_percent - self.bid_percent


@dataclass(frozen=True, slots=True)
class MoexBondSnapshot:
    facts: MoexBondFacts
    market: MoexBondMarketData | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class MoexBondCoupon:
    coupon_date: date
    record_date: date | None
    start_date: date | None
    face_value: Decimal | None
    currency: str | None
    value: Decimal | None
    annual_percent: Decimal | None
    value_rub: Decimal | None


@dataclass(frozen=True, slots=True)
class MoexBondAmortization:
    amortization_date: date
    face_value: Decimal | None
    initial_face_value: Decimal | None
    currency: str | None
    value_percent: Decimal | None
    value: Decimal | None
    value_rub: Decimal | None
    data_source: str | None


@dataclass(frozen=True, slots=True)
class MoexBondOffer:
    offer_date: date | None
    offer_start_date: date | None
    offer_end_date: date | None
    face_value: Decimal | None
    currency: str | None
    price_percent: Decimal | None
    value: Decimal | None
    agent: str | None
    offer_type: str | None


@dataclass(frozen=True, slots=True)
class MoexBondSchedule:
    secid: str
    isin: str
    coupons: tuple[MoexBondCoupon, ...]
    amortizations: tuple[MoexBondAmortization, ...]
    offers: tuple[MoexBondOffer, ...]
    source_url: str
    fetched_at: datetime


class MoexIssClient:
    """Fetch normalized bond and emitter facts from official MOEX ISS."""

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

    def fetch_bond(self, secid: str) -> MoexBondSnapshot:
        normalized_secid = secid.strip().upper()
        if not normalized_secid:
            raise ValueError("secid is required")

        security_url = self._security_url(normalized_secid)
        security_payload = self._get_json(security_url, operation=f"security:{normalized_secid}")
        description = _description(security_payload)
        if description.get("GROUP") != "stock_bonds":
            raise MoexContractError(f"MOEX {normalized_secid} is not a bond")
        primary_board = _primary_bond_board(security_payload)
        facts = _bond_facts(
            description,
            secid=normalized_secid,
            primary_board=primary_board,
            source_url=security_url,
        )

        market_url = self._market_url(normalized_secid, primary_board)
        market_payload = self._get_json(market_url, operation=f"market:{normalized_secid}")
        market = _market_data(market_payload, board=primary_board, source_url=market_url)
        return MoexBondSnapshot(facts=facts, market=market, fetched_at=self._now())

    def fetch_emitter(self, emitter_id: int) -> MoexEmitter:
        if emitter_id <= 0:
            raise ValueError("emitter_id must be positive")
        url = f"{ISS_BASE_URL}/emitters/{emitter_id}.json?iss.meta=off"
        payload = self._get_json(url, operation=f"emitter:{emitter_id}")
        rows = _table(payload, "emitter")
        if len(rows) != 1:
            raise MoexContractError(f"MOEX emitter {emitter_id} must contain one row")
        row = rows[0]
        returned_id = _integer(row.get("EMITTER_ID"))
        if returned_id != emitter_id:
            raise MoexContractError(f"MOEX emitter {emitter_id} id mismatch")
        return MoexEmitter(
            emitter_id=emitter_id,
            title=_required_text(row.get("TITLE"), "emitter.TITLE"),
            short_title=_required_text(row.get("SHORT_TITLE"), "emitter.SHORT_TITLE"),
            inn=_optional_text(row.get("INN")),
            ogrn=_optional_text(row.get("OGRN")),
            website=_optional_text(row.get("URL")),
            source_url=url,
        )

    def fetch_bond_schedule(self, secid: str) -> MoexBondSchedule:
        normalized_secid = secid.strip().upper()
        if not normalized_secid:
            raise ValueError("secid is required")
        source_url = self._bondization_url(normalized_secid)
        initial = self._get_json(source_url, operation=f"bondization:{normalized_secid}")
        coupon_rows = self._all_bondization_rows(
            initial,
            secid=normalized_secid,
            table="coupons",
        )
        amortization_rows = self._all_bondization_rows(
            initial,
            secid=normalized_secid,
            table="amortizations",
        )
        offer_rows = _table(initial, "offers")
        isins = {
            _required_text(row.get("isin"), f"bondization.{table}.isin").upper()
            for table, rows in (
                ("coupons", coupon_rows),
                ("amortizations", amortization_rows),
                ("offers", offer_rows),
            )
            for row in rows
        }
        if len(isins) != 1:
            raise MoexContractError(
                f"MOEX bondization {normalized_secid} must contain exactly one ISIN"
            )
        isin = next(iter(isins))
        return MoexBondSchedule(
            secid=normalized_secid,
            isin=isin,
            coupons=tuple(_coupon(row, normalized_secid, isin) for row in coupon_rows),
            amortizations=tuple(
                _amortization(row, normalized_secid, isin) for row in amortization_rows
            ),
            offers=tuple(_offer(row, normalized_secid, isin) for row in offer_rows),
            source_url=source_url,
            fetched_at=self._now(),
        )

    def _all_bondization_rows(
        self,
        initial: Mapping[str, Any],
        *,
        secid: str,
        table: str,
    ) -> list[dict[str, Any]]:
        rows = _table(initial, table)
        cursor = _table(initial, f"{table}.cursor")
        if len(cursor) != 1:
            raise MoexContractError(f"MOEX {table}.cursor must contain one row")
        total = _integer(cursor[0].get("TOTAL"))
        page_size = _integer(cursor[0].get("PAGESIZE"))
        if total is None or total < 0 or page_size is None or page_size <= 0:
            raise MoexContractError(f"MOEX {table}.cursor values are invalid")
        for start in range(page_size, total, page_size):
            page_url = self._bondization_url(secid, table=table, start=start)
            page = self._get_json(
                page_url,
                operation=f"bondization:{secid}:{table}:{start}",
            )
            rows.extend(_table(page, table))
        if len(rows) != total:
            raise MoexContractError(f"MOEX {table} pagination item count mismatch")
        return rows

    def _get_json(self, url: str, *, operation: str) -> Mapping[str, Any]:
        response: HttpResponse | None = None
        try:
            for attempt in range(self._max_attempts):
                response = self._transport.request(
                    method="GET",
                    url=url,
                    headers={"Accept": "application/json"},
                )
                if response.status != 429 and not 500 <= response.status < 600:
                    break
                if attempt + 1 < self._max_attempts:
                    self._sleep(0.25 * (2**attempt))
        except OSError as error:
            raise MoexApiError(operation, None) from error
        assert response is not None
        if not 200 <= response.status < 300:
            raise MoexApiError(operation, response.status)
        try:
            payload = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise MoexContractError(f"MOEX {operation} returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise MoexContractError(f"MOEX {operation} must return an object")
        return payload

    @staticmethod
    def _security_url(secid: str) -> str:
        query = urllib.parse.urlencode(
            {
                "iss.meta": "off",
                "iss.only": "description,boards",
                "description.columns": ",".join(DESCRIPTION_COLUMNS),
                "boards.columns": ",".join(BOARD_COLUMNS),
            }
        )
        return f"{ISS_BASE_URL}/securities/{urllib.parse.quote(secid, safe='')}.json?{query}"

    @staticmethod
    def _market_url(secid: str, board: str) -> str:
        query = urllib.parse.urlencode(
            {
                "iss.meta": "off",
                "iss.only": "marketdata,marketdata_yields",
                "marketdata.columns": ",".join(MARKETDATA_COLUMNS),
                "marketdata_yields.columns": ",".join(YIELD_COLUMNS),
            }
        )
        return (
            f"{ISS_BASE_URL}/engines/stock/markets/bonds/boards/"
            f"{urllib.parse.quote(board, safe='')}/securities/"
            f"{urllib.parse.quote(secid, safe='')}.json?{query}"
        )

    @staticmethod
    def _bondization_url(
        secid: str,
        *,
        table: str | None = None,
        start: int | None = None,
    ) -> str:
        query: dict[str, str | int] = {"iss.meta": "off"}
        if table is not None and start is not None:
            query[f"{table}.start"] = start
        encoded = urllib.parse.urlencode(query)
        return f"{BONDIZATION_BASE_URL}/{urllib.parse.quote(secid, safe='')}.json?{encoded}"


def _description(payload: Mapping[str, Any]) -> dict[str, Any]:
    rows = _table(payload, "description")
    result: dict[str, Any] = {}
    for row in rows:
        name = _required_text(row.get("name"), "description.name")
        result[name] = row.get("value")
    if not result:
        raise MoexContractError("MOEX description is empty")
    return result


def _primary_bond_board(payload: Mapping[str, Any]) -> str:
    candidates = [
        row
        for row in _table(payload, "boards")
        if row.get("engine") == "stock"
        and row.get("market") == "bonds"
        and _boolean(row.get("is_primary")) is True
    ]
    if len(candidates) != 1:
        raise MoexContractError("MOEX bond must have exactly one primary stock board")
    return _required_text(candidates[0].get("boardid"), "boards.boardid")


def _bond_facts(
    description: Mapping[str, Any],
    *,
    secid: str,
    primary_board: str,
    source_url: str,
) -> MoexBondFacts:
    returned_secid = _required_text(description.get("SECID"), "description.SECID")
    if returned_secid != secid:
        raise MoexContractError(f"MOEX security {secid} id mismatch")
    emitter_id = _integer(description.get("EMITTER_ID"))
    if emitter_id is None or emitter_id <= 0:
        raise MoexContractError(f"MOEX security {secid} has no emitter id")
    return MoexBondFacts(
        secid=secid,
        isin=_required_text(description.get("ISIN"), "description.ISIN"),
        name=_required_text(description.get("NAME"), "description.NAME"),
        short_name=_required_text(description.get("SHORTNAME"), "description.SHORTNAME"),
        primary_board=primary_board,
        emitter_id=emitter_id,
        issue_name=_required_text(description.get("ISSUENAME"), "description.ISSUENAME"),
        registration_number=_optional_text(description.get("REGNUMBER")),
        issue_date=_date(description.get("ISSUEDATE")),
        maturity_date=_date(description.get("MATDATE")),
        face_value=_decimal(description.get("FACEVALUE")),
        face_currency=_optional_text(description.get("FACEUNIT")),
        issue_size=_decimal(description.get("ISSUESIZE")),
        list_level=_integer(description.get("LISTLEVEL")),
        coupon_percent=_decimal(description.get("COUPONPERCENT")),
        coupon_value=_decimal(description.get("COUPONVALUE")),
        coupon_frequency=_integer(description.get("COUPONFREQUENCY")),
        coupon_benchmark=_optional_text(description.get("COUPON_BENCHMARK")),
        coupon_benchmark_spread=_decimal(description.get("COUPON_BENCHMARK_SPREAD")),
        next_coupon_date=_date(description.get("COUPONDATE")),
        offer_date=_date(description.get("OFFERDATE") or description.get("BUYBACKDATE")),
        bond_type=_optional_text(description.get("BOND_TYPE")),
        bond_subtype=_optional_text(description.get("BOND_SUBTYPE")),
        qualified_only=_boolean(description.get("ISQUALIFIEDINVESTORS")),
        has_default=_boolean(description.get("HASDEFAULT")),
        has_technical_default=_boolean(description.get("HASTECHNICALDEFAULT")),
        source_url=source_url,
    )


def _market_data(
    payload: Mapping[str, Any],
    *,
    board: str,
    source_url: str,
) -> MoexBondMarketData | None:
    market_rows = _table(payload, "marketdata")
    yield_rows = _table(payload, "marketdata_yields")
    if not market_rows and not yield_rows:
        return None
    market = _one_or_empty(market_rows, "marketdata")
    yields = _one_or_empty(yield_rows, "marketdata_yields")
    returned_boards = {
        value for value in (market.get("BOARDID"), yields.get("BOARDID")) if value is not None
    }
    if returned_boards and returned_boards != {board}:
        raise MoexContractError(f"MOEX market board mismatch: expected {board}")
    return MoexBondMarketData(
        board_id=board,
        bid_percent=_decimal(market.get("BID")),
        offer_percent=_decimal(market.get("OFFER")),
        last_percent=_decimal(market.get("LAST")),
        wap_percent=_first_decimal(yields.get("WAPRICE"), market.get("WAPRICE")),
        yield_at_wap_percent=_decimal(market.get("YIELDATWAPRICE")),
        effective_yield_percent=_first_decimal(
            yields.get("EFFECTIVEYIELDWAPRICE"),
            yields.get("EFFECTIVEYIELD"),
            market.get("YIELDATWAPRICE"),
            market.get("YIELD"),
        ),
        yield_date=_date(yields.get("YIELDDATE")),
        yield_date_type=_optional_text(yields.get("YIELDDATETYPE")),
        duration_days=_first_decimal(
            yields.get("DURATIONWAPRICE"),
            yields.get("DURATION"),
            market.get("DURATION"),
        ),
        z_spread_bps=_decimal(yields.get("ZSPREADBP")),
        g_spread_bps=_decimal(yields.get("GSPREADBP")),
        trades_today=_integer(market.get("NUMTRADES")),
        volume_today=_decimal(market.get("VOLTODAY")),
        turnover_today_rub=_decimal(market.get("VALTODAY_RUR")),
        trading_status=_optional_text(market.get("TRADINGSTATUS")),
        trade_moment=_datetime(yields.get("TRADEMOMENT")),
        system_moment=_first_datetime(yields.get("SYSTIME"), market.get("SYSTIME")),
        source_url=source_url,
    )


def _coupon(row: Mapping[str, Any], secid: str, isin: str) -> MoexBondCoupon:
    _validate_bondization_identity(row, secid=secid, isin=isin, table="coupons")
    return MoexBondCoupon(
        coupon_date=_required_date(row.get("coupondate"), "coupons.coupondate"),
        record_date=_date(row.get("recorddate")),
        start_date=_date(row.get("startdate")),
        face_value=_decimal(row.get("facevalue")),
        currency=_optional_text(row.get("faceunit")),
        value=_decimal(row.get("value")),
        annual_percent=_decimal(row.get("valueprc")),
        value_rub=_decimal(row.get("value_rub")),
    )


def _amortization(
    row: Mapping[str, Any],
    secid: str,
    isin: str,
) -> MoexBondAmortization:
    _validate_bondization_identity(row, secid=secid, isin=isin, table="amortizations")
    return MoexBondAmortization(
        amortization_date=_required_date(row.get("amortdate"), "amortizations.amortdate"),
        face_value=_decimal(row.get("facevalue")),
        initial_face_value=_decimal(row.get("initialfacevalue")),
        currency=_optional_text(row.get("faceunit")),
        value_percent=_decimal(row.get("valueprc")),
        value=_decimal(row.get("value")),
        value_rub=_decimal(row.get("value_rub")),
        data_source=_optional_text(row.get("data_source")),
    )


def _offer(row: Mapping[str, Any], secid: str, isin: str) -> MoexBondOffer:
    _validate_bondization_identity(row, secid=secid, isin=isin, table="offers")
    return MoexBondOffer(
        offer_date=_date(row.get("offerdate")),
        offer_start_date=_date(row.get("offerdatestart")),
        offer_end_date=_date(row.get("offerdateend")),
        face_value=_decimal(row.get("facevalue")),
        currency=_optional_text(row.get("faceunit")),
        price_percent=_decimal(row.get("price")),
        value=_decimal(row.get("value")),
        agent=_optional_text(row.get("agent")),
        offer_type=_optional_text(row.get("offertype")),
    )


def _validate_bondization_identity(
    row: Mapping[str, Any],
    *,
    secid: str,
    isin: str,
    table: str,
) -> None:
    returned_secid = _required_text(row.get("secid"), f"bondization.{table}.secid").upper()
    returned_isin = _required_text(row.get("isin"), f"bondization.{table}.isin").upper()
    if returned_secid != secid or returned_isin != isin:
        raise MoexContractError(f"MOEX bondization {table} identity mismatch")


def _table(payload: Mapping[str, Any], name: str) -> list[dict[str, Any]]:
    raw = payload.get(name)
    if not isinstance(raw, dict):
        raise MoexContractError(f"MOEX table {name} is missing")
    columns = raw.get("columns")
    data = raw.get("data")
    if not isinstance(columns, list) or not all(isinstance(item, str) for item in columns):
        raise MoexContractError(f"MOEX table {name}.columns is invalid")
    if not isinstance(data, list):
        raise MoexContractError(f"MOEX table {name}.data is invalid")
    rows: list[dict[str, Any]] = []
    for index, values in enumerate(data):
        if not isinstance(values, list) or len(values) != len(columns):
            raise MoexContractError(f"MOEX table {name}.data[{index}] is invalid")
        rows.append(dict(zip(columns, values, strict=True)))
    return rows


def _one_or_empty(rows: list[dict[str, Any]], table: str) -> dict[str, Any]:
    if len(rows) > 1:
        raise MoexContractError(f"MOEX table {table} must contain at most one row")
    return rows[0] if rows else {}


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MoexContractError(f"MOEX {field} must be non-empty text")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise MoexContractError("MOEX numeric value is invalid") from error
    if not result.is_finite():
        raise MoexContractError("MOEX numeric value must be finite")
    return result


def _first_decimal(*values: Any) -> Decimal | None:
    for value in values:
        result = _decimal(value)
        if result is not None:
            return result
    return None


def _integer(value: Any) -> int | None:
    decimal = _decimal(value)
    if decimal is None:
        return None
    if decimal != decimal.to_integral_value():
        raise MoexContractError("MOEX integer value is fractional")
    return int(decimal)


def _boolean(value: Any) -> bool | None:
    if value in (1, "1", True):
        return True
    if value in (0, "0", False):
        return False
    if value is None or value == "":
        return None
    raise MoexContractError("MOEX boolean value is invalid")


def _date(value: Any) -> date | None:
    if value in (None, "", "0000-00-00"):
        return None
    if not isinstance(value, str):
        raise MoexContractError("MOEX date value must be text")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise MoexContractError("MOEX date value is invalid") from error


def _required_date(value: Any, field: str) -> date:
    parsed = _date(value)
    if parsed is None:
        raise MoexContractError(f"MOEX {field} is required")
    return parsed


def _datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise MoexContractError("MOEX datetime value must be text")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise MoexContractError("MOEX datetime value is invalid") from error
    return parsed.replace(tzinfo=MOSCOW_TZ) if parsed.tzinfo is None else parsed


def _first_datetime(*values: Any) -> datetime | None:
    for value in values:
        result = _datetime(value)
        if result is not None:
            return result
    return None
