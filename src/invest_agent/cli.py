"""Local tools invoked by Codex. They are not a separate user interface."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

from invest_agent.approval import ApprovalViolation
from invest_agent.asset_dynamics import PortfolioAssetDynamicsAnalyzer
from invest_agent.audit import PortfolioAuditor, render_audit_json, render_audit_text
from invest_agent.bond_report import (
    BondPortfolioEnricher,
    render_bond_report_json,
    render_bond_report_text,
)
from invest_agent.broker_checks import BcsTradeVerifier
from invest_agent.brokers.bcs import BcsApiError, BcsReadClient
from invest_agent.brokers.bcs_trade import BcsTradeClient
from invest_agent.credit import (
    CreditAnalysisPolicy,
    CreditPortfolioAnalyzer,
    render_credit_report_json,
    render_credit_report_text,
)
from invest_agent.disclosure.interfax import EDisclosureClient
from invest_agent.disclosures import (
    DisclosurePolicy,
    DisclosurePortfolioAnalyzer,
    render_disclosure_report_json,
    render_disclosure_report_text,
)
from invest_agent.domain import Side
from invest_agent.executor import (
    ExactPackageExecutor,
    ExecutionViolation,
    LocalExecutionStore,
)
from invest_agent.financial.fns import GirboClient
from invest_agent.fundamentals import (
    FundamentalPolicy,
    FundamentalPortfolioAnalyzer,
    render_fundamental_report_json,
    render_fundamental_report_text,
)
from invest_agent.manager import (
    ManagerPolicy,
    PortfolioManager,
    render_manager_report_text,
)
from invest_agent.market.moex import MoexIssClient
from invest_agent.policy import InvestmentPolicy, PolicyViolation
from invest_agent.portfolio import (
    BcsPortfolioNormalizer,
    PortfolioContractError,
    bond_income_as_dict,
    render_portfolio_json,
    render_portfolio_text,
)
from invest_agent.rates import (
    BondRateModel,
    CbrKeyRateClient,
    CbrKeyRateError,
    RateScenarioPolicy,
)
from invest_agent.ratings.cbr import CbrRatingsClient
from invest_agent.reader import PortfolioReader
from invest_agent.secrets import (
    PrivateFileRefreshTokenStore,
    SecretStoreError,
)
from invest_agent.trade_proposal import (
    CodexConfirmationGate,
    ExactTradeProposalBuilder,
    LocalTradeGateStore,
    TradeProposalError,
    TradeProposalPolicy,
    proposal_as_dict,
)
from invest_agent.universe import BondCandidateScreener, BondUniversePolicy

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCAL_ENV_FILE = PROJECT_ROOT / ".env"
TOKEN_FILE_ENV_KEY = "INVEST_AGENT_TOKEN_FILE"
TRADE_TOKEN_FILE_ENV_KEY = "INVEST_AGENT_TRADE_TOKEN_FILE"
POLICY_FILE = PROJECT_ROOT / "config" / "investment_policy.toml"
TRADE_GATE_ROOT = PROJECT_ROOT / ".local" / "state" / "trade-gate"
TRADE_EXECUTION_ROOT = TRADE_GATE_ROOT / "executions"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invest-agent")
    commands = parser.add_subparsers(dest="command", required=True)

    portfolio = commands.add_parser("portfolio", help="Read and normalize the BCS portfolio")
    _add_read_options(portfolio)

    cash_terms = commands.add_parser(
        "cash-terms",
        help="Read RUB cash separately for each BCS settlement term",
    )
    _add_read_options(cash_terms)

    bond_income = commands.add_parser(
        "bond-income",
        help="Read current BCS bond cost basis, unrealized P&L and accrued income",
    )
    _add_read_options(bond_income)

    audit = commands.add_parser("audit", help="Run a deterministic point-in-time audit")
    _add_read_options(audit)

    bonds = commands.add_parser("bonds", help="Enrich portfolio bonds with MOEX ISS data")
    _add_read_options(bonds)

    credit = commands.add_parser(
        "credit",
        help="Build credit passports from MOEX and the Bank of Russia ratings repository",
    )
    _add_read_options(credit)

    fundamentals = commands.add_parser(
        "fundamentals",
        help="Build RAS and bond payment-schedule passports from FNS and MOEX",
    )
    _add_read_options(fundamentals)

    disclosures = commands.add_parser(
        "disclosures",
        help="Index consolidated and exact-issue issuer-filed disclosures",
    )
    _add_read_options(disclosures)

    recommend = commands.add_parser(
        "recommend",
        help="Build an approval-gated portfolio-manager recommendation",
    )
    _add_read_options(recommend)

    proposal = commands.add_parser(
        "proposal",
        help="Build and persist an exact BCS-verified list of limit orders",
    )
    _add_read_options(proposal)
    proposal.add_argument("--isin", help="Exact ISIN from the manager report")
    proposal.add_argument("--side", choices=("BUY", "SELL"))
    proposal.add_argument(
        "--action",
        action="append",
        help="Repeatable exact action in SIDE:ISIN form; cannot be mixed with --isin/--side",
    )
    proposal.add_argument(
        "--current-cash-only",
        action="store_true",
        help="Size buys only from current portfolio cash, excluding planned sale proceeds",
    )
    proposal.add_argument(
        "--settlement-term",
        choices=("T0", "T1", "T2"),
        default="T0",
        help="BCS cash settlement term used for buy sizing and exact cash checks",
    )
    proposal.add_argument(
        "--exclude-buy-isin",
        action="append",
        default=[],
        help="Exclude a stale or otherwise non-actionable buy ISIN before cash allocation",
    )

    confirm = commands.add_parser(
        "confirm",
        help="Record an explicit semantic Codex confirmation without sending an order",
    )
    confirm.add_argument("--digest", required=True, help="Full SHA-256 proposal digest")
    confirm.add_argument("--user-confirmation", required=True)
    confirm.add_argument("--format", choices=("text", "json"), default="text")

    token_check = commands.add_parser(
        "trade-token-check",
        help="Validate and rotate the isolated BCS trading token without an order request",
    )
    token_check.add_argument("--format", choices=("text", "json"), default="text")
    token_check.add_argument("--token-file", type=Path)

    execute = commands.add_parser(
        "execute",
        help="Execute only a persisted exact order list with its Codex approval receipt",
    )
    execute.add_argument("--digest", required=True, help="Full SHA-256 proposal digest")
    execute.add_argument("--format", choices=("text", "json"), default="text")

    execution_status = commands.add_parser(
        "execution-status",
        help="Read the local safe execution journal without broker access",
    )
    execution_status.add_argument("--digest", required=True)
    execution_status.add_argument("--format", choices=("text", "json"), default="text")

    reconcile = commands.add_parser(
        "reconcile",
        help="Refresh or cancel already-submitted orders without creating new ones",
    )
    reconcile.add_argument("--digest", required=True)
    reconcile.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def _add_read_options(command: argparse.ArgumentParser) -> None:
    command.add_argument("--format", choices=("text", "json"), default="text")
    command.add_argument(
        "--token-file",
        type=Path,
        help="Private mode-600 refresh-token file; defaults to INVEST_AGENT_TOKEN_FILE in .env",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "trade-token-check":
            token_file = args.token_file or _configured_path_from_local_env(
                LOCAL_ENV_FILE,
                TRADE_TOKEN_FILE_ENV_KEY,
                "торговому токену",
            )
            store = PrivateFileRefreshTokenStore(token_file)
            pair = BcsTradeClient().exchange_trade_refresh_token(store.get())
            store.set(pair.refresh_token)
            payload = {
                "status": "VALID",
                "authority": "BCS_TRADE",
                "access_expires_at": pair.access_token.expires_at.isoformat(),
                "refresh_expires_at": pair.refresh_expires_at.isoformat(),
                "order_requests_sent": 0,
            }
            if args.format == "json":
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(
                    "Торговый токен БКС действителен "
                    "и безопасно ротирован.\n"
                    f"Access действует до: {payload['access_expires_at']}\n"
                    f"Refresh действует до: {payload['refresh_expires_at']}\n"
                    "Заявки в БКС не отправлялись."
                )
            return 0
        if args.command == "execution-status":
            report = LocalExecutionStore(TRADE_EXECUTION_ROOT).report(args.digest)
            if args.format == "json":
                print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
            else:
                print(
                    f"Исполнение списка заявок {report.proposal_digest}: {report.state}\n"
                    f"Последнее обновление: {report.updated_at.isoformat()}"
                )
            return 0
        if args.command == "execute":
            executor = _package_executor()
            report = executor.execute(args.digest)
            if args.format == "json":
                print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
            else:
                print(
                    f"Исполнение точного списка заявок: {report.state}\n"
                    f"Digest: {report.proposal_digest}\n"
                    f"Последнее обновление: {report.updated_at.isoformat()}"
                )
            return 0
        if args.command == "reconcile":
            report = _package_executor().reconcile(args.digest)
            if args.format == "json":
                print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
            else:
                print(
                    f"Сверка точного списка заявок: {report.state}\n"
                    f"Digest: {report.proposal_digest}\n"
                    f"Последнее обновление: {report.updated_at.isoformat()}"
                )
            return 0
        if args.command == "confirm":
            proposal_policy = TradeProposalPolicy.from_toml(POLICY_FILE)
            receipt = CodexConfirmationGate(
                store=LocalTradeGateStore(TRADE_GATE_ROOT),
                approval_ttl_seconds=proposal_policy.proposal_ttl_seconds,
            ).confirm_semantic(
                proposal_digest=args.digest,
                user_message=args.user_confirmation,
            )
            if args.format == "json":
                print(json.dumps(receipt.as_dict(), ensure_ascii=False, indent=2))
            else:
                print(
                    "Точный список заявок подтверждён в Codex.\n"
                    f"Digest: {receipt.approval.proposal_digest}\n"
                    "Подтверждение действует до: "
                    f"{receipt.approval.expires_at.isoformat()}\n"
                    "Заявки в БКС не отправлены: "
                    "исполнитель запускается Codex только "
                    "для этого digest."
                )
            return 0
        if args.command in {
            "portfolio",
            "cash-terms",
            "bond-income",
            "audit",
            "bonds",
            "credit",
            "fundamentals",
            "disclosures",
            "recommend",
            "proposal",
        }:
            token_file = args.token_file or _token_file_from_local_env(LOCAL_ENV_FILE)
            bcs = BcsReadClient()
            session = PortfolioReader(
                client=bcs,
                token_store=PrivateFileRefreshTokenStore(token_file),
                normalizer=BcsPortfolioNormalizer(),
                is_iis=True,
            ).refresh_session()
            snapshot = session.snapshot
        if args.command == "cash-terms":
            payload = {
                "as_of": snapshot.as_of.isoformat(),
                "selected_t0_cash_rub": str(snapshot.cash_rub),
                "cash_by_settlement_term_rub": {
                    term: str(value)
                    for term, value in session.cash_by_settlement_term_rub
                },
            }
            if args.format == "json":
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                lines = [
                    f"Снимок: {payload['as_of']}",
                    f"T0: {payload['selected_t0_cash_rub']} ₽",
                ]
                lines.extend(
                    f"{term}: {value} ₽"
                    for term, value in payload["cash_by_settlement_term_rub"].items()
                )
                print("\n".join(lines))
            return 0
        if args.command == "bond-income":
            payload = bond_income_as_dict(session.bond_income_summary)
            if args.format == "json":
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(
                    "Облигации БКС:\n"
                    f"Стоимость: {payload['current_value_rub']} ₽\n"
                    f"Себестоимость: {payload['cost_basis_rub']} ₽\n"
                    f"Нереализованный P&L: {payload['unrealized_pl_rub']} ₽ "
                    f"({payload['unrealized_return_percent']}%)\n"
                    f"НКД: {payload['accrued_income_rub']} ₽"
                )
            return 0
        if args.command == "portfolio":
            renderer = render_portfolio_json if args.format == "json" else render_portfolio_text
            print(renderer(snapshot))
            return 0
        if args.command == "audit":
            audit = PortfolioAuditor(InvestmentPolicy.from_toml(POLICY_FILE)).audit(snapshot)
            renderer = render_audit_json if args.format == "json" else render_audit_text
            print(renderer(audit))
            return 0
        if args.command == "bonds":
            report = BondPortfolioEnricher(MoexIssClient()).enrich(snapshot)
            renderer = render_bond_report_json if args.format == "json" else render_bond_report_text
            print(renderer(report))
            return 0
        if args.command == "credit":
            bonds = BondPortfolioEnricher(MoexIssClient()).enrich(snapshot)
            report = CreditPortfolioAnalyzer(
                CbrRatingsClient(),
                CreditAnalysisPolicy.from_toml(POLICY_FILE),
            ).analyze(bonds)
            renderer = (
                render_credit_report_json if args.format == "json" else render_credit_report_text
            )
            print(renderer(report))
            return 0
        if args.command == "fundamentals":
            moex = MoexIssClient()
            bonds = BondPortfolioEnricher(moex).enrich(snapshot)
            report = FundamentalPortfolioAnalyzer(
                GirboClient(),
                moex,
                FundamentalPolicy.from_toml(POLICY_FILE),
            ).analyze(bonds)
            renderer = (
                render_fundamental_report_json
                if args.format == "json"
                else render_fundamental_report_text
            )
            print(renderer(report))
            return 0
        if args.command == "disclosures":
            bonds = BondPortfolioEnricher(MoexIssClient()).enrich(snapshot)
            report = DisclosurePortfolioAnalyzer(
                EDisclosureClient(),
                DisclosurePolicy.from_toml(POLICY_FILE),
            ).analyze(bonds)
            renderer = (
                render_disclosure_report_json
                if args.format == "json"
                else render_disclosure_report_text
            )
            print(renderer(report))
            return 0
        if args.command in {"recommend", "proposal"}:
            investment_policy, audit, bonds, manager, report = _build_manager_report(
                snapshot,
                buy_availability=lambda isins: _bcs_buy_available_isins(
                    bcs,
                    session.access_token,
                    isins,
                ),
            )
            if args.command == "proposal":
                cash_by_term = dict(session.cash_by_settlement_term_rub)
                proposal_cash_rub = cash_by_term.get(args.settlement_term)
                if proposal_cash_rub is None:
                    raise TradeProposalError(
                        f"BCS portfolio has no RUB cash for {args.settlement_term}"
                    )
                report, _ = _apply_bcs_manager_constraints(
                    manager,
                    report,
                    audit,
                    bonds,
                    bcs,
                    session.access_token,
                    include_planned_sale_proceeds=not args.current_cash_only,
                    cash_override_rub=proposal_cash_rub,
                    excluded_buy_isins={
                        isin.strip().upper()
                        for isin in args.exclude_buy_isin
                        if isin.strip()
                    },
                )
                actions = _parse_exact_actions(args)
                proposal = ExactTradeProposalBuilder(
                    client=bcs,
                    investment_policy=investment_policy,
                    proposal_policy=TradeProposalPolicy.from_toml(POLICY_FILE),
                ).build_manager_actions(
                    report=report,
                    snapshot=snapshot,
                    access_token=session.access_token,
                    actions=actions,
                    available_cash_rub=proposal_cash_rub,
                )
                LocalTradeGateStore(TRADE_GATE_ROOT).save_proposal(proposal)
                payload = proposal_as_dict(proposal)
                payload["user_facing_name"] = "список заявок"
                payload["manager_actions"] = {
                    "existing": [
                        {
                            "isin": decision.isin,
                            "action": decision.action.value,
                            "recommended_add_rub": format(
                                decision.recommended_add_rub,
                                "f",
                            ),
                            "recommended_reduce_rub": format(
                                decision.recommended_reduce_rub,
                                "f",
                            ),
                        }
                        for decision in report.decisions
                        if decision.recommended_add_rub > 0
                        or decision.recommended_reduce_rub > 0
                    ],
                    "new": [
                        {
                            "isin": candidate.isin,
                            "recommended_add_rub": format(
                                candidate.recommended_add_rub,
                                "f",
                            ),
                            "estimated_lot_cost_rub": format(
                                candidate.estimated_lot_cost_rub,
                                "f",
                            ),
                        }
                        for candidate in report.new_bond_candidates
                        if candidate.recommended_add_rub > 0
                    ],
                }
                payload["buy_bond_details"] = _buy_bond_details(proposal)
                if args.format == "json":
                    print(
                        json.dumps(
                            _public_order_list_payload(payload),
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                else:
                    order_lines = "\n".join(
                        f"- {order.side.value} {order.ticker}: {order.lots} лот(ов), "
                        f"{order.quantity_units} шт., лимит {order.limit_price}; "
                        f"до {order.order_valid_until.isoformat()}; "
                        f"расчётно {order.estimated_cash_rub} ₽"
                        for order in proposal.orders
                    )
                    print(
                        "Точный список лимитных заявок "
                        "сформирован.\n"
                        f"{order_lines}\n"
                        "Заявки в БКС не отправлены."
                    )
                return 0

            try:
                checks_complete = True
                report, checks = _apply_bcs_manager_constraints(
                    manager,
                    report,
                    audit,
                    bonds,
                    bcs,
                    session.access_token,
                )
            except BcsApiError as error:
                checks_complete = False
                checks = ()
                report = replace(
                    report,
                    data_failures=tuple(sorted({*report.data_failures, str(error)})),
                )
            payload = report.as_dict()
            payload["bcs_trade_checks"] = [check.as_dict() for check in checks]
            payload["bond_income"] = bond_income_as_dict(
                session.bond_income_summary
            )
            dynamics = PortfolioAssetDynamicsAnalyzer(MoexIssClient()).analyze(
                snapshot,
                bonds,
            )
            payload["asset_dynamics"] = dynamics.as_dict()
            try:
                rate_report = BondRateModel(
                    RateScenarioPolicy.from_toml(POLICY_FILE)
                ).analyze(bonds, CbrKeyRateClient().fetch())
                payload["rate_model"] = rate_report.as_dict()
                payload["bond_income"]["forward_market_estimate"] = (
                    _forward_bond_income(report, rate_report)
                )
            except CbrKeyRateError as error:
                rate_report = None
                payload["rate_model"] = {"status": "UNAVAILABLE", "error": str(error)}
                payload["data_failures"].append(str(error))
            payload["data_failures"] = sorted(set(payload["data_failures"]))
            payload["trade_gate"].update(
                {
                    "bcs_catalog_checks_complete": checks_complete,
                    "exact_package_command_available": True,
                    "codex_confirmation_available": True,
                    "broker_executor_available": True,
                }
            )
            if args.format == "json":
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                additions = _render_recommendation_additions(
                    checks,
                    rate_report,
                    checks_complete=checks_complete,
                )
                print(render_manager_report_text(report) + additions)
            return 0
    except (
        ApprovalViolation,
        BcsApiError,
        CbrKeyRateError,
        ExecutionViolation,
        PortfolioContractError,
        PolicyViolation,
        SecretStoreError,
        TradeProposalError,
    ) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2
    raise AssertionError("unreachable command")


def _token_file_from_local_env(env_file: Path) -> Path:
    return _configured_path_from_local_env(
        env_file,
        TOKEN_FILE_ENV_KEY,
        "read-only токену",
    )


def _package_executor() -> ExactPackageExecutor:
    return ExactPackageExecutor(
        gate_store=LocalTradeGateStore(TRADE_GATE_ROOT),
        execution_store=LocalExecutionStore(TRADE_EXECUTION_ROOT),
        read_client=BcsReadClient(),
        trade_client=BcsTradeClient(),
        read_token_store=PrivateFileRefreshTokenStore(
            _configured_path_from_local_env(
                LOCAL_ENV_FILE,
                TOKEN_FILE_ENV_KEY,
                "read-only токену",
            )
        ),
        trade_token_store=PrivateFileRefreshTokenStore(
            _configured_path_from_local_env(
                LOCAL_ENV_FILE,
                TRADE_TOKEN_FILE_ENV_KEY,
                "торговому токену",
            )
        ),
        normalizer=BcsPortfolioNormalizer(),
        investment_policy=InvestmentPolicy.from_toml(POLICY_FILE),
        proposal_policy=TradeProposalPolicy.from_toml(POLICY_FILE),
    )


def _configured_path_from_local_env(
    env_file: Path,
    key_name: str,
    token_label: str,
) -> Path:
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise SecretStoreError(
            f"Локальный .env с путём к {token_label} не найден"
        ) from error

    configured: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if separator and key.strip() == key_name:
            configured.append(value.strip().strip('"').strip("'"))
    if len(configured) != 1 or not configured[0]:
        raise SecretStoreError(
            f"В .env должен быть ровно один {key_name}"
        )

    path = Path(configured[0]).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _parse_exact_actions(args) -> tuple[tuple[str, Side], ...]:
    raw_actions = args.action or []
    if raw_actions and (args.isin is not None or args.side is not None):
        raise TradeProposalError("--action cannot be mixed with --isin or --side")
    if raw_actions:
        parsed: list[tuple[str, Side]] = []
        for raw in raw_actions:
            side_text, separator, isin = raw.partition(":")
            if not separator or not isin.strip():
                raise TradeProposalError("each --action must use SIDE:ISIN")
            try:
                side = Side(side_text.strip().upper())
            except ValueError as error:
                raise TradeProposalError("each --action side must be BUY or SELL") from error
            parsed.append((isin.strip().upper(), side))
        return tuple(parsed)
    if args.isin is None or args.side is None:
        raise TradeProposalError("proposal requires --action or both --isin and --side")
    return ((args.isin.strip().upper(), Side(args.side)),)


def _build_manager_report(snapshot, *, buy_availability=None):
    investment_policy = InvestmentPolicy.from_toml(POLICY_FILE)
    audit = PortfolioAuditor(investment_policy).audit(snapshot)
    moex = MoexIssClient()
    ratings = CbrRatingsClient()
    credit_policy = CreditAnalysisPolicy.from_toml(POLICY_FILE)
    bonds = BondPortfolioEnricher(moex).enrich(snapshot)
    credit = CreditPortfolioAnalyzer(ratings, credit_policy).analyze(bonds)
    universe = BondCandidateScreener(
        moex,
        ratings,
        credit_policy,
        BondUniversePolicy.from_toml(POLICY_FILE),
        buy_availability=buy_availability,
    ).screen(snapshot, bonds)
    manager = PortfolioManager(
        investment_policy,
        ManagerPolicy.from_toml(POLICY_FILE),
    )
    report = manager.recommend(audit, bonds, credit, universe)
    return investment_policy, audit, bonds, manager, report


def _apply_bcs_manager_constraints(
    manager,
    report,
    audit,
    bonds,
    bcs,
    access_token,
    *,
    include_planned_sale_proceeds=True,
    cash_override_rub=None,
    excluded_buy_isins=None,
):
    verifier = BcsTradeVerifier(bcs)
    preliminary_checks = verifier.verify_manager_actions(
        report,
        access_token,
        include_unallocated_buys=True,
    )
    excluded = set() if excluded_buy_isins is None else excluded_buy_isins
    constrained = manager.apply_bcs_buy_availability(
        report,
        audit,
        bonds,
        {
            check.isin
            for check in preliminary_checks
            if check.side is Side.BUY and check.broker_catalog_available
        }
        - excluded,
        include_planned_sale_proceeds=include_planned_sale_proceeds,
        cash_override_rub=cash_override_rub,
        buy_lot_cost_rub={
            check.isin: lot_cost
            for check in preliminary_checks
            if check.side is Side.BUY
            and (lot_cost := _bcs_buy_lot_cost(check)) is not None
        },
    )
    checks = verifier.verify_manager_actions(constrained, access_token)
    return constrained, checks


def _bcs_buy_available_isins(bcs, access_token, isins) -> set[str]:
    available: set[str] = set()
    batch_size = 50
    for start in range(0, len(isins), batch_size):
        instruments = bcs.fetch_instruments_by_isins(
            access_token,
            tuple(isins[start : start + batch_size]),
        )
        available.update(
            instrument.isin
            for instrument in instruments
            if instrument.is_ruble_bond
            and not instrument.is_blocked
            and not instrument.is_qualified_only
            and instrument.available_for_unqualified
            and instrument.lot_size > 0
            and instrument.minimum_step > 0
        )
    return available


def _render_recommendation_additions(
    checks,
    rate_report,
    *,
    checks_complete: bool = True,
) -> str:
    lines = ["", "", "Проверка БКС:"]
    if not checks_complete:
        lines.append("- проверка недоступна; рекомендации нельзя считать исполнимыми")
    elif not checks:
        lines.append("- нет действий с положительной суммой для точной проверки")
    for check in checks:
        status = (
            "доступен в справочнике"
            if check.broker_catalog_available
            else "заблокирован"
        )
        line = (
            f"- {check.side.value} {check.isin}: {status}; "
            f"лот {check.lot_size or 'н/д'}; сессия "
            f"{'открыта' if check.trading_is_open else 'закрыта'}"
        )
        estimated = _estimate_bond_action(check)
        if estimated is not None:
            lots, units, price, cash = estimated
            quote_side = "offer" if check.side is Side.BUY else "bid"
            line += (
                f"; ориентир {units} шт. ({lots} лот.), {quote_side} "
                f"{price}% и грязная сумма {cash:,.0f} ₽"
            )
        lines.append(line)
    lines.extend(["", "Модель ставки:"])
    if rate_report is None:
        lines.append("- недоступна")
    else:
        lines.append(
            f"- ключевая ставка {rate_report.observation.value_percent}% "
            f"с {rate_report.observation.effective_date.isoformat()}"
        )
        for bond in rate_report.bonds:
            lines.append(f"- {bond.ticker}: {bond.model_type}")
    return "\n".join(lines)


def _estimate_bond_action(check) -> tuple[int, int, Decimal, Decimal] | None:
    price = check.offer if check.side is Side.BUY else check.bid
    if (
        price is None
        or check.face_value is None
        or check.accrued_interest is None
        or check.lot_size is None
        or check.lot_size <= 0
    ):
        return None
    dirty_unit_cost = (
        check.face_value * price / Decimal("100") + check.accrued_interest
    )
    if dirty_unit_cost <= 0:
        return None
    units_by_amount = int(
        (check.target_amount_rub / dirty_unit_cost).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    lots = units_by_amount // check.lot_size
    if lots <= 0:
        return None
    units = lots * check.lot_size
    return lots, units, price, dirty_unit_cost * units


def _bcs_buy_lot_cost(check) -> Decimal | None:
    if (
        check.offer is None
        or check.face_value is None
        or check.accrued_interest is None
        or check.lot_size is None
        or check.lot_size <= 0
    ):
        return None
    dirty_unit_cost = (
        check.face_value * check.offer / Decimal("100")
        + check.accrued_interest
    )
    if dirty_unit_cost <= 0:
        return None
    return dirty_unit_cost * check.lot_size


def _buy_bond_details(proposal) -> list[dict[str, object]]:
    moex = MoexIssClient()
    details: list[dict[str, object]] = []
    for order in proposal.orders:
        if order.side is not Side.BUY:
            continue
        isin = order.instrument_uid.rsplit(":", 1)[-1]
        bond = moex.fetch_bond(isin)
        schedule = moex.fetch_bond_schedule(isin)
        market = bond.market
        today = schedule.fetched_at.date()
        coupons = [
            coupon for coupon in schedule.coupons if coupon.coupon_date >= today
        ]
        amortizations = [
            item
            for item in schedule.amortizations
            if item.amortization_date >= today
        ]
        next_coupon = coupons[0] if coupons else None
        coupon_window_end = today + timedelta(days=365)
        coupons_next_12m = [
            coupon
            for coupon in coupons
            if coupon.coupon_date <= coupon_window_end
        ]
        amortizations_next_12m = [
            item
            for item in amortizations
            if item.amortization_date <= coupon_window_end
        ]
        coupon_per_unit = bond.facts.coupon_value
        if coupon_per_unit is None and next_coupon is not None:
            coupon_per_unit = next_coupon.value_rub or next_coupon.value
        frequency = bond.facts.coupon_frequency
        annual_coupon_per_unit = (
            None
            if coupon_per_unit is None or frequency is None
            else coupon_per_unit * frequency
        )
        face_value = bond.facts.face_value
        dirty_per_unit = order.estimated_cash_rub / Decimal(order.quantity_units)
        clean_per_unit = (
            None
            if face_value is None
            else face_value * order.limit_price / Decimal("100")
        )
        accrued_per_unit = (
            None
            if clean_per_unit is None
            else dirty_per_unit - clean_per_unit
        )
        details.append(
            {
                "isin": isin,
                "name": bond.facts.name,
                "quantity_units": order.quantity_units,
                "current_face_value_rub": _decimal_or_none(face_value),
                "clean_price_percent_of_face": format(order.limit_price, "f"),
                "clean_price_rub_per_unit": _decimal_or_none(clean_per_unit),
                "accrued_income_rub_per_unit": _decimal_or_none(accrued_per_unit),
                "dirty_price_rub_per_unit": format(dirty_per_unit, "f"),
                "dirty_purchase_cash_rub": format(order.estimated_cash_rub, "f"),
                "coupon_rate_percent": _decimal_or_none(
                    bond.facts.coupon_percent
                ),
                "coupon_rub_per_unit": _decimal_or_none(coupon_per_unit),
                "coupon_payments_per_year": frequency,
                "next_coupon_date": (
                    None
                    if next_coupon is None
                    else next_coupon.coupon_date.isoformat()
                ),
                "next_coupon_cash_for_order_rub": (
                    None
                    if coupon_per_unit is None
                    else format(
                        coupon_per_unit * order.quantity_units,
                        "f",
                    )
                ),
                "annual_coupon_cash_for_order_rub": (
                    None
                    if annual_coupon_per_unit is None
                    else format(
                        annual_coupon_per_unit * order.quantity_units,
                        "f",
                    )
                ),
                "scheduled_coupon_cash_next_12m_for_order_rub": format(
                    sum(
                        (
                            (coupon.value_rub or coupon.value or Decimal("0"))
                            * order.quantity_units
                            for coupon in coupons_next_12m
                        ),
                        Decimal("0"),
                    ),
                    "f",
                ),
                "scheduled_principal_cash_next_12m_for_order_rub": format(
                    sum(
                        (
                            (item.value_rub or item.value or Decimal("0"))
                            * order.quantity_units
                            for item in amortizations_next_12m
                        ),
                        Decimal("0"),
                    ),
                    "f",
                ),
                "effective_yield_percent": _decimal_or_none(
                    None if market is None else market.effective_yield_percent
                ),
                "duration_days": _decimal_or_none(
                    None if market is None else market.duration_days
                ),
                "maturity_date": (
                    None
                    if bond.facts.maturity_date is None
                    else bond.facts.maturity_date.isoformat()
                ),
                "offer_date": (
                    None
                    if bond.facts.offer_date is None
                    else bond.facts.offer_date.isoformat()
                ),
                "next_coupons": [
                    {
                        "date": coupon.coupon_date.isoformat(),
                        "rub_per_unit": _decimal_or_none(
                            coupon.value_rub or coupon.value
                        ),
                    }
                    for coupon in coupons[:3]
                ],
                "next_amortizations": [
                    {
                        "date": item.amortization_date.isoformat(),
                        "rub_per_unit": _decimal_or_none(
                            item.value_rub or item.value
                        ),
                        "percent_of_initial_face": _decimal_or_none(
                            item.value_percent
                        ),
                    }
                    for item in amortizations[:3]
                ],
                "sources": {
                    "security": bond.facts.source_url,
                    "market": None if market is None else market.source_url,
                    "schedule": schedule.source_url,
                },
                "limitations": [
                    "Coupon schedule may change for floaters or after issuer actions.",
                    "Effective yield is annualized and assumes scheduled payments; taxes, fees and reinvestment are excluded.",
                ],
            }
        )
    return details


def _decimal_or_none(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _public_order_list_payload(payload: dict[str, object]) -> dict[str, object]:
    hidden = {
        "proposal_id",
        "proposal_digest",
        "portfolio_snapshot_digest",
        "created_at",
        "expires_at",
        "confirmation_examples",
        "confirmation_mode",
        "broker_executor_available",
    }
    public = {key: value for key, value in payload.items() if key not in hidden}
    public["orders"] = [
        {
            key: value
            for key, value in order.items()
            if key not in {"quote_observed_at", "order_valid_until"}
        }
        for order in payload.get("orders", [])
    ]
    public["state"] = "AWAITING_USER_DECISION"
    return public


def _forward_bond_income(report, rate_report) -> dict[str, object]:
    comparable_value = (
        report.bond_value_rub * report.comparable_yield_coverage_share
    )
    comparable_income = Decimal("0")
    if report.current_comparable_bond_yield_percent is not None:
        comparable_income = (
            comparable_value
            * report.current_comparable_bond_yield_percent
            / Decimal("100")
        )
    decision_values = {
        decision.isin: decision.market_value_rub for decision in report.decisions
    }
    scenarios: dict[str, dict[str, object]] = {}
    for sensitivity in rate_report.bonds:
        if sensitivity.model_type != "KEY_RATE_FLOATER":
            continue
        value = decision_values.get(sensitivity.isin, Decimal("0"))
        for scenario in sensitivity.scenarios:
            carry = scenario.projected_coupon_or_carry_percent
            if carry is None:
                continue
            bucket = scenarios.setdefault(
                scenario.code,
                {
                    "label": scenario.label,
                    "gross_annual_rub": comparable_income,
                },
            )
            bucket["gross_annual_rub"] = (
                bucket["gross_annual_rub"]
                + value * carry / Decimal("100")
            )
    normalized: dict[str, dict[str, str]] = {}
    for code, bucket in scenarios.items():
        gross = bucket["gross_annual_rub"]
        assert isinstance(gross, Decimal)
        normalized[code] = {
            "label": str(bucket["label"]),
            "gross_annual_rub": format(gross, "f"),
            "gross_annual_percent_of_bond_value": (
                "0"
                if report.bond_value_rub == 0
                else format(gross / report.bond_value_rub * Decimal("100"), "f")
            ),
        }
    return {
        "comparable_fixed_bond_value_rub": format(comparable_value, "f"),
        "comparable_fixed_gross_annual_rub": format(comparable_income, "f"),
        "scenarios": normalized,
        "limitations": [
            "Market yield and floater carry are forward-looking annualized estimates, not realized profit.",
            "Taxes, broker fees, defaults, calls, reinvestment and future spread changes are excluded.",
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
