"""Official key-rate observation and transparent bond rate scenarios."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from invest_agent.bond_report import BondMarketReport, EnrichedBondPosition
from invest_agent.http import HttpTransport, UrllibTransport

CBR_KEY_RATE_URL = "https://www.cbr.ru/hd_base/KeyRate/"


class CbrKeyRateError(RuntimeError):
    """The official key-rate page is unavailable or changed its contract."""


@dataclass(frozen=True, slots=True)
class KeyRateObservation:
    value_percent: Decimal
    effective_date: date
    fetched_at: datetime
    source_url: str


@dataclass(frozen=True, slots=True)
class RateScenario:
    code: str
    label: str
    key_rate_change_percent: Decimal


@dataclass(frozen=True, slots=True)
class RateScenarioPolicy:
    horizon_months: int
    long_duration_threshold_days: Decimal
    corporate_rate_pass_through: Decimal
    scenarios: tuple[RateScenario, ...]

    @classmethod
    def from_toml(cls, path: str | Path) -> RateScenarioPolicy:
        with Path(path).open("rb") as source:
            raw = tomllib.load(source)["rate_model"]
        scenarios = tuple(
            RateScenario(
                code=_required_text(item, "code"),
                label=_required_text(item, "label"),
                key_rate_change_percent=_decimal(
                    item.get("key_rate_change_percent"),
                    "rate_model.scenarios.key_rate_change_percent",
                ),
            )
            for item in raw["scenarios"]
        )
        policy = cls(
            horizon_months=int(raw["horizon_months"]),
            long_duration_threshold_days=_positive_decimal(
                raw["long_duration_threshold_days"],
                "rate_model.long_duration_threshold_days",
            ),
            corporate_rate_pass_through=_fraction(
                raw["corporate_rate_pass_through"],
                "rate_model.corporate_rate_pass_through",
            ),
            scenarios=scenarios,
        )
        if policy.horizon_months <= 0 or not policy.scenarios:
            raise ValueError("rate_model requires a positive horizon and scenarios")
        if len({scenario.code for scenario in policy.scenarios}) != len(policy.scenarios):
            raise ValueError("rate_model scenario codes must be unique")
        return policy


@dataclass(frozen=True, slots=True)
class BondRateScenarioResult:
    code: str
    label: str
    projected_key_rate_percent: Decimal
    projected_coupon_or_carry_percent: Decimal | None
    approximate_price_return_percent: Decimal | None
    approximate_total_return_percent: Decimal | None


@dataclass(frozen=True, slots=True)
class BondRateSensitivity:
    ticker: str
    isin: str
    model_type: str
    duration_days: Decimal | None
    benchmark: str | None
    spread_percent: Decimal | None
    spread_source: str | None
    scenarios: tuple[BondRateScenarioResult, ...]
    limitations: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "isin": self.isin,
            "model_type": self.model_type,
            "duration_days": _optional_decimal_text(self.duration_days),
            "benchmark": self.benchmark,
            "spread_percent": _optional_decimal_text(self.spread_percent),
            "spread_source": self.spread_source,
            "scenarios": [
                {
                    "code": scenario.code,
                    "label": scenario.label,
                    "projected_key_rate_percent": _decimal_text(
                        scenario.projected_key_rate_percent
                    ),
                    "projected_coupon_or_carry_percent": _optional_decimal_text(
                        scenario.projected_coupon_or_carry_percent
                    ),
                    "approximate_price_return_percent": _optional_decimal_text(
                        scenario.approximate_price_return_percent
                    ),
                    "approximate_total_return_percent": _optional_decimal_text(
                        scenario.approximate_total_return_percent
                    ),
                }
                for scenario in self.scenarios
            ],
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, slots=True)
class RateModelReport:
    observation: KeyRateObservation
    horizon_months: int
    bonds: tuple[BondRateSensitivity, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": {
                "provider": "Bank of Russia",
                "url": self.observation.source_url,
                "effective_date": self.observation.effective_date.isoformat(),
                "fetched_at": self.observation.fetched_at.isoformat(),
            },
            "current_key_rate_percent": _decimal_text(self.observation.value_percent),
            "horizon_months": self.horizon_months,
            "bonds": [bond.as_dict() for bond in self.bonds],
            "model_boundaries": [
                "scenarios are deterministic assumptions, not a key-rate forecast",
                "floater carry ignores discount/premium, taxes, fees and reset lag",
                "fixed-bond price effect is a first-order duration approximation",
                "credit-spread changes can dominate the key-rate effect",
            ],
        }


class CbrKeyRateClient:
    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._transport = transport or UrllibTransport()
        self._now = now or (lambda: datetime.now(tz=UTC))

    def fetch(self) -> KeyRateObservation:
        try:
            response = self._transport.request(
                method="GET",
                url=CBR_KEY_RATE_URL,
                headers={"Accept": "text/html"},
                timeout_seconds=20,
            )
        except OSError as error:
            raise CbrKeyRateError("Bank of Russia key-rate request failed") from error
        if not 200 <= response.status < 300:
            raise CbrKeyRateError(
                f"Bank of Russia key-rate request failed (HTTP {response.status})"
            )
        try:
            html = response.body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CbrKeyRateError("Bank of Russia key-rate page is not UTF-8") from error
        parser = _TableCellParser()
        parser.feed(html)
        observations: list[tuple[date, Decimal]] = []
        for first, second in zip(parser.cells, parser.cells[1:], strict=False):
            if not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", first):
                continue
            if not re.fullmatch(r"\d{1,2},\d{2}", second):
                continue
            try:
                effective = datetime.strptime(first, "%d.%m.%Y").date()
                rate = Decimal(second.replace(",", "."))
            except (ValueError, InvalidOperation):
                continue
            if rate > 0:
                observations.append((effective, rate))
        if not observations:
            raise CbrKeyRateError("Bank of Russia key-rate table contract changed")
        effective_date, rate = max(observations, key=lambda item: item[0])
        return KeyRateObservation(
            value_percent=rate,
            effective_date=effective_date,
            fetched_at=self._now(),
            source_url=CBR_KEY_RATE_URL,
        )


class BondRateModel:
    def __init__(self, policy: RateScenarioPolicy) -> None:
        self._policy = policy

    def analyze(
        self,
        bonds: BondMarketReport,
        observation: KeyRateObservation,
    ) -> RateModelReport:
        sensitivities = tuple(
            result
            for position in bonds.positions
            if (result := self._analyze_bond(position, observation)) is not None
        )
        return RateModelReport(
            observation=observation,
            horizon_months=self._policy.horizon_months,
            bonds=tuple(sorted(sensitivities, key=lambda item: item.ticker)),
        )

    def _analyze_bond(
        self,
        position: EnrichedBondPosition,
        observation: KeyRateObservation,
    ) -> BondRateSensitivity | None:
        facts = position.moex.facts
        market = position.moex.market
        duration = None if market is None else market.duration_days
        if _is_floater(facts.bond_type):
            return self._floater(position, observation, duration)
        if duration is None or duration < self._policy.long_duration_threshold_days:
            return None
        current_yield = position.comparable_effective_yield_percent
        limitations = (
            "parallel rate move and constant credit spread are assumed",
            "convexity and coupon reinvestment are omitted",
        )
        scenarios = tuple(
            self._fixed_scenario(
                scenario,
                observation,
                duration_days=duration,
                current_yield_percent=current_yield,
            )
            for scenario in self._policy.scenarios
        )
        return BondRateSensitivity(
            ticker=position.position.ticker,
            isin=facts.isin,
            model_type="LONG_FIXED_DURATION",
            duration_days=duration,
            benchmark=None,
            spread_percent=None,
            spread_source=None,
            scenarios=scenarios,
            limitations=limitations,
        )

    def _floater(
        self,
        position: EnrichedBondPosition,
        observation: KeyRateObservation,
        duration: Decimal | None,
    ) -> BondRateSensitivity:
        facts = position.moex.facts
        benchmark = facts.coupon_benchmark
        benchmark_is_key_rate = is_key_rate_benchmark(benchmark)
        spread = facts.coupon_benchmark_spread
        spread_source: str | None = "MOEX_EXPLICIT" if spread is not None else None
        limitations: list[str] = [
            "coupon reset lag and day-count convention are not modelled",
            "market discount/premium and credit-spread changes are omitted",
        ]
        if spread is None and benchmark_is_key_rate and facts.coupon_percent is not None:
            spread = facts.coupon_percent - observation.value_percent
            spread_source = "INFERRED_FROM_CURRENT_COUPON"
            limitations.append("spread is inferred from the current coupon and may be stale")
        if not benchmark_is_key_rate:
            limitations.append("coupon benchmark is not verified as the Bank of Russia key rate")
        scenarios = tuple(
            self._floater_scenario(
                scenario,
                observation,
                spread=spread if benchmark_is_key_rate else None,
            )
            for scenario in self._policy.scenarios
        )
        return BondRateSensitivity(
            ticker=position.position.ticker,
            isin=facts.isin,
            model_type="KEY_RATE_FLOATER" if benchmark_is_key_rate else "UNSUPPORTED_FLOATER",
            duration_days=duration,
            benchmark=benchmark,
            spread_percent=spread,
            spread_source=spread_source,
            scenarios=scenarios,
            limitations=tuple(limitations),
        )

    def _floater_scenario(
        self,
        scenario: RateScenario,
        observation: KeyRateObservation,
        *,
        spread: Decimal | None,
    ) -> BondRateScenarioResult:
        projected_rate = max(
            Decimal("0"),
            observation.value_percent + scenario.key_rate_change_percent,
        )
        carry = None if spread is None else max(Decimal("0"), projected_rate + spread)
        return BondRateScenarioResult(
            code=scenario.code,
            label=scenario.label,
            projected_key_rate_percent=projected_rate,
            projected_coupon_or_carry_percent=carry,
            approximate_price_return_percent=Decimal("0") if carry is not None else None,
            approximate_total_return_percent=carry,
        )

    def _fixed_scenario(
        self,
        scenario: RateScenario,
        observation: KeyRateObservation,
        *,
        duration_days: Decimal,
        current_yield_percent: Decimal | None,
    ) -> BondRateScenarioResult:
        projected_rate = max(
            Decimal("0"),
            observation.value_percent + scenario.key_rate_change_percent,
        )
        duration_years = duration_days / Decimal("365")
        price_return = (
            -duration_years
            * scenario.key_rate_change_percent
            * self._policy.corporate_rate_pass_through
        )
        total = None if current_yield_percent is None else current_yield_percent + price_return
        return BondRateScenarioResult(
            code=scenario.code,
            label=scenario.label,
            projected_key_rate_percent=projected_rate,
            projected_coupon_or_carry_percent=current_yield_percent,
            approximate_price_return_percent=price_return,
            approximate_total_return_percent=total,
        )


class _TableCellParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.cells: list[str] = []
        self._inside_cell = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "td":
            self._inside_cell = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._inside_cell:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "td" and self._inside_cell:
            text = " ".join("".join(self._parts).split())
            self.cells.append(text)
            self._inside_cell = False
            self._parts = []


def _is_floater(value: str | None) -> bool:
    return value is not None and "флоат" in value.casefold()


def is_key_rate_benchmark(value: str | None) -> bool:
    if value is None:
        return False
    normalized = re.sub(r"[^a-zа-я0-9]", "", value.casefold())
    return (
        "ключев" in normalized
        or "keyrate" in normalized
        or "rrefkeyr" in normalized
    )


def _required_text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"rate_model.scenarios.{field} must be non-empty text")
    return value.strip()


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field} must be numeric") from error
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def _positive_decimal(value: Any, field: str) -> Decimal:
    result = _decimal(value, field)
    if result <= 0:
        raise ValueError(f"{field} must be positive")
    return result


def _fraction(value: Any, field: str) -> Decimal:
    result = _decimal(value, field)
    if not Decimal("0") < result <= Decimal("1"):
        raise ValueError(f"{field} must be in (0, 1]")
    return result


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else _decimal_text(value)
