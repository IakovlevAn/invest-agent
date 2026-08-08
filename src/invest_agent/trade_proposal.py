"""Exact, persisted and approval-gated trade packages for the Codex interface."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import tomllib
import unicodedata
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from invest_agent.approval import ApprovalViolation, validate_approval
from invest_agent.brokers.bcs import (
    BcsAccessToken,
    BcsInstrument,
    BcsOrderBook,
    BcsQuote,
    BcsReadClient,
)
from invest_agent.domain import (
    Approval,
    InstrumentType,
    OrderIntent,
    OrderType,
    PortfolioSnapshot,
    ProposalBundle,
    Side,
)
from invest_agent.manager import ManagerAction, PortfolioManagerReport
from invest_agent.policy import InvestmentPolicy


class TradeProposalError(ValueError):
    """An exact package cannot be safely created or confirmed."""


@dataclass(frozen=True, slots=True)
class TradeProposalPolicy:
    proposal_ttl_seconds: int
    order_validity_seconds: int
    maximum_quote_age_seconds: int

    @classmethod
    def from_toml(cls, path: str | Path) -> TradeProposalPolicy:
        with Path(path).open("rb") as source:
            trading = tomllib.load(source)["trading"]
        policy = cls(
            proposal_ttl_seconds=int(trading["approval_ttl_seconds"]),
            order_validity_seconds=int(trading["order_validity_seconds"]),
            maximum_quote_age_seconds=int(trading["maximum_quote_age_seconds"]),
        )
        if min(
            policy.proposal_ttl_seconds,
            policy.order_validity_seconds,
            policy.maximum_quote_age_seconds,
        ) <= 0:
            raise ValueError("trading time limits must be positive")
        if policy.order_validity_seconds < policy.proposal_ttl_seconds:
            raise ValueError("order validity cannot be shorter than proposal TTL")
        return policy


@dataclass(frozen=True, slots=True)
class ConfirmationReceipt:
    approval: Approval
    proposal_id: str
    state: str
    orders_created: bool
    confirmation_mode: str = "EXACT_DIGEST_TEXT"
    confirmation_evidence_digest: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval.approval_id,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.approval.proposal_digest,
            "approved_by": self.approval.approved_by,
            "approved_at": self.approval.approved_at.isoformat(),
            "approval_expires_at": self.approval.expires_at.isoformat(),
            "state": self.state,
            "orders_created": self.orders_created,
            "confirmation_mode": self.confirmation_mode,
            "confirmation_evidence_digest": self.confirmation_evidence_digest,
        }


class ExactTradeProposalBuilder:
    def __init__(
        self,
        *,
        client: BcsReadClient,
        investment_policy: InvestmentPolicy,
        proposal_policy: TradeProposalPolicy,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._investment_policy = investment_policy
        self._proposal_policy = proposal_policy
        self._now = now or (lambda: datetime.now(tz=UTC))

    def build_manager_action(
        self,
        *,
        report: PortfolioManagerReport,
        snapshot: PortfolioSnapshot,
        access_token: BcsAccessToken,
        isin: str,
        side: Side,
    ) -> ProposalBundle:
        return self.build_manager_actions(
            report=report,
            snapshot=snapshot,
            access_token=access_token,
            actions=((isin, side),),
        )

    def build_manager_actions(
        self,
        *,
        report: PortfolioManagerReport,
        snapshot: PortfolioSnapshot,
        access_token: BcsAccessToken,
        actions: tuple[tuple[str, Side], ...],
    ) -> ProposalBundle:
        if report.snapshot_digest != snapshot.digest:
            raise TradeProposalError("manager report and live portfolio snapshot do not match")
        normalized_actions = tuple(
            (isin.strip().upper(), side) for isin, side in actions if isin.strip()
        )
        if not normalized_actions or len(normalized_actions) != len(actions):
            raise TradeProposalError("at least one exact manager action is required")
        if len({isin for isin, _ in normalized_actions}) != len(normalized_actions):
            raise TradeProposalError("an ISIN may occur only once in an exact order list")
        targets = tuple(
            (isin, side, *_manager_target(report, isin, side))
            for isin, side in normalized_actions
        )
        instruments = self._client.fetch_instruments_by_isins(
            access_token,
            tuple(isin for isin, _ in normalized_actions),
        )
        by_isin = {instrument.isin: instrument for instrument in instruments}
        if len(by_isin) != len(normalized_actions) or any(
            isin not in by_isin for isin, _ in normalized_actions
        ):
            raise TradeProposalError("BCS did not resolve every exact instrument once")
        quote_items = self._client.fetch_quotes(
            access_token,
            tuple(
                (by_isin[isin].ticker, by_isin[isin].primary_board)
                for isin, _ in normalized_actions
            ),
        )
        by_pair = {(quote.ticker, quote.class_code): quote for quote in quote_items}
        if len(by_pair) != len(normalized_actions):
            raise TradeProposalError("BCS did not return every current quote once")
        books = {
            isin: self._client.fetch_order_book(
                access_token,
                ticker=instrument.ticker,
                class_code=instrument.primary_board,
            )
            for isin, instrument in by_isin.items()
        }
        created_at = self._now()
        orders = tuple(
            self._exact_order(
                snapshot=snapshot,
                instrument=by_isin[isin],
                quote=_required_quote(by_pair, by_isin[isin]),
                order_book=books[isin],
                side=side,
                target_amount_rub=target_amount,
                created_at=created_at,
            )
            for isin, side, target_amount, _ in targets
        )
        total_buy_cash = sum(
            (order.estimated_cash_rub for order in orders if order.side is Side.BUY),
            start=Decimal("0"),
        )
        if total_buy_cash > snapshot.cash_rub:
            raise TradeProposalError("exact proposed purchases exceed current free cash")
        projected_return = _projected_return(
            report,
            Side.BUY if any(order.side is Side.BUY for order in orders) else Side.SELL,
        )
        projected_stress_loss = _projected_credit_stress_package(
            report,
            tuple(
                (isin, side, order.estimated_cash_rub)
                for (isin, side), order in zip(normalized_actions, orders, strict=True)
            ),
        )
        proposal = ProposalBundle(
            proposal_id=str(uuid.uuid4()),
            portfolio_snapshot_digest=snapshot.digest,
            created_at=created_at,
            expires_at=created_at
            + timedelta(seconds=self._proposal_policy.proposal_ttl_seconds),
            orders=orders,
            rationale=tuple(
                dict.fromkeys(
                    (
                        *(
                            reason
                            for _, _, _, rationale in targets
                            for reason in rationale
                        ),
                        "BCS catalogue, quote and order book verified with a read-only token",
                        "estimated cash includes accrued interest but excludes broker fees",
                    )
                )
            ),
            projected_annual_return=projected_return,
            projected_stress_loss=projected_stress_loss,
        )
        self._investment_policy.validate_proposal(proposal)
        return proposal

    def _exact_order(
        self,
        *,
        snapshot: PortfolioSnapshot,
        instrument: BcsInstrument,
        quote: BcsQuote,
        order_book: BcsOrderBook,
        side: Side,
        target_amount_rub: Decimal,
        created_at: datetime,
    ) -> OrderIntent:
        blockers: list[str] = []
        if instrument.isin == "" or not instrument.is_ruble_bond:
            blockers.append("BCS instrument is not a ruble bond")
        if instrument.is_blocked:
            blockers.append("BCS marks the instrument class as blocked")
        if side is Side.BUY and (
            instrument.is_qualified_only or not instrument.available_for_unqualified
        ):
            blockers.append("BCS does not mark the bond available to an unqualified investor")
        if not quote.trading_is_open:
            blockers.append("BCS reports that trading in the instrument is closed")
        if quote.currency != "RUB":
            blockers.append("BCS quote currency is not RUB")
        if (quote.ticker, quote.class_code) != (
            instrument.ticker,
            instrument.primary_board,
        ):
            blockers.append("BCS quote does not match the resolved instrument")
        if (order_book.ticker, order_book.class_code) != (
            instrument.ticker,
            instrument.primary_board,
        ):
            blockers.append("BCS order book does not match the resolved instrument")
        quote_moment = max(quote.observed_at, order_book.observed_at)
        age_seconds = (created_at - quote_moment).total_seconds()
        if age_seconds < -5 or age_seconds > self._proposal_policy.maximum_quote_age_seconds:
            blockers.append("BCS quote/order book is stale for an exact proposal")
        raw_price = order_book.best_offer if side is Side.BUY else order_book.best_bid
        if raw_price is None:
            blockers.append("BCS order book has no executable top-of-book price")
        if instrument.minimum_step <= 0:
            blockers.append("BCS minimum price step is invalid")
        if instrument.face_value <= 0 or instrument.lot_size <= 0:
            blockers.append("BCS face value or lot size is invalid")
        if blockers:
            raise TradeProposalError("; ".join(blockers))
        assert raw_price is not None

        limit_price = _align_price(raw_price, instrument.minimum_step, side)
        dirty_unit_value = (
            instrument.face_value * limit_price / Decimal("100")
            + instrument.accrued_interest
        )
        dirty_lot_value = dirty_unit_value * instrument.lot_size
        if dirty_lot_value <= 0:
            raise TradeProposalError("calculated dirty lot value is not positive")
        requested_lots = int(
            (target_amount_rub / dirty_lot_value).to_integral_value(rounding=ROUND_FLOOR)
        )
        if requested_lots <= 0:
            raise TradeProposalError("target amount is smaller than one BCS lot")

        if side is Side.SELL:
            position = _find_position(snapshot, instrument)
            available_lots = int(
                (position.available_quantity / Decimal(instrument.lot_size)).to_integral_value(
                    rounding=ROUND_FLOOR
                )
            )
            lots = min(requested_lots, available_lots)
            if lots <= 0:
                raise TradeProposalError("the portfolio has no unlocked full lot to sell")
        else:
            affordable_lots = int(
                (snapshot.cash_rub / dirty_lot_value).to_integral_value(rounding=ROUND_FLOOR)
            )
            lots = min(requested_lots, affordable_lots)
            if lots <= 0:
                raise TradeProposalError("free cash is insufficient for one BCS lot")

        executable_units = sum(
            level.quantity
            for level in (order_book.asks if side is Side.BUY else order_book.bids)
            if (
                level.price <= limit_price
                if side is Side.BUY
                else level.price >= limit_price
            )
        )
        executable_lots = executable_units // instrument.lot_size
        lots = min(lots, executable_lots)
        if lots <= 0:
            raise TradeProposalError(
                "BCS order book has less than one full lot at the limit price"
            )

        estimated_cash = dirty_lot_value * lots
        return OrderIntent(
            instrument_uid=f"BCS:{instrument.primary_board}:{instrument.isin}",
            ticker=instrument.ticker,
            class_code=instrument.primary_board,
            instrument_type=InstrumentType.BOND,
            side=side,
            order_type=OrderType.LIMIT,
            lots=lots,
            limit_price=limit_price,
            currency="RUB",
            lot_size=instrument.lot_size,
            price_step=instrument.minimum_step,
            estimated_cash_rub=estimated_cash,
            quote_observed_at=quote_moment,
            order_valid_until=created_at
            + timedelta(seconds=self._proposal_policy.order_validity_seconds),
        )


class LocalTradeGateStore:
    """Mode-600 local state for pending packages and approval receipts."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.pending = self.root / "pending"
        self.approved = self.root / "approved"
        self.active = self.root / "active.json"

    def save_proposal(self, proposal: ProposalBundle) -> Path:
        payload = {
            "digest": proposal.digest,
            "state": "PENDING_EXPLICIT_CONFIRMATION",
            "proposal": proposal.canonical_payload(),
        }
        path = self.pending / f"{proposal.digest}.json"
        self._atomic_json(path, payload)
        self._atomic_json(
            self.active,
            {
                "digest": proposal.digest,
                "proposal_id": proposal.proposal_id,
                "state": "ACTIVE_PENDING_CONFIRMATION",
            },
        )
        return path

    def load_proposal(self, digest: str) -> ProposalBundle:
        path = self.pending / f"{_validated_digest(digest)}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise TradeProposalError("pending proposal was not found or is invalid") from error
        if not isinstance(payload, dict) or payload.get("digest") != digest:
            raise TradeProposalError("stored proposal digest does not match")
        proposal_payload = payload.get("proposal")
        if not isinstance(proposal_payload, dict):
            raise TradeProposalError("stored proposal payload is invalid")
        proposal = _proposal_from_dict(proposal_payload)
        if proposal.digest != digest:
            raise TradeProposalError("stored exact proposal was modified")
        return proposal

    def save_approval(self, receipt: ConfirmationReceipt) -> Path:
        path = self.approved / f"{receipt.approval.proposal_digest}.json"
        self._atomic_json(path, receipt.as_dict(), exclusive=True)
        return path

    def load_approval(self, digest: str) -> ConfirmationReceipt:
        validated_digest = _validated_digest(digest)
        path = self.approved / f"{validated_digest}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise TradeProposalError(
                "explicit approval receipt was not found or is invalid"
            ) from error
        if not isinstance(payload, dict):
            raise TradeProposalError("stored approval receipt is invalid")
        try:
            approval = Approval(
                approval_id=str(payload["approval_id"]),
                proposal_digest=str(payload["proposal_digest"]),
                approved_by=str(payload["approved_by"]),
                approved_at=_datetime(payload["approved_at"]),
                expires_at=_datetime(payload["approval_expires_at"]),
            )
            receipt = ConfirmationReceipt(
                approval=approval,
                proposal_id=str(payload["proposal_id"]),
                state=str(payload["state"]),
                orders_created=bool(payload["orders_created"]),
                confirmation_mode=str(
                    payload.get("confirmation_mode", "EXACT_DIGEST_TEXT")
                ),
                confirmation_evidence_digest=(
                    None
                    if payload.get("confirmation_evidence_digest") is None
                    else str(payload["confirmation_evidence_digest"])
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise TradeProposalError("stored approval receipt is invalid") from error
        if approval.proposal_digest != validated_digest:
            raise TradeProposalError("stored approval digest does not match")
        if receipt.state != "APPROVED_AWAITING_ISOLATED_EXECUTOR":
            raise TradeProposalError("stored approval is not executable")
        if receipt.orders_created:
            raise TradeProposalError("stored approval unexpectedly claims created orders")
        if receipt.confirmation_mode not in {"EXACT_DIGEST_TEXT", "CODEX_SEMANTIC"}:
            raise TradeProposalError("stored approval confirmation mode is invalid")
        evidence = receipt.confirmation_evidence_digest
        if evidence is not None and (
            len(evidence) != 64
            or any(character not in "0123456789abcdef" for character in evidence)
        ):
            raise TradeProposalError("stored approval confirmation evidence is invalid")
        if receipt.confirmation_mode == "CODEX_SEMANTIC" and evidence is None:
            raise TradeProposalError("semantic approval has no confirmation evidence")
        return receipt

    def approval_exists(self, digest: str) -> bool:
        return (self.approved / f"{_validated_digest(digest)}.json").is_file()

    def active_unapproved_digests(self, *, now: datetime) -> tuple[str, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        try:
            pointer = json.loads(self.active.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ()
        except (OSError, json.JSONDecodeError) as error:
            raise TradeProposalError("active proposal pointer is invalid") from error
        if not isinstance(pointer, dict) or not isinstance(pointer.get("digest"), str):
            raise TradeProposalError("active proposal pointer is invalid")
        digest = pointer["digest"]
        proposal = self.load_proposal(digest)
        if pointer.get("proposal_id") != proposal.proposal_id:
            raise TradeProposalError("active proposal pointer does not match")
        if now >= proposal.expires_at or self.approval_exists(digest):
            return ()
        return (digest,)

    @staticmethod
    def _atomic_json(
        path: Path,
        payload: Mapping[str, Any],
        *,
        exclusive: bool = False,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        descriptor = -1
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            temporary = Path(name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as target:
                descriptor = -1
                json.dump(payload, target, ensure_ascii=False, indent=2)
                target.write("\n")
                target.flush()
                os.fsync(target.fileno())
            if exclusive:
                os.link(temporary, path)
                temporary.unlink()
                temporary = None
            else:
                os.replace(temporary, path)
                temporary = None
            path.chmod(0o600)
        except FileExistsError as error:
            raise TradeProposalError("proposal was already confirmed") from error
        except OSError as error:
            raise TradeProposalError("could not persist local trade-gate state") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class CodexConfirmationGate:
    def __init__(
        self,
        *,
        store: LocalTradeGateStore,
        approval_ttl_seconds: int,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._approval_ttl_seconds = approval_ttl_seconds
        self._now = now or (lambda: datetime.now(tz=UTC))

    def confirm_semantic(
        self,
        *,
        proposal_digest: str,
        user_message: str,
        approved_by: str = "portfolio-owner-via-codex-semantic",
    ) -> ConfirmationReceipt:
        digest = _validated_digest(proposal_digest)
        normalized_message = validate_semantic_confirmation(user_message)
        if self._store.approval_exists(digest):
            raise ApprovalViolation("proposal was already confirmed")
        proposal = self._store.load_proposal(digest)
        _validate_semantic_sides(normalized_message, proposal)
        now = self._now()
        if self._store.active_unapproved_digests(now=now) != (digest,):
            raise ApprovalViolation(
                "semantic confirmation requires exactly one active proposal"
            )
        if now >= proposal.expires_at:
            raise ApprovalViolation("proposal has expired")
        approval_expires = min(
            proposal.expires_at,
            now + timedelta(seconds=self._approval_ttl_seconds),
        )
        approval = Approval(
            approval_id=str(uuid.uuid4()),
            proposal_digest=digest,
            approved_by=approved_by,
            approved_at=now,
            expires_at=approval_expires,
        )
        validate_approval(proposal, approval, now=now)
        receipt = ConfirmationReceipt(
            approval=approval,
            proposal_id=proposal.proposal_id,
            state="APPROVED_AWAITING_ISOLATED_EXECUTOR",
            orders_created=False,
            confirmation_mode="CODEX_SEMANTIC",
            confirmation_evidence_digest=_confirmation_evidence_digest(
                normalized_message
            ),
        )
        self._store.save_approval(receipt)
        return receipt


def proposal_as_dict(proposal: ProposalBundle) -> dict[str, Any]:
    payload = proposal.canonical_payload()
    payload["proposal_digest"] = proposal.digest
    payload["confirmation_mode"] = "CODEX_SEMANTIC"
    side_examples = {
        frozenset({Side.BUY}): "Да, покупаем предложенные активы",
        frozenset({Side.SELL}): "Да, продаем предложенные позиции",
    }
    payload["confirmation_examples"] = [
        "Подтверждаю выставление всех предложенных заявок",
    ]
    side_example = side_examples.get(frozenset(order.side for order in proposal.orders))
    if side_example is not None:
        payload["confirmation_examples"].append(side_example)
    payload["state"] = "PENDING_EXPLICIT_CONFIRMATION"
    payload["orders_created"] = False
    payload["broker_executor_available"] = True
    return payload


def validate_semantic_confirmation(user_message: str) -> str:
    if not isinstance(user_message, str):
        raise ApprovalViolation("semantic confirmation must be text")
    normalized = unicodedata.normalize("NFKC", user_message).casefold().replace("ё", "е")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized or len(normalized) > 500:
        raise ApprovalViolation("semantic confirmation is empty or unexpectedly long")
    if "?" in normalized:
        raise ApprovalViolation("a question cannot authorize a trade")
    if re.search(r"\d", normalized):
        raise ApprovalViolation(
            "semantic confirmation cannot override numeric trade parameters"
        )
    if re.search(
        r"\b(?:не|нет|без|но|кроме|часть\w*|половин\w*|друг\w*|услов\w*|"
        r"отмен\w*|стоп|позже|потом|после|пока|если|когда|возможн\w*|"
        r"наверн\w*|дума\w*|можно|хочу)\b",
        normalized,
    ):
        raise ApprovalViolation("ambiguous or negative wording cannot authorize a trade")
    confirmation = re.search(
        r"\b(?:подтвержда\w*|одобря\w*|соглас(?:ен|на|ны))\b",
        normalized,
    )
    action = re.search(
        r"\b(?:выстав\w*|исполн\w*|покуп\w*|прода\w*|соверш\w*|отправ\w*)\b",
        normalized,
    )
    trade_object = re.search(
        r"\b(?:пакет\w*|заяв\w*|сделк\w*|покупк\w*|продаж\w*|ордер\w*|"
        r"актив\w*|позиц\w*|бумаг\w*)\b",
        normalized,
    )
    if trade_object is None or (confirmation is None and action is None):
        raise ApprovalViolation(
            "semantic confirmation must explicitly authorize the proposed trades"
        )
    return normalized


def _confirmation_evidence_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_semantic_sides(
    normalized_message: str,
    proposal: ProposalBundle,
) -> None:
    proposal_sides = {order.side for order in proposal.orders}
    mentions_buy = re.search(r"\bпокуп\w*", normalized_message) is not None
    mentions_sell = re.search(r"\b(?:прода\w*|продаж\w*)", normalized_message) is not None
    mentioned_sides = {
        side
        for side, mentioned in (
            (Side.BUY, mentions_buy),
            (Side.SELL, mentions_sell),
        )
        if mentioned
    }
    if not mentioned_sides:
        return
    if mentioned_sides != proposal_sides:
        raise ApprovalViolation(
            "semantic confirmation side does not match all proposed trades"
        )


def _manager_target(
    report: PortfolioManagerReport,
    isin: str,
    side: Side,
) -> tuple[Decimal, tuple[str, ...]]:
    for decision in report.decisions:
        if decision.isin != isin:
            continue
        amount = (
            decision.recommended_reduce_rub
            if side is Side.SELL
            else decision.recommended_add_rub
        )
        if amount <= 0:
            raise TradeProposalError("manager report has no matching positive action amount")
        return amount, decision.reasons
    if side is Side.BUY:
        for candidate in report.new_bond_candidates:
            if candidate.isin == isin and candidate.recommended_add_rub > 0:
                return candidate.recommended_add_rub, candidate.reasons
    raise TradeProposalError("manager report has no matching exact action")


def _projected_return(report: PortfolioManagerReport, side: Side) -> Decimal:
    if side is Side.BUY:
        invested = next(
            (
                scenario.comparable_bond_yield_percent
                for scenario in report.scenarios
                if scenario.code == "INVEST_CURRENT_CASH"
            ),
            None,
        )
        if invested is not None:
            return invested / Decimal("100")
    current = report.current_comparable_bond_yield_percent
    return Decimal("0") if current is None else current / Decimal("100")


def _projected_credit_stress_package(
    report: PortfolioManagerReport,
    actions: tuple[tuple[str, Side, Decimal], ...],
) -> Decimal:
    by_isin = {isin: (side, amount) for isin, side, amount in actions}
    stressed_value = Decimal("0")
    for decision in report.decisions:
        shock = _rating_shock(
            None if decision.broad_rating_band is None else decision.broad_rating_band.value
        )
        value = decision.market_value_rub
        action = by_isin.get(decision.isin)
        if action is not None:
            side, amount = action
            value = (
                max(Decimal("0"), value - amount)
                if side is Side.SELL
                else value + amount
            )
        stressed_value += value * shock
    current_isins = {decision.isin for decision in report.decisions}
    for isin, (side, amount) in by_isin.items():
        if side is not Side.BUY or isin in current_isins:
            continue
        candidate = next(
            (item for item in report.new_bond_candidates if item.isin == isin),
            None,
        )
        if candidate is not None:
            stressed_value += amount * _rating_shock(candidate.broad_rating_band.value)
    return (
        Decimal("0")
        if report.managed_value_rub == 0
        else stressed_value / report.managed_value_rub
    )


def _rating_shock(value: str | None) -> Decimal:
    return {
        "HIGHEST": Decimal("0.02"),
        "HIGH": Decimal("0.04"),
        "STRONG": Decimal("0.07"),
        "ADEQUATE": Decimal("0.15"),
        "SPECULATIVE": Decimal("0.50"),
        "UNRATED": Decimal("1.00"),
        None: Decimal("1.00"),
    }[value]


def _align_price(price: Decimal, step: Decimal, side: Side) -> Decimal:
    rounding = ROUND_CEILING if side is Side.BUY else ROUND_FLOOR
    steps = (price / step).to_integral_value(rounding=rounding)
    return steps * step


def _find_position(snapshot: PortfolioSnapshot, instrument: BcsInstrument):
    matches = [
        position
        for position in snapshot.positions
        if position.instrument_type is InstrumentType.BOND
        and position.ticker in {instrument.ticker, instrument.isin}
        and position.class_code == instrument.primary_board
    ]
    if len(matches) != 1:
        raise TradeProposalError("portfolio does not contain exactly one matching bond position")
    return matches[0]


def _required_quote(
    quotes: Mapping[tuple[str, str], BcsQuote],
    instrument: BcsInstrument,
) -> BcsQuote:
    try:
        return quotes[(instrument.ticker, instrument.primary_board)]
    except KeyError as error:
        raise TradeProposalError("BCS did not return every current quote once") from error


def _validated_digest(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise TradeProposalError("proposal digest must be a lowercase SHA-256 value")
    return value


def _proposal_from_dict(payload: Mapping[str, Any]) -> ProposalBundle:
    try:
        orders = tuple(_order_from_dict(item) for item in payload["orders"])
        rationale = tuple(str(item) for item in payload["rationale"])
        return ProposalBundle(
            proposal_id=str(payload["proposal_id"]),
            portfolio_snapshot_digest=str(payload["portfolio_snapshot_digest"]),
            created_at=_datetime(payload["created_at"]),
            expires_at=_datetime(payload["expires_at"]),
            orders=orders,
            rationale=rationale,
            projected_annual_return=Decimal(str(payload["projected_annual_return"])),
            projected_stress_loss=Decimal(str(payload["projected_stress_loss"])),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise TradeProposalError("stored proposal payload is invalid") from error


def _order_from_dict(payload: Mapping[str, Any]) -> OrderIntent:
    return OrderIntent(
        instrument_uid=str(payload["instrument_uid"]),
        ticker=str(payload["ticker"]),
        class_code=str(payload["class_code"]),
        instrument_type=InstrumentType(str(payload["instrument_type"])),
        side=Side(str(payload["side"])),
        order_type=OrderType(str(payload["order_type"])),
        lots=int(payload["lots"]),
        limit_price=(
            None if payload["limit_price"] is None else Decimal(str(payload["limit_price"]))
        ),
        currency=str(payload["currency"]),
        lot_size=int(payload["lot_size"]),
        price_step=Decimal(str(payload["price_step"])),
        estimated_cash_rub=Decimal(str(payload["estimated_cash_rub"])),
        quote_observed_at=_datetime(payload["quote_observed_at"]),
        order_valid_until=_datetime(payload["order_valid_until"]),
        tradable=bool(payload["tradable"]),
        blocked_reason=(
            None if payload["blocked_reason"] is None else str(payload["blocked_reason"])
        ),
    )


def _datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("datetime value must be text")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("datetime value must be timezone-aware")
    return result
