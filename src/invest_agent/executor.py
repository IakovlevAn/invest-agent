"""Approval-gated, idempotent execution of persisted exact BCS packages."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from invest_agent.approval import validate_approval
from invest_agent.brokers.bcs import BcsAccessToken, BcsReadClient
from invest_agent.brokers.bcs_trade import (
    BcsOrderNotFound,
    BcsOrderState,
    BcsTradeClient,
)
from invest_agent.domain import OrderIntent, OrderType, PortfolioSnapshot, ProposalBundle, Side
from invest_agent.policy import InvestmentPolicy
from invest_agent.portfolio import BcsPortfolioNormalizer
from invest_agent.secrets import RefreshTokenStore
from invest_agent.trade_proposal import (
    ConfirmationReceipt,
    LocalTradeGateStore,
    TradeProposalError,
    TradeProposalPolicy,
)

ORDER_ID_NAMESPACE = uuid.UUID("6b7518c0-dddf-4870-8ae2-35adcd965b66")


class ExecutionViolation(ValueError):
    """Execution was stopped before or during broker submission."""


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    proposal_digest: str
    state: str
    started_at: datetime
    updated_at: datetime
    orders: tuple[Mapping[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_digest": self.proposal_digest,
            "state": self.state,
            "started_at": self.started_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "orders": [dict(order) for order in self.orders],
        }


class LocalExecutionStore:
    """Mode-600 append-by-replacement journal with a one-time initial claim."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def claim(
        self,
        proposal: ProposalBundle,
        receipt: ConfirmationReceipt,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        path = self._path(proposal.digest)
        if path.exists():
            journal = self.load(proposal.digest)
            if (
                journal.get("proposal_id") != proposal.proposal_id
                or journal.get("approval_id") != receipt.approval.approval_id
            ):
                raise ExecutionViolation("execution journal does not match the approval")
            return journal
        journal: dict[str, Any] = {
            "schema_version": 1,
            "proposal_digest": proposal.digest,
            "proposal_id": proposal.proposal_id,
            "approval_id": receipt.approval.approval_id,
            "state": "CLAIMED",
            "started_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "orders": [
                {
                    "index": index,
                    "client_order_id": _order_client_id(proposal.digest, index),
                    "cancel_client_id": _cancel_client_id(proposal.digest, index),
                    "state": "PLANNED",
                    "ticker": order.ticker,
                    "class_code": order.class_code,
                    "side": order.side.value,
                    "quantity_units": order.quantity_units,
                    "limit_price": str(order.limit_price),
                }
                for index, order in enumerate(proposal.orders)
            ],
        }
        self._atomic_json(path, journal, exclusive=True)
        claimed = self.load(proposal.digest)
        if (
            claimed.get("proposal_id") != proposal.proposal_id
            or claimed.get("approval_id") != receipt.approval.approval_id
        ):
            raise ExecutionViolation("execution journal does not match the approval")
        return claimed

    def load(self, digest: str) -> dict[str, Any]:
        path = self._path(digest)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ExecutionViolation("execution journal was not found or is invalid") from error
        if not isinstance(payload, dict) or payload.get("proposal_digest") != digest:
            raise ExecutionViolation("execution journal digest does not match")
        if not isinstance(payload.get("orders"), list):
            raise ExecutionViolation("execution journal orders are invalid")
        return payload

    def save(self, journal: Mapping[str, Any]) -> None:
        digest = journal.get("proposal_digest")
        if not isinstance(digest, str):
            raise ExecutionViolation("execution journal has no digest")
        self._atomic_json(self._path(digest), journal)

    def report(self, digest: str) -> ExecutionReport:
        journal = self.load(digest)
        try:
            return ExecutionReport(
                proposal_digest=digest,
                state=str(journal["state"]),
                started_at=_aware_datetime(journal["started_at"]),
                updated_at=_aware_datetime(journal["updated_at"]),
                orders=tuple(dict(item) for item in journal["orders"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ExecutionViolation("execution journal is invalid") from error

    def _path(self, digest: str) -> Path:
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ExecutionViolation("proposal digest must be a lowercase SHA-256 value")
        return self.root / f"{digest}.json"

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
        except FileExistsError:
            # Another process won the claim. The caller will resume it on retry.
            if exclusive:
                return
            raise
        except OSError as error:
            raise ExecutionViolation("could not persist execution journal") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class ExactPackageExecutor:
    """Execute only the persisted package referenced by an exact approval receipt."""

    TERMINAL_JOURNAL_STATES = {
        "COMPLETE",
        "CANCEL_REQUESTED",
        "BLOCKED_BEFORE_SUBMISSION",
        "FAILED_PARTIAL",
    }

    def __init__(
        self,
        *,
        gate_store: LocalTradeGateStore,
        execution_store: LocalExecutionStore,
        read_client: BcsReadClient,
        trade_client: BcsTradeClient,
        read_token_store: RefreshTokenStore,
        trade_token_store: RefreshTokenStore,
        normalizer: BcsPortfolioNormalizer,
        investment_policy: InvestmentPolicy,
        proposal_policy: TradeProposalPolicy,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        poll_seconds: float = 2.0,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self._gate_store = gate_store
        self._execution_store = execution_store
        self._read_client = read_client
        self._trade_client = trade_client
        self._read_token_store = read_token_store
        self._trade_token_store = trade_token_store
        self._normalizer = normalizer
        self._investment_policy = investment_policy
        self._proposal_policy = proposal_policy
        self._now = now or (lambda: datetime.now(tz=UTC))
        if sleep is None:
            import time

            self._sleep = time.sleep
        else:
            self._sleep = sleep
        self._poll_seconds = poll_seconds
        self._trade_access_token: BcsAccessToken | None = None

    def execute(self, proposal_digest: str) -> ExecutionReport:
        proposal = self._gate_store.load_proposal(proposal_digest)
        receipt = self._gate_store.load_approval(proposal_digest)
        now = self._now()
        validate_approval(proposal, receipt.approval, now=now)
        self._investment_policy.validate_proposal(proposal)
        journal = self._execution_store.claim(proposal, receipt, now=now)
        if journal["state"] in self.TERMINAL_JOURNAL_STATES:
            return self._execution_store.report(proposal_digest)

        try:
            snapshot, read_access = self._refresh_read_session()
            self._preflight(proposal, snapshot, read_access)
            validate_approval(proposal, receipt.approval, now=self._now())
            journal["state"] = "PREFLIGHT_PASSED"
            self._save(journal)
            trade_access = self._refresh_trade_session()
            self._verify_trade_account(snapshot.account_ref, trade_access)
            validate_approval(proposal, receipt.approval, now=self._now())
            self._submit_all(proposal, journal)
            self._monitor_all(proposal, journal)
        except Exception as error:
            if any(item.get("state") != "PLANNED" for item in journal["orders"]):
                self._emergency_cancel(proposal, journal)
                journal["state"] = "FAILED_PARTIAL"
            else:
                journal["state"] = "BLOCKED_BEFORE_SUBMISSION"
            journal["error"] = _safe_error_name(error)
            self._save(journal)
            if isinstance(error, (ExecutionViolation, TradeProposalError)):
                raise
            raise ExecutionViolation("broker execution failed; see local safe journal") from error
        return self._execution_store.report(proposal_digest)

    def reconcile(self, proposal_digest: str) -> ExecutionReport:
        """Refresh/cancel existing broker orders; never create a new order."""
        proposal = self._gate_store.load_proposal(proposal_digest)
        journal = self._execution_store.load(proposal_digest)
        if journal.get("proposal_id") != proposal.proposal_id:
            raise ExecutionViolation("execution journal does not match the proposal")
        submitted = [
            record
            for record in journal["orders"]
            if record.get("state") not in {"PLANNED", "BLOCKED_BEFORE_SUBMISSION"}
        ]
        if not submitted:
            return self._execution_store.report(proposal_digest)

        self._refresh_trade_session()
        needs_attention = False
        active = False
        cancel_requested = False
        for order, record in zip(proposal.orders, journal["orders"], strict=True):
            if record not in submitted:
                continue
            try:
                state = self._trade_client.get_order_status(
                    self._trade_token(),
                    client_order_id=str(record["client_order_id"]),
                )
            except BcsOrderNotFound:
                record["state"] = "UNKNOWN_AT_BROKER"
                needs_attention = True
                continue
            state.validate_against(order)
            _record_status(record, state)
            if state.is_terminal:
                continue
            active = True
            if self._now() >= order.order_valid_until:
                self._cancel_record(record)
                cancel_requested = True

        if needs_attention:
            journal["state"] = "NEEDS_ATTENTION"
        elif cancel_requested:
            journal["state"] = "CANCEL_REQUESTED"
        elif active:
            journal["state"] = "ACTIVE"
        else:
            journal["state"] = "COMPLETE"
        self._save(journal)
        return self._execution_store.report(proposal_digest)

    def _refresh_read_session(self) -> tuple[PortfolioSnapshot, BcsAccessToken]:
        pair = self._read_client.exchange_read_only_refresh_token(
            self._read_token_store.get()
        )
        self._read_token_store.set(pair.refresh_token)
        raw = self._read_client.fetch_raw_portfolio(pair.access_token)
        return self._normalizer.normalize(raw, is_iis=True), pair.access_token

    def _refresh_trade_session(self) -> BcsAccessToken:
        pair = self._trade_client.exchange_trade_refresh_token(
            self._trade_token_store.get()
        )
        # Rotation is persisted before any order or status call.
        self._trade_token_store.set(pair.refresh_token)
        self._trade_access_token = pair.access_token
        return pair.access_token

    def _trade_token(self) -> BcsAccessToken:
        token = self._trade_access_token
        if token is None or token.expires_at <= self._now():
            return self._refresh_trade_session()
        return token

    def _verify_trade_account(
        self,
        expected_account_ref: str,
        access_token: BcsAccessToken,
    ) -> None:
        raw = self._read_client.fetch_raw_portfolio(access_token)
        snapshot = self._normalizer.normalize(raw, is_iis=True)
        if snapshot.account_ref != expected_account_ref:
            raise ExecutionViolation(
                "trade token and read-only token refer to different BCS accounts"
            )

    def _preflight(
        self,
        proposal: ProposalBundle,
        snapshot: PortfolioSnapshot,
        access_token: BcsAccessToken,
    ) -> None:
        now = self._now()
        if now >= proposal.expires_at:
            raise ExecutionViolation("proposal expired before execution preflight")
        isins = tuple(_order_isin(order) for order in proposal.orders)
        instruments = self._read_client.fetch_instruments_by_isins(access_token, isins)
        by_isin = {instrument.isin: instrument for instrument in instruments}
        if len(by_isin) != len(isins):
            raise ExecutionViolation("BCS catalogue did not resolve every approved instrument")
        quotes = self._read_client.fetch_quotes(
            access_token,
            tuple((order.ticker, order.class_code) for order in proposal.orders),
        )
        by_pair = {(quote.ticker, quote.class_code): quote for quote in quotes}
        if len(by_pair) != len(proposal.orders):
            raise ExecutionViolation("BCS did not return every approved live quote")

        current_buy_cash = Decimal("0")
        for order, isin in zip(proposal.orders, isins, strict=True):
            self._investment_policy.validate_order(order)
            if order.order_type is not OrderType.LIMIT or order.limit_price is None:
                raise ExecutionViolation("executor accepts limit orders only")
            if now >= order.order_valid_until:
                raise ExecutionViolation("approved order validity has expired")
            instrument = by_isin.get(isin)
            if instrument is None:
                raise ExecutionViolation("approved instrument is unavailable in BCS")
            if (
                instrument.ticker != order.ticker
                or instrument.primary_board != order.class_code
                or not instrument.is_ruble_bond
                or instrument.is_blocked
                or instrument.lot_size != order.lot_size
                or instrument.minimum_step != order.price_step
            ):
                raise ExecutionViolation("current BCS instrument contract changed")
            if order.side is Side.BUY and (
                instrument.is_qualified_only
                or not instrument.available_for_unqualified
            ):
                raise ExecutionViolation("approved bond is not available to this investor")
            if order.side is Side.BUY:
                current_buy_cash += (
                    instrument.face_value * order.limit_price / Decimal("100")
                    + instrument.accrued_interest
                ) * order.quantity_units
            if order.side is Side.SELL:
                matches = [
                    position
                    for position in snapshot.positions
                    if position.ticker in {order.ticker, isin}
                    and position.class_code == order.class_code
                ]
                if (
                    len(matches) != 1
                    or not matches[0].tradable
                    or matches[0].available_quantity < Decimal(order.quantity_units)
                ):
                    raise ExecutionViolation("current unlocked position is insufficient to sell")

            quote = by_pair.get((order.ticker, order.class_code))
            if quote is None or not quote.trading_is_open or quote.currency != "RUB":
                raise ExecutionViolation("BCS trading session is closed or quote is invalid")
            book = self._read_client.fetch_order_book(
                access_token,
                ticker=order.ticker,
                class_code=order.class_code,
            )
            if (book.ticker, book.class_code) != (order.ticker, order.class_code):
                raise ExecutionViolation("BCS order book does not match the approved order")
            for observed_at in (quote.observed_at, book.observed_at):
                age = (now - observed_at).total_seconds()
                if age < -5 or age > self._proposal_policy.maximum_quote_age_seconds:
                    raise ExecutionViolation("BCS quote or order book is stale")
            levels = book.asks if order.side is Side.BUY else book.bids
            executable_units = sum(
                level.quantity
                for level in levels
                if (
                    level.price <= order.limit_price
                    if order.side is Side.BUY
                    else level.price >= order.limit_price
                )
            )
            if executable_units < order.quantity_units:
                raise ExecutionViolation(
                    "current BCS order book cannot fill the exact approved quantity at its limit"
                )
        if current_buy_cash > snapshot.cash_rub:
            raise ExecutionViolation("current free cash is below the approved package estimate")

    def _submit_all(
        self,
        proposal: ProposalBundle,
        journal: dict[str, Any],
    ) -> None:
        for order, record in zip(proposal.orders, journal["orders"], strict=True):
            client_order_id = str(record["client_order_id"])
            try:
                state = self._trade_client.get_order_status(
                    self._trade_token(), client_order_id=client_order_id
                )
            except BcsOrderNotFound:
                record["state"] = "SUBMISSION_INTENT"
                self._save(journal)
                ack = self._trade_client.create_limit_order(
                    self._trade_token(),
                    order=order,
                    client_order_id=client_order_id,
                )
                record["state"] = "SUBMITTED"
                record["broker_ack_status"] = ack.status
            else:
                state.validate_against(order)
                _record_status(record, state)
            self._save(journal)
        journal["state"] = "SUBMITTED"
        self._save(journal)

    def _monitor_all(
        self,
        proposal: ProposalBundle,
        journal: dict[str, Any],
    ) -> None:
        while True:
            active = False
            for order, record in zip(proposal.orders, journal["orders"], strict=True):
                try:
                    state = self._trade_client.get_order_status(
                        self._trade_token(),
                        client_order_id=str(record["client_order_id"]),
                    )
                except BcsOrderNotFound:
                    active = True
                    continue
                state.validate_against(order)
                _record_status(record, state)
                if state.is_terminal:
                    continue
                active = True
                if self._now() >= order.order_valid_until or not state.is_active:
                    self._cancel_record(record)
            self._save(journal)
            if not active:
                journal["state"] = "COMPLETE"
                self._save(journal)
                return
            if all(self._now() >= order.order_valid_until for order in proposal.orders):
                for record in journal["orders"]:
                    if record.get("state") not in {
                        "FILLED",
                        "CANCELLED",
                        "REJECTED",
                        "CANCEL_REQUESTED",
                    }:
                        self._cancel_record(record)
                journal["state"] = "CANCEL_REQUESTED"
                self._save(journal)
                return
            self._sleep(self._poll_seconds)

    def _cancel_record(self, record: dict[str, Any]) -> None:
        if "cancel_ack_status" in record:
            return
        ack = self._trade_client.cancel_order(
            self._trade_token(),
            order_client_id=str(record["client_order_id"]),
            cancel_client_id=str(record["cancel_client_id"]),
        )
        record["state"] = "CANCEL_REQUESTED"
        record["cancel_ack_status"] = ack.status

    def _emergency_cancel(
        self,
        proposal: ProposalBundle,
        journal: dict[str, Any],
    ) -> None:
        for order, record in zip(proposal.orders, journal["orders"], strict=True):
            if record.get("state") == "PLANNED":
                continue
            try:
                state = self._trade_client.get_order_status(
                    self._trade_token(),
                    client_order_id=str(record["client_order_id"]),
                )
                state.validate_against(order)
                _record_status(record, state)
                if not state.is_terminal:
                    self._cancel_record(record)
            except Exception as error:
                record["emergency_cancel_error"] = _safe_error_name(error)
            self._save(journal)

    def _save(self, journal: dict[str, Any]) -> None:
        journal["updated_at"] = self._now().isoformat()
        self._execution_store.save(journal)


def _order_isin(order: OrderIntent) -> str:
    prefix = f"BCS:{order.class_code}:"
    if not order.instrument_uid.startswith(prefix):
        raise ExecutionViolation("approved order has an invalid BCS instrument identifier")
    isin = order.instrument_uid[len(prefix) :]
    if len(isin) != 12 or not isin.isalnum() or isin != isin.upper():
        raise ExecutionViolation("approved order has an invalid ISIN")
    return isin


def _order_client_id(digest: str, index: int) -> str:
    return str(uuid.uuid5(ORDER_ID_NAMESPACE, f"order:{digest}:{index}"))


def _cancel_client_id(digest: str, index: int) -> str:
    return str(uuid.uuid5(ORDER_ID_NAMESPACE, f"cancel:{digest}:{index}"))


def _record_status(record: dict[str, Any], state: BcsOrderState) -> None:
    record.update(
        {
            "state": _journal_order_state(state),
            "broker_order_status": state.order_status,
            "broker_execution_type": state.execution_type,
            "executed_quantity": state.executed_quantity,
            "remained_quantity": state.remained_quantity,
            "broker_order_id": state.order_id,
            "transaction_time": state.transaction_time.isoformat(),
            "reject_reason": state.reject_reason,
        }
    )


def _journal_order_state(state: BcsOrderState) -> str:
    if state.executed_quantity == state.order_quantity and state.remained_quantity == 0:
        return "FILLED"
    if state.order_status == "4":
        return "CANCELLED"
    if state.order_status == "5":
        return "REPLACED"
    if state.order_status == "8":
        return "REJECTED"
    return "ACTIVE" if state.is_active else "UNKNOWN"


def _aware_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("datetime value must be text")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("datetime value must be timezone-aware")
    return parsed


def _safe_error_name(error: Exception) -> str:
    return type(error).__name__
