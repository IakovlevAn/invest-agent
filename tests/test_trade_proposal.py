from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from invest_agent.approval import ApprovalViolation
from invest_agent.brokers.bcs import (
    BcsBoard,
    BcsInstrument,
    BcsOrderBook,
    BcsOrderBookLevel,
    BcsQuote,
)
from invest_agent.credit import CreditSignal, RatingBand, SignalSeverity
from invest_agent.domain import InstrumentType, PortfolioSnapshot, Side
from invest_agent.manager import ManagerPolicy, PortfolioManager
from invest_agent.policy import InvestmentPolicy
from invest_agent.trade_proposal import (
    CodexConfirmationGate,
    ExactTradeProposalBuilder,
    LocalTradeGateStore,
    TradeProposalPolicy,
    proposal_as_dict,
)
from test_manager import bond, inputs, rating

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
POLICY_PATH = Path(__file__).parents[1] / "config" / "investment_policy.toml"
ISIN = "RU000A000002"


class FakeBcsClient:
    def __init__(self) -> None:
        self.instrument = BcsInstrument(
            ticker=ISIN,
            isin=ISIN,
            display_name="Тестовый выпуск",
            instrument_type="BONDS",
            boards=(BcsBoard(class_code="TQCB", exchange="MOEX"),),
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

    def fetch_instruments_by_isins(self, access_token, isins):
        return (self.instrument,)

    def fetch_quotes(self, access_token, instruments):
        return (
            BcsQuote(
                ticker=ISIN,
                class_code="TQCB",
                observed_at=NOW - timedelta(seconds=10),
                security_trading_status=17,
                currency="RUB",
                bid=Decimal("99.90"),
                offer=Decimal("100.10"),
                last=Decimal("100"),
                bid_yield=Decimal("25"),
                offer_yield=Decimal("24"),
            ),
        )

    def fetch_order_book(self, access_token, **kwargs):
        return BcsOrderBook(
            ticker=ISIN,
            class_code="TQCB",
            observed_at=NOW - timedelta(seconds=8),
            bids=(BcsOrderBookLevel(price=Decimal("99.90"), quantity=100),),
            asks=(BcsOrderBookLevel(price=Decimal("100.10"), quantity=100),),
        )


class MultiFakeBcsClient(FakeBcsClient):
    def fetch_instruments_by_isins(self, access_token, isins):
        return tuple(
            replace(self.instrument, ticker=isin, isin=isin)
            for isin in isins
        )

    def fetch_quotes(self, access_token, instruments):
        template = super().fetch_quotes(access_token, instruments)[0]
        return tuple(
            replace(
                template,
                ticker=ticker,
                class_code=class_code,
            )
            for ticker, class_code in instruments
        )

    def fetch_order_book(self, access_token, **kwargs):
        return replace(
            super().fetch_order_book(access_token, **kwargs),
            ticker=kwargs["ticker"],
            class_code=kwargs["class_code"],
        )


def manager_report_and_snapshot():
    record = bond(ISIN, emitter_id=2, value="21000", yield_percent="28")
    record = replace(record, position=replace(record.position, quantity=Decimal("21")))
    audit, bonds, credit = inputs(
        (record,),
        (
            rating(
                ISIN,
                emitter_id=2,
                band=RatingBand.SPECULATIVE,
                signal=CreditSignal(
                    "SPECULATIVE_RATING",
                    SignalSeverity.WARNING,
                    "рейтинг BB или ниже",
                ),
            ),
        ),
    )
    policy = InvestmentPolicy.from_toml(POLICY_PATH)
    report = PortfolioManager(
        policy,
        ManagerPolicy(
            max_bond_issuer_share_after_add=Decimal("0.15"),
            speculative_reduce_fraction=Decimal("0.50"),
            minimum_allocation_rub=Decimal("5000"),
            allocation_rounding_rub=Decimal("100"),
            maximum_single_purchase_share_of_cash=Decimal("0.40"),
        ),
        now=lambda: NOW,
    ).recommend(audit, bonds, credit)
    snapshot = PortfolioSnapshot(
        account_ref="bcs:test",
        is_iis=True,
        as_of=NOW,
        cash_rub=Decimal("50000"),
        positions=(record.position,),
    )
    return policy, replace(report, snapshot_digest=snapshot.digest), snapshot


class ExactTradeProposalTests(unittest.TestCase):
    def test_builds_one_digest_for_multiple_exact_orders(self) -> None:
        second_isin = "RU000A000003"
        first = bond(ISIN, emitter_id=2, value="20000", yield_percent="28")
        second = bond(second_isin, emitter_id=3, value="20000", yield_percent="27")
        records = tuple(
            replace(record, position=replace(record.position, quantity=Decimal("20")))
            for record in (first, second)
        )
        audit, bonds, credit = inputs(
            records,
            tuple(
                rating(
                    record.moex.facts.isin,
                    emitter_id=record.emitter.emitter_id,
                    band=RatingBand.SPECULATIVE,
                    signal=CreditSignal(
                        "SPECULATIVE_RATING",
                        SignalSeverity.WARNING,
                        "рейтинг BB или ниже",
                    ),
                )
                for record in records
            ),
        )
        policy = InvestmentPolicy.from_toml(POLICY_PATH)
        report = PortfolioManager(
            policy,
            ManagerPolicy(
                max_bond_issuer_share_after_add=Decimal("0.15"),
                speculative_reduce_fraction=Decimal("0.50"),
                minimum_allocation_rub=Decimal("5000"),
                allocation_rounding_rub=Decimal("100"),
                maximum_single_purchase_share_of_cash=Decimal("0.40"),
            ),
            now=lambda: NOW,
        ).recommend(audit, bonds, credit)
        snapshot = PortfolioSnapshot(
            account_ref="bcs:test",
            is_iis=True,
            as_of=NOW,
            cash_rub=Decimal("50000"),
            positions=tuple(record.position for record in records),
        )
        report = replace(report, snapshot_digest=snapshot.digest)

        proposal = ExactTradeProposalBuilder(
            client=MultiFakeBcsClient(),
            investment_policy=policy,
            proposal_policy=TradeProposalPolicy.from_toml(POLICY_PATH),
            now=lambda: NOW,
        ).build_manager_actions(
            report=report,
            snapshot=snapshot,
            access_token=object(),
            actions=((ISIN, Side.SELL), (second_isin, Side.SELL)),
        )

        self.assertEqual(len(proposal.orders), 2)
        self.assertEqual({order.ticker for order in proposal.orders}, {ISIN, second_isin})
        self.assertEqual(len(proposal.digest), 64)

    def test_builds_exact_lots_price_validity_and_digest(self) -> None:
        policy, report, snapshot = manager_report_and_snapshot()

        proposal = ExactTradeProposalBuilder(
            client=FakeBcsClient(),
            investment_policy=policy,
            proposal_policy=TradeProposalPolicy.from_toml(POLICY_PATH),
            now=lambda: NOW,
        ).build_manager_action(
            report=report,
            snapshot=snapshot,
            access_token=object(),
            isin=ISIN,
            side=Side.SELL,
        )

        order = proposal.orders[0]
        self.assertEqual(order.limit_price, Decimal("99.90"))
        self.assertEqual(order.lots, 10)
        self.assertEqual(order.quantity_units, 10)
        self.assertEqual(order.estimated_cash_rub, Decimal("10090.00"))
        self.assertEqual(proposal.expires_at, NOW + timedelta(minutes=10))
        self.assertEqual(order.order_valid_until, NOW + timedelta(hours=1))
        self.assertEqual(len(proposal.digest), 64)

    def test_codex_confirmation_requires_explicit_semantics_and_is_one_time(self) -> None:
        policy, report, snapshot = manager_report_and_snapshot()
        proposal = ExactTradeProposalBuilder(
            client=FakeBcsClient(),
            investment_policy=policy,
            proposal_policy=TradeProposalPolicy.from_toml(POLICY_PATH),
            now=lambda: NOW,
        ).build_manager_action(
            report=report,
            snapshot=snapshot,
            access_token=object(),
            isin=ISIN,
            side=Side.SELL,
        )
        with tempfile.TemporaryDirectory() as directory:
            store = LocalTradeGateStore(Path(directory))
            store.save_proposal(proposal)
            gate = CodexConfirmationGate(
                store=store,
                approval_ttl_seconds=600,
                now=lambda: NOW + timedelta(minutes=1),
            )
            with self.assertRaisesRegex(ApprovalViolation, "explicitly authorize"):
                gate.confirm_semantic(
                    proposal_digest=proposal.digest,
                    user_message="давай",
                )

            receipt = gate.confirm_semantic(
                proposal_digest=proposal.digest,
                user_message=(
                    "Подтверждаю выставление всех предложенных заявок"
                ),
            )

            self.assertEqual(receipt.state, "APPROVED_AWAITING_ISOLATED_EXECUTOR")
            self.assertFalse(receipt.orders_created)
            with self.assertRaisesRegex(ApprovalViolation, "already confirmed"):
                gate.confirm_semantic(
                    proposal_digest=proposal.digest,
                    user_message=(
                        "Подтверждаю выставление всех предложенных заявок"
                    ),
                )

    def test_semantic_confirmation_needs_no_digest_and_binds_active_package(self) -> None:
        policy, report, snapshot = manager_report_and_snapshot()
        proposal = ExactTradeProposalBuilder(
            client=FakeBcsClient(),
            investment_policy=policy,
            proposal_policy=TradeProposalPolicy.from_toml(POLICY_PATH),
            now=lambda: NOW,
        ).build_manager_action(
            report=report,
            snapshot=snapshot,
            access_token=object(),
            isin=ISIN,
            side=Side.SELL,
        )
        with tempfile.TemporaryDirectory() as directory:
            store = LocalTradeGateStore(Path(directory))
            store.save_proposal(proposal)
            gate = CodexConfirmationGate(
                store=store,
                approval_ttl_seconds=600,
                now=lambda: NOW + timedelta(minutes=1),
            )

            receipt = gate.confirm_semantic(
                proposal_digest=proposal.digest,
                user_message="Да, продаем предложенные позиции",
            )

        self.assertEqual(receipt.confirmation_mode, "CODEX_SEMANTIC")
        self.assertIsNotNone(receipt.confirmation_evidence_digest)
        self.assertNotIn(proposal.digest, "Да, продаем предложенные позиции")

    def test_semantic_confirmation_rejects_acknowledgement_question_and_wrong_side(self) -> None:
        policy, report, snapshot = manager_report_and_snapshot()
        proposal = ExactTradeProposalBuilder(
            client=FakeBcsClient(),
            investment_policy=policy,
            proposal_policy=TradeProposalPolicy.from_toml(POLICY_PATH),
            now=lambda: NOW,
        ).build_manager_action(
            report=report,
            snapshot=snapshot,
            access_token=object(),
            isin=ISIN,
            side=Side.SELL,
        )
        for message in (
            "давай",
            "подтверждаю анализ",
            "продаем этот пакет?",
            "покупаем пакет",
            "подтверждаю пакет при условии другой цены",
            "подтверждаю продажу пакета на 5 лотов",
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                store = LocalTradeGateStore(Path(directory))
                store.save_proposal(proposal)
                gate = CodexConfirmationGate(
                    store=store,
                    approval_ttl_seconds=600,
                    now=lambda: NOW + timedelta(minutes=1),
                )

                with self.assertRaises(ApprovalViolation):
                    gate.confirm_semantic(
                        proposal_digest=proposal.digest,
                        user_message=message,
                    )

    def test_semantic_confirmation_targets_only_latest_active_package(self) -> None:
        policy, report, snapshot = manager_report_and_snapshot()
        first = ExactTradeProposalBuilder(
            client=FakeBcsClient(),
            investment_policy=policy,
            proposal_policy=TradeProposalPolicy.from_toml(POLICY_PATH),
            now=lambda: NOW,
        ).build_manager_action(
            report=report,
            snapshot=snapshot,
            access_token=object(),
            isin=ISIN,
            side=Side.SELL,
        )
        second = replace(first, proposal_id="newer-proposal")
        with tempfile.TemporaryDirectory() as directory:
            store = LocalTradeGateStore(Path(directory))
            store.save_proposal(first)
            store.save_proposal(second)
            gate = CodexConfirmationGate(
                store=store,
                approval_ttl_seconds=600,
                now=lambda: NOW + timedelta(minutes=1),
            )

            with self.assertRaisesRegex(ApprovalViolation, "active proposal"):
                gate.confirm_semantic(
                    proposal_digest=first.digest,
                    user_message="Да, продаем предложенные позиции",
                )

    def test_confirmation_examples_avoid_package_wording(self) -> None:
        policy, report, snapshot = manager_report_and_snapshot()
        proposal = ExactTradeProposalBuilder(
            client=FakeBcsClient(),
            investment_policy=policy,
            proposal_policy=TradeProposalPolicy.from_toml(POLICY_PATH),
            now=lambda: NOW,
        ).build_manager_action(
            report=report,
            snapshot=snapshot,
            access_token=object(),
            isin=ISIN,
            side=Side.SELL,
        )

        examples = proposal_as_dict(proposal)["confirmation_examples"]

        self.assertTrue(examples)
        self.assertTrue(all("пакет" not in example.casefold() for example in examples))

    def test_caps_lots_to_displayed_units_at_limit_price(self) -> None:
        policy, report, snapshot = manager_report_and_snapshot()
        client = FakeBcsClient()
        original = client.fetch_order_book

        def shallow_book(access_token, **kwargs):
            book = original(access_token, **kwargs)
            return replace(
                book,
                bids=(BcsOrderBookLevel(price=Decimal("99.90"), quantity=5),),
            )

        client.fetch_order_book = shallow_book
        proposal = ExactTradeProposalBuilder(
            client=client,
            investment_policy=policy,
            proposal_policy=TradeProposalPolicy.from_toml(POLICY_PATH),
            now=lambda: NOW,
        ).build_manager_action(
            report=report,
            snapshot=snapshot,
            access_token=object(),
            isin=ISIN,
            side=Side.SELL,
        )

        self.assertEqual(proposal.orders[0].lots, 5)
        self.assertEqual(proposal.orders[0].quantity_units, 5)


if __name__ == "__main__":
    unittest.main()
