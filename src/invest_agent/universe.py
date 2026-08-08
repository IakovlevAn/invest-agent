"""Fast MOEX/CBR candidate search outside the current portfolio."""

from __future__ import annotations

import tomllib
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from invest_agent.bond_report import (
    BondMarketReport,
    EnrichedBondPosition,
    IssuerExposure,
)
from invest_agent.credit import (
    CreditAnalysisPolicy,
    CreditPortfolioAnalyzer,
    CreditRatingEvidence,
    RatingBand,
    RatingsClient,
    SignalSeverity,
)
from invest_agent.domain import InstrumentType, PortfolioSnapshot, Position
from invest_agent.market.moex import (
    MoexApiError,
    MoexBondUniverseQuote,
    MoexContractError,
    MoexIssClient,
)


@dataclass(frozen=True, slots=True)
class BondUniversePolicy:
    board_id: str
    pre_credit_limit: int
    result_limit: int
    minimum_yield_percent: Decimal
    maximum_yield_percent: Decimal
    minimum_duration_days: Decimal
    maximum_duration_days: Decimal
    minimum_days_to_maturity: int
    minimum_issue_notional_rub: Decimal
    minimum_turnover_rub: Decimal
    maximum_lot_share_of_cash: Decimal
    maximum_list_level: int
    third_level_minimum_rating_band: RatingBand = RatingBand.STRONG

    @classmethod
    def from_toml(cls, path: str | Path) -> BondUniversePolicy:
        with Path(path).open("rb") as source:
            raw = tomllib.load(source)["universe"]
        policy = cls(
            board_id=_required_text(raw["board_id"], "universe.board_id").upper(),
            pre_credit_limit=_positive_int(
                raw["pre_credit_limit"],
                "universe.pre_credit_limit",
            ),
            result_limit=_positive_int(raw["result_limit"], "universe.result_limit"),
            minimum_yield_percent=_positive_decimal(
                raw["minimum_yield_percent"],
                "universe.minimum_yield_percent",
            ),
            maximum_yield_percent=_positive_decimal(
                raw["maximum_yield_percent"],
                "universe.maximum_yield_percent",
            ),
            minimum_duration_days=_positive_decimal(
                raw["minimum_duration_days"],
                "universe.minimum_duration_days",
            ),
            maximum_duration_days=_positive_decimal(
                raw["maximum_duration_days"],
                "universe.maximum_duration_days",
            ),
            minimum_days_to_maturity=_positive_int(
                raw["minimum_days_to_maturity"],
                "universe.minimum_days_to_maturity",
            ),
            minimum_issue_notional_rub=_positive_decimal(
                raw["minimum_issue_notional_rub"],
                "universe.minimum_issue_notional_rub",
            ),
            minimum_turnover_rub=_positive_decimal(
                raw["minimum_turnover_rub"],
                "universe.minimum_turnover_rub",
            ),
            maximum_lot_share_of_cash=_fraction(
                raw["maximum_lot_share_of_cash"],
                "universe.maximum_lot_share_of_cash",
            ),
            maximum_list_level=_positive_int(
                raw["maximum_list_level"],
                "universe.maximum_list_level",
            ),
            third_level_minimum_rating_band=_rating_band(
                raw["third_level_minimum_rating_band"],
                "universe.third_level_minimum_rating_band",
            ),
        )
        if policy.result_limit > policy.pre_credit_limit:
            raise ValueError("universe.result_limit cannot exceed pre_credit_limit")
        if policy.minimum_yield_percent >= policy.maximum_yield_percent:
            raise ValueError("universe yield range is invalid")
        if policy.minimum_duration_days >= policy.maximum_duration_days:
            raise ValueError("universe duration range is invalid")
        return policy


@dataclass(frozen=True, slots=True)
class BondUniverseCandidate:
    ticker: str
    isin: str
    name: str
    emitter_id: int
    emitter_name: str
    current_issuer_share_of_bonds: Decimal
    broad_rating_band: RatingBand
    effective_yield_percent: Decimal
    duration_days: Decimal
    lot_size: int
    estimated_lot_cost_rub: Decimal
    reference_buy_price_percent: Decimal
    turnover_today_rub: Decimal
    ranking_score: Decimal
    reasons: tuple[str, ...]
    moex_security_url: str
    moex_market_url: str
    list_level: int = 2


@dataclass(frozen=True, slots=True)
class BondUniverseReport:
    account_ref: str
    portfolio_as_of: datetime
    fetched_at: datetime
    board_id: str
    scanned_count: int
    coarse_eligible_count: int
    detailed_count: int
    candidates: tuple[BondUniverseCandidate, ...]
    rejection_counts: tuple[tuple[str, int], ...]
    failures: tuple[str, ...]


class BondCandidateScreener:
    def __init__(
        self,
        moex: MoexIssClient,
        ratings: RatingsClient,
        credit_policy: CreditAnalysisPolicy,
        universe_policy: BondUniversePolicy,
        *,
        buy_availability: Callable[[tuple[str, ...]], set[str]] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._moex = moex
        self._ratings = ratings
        self._credit_policy = credit_policy
        self._policy = universe_policy
        self._buy_availability = buy_availability
        self._now = now or (lambda: datetime.now(tz=UTC))

    def screen(
        self,
        snapshot: PortfolioSnapshot,
        current_bonds: BondMarketReport,
    ) -> BondUniverseReport:
        if snapshot.account_ref != current_bonds.account_ref:
            raise ValueError("universe input account mismatch")
        fetched_at = self._now()
        quotes = self._moex.fetch_bond_universe(self._policy.board_id)
        existing_tickers = {position.ticker for position in snapshot.positions}
        coarse = [
            quote
            for quote in quotes
            if self._coarse_eligible(
                quote,
                cash_rub=snapshot.cash_rub,
                existing_tickers=existing_tickers,
                as_of=fetched_at,
            )
        ]
        coarse_eligible_count = len(coarse)
        rejection_counts: defaultdict[str, int] = defaultdict(int)
        if self._buy_availability is not None and coarse:
            available_isins = self._buy_availability(
                tuple(dict.fromkeys(quote.isin for quote in coarse))
            )
            unavailable_count = sum(
                quote.isin not in available_isins for quote in coarse
            )
            if unavailable_count:
                rejection_counts["bcs_buy_unavailable"] += unavailable_count
            coarse = [quote for quote in coarse if quote.isin in available_isins]
        shortlist = _diversified_shortlist(coarse, self._policy.pre_credit_limit)
        records: list[EnrichedBondPosition] = []
        failures: list[str] = []
        emitter_cache: dict[int, Any] = {}
        quote_by_ticker = {quote.secid: quote for quote in shortlist}
        for quote in shortlist:
            try:
                bond = self._moex.fetch_bond(quote.secid)
                if not self._detail_eligible(bond.facts):
                    rejection_counts["detail_policy"] += 1
                    continue
                emitter_id = bond.facts.emitter_id
                if emitter_id not in emitter_cache:
                    emitter_cache[emitter_id] = self._moex.fetch_emitter(emitter_id)
                emitter = emitter_cache[emitter_id]
                lot_cost = quote.estimated_lot_cost_rub
                if lot_cost is None:
                    continue
                records.append(
                    EnrichedBondPosition(
                        position=Position(
                            instrument_uid=f"MOEX:{quote.board_id}:{quote.secid}",
                            ticker=quote.secid,
                            class_code=quote.board_id,
                            instrument_type=InstrumentType.BOND,
                            quantity=Decimal(quote.lot_size),
                            market_price=lot_cost / quote.lot_size,
                            market_value_rub=lot_cost,
                            tradable=True,
                            display_name=quote.short_name,
                        ),
                        moex=bond,
                        emitter=emitter,
                        missing_fields=(),
                    )
                )
            except (MoexApiError, MoexContractError, ValueError) as error:
                failures.append(f"{quote.secid}: {error}")

        candidate_report = _candidate_market_report(snapshot, records, fetched_at)
        credit = CreditPortfolioAnalyzer(
            self._ratings,
            self._credit_policy,
            now=lambda: fetched_at,
        ).analyze(candidate_report)
        failures.extend(
            f"{failure.ticker}/{failure.stage}: {failure.message}"
            for failure in credit.failures
        )
        passport_by_ticker = {item.ticker: item for item in credit.passports}
        current_shares = {
            item.emitter_id: item.share_of_covered_bonds
            for item in current_bonds.issuer_exposures
        }
        candidates: list[BondUniverseCandidate] = []
        for record in records:
            passport = passport_by_ticker.get(record.position.ticker)
            quote = quote_by_ticker[record.position.ticker]
            if passport is None or not passport.current_ratings:
                rejection_counts["no_current_rating"] += 1
                continue
            band = _worst_band(passport.current_ratings)
            if band not in {
                RatingBand.HIGHEST,
                RatingBand.HIGH,
                RatingBand.STRONG,
                RatingBand.ADEQUATE,
            }:
                rejection_counts["rating_below_BBB"] += 1
                continue
            blocking_signals = [
                signal
                for signal in passport.signals
                if signal.severity is SignalSeverity.CRITICAL
                or (
                    signal.severity is SignalSeverity.WARNING
                    and signal.code != "RATING_DOWNGRADE"
                )
            ]
            if blocking_signals:
                rejection_counts["blocking_credit_signal"] += 1
                continue
            downgrade_signals = [
                signal
                for signal in passport.signals
                if signal.severity is SignalSeverity.WARNING
                and signal.code == "RATING_DOWNGRADE"
            ]
            if downgrade_signals and _band_rank(band) > _band_rank(RatingBand.STRONG):
                rejection_counts["downgrade_below_A"] += 1
                continue
            if (
                record.moex.facts.list_level == 3
                and _band_rank(band)
                > _band_rank(self._policy.third_level_minimum_rating_band)
            ):
                rejection_counts["third_level_rating_below_A"] += 1
                continue
            market = record.moex.market
            lot_cost = quote.estimated_lot_cost_rub
            price = quote.reference_buy_price_percent
            turnover = quote.turnover_today_rub
            comparable_yield = record.comparable_effective_yield_percent
            if (
                market is None
                or market.duration_days is None
                or lot_cost is None
                or price is None
                or turnover is None
                or comparable_yield is None
            ):
                rejection_counts["incomplete_market_data"] += 1
                continue
            current_share = current_shares.get(record.moex.facts.emitter_id, Decimal("0"))
            score = _ranking_score(
                comparable_yield,
                market.duration_days,
                band,
                current_share,
                list_level=record.moex.facts.list_level,
                has_stable_downgrade=bool(downgrade_signals),
            )
            reasons = [
                "текущий рейтинг не ниже широкого класса BBB",
                "нет критических рейтинговых сигналов или негативного пересмотра",
                "выпуск прошёл фильтры доходности, срока, размера и ликвидности",
            ]
            if downgrade_signals:
                reasons.append(
                    "есть прошлое понижение рейтинга, но текущий класс не ниже A "
                    "и нет негативного/развивающегося прогноза"
                )
            candidates.append(
                BondUniverseCandidate(
                    ticker=record.position.ticker,
                    isin=record.moex.facts.isin,
                    name=record.moex.facts.short_name,
                    emitter_id=record.moex.facts.emitter_id,
                    emitter_name=passport.emitter_name,
                    current_issuer_share_of_bonds=current_share,
                    broad_rating_band=band,
                    effective_yield_percent=comparable_yield,
                    duration_days=market.duration_days,
                    lot_size=quote.lot_size,
                    estimated_lot_cost_rub=lot_cost,
                    reference_buy_price_percent=price,
                    turnover_today_rub=turnover,
                    ranking_score=score,
                    reasons=tuple(reasons),
                    moex_security_url=record.moex.facts.source_url,
                    moex_market_url=market.source_url,
                    list_level=record.moex.facts.list_level,
                )
            )
        candidates.sort(key=lambda item: (-item.ranking_score, item.ticker))
        unique_candidates: list[BondUniverseCandidate] = []
        selected_emitters: set[int] = set()
        for candidate in candidates:
            if candidate.emitter_id in selected_emitters:
                rejection_counts["duplicate_issuer"] += 1
                continue
            selected_emitters.add(candidate.emitter_id)
            unique_candidates.append(candidate)
        return BondUniverseReport(
            account_ref=snapshot.account_ref,
            portfolio_as_of=snapshot.as_of,
            fetched_at=fetched_at,
            board_id=self._policy.board_id,
            scanned_count=len(quotes),
            coarse_eligible_count=coarse_eligible_count,
            detailed_count=len(records),
            candidates=tuple(unique_candidates[: self._policy.result_limit]),
            rejection_counts=tuple(sorted(rejection_counts.items())),
            failures=tuple(dict.fromkeys(failures)),
        )

    def _coarse_eligible(
        self,
        quote: MoexBondUniverseQuote,
        *,
        cash_rub: Decimal,
        existing_tickers: set[str],
        as_of: datetime,
    ) -> bool:
        if quote.secid in existing_tickers or quote.status != "A":
            return False
        if quote.list_level is None or quote.list_level > self._policy.maximum_list_level:
            return False
        if quote.face_currency not in {"RUB", "SUR"} or not quote.isin.startswith("RU"):
            return False
        if quote.maturity_date is None:
            return False
        if quote.maturity_date <= as_of.date() + timedelta(
            days=self._policy.minimum_days_to_maturity
        ):
            return False
        yield_percent = quote.effective_yield_percent
        duration = quote.duration_days
        if yield_percent is None or not (
            self._policy.minimum_yield_percent
            <= yield_percent
            <= self._policy.maximum_yield_percent
        ):
            return False
        if duration is None or not (
            self._policy.minimum_duration_days
            <= duration
            <= self._policy.maximum_duration_days
        ):
            return False
        notional = quote.issue_notional_rub
        if notional is None or notional < self._policy.minimum_issue_notional_rub:
            return False
        turnover = quote.turnover_today_rub
        if turnover is None or turnover < self._policy.minimum_turnover_rub:
            return False
        lot_cost = quote.estimated_lot_cost_rub
        return not (
            lot_cost is None
            or lot_cost > cash_rub * self._policy.maximum_lot_share_of_cash
        )

    def _detail_eligible(self, facts: Any) -> bool:
        return (
            facts.face_currency in {"RUB", "SUR"}
            and facts.qualified_only is False
            and facts.has_default is False
            and facts.has_technical_default is False
            and facts.list_level is not None
            and facts.list_level <= self._policy.maximum_list_level
        )


def _candidate_market_report(
    snapshot: PortfolioSnapshot,
    records: list[EnrichedBondPosition],
    fetched_at: datetime,
) -> BondMarketReport:
    value = sum((record.position.market_value_rub for record in records), Decimal("0"))
    issuer_values: defaultdict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    issuer_names: dict[int, str] = {}
    issuer_issues: defaultdict[int, list[str]] = defaultdict(list)
    for record in records:
        if record.emitter is None:
            continue
        emitter_id = record.emitter.emitter_id
        issuer_values[emitter_id] += record.position.market_value_rub
        issuer_names[emitter_id] = record.emitter.short_title
        issuer_issues[emitter_id].append(record.position.ticker)
    exposures = tuple(
        IssuerExposure(
            emitter_id=emitter_id,
            emitter_name=issuer_names[emitter_id],
            value_rub=issuer_value,
            share_of_covered_bonds=(
                Decimal("0") if value == 0 else issuer_value / value
            ),
            issues=tuple(sorted(issuer_issues[emitter_id])),
        )
        for emitter_id, issuer_value in sorted(issuer_values.items())
    )
    hhi = sum((exposure.share_of_covered_bonds**2 for exposure in exposures), Decimal("0"))
    return BondMarketReport(
        account_ref=snapshot.account_ref,
        portfolio_as_of=snapshot.as_of,
        fetched_at=fetched_at,
        total_portfolio_value_rub=snapshot.total_value_rub,
        total_bond_positions=len(records),
        bond_value_rub=value,
        covered_bond_value_rub=value,
        market_covered_bond_value_rub=value,
        issuer_covered_value_rub=value,
        positions=tuple(records),
        issuer_exposures=exposures,
        failures=(),
        issuer_hhi_on_covered_bonds=hhi,
        effective_issuer_count=None if hhi == 0 else Decimal("1") / hhi,
    )


def _worst_band(ratings: tuple[CreditRatingEvidence, ...]) -> RatingBand:
    return max((rating.band for rating in ratings), key=_band_rank)


def _band_rank(band: RatingBand) -> int:
    return {
        RatingBand.HIGHEST: 0,
        RatingBand.HIGH: 1,
        RatingBand.STRONG: 2,
        RatingBand.ADEQUATE: 3,
        RatingBand.SPECULATIVE: 4,
        RatingBand.HIGH_RISK: 5,
        RatingBand.VERY_HIGH_RISK: 6,
        RatingBand.DEFAULT: 7,
        RatingBand.UNRATED: 8,
    }[band]


def _ranking_score(
    yield_percent: Decimal,
    duration_days: Decimal,
    band: RatingBand,
    existing_share: Decimal,
    *,
    list_level: int = 2,
    has_stable_downgrade: bool = False,
) -> Decimal:
    rating_penalty = {
        RatingBand.HIGHEST: Decimal("0"),
        RatingBand.HIGH: Decimal("0.75"),
        RatingBand.STRONG: Decimal("1.50"),
        RatingBand.ADEQUATE: Decimal("3.00"),
    }[band]
    duration_penalty = duration_days / Decimal("365") * Decimal("0.25")
    concentration_penalty = existing_share * Decimal("10")
    listing_penalty = {
        1: Decimal("0"),
        2: Decimal("0.30"),
        3: Decimal("1.20"),
    }.get(list_level, Decimal("2"))
    downgrade_penalty = Decimal("1.50") if has_stable_downgrade else Decimal("0")
    return (
        yield_percent
        - rating_penalty
        - duration_penalty
        - concentration_penalty
        - listing_penalty
        - downgrade_penalty
    )


def _coarse_sort_key(
    quote: MoexBondUniverseQuote,
) -> tuple[int, Decimal, Decimal, Decimal, str]:
    assert quote.effective_yield_percent is not None
    turnover = quote.turnover_today_rub or Decimal("0")
    duration = quote.duration_days or Decimal("999999")
    return (
        quote.list_level or 99,
        -turnover,
        -quote.effective_yield_percent,
        duration,
        quote.secid,
    )


def _diversified_shortlist(
    quotes: list[MoexBondUniverseQuote],
    limit: int,
) -> list[MoexBondUniverseQuote]:
    bands = (
        [quote for quote in quotes if quote.effective_yield_percent < Decimal("20")],
        [
            quote
            for quote in quotes
            if Decimal("20") <= quote.effective_yield_percent < Decimal("24")
        ],
        [quote for quote in quotes if quote.effective_yield_percent >= Decimal("24")],
    )
    selected: list[MoexBondUniverseQuote] = []
    quota = max(1, limit // len(bands))
    for band in bands:
        selected.extend(sorted(band, key=_coarse_sort_key)[:quota])
    selected_ids = {quote.secid for quote in selected}
    remaining = sorted(
        (quote for quote in quotes if quote.secid not in selected_ids),
        key=_coarse_sort_key,
    )
    selected.extend(remaining[: max(0, limit - len(selected))])
    return selected[:limit]


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _positive_decimal(value: Any, field: str) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{field} must be positive")
    return result


def _rating_band(value: Any, field: str) -> RatingBand:
    try:
        band = RatingBand(str(value).strip().upper())
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a supported rating band") from error
    if band not in {
        RatingBand.HIGHEST,
        RatingBand.HIGH,
        RatingBand.STRONG,
        RatingBand.ADEQUATE,
    }:
        raise ValueError(f"{field} must be investment grade")
    return band


def _fraction(value: Any, field: str) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or not Decimal("0") < result <= Decimal("1"):
        raise ValueError(f"{field} must be in (0, 1]")
    return result


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value.strip()
