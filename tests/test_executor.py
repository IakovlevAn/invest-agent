from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from invest_agent.brokers.bcs import (
    BcsAccessToken,
    BcsBoard,
    BcsInstrument,
    BcsOrderBook,
    BcsOrderBookLevel,
    BcsQuote,
    BcsTokenPair,
)
from invest_agent.brokers.bcs_trade import (
    BcsOperationAck,
    BcsOrderNotFound,
    BcsOrderState,
)
from invest_agent.domain import (
    InstrumentType,
    OrderIntent,
    OrderType,
    PortfolioSnapshot,
    ProposalBundle,
    Side,
)
from invest_agent.executor import (
    ExactPackageExecutor,
    ExecutionViolation,
    LocalExecutionStore,
)
from invest_agent.policy import InvestmentPolicy
from invest_agent.trade_proposal import (
    CodexConfirmationGate,
    LocalTradeGateStore,
    TradeProposalError,
    TradeProposalPolicy,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
ISIN = "RU000A000001"
SECOND_ISIN = "RU000A000002"
POLICY_PATH = Path(__file__).parents[1] / "config" / "investment_policy.toml"


class FakeTokenStore:
    def __init__(self, token: str, events: list[str]) -> None:
        self.token = token
        self.events = events

    def get(self) -> str:
        self.events.append("token-get")
        return self.token

    def set(self, token: str) -> None:
        self.token = token
        self.events.append("token-set")


class FakeNormalizer:
    def __init__(self, snapshot: PortfolioSnapshot) -> None:
        self.snapshot = snapshot

    def normalize(self, raw, *, is_iis: bool) -> PortfolioSnapshot:
        return self.snapshot


class AlternatingNormalizer(FakeNormalizer):
    def __init__(self, first: PortfolioSnapshot, second: PortfolioSnapshot) -> None:
        super().__init__(first)
        self.second = second
        self.calls = 0

    def normalize(self, raw, *, is_iis: bool) -> PortfolioSnapshot:
        self.calls += 1
        return self.snapshot if self.calls == 1 else self.second


class FakeReadClient:
    def __init__(self, *, shallow_book: bool = False) -> None:
        self.shallow_book = shallow_book

    def exchange_read_only_refresh_token(self, refresh_token: str) -> BcsTokenPair:
        return token_pair("read")

    def fetch_raw_portfolio(self, access_token: BcsAccessToken):
        return {"unused": True}

    def fetch_instruments_by_isins(self, access_token, isins):
        return tuple(instrument(isin) for isin in isins)

    def fetch_quotes(self, access_token, instruments):
        return tuple(
            BcsQuote(
                ticker=ticker,
                class_code=class_code,
                observed_at=NOW - timedelta(seconds=1),
                security_trading_status=17,
                currency="RUB",
                bid=Decimal("99.90"),
                offer=Decimal("100.10"),
                last=Decimal("100"),
                bid_yield=Decimal("20"),
                offer_yield=Decimal("20"),
            )
            for ticker, class_code in instruments
        )

    def fetch_order_book(self, access_token, *, ticker, class_code):
        quantity = 1 if self.shallow_book else 100
        return BcsOrderBook(
            ticker=ticker,
            class_code=class_code,
            observed_at=NOW - timedelta(seconds=1),
            bids=(BcsOrderBookLevel(Decimal("99.90"), 100),),
            asks=(BcsOrderBookLevel(Decimal("100.10"), quantity),),
        )


class FakeTradeClient:
    def __init__(
        self,
        events: list[str],
        *,
        active: bool = False,
        fail_on_create_number: int | None = None,
    ) -> None:
        self.events = events
        self.active = active
        self.fail_on_create_number = fail_on_create_number
        self.created: dict[str, OrderIntent] = {}
        self.create_count = 0
        self.cancelled: list[str] = []

    def exchange_trade_refresh_token(self, refresh_token: str) -> BcsTokenPair:
        self.events.append("trade-exchange")
        return token_pair("trade")

    def get_order_status(self, access_token, *, client_order_id: str):
        order = self.created.get(client_order_id)
        if order is None:
            raise BcsOrderNotFound("order-status", 404)
        status = "1" if self.active else "2"
        executed = 0 if self.active else order.quantity_units
        remained = order.quantity_units if self.active else 0
        return BcsOrderState(
            client_order_id=client_order_id,
            order_status=status,
            execution_type="12",
            order_quantity=order.quantity_units,
            executed_quantity=executed,
            remained_quantity=remained,
            ticker=order.ticker,
            class_code=order.class_code,
            side="1" if order.side is Side.BUY else "2",
            order_type="2",
            price=order.limit_price or Decimal("0"),
            currency="RUB",
            order_id=f"broker-{client_order_id}",
            transaction_time=NOW,
            reject_reason=None,
        )

    def create_limit_order(self, access_token, *, order, client_order_id):
        self.events.append("create")
        if "token-set" not in self.events[self.events.index("trade-exchange") + 1 :]:
            raise AssertionError("rotated trade token was not stored before the order")
        self.create_count += 1
        if self.create_count == self.fail_on_create_number:
            raise RuntimeError("simulated broker failure")
        self.created[client_order_id] = order
        return BcsOperationAck(client_order_id, "OK")

    def cancel_order(self, access_token, *, order_client_id, cancel_client_id):
        self.events.append("cancel")
        self.cancelled.append(order_client_id)
        return BcsOperationAck(cancel_client_id, "OK")


def token_pair(label: str) -> BcsTokenPair:
    return BcsTokenPair(
        access_token=BcsAccessToken(f"{label}-access", NOW + timedelta(hours=2)),
        refresh_token=f"{label}-rotated-refresh",
        refresh_expires_at=NOW + timedelta(days=1),
    )


def instrument(isin: str) -> BcsInstrument:
    return BcsInstrument(
        ticker=isin,
        isin=isin,
        display_name="Тестовая облигация",
        instrument_type="BONDS",
        boards=(BcsBoard("TQCB", "MOEX"),),
        primary_board="TQCB",
        trading_currency="RUB",
        settlement_currency="RUB",
        face_value=Decimal("1000"),
        lot_size=1,
        minimum_step=Decimal("0.01"),
        accrued_interest=Decimal("10"),
        scale=2,
        is_blocked=False,
        is_qualified_only=False,
        available_for_unqualified=True,
        coupon_type_name="Постоянный",
    )


def order(isin: str = ISIN, *, valid_seconds: int = 3600) -> OrderIntent:
    return OrderIntent(
        instrument_uid=f"BCS:TQCB:{isin}",
        ticker=isin,
        class_code="TQCB",
        instrument_type=InstrumentType.BOND,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        lots=5,
        limit_price=Decimal("100.10"),
        currency="RUB",
        lot_size=1,
        price_step=Decimal("0.01"),
        estimated_cash_rub=Decimal("5055"),
        quote_observed_at=NOW - timedelta(seconds=2),
        order_valid_until=NOW + timedelta(seconds=valid_seconds),
    )


def proposal(*orders: OrderIntent, ttl_seconds: int = 600) -> ProposalBundle:
    return ProposalBundle(
        proposal_id="proposal-test",
        portfolio_snapshot_digest="snapshot-test",
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=ttl_seconds),
        orders=orders or (order(),),
        rationale=("test",),
        projected_annual_return=Decimal("0.20"),
        projected_stress_loss=Decimal("0.05"),
    )


def snapshot() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        account_ref="bcs:test",
        is_iis=True,
        as_of=NOW,
        cash_rub=Decimal("50000"),
        positions=(),
    )


class MutableClock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class ExactPackageExecutorTests(unittest.TestCase):
    def _executor(
        self,
        directory: str,
        package: ProposalBundle,
        *,
        approve: bool = True,
        read_client: FakeReadClient | None = None,
        trade_client: FakeTradeClient | None = None,
        clock: MutableClock | None = None,
        normalizer: FakeNormalizer | None = None,
    ):
        root = Path(directory)
        gate_store = LocalTradeGateStore(root / "gate")
        gate_store.save_proposal(package)
        if approve:
            CodexConfirmationGate(
                store=gate_store,
                approval_ttl_seconds=600,
                now=(clock.now if clock else lambda: NOW),
            ).confirm(
                proposal_digest=package.digest,
                confirmation_text=CodexConfirmationGate.required_text(package.digest),
            )
        events: list[str] = []
        trade = trade_client or FakeTradeClient(events)
        if trade_client is not None:
            events = trade_client.events
        effective_clock = clock or MutableClock()
        executor = ExactPackageExecutor(
            gate_store=gate_store,
            execution_store=LocalExecutionStore(root / "executions"),
            read_client=read_client or FakeReadClient(),
            trade_client=trade,
            read_token_store=FakeTokenStore("read-refresh", events),
            trade_token_store=FakeTokenStore("trade-refresh", events),
            normalizer=normalizer or FakeNormalizer(snapshot()),
            investment_policy=InvestmentPolicy.from_toml(POLICY_PATH),
            proposal_policy=TradeProposalPolicy.from_toml(POLICY_PATH),
            now=effective_clock.now,
            sleep=effective_clock.sleep,
            poll_seconds=2,
        )
        return executor, trade, events

    def test_exact_approval_submits_once_and_retry_does_not_duplicate(self) -> None:
        package = proposal(order())
        with tempfile.TemporaryDirectory() as directory:
            executor, trade, events = self._executor(directory, package)

            first = executor.execute(package.digest)
            second = executor.execute(package.digest)

        self.assertEqual(first.state, "COMPLETE")
        self.assertEqual(second.state, "COMPLETE")
        self.assertEqual(trade.create_count, 1)
        self.assertLess(events.index("trade-exchange"), events.index("create"))

    def test_absent_approval_blocks_before_trade_authorization(self) -> None:
        package = proposal(order())
        with tempfile.TemporaryDirectory() as directory:
            executor, trade, events = self._executor(directory, package, approve=False)

            with self.assertRaises(TradeProposalError):
                executor.execute(package.digest)

        self.assertEqual(trade.create_count, 0)
        self.assertNotIn("trade-exchange", events)

    def test_changed_order_book_blocks_entire_package_before_trade_authorization(self) -> None:
        package = proposal(order())
        with tempfile.TemporaryDirectory() as directory:
            executor, trade, events = self._executor(
                directory,
                package,
                read_client=FakeReadClient(shallow_book=True),
            )

            with self.assertRaisesRegex(ExecutionViolation, "exact approved quantity"):
                executor.execute(package.digest)

        self.assertEqual(trade.create_count, 0)
        self.assertNotIn("trade-exchange", events)

    def test_different_trade_token_account_blocks_before_order(self) -> None:
        package = proposal(order())
        other_account = replace(snapshot(), account_ref="bcs:other")
        with tempfile.TemporaryDirectory() as directory:
            executor, trade, events = self._executor(
                directory,
                package,
                normalizer=AlternatingNormalizer(snapshot(), other_account),
            )

            with self.assertRaisesRegex(ExecutionViolation, "different BCS accounts"):
                executor.execute(package.digest)

        self.assertEqual(trade.create_count, 0)
        self.assertIn("trade-exchange", events)

    def test_later_submission_failure_requests_cancel_for_earlier_order(self) -> None:
        events: list[str] = []
        trade = FakeTradeClient(events, active=True, fail_on_create_number=2)
        package = proposal(order(), order(SECOND_ISIN))
        with tempfile.TemporaryDirectory() as directory:
            executor, _, _ = self._executor(
                directory,
                package,
                trade_client=trade,
            )

            with self.assertRaisesRegex(ExecutionViolation, "broker execution failed"):
                executor.execute(package.digest)

        self.assertEqual(trade.create_count, 2)
        self.assertEqual(len(trade.cancelled), 1)

    def test_deadline_requests_cancel_for_active_order(self) -> None:
        clock = MutableClock()
        events: list[str] = []
        trade = FakeTradeClient(events, active=True)
        package = proposal(order(valid_seconds=3), ttl_seconds=2)
        with tempfile.TemporaryDirectory() as directory:
            executor, _, _ = self._executor(
                directory,
                package,
                trade_client=trade,
                clock=clock,
            )

            report = executor.execute(package.digest)

        self.assertEqual(report.state, "CANCEL_REQUESTED")
        self.assertEqual(len(trade.cancelled), 1)

    def test_reconcile_after_approval_expiry_never_creates_and_cancels_overdue(self) -> None:
        clock = MutableClock()
        events: list[str] = []
        trade = FakeTradeClient(events, active=True)
        package = proposal(order(valid_seconds=3600))
        with tempfile.TemporaryDirectory() as directory:
            executor, _, _ = self._executor(
                directory,
                package,
                trade_client=trade,
                clock=clock,
            )
            gate = executor._gate_store
            journal = executor._execution_store.claim(
                package,
                gate.load_approval(package.digest),
                now=NOW,
            )
            record = journal["orders"][0]
            record["state"] = "SUBMITTED"
            trade.created[str(record["client_order_id"])] = package.orders[0]
            executor._execution_store.save(journal)
            clock.value = NOW + timedelta(hours=2)

            report = executor.reconcile(package.digest)

        self.assertEqual(report.state, "CANCEL_REQUESTED")
        self.assertEqual(trade.create_count, 0)
        self.assertEqual(len(trade.cancelled), 1)


if __name__ == "__main__":
    unittest.main()
