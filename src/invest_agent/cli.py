"""Local tools invoked by Codex. They are not a separate user interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from invest_agent.approval import ApprovalViolation
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
    render_portfolio_json,
    render_portfolio_text,
)
from invest_agent.ratings.cbr import CbrRatingsClient
from invest_agent.reader import PortfolioReader
from invest_agent.rates import (
    BondRateModel,
    CbrKeyRateClient,
    CbrKeyRateError,
    RateScenarioPolicy,
)
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

    recommend = commands.add_parser(
        "recommend",
        help="Build an approval-gated portfolio-manager recommendation",
    )
    _add_read_options(recommend)

    proposal = commands.add_parser(
        "proposal",
        help="Build and persist an exact BCS-verified limit-order package",
    )
    _add_read_options(proposal)
    proposal.add_argument("--isin", help="Exact ISIN from the manager report")
    proposal.add_argument("--side", choices=("BUY", "SELL"))
    proposal.add_argument(
        "--action",
        action="append",
        help="Repeatable exact action in SIDE:ISIN form; cannot be mixed with --isin/--side",
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
        help="Execute only a persisted package with its exact Codex approval receipt",
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
        help="Refresh or cancel already-submitted package orders without creating new ones",
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
                    f"Исполнение пакета {report.proposal_digest}: {report.state}\n"
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
                    f"Исполнение точного пакета: {report.state}\n"
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
                    f"Сверка точного пакета: {report.state}\n"
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
                    "Точный пакет подтверждён в Codex.\n"
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
            "audit",
            "bonds",
            "credit",
            "fundamentals",
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
        if args.command in {"recommend", "proposal"}:
            investment_policy, bonds, report = _build_manager_report(snapshot)
            if args.command == "proposal":
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
                )
                LocalTradeGateStore(TRADE_GATE_ROOT).save_proposal(proposal)
                payload = proposal_as_dict(proposal)
                if args.format == "json":
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    order_lines = "\n".join(
                        f"- {order.side.value} {order.ticker}: {order.lots} лот(ов), "
                        f"{order.quantity_units} шт., лимит {order.limit_price}; "
                        f"до {order.order_valid_until.isoformat()}; "
                        f"расчётно {order.estimated_cash_rub} ₽"
                        for order in proposal.orders
                    )
                    confirmation_examples = "» или «".join(
                        payload["confirmation_examples"]
                    )
                    print(
                        "Точный пакет лимитных заявок "
                        "сформирован.\n"
                        f"{order_lines}\n"
                        f"Digest: {proposal.digest}\n"
                        "Для подтверждения ответь обычной однозначной фразой, "
                        f"например: «{confirmation_examples}». "
                        "Digest вводить не нужно.\n"
                        "Заявки в БКС не отправлены."
                    )
                return 0

            payload = report.as_dict()
            try:
                checks_complete = True
                checks = BcsTradeVerifier(bcs).verify_manager_actions(
                    report,
                    session.access_token,
                )
                payload["bcs_trade_checks"] = [check.as_dict() for check in checks]
            except BcsApiError as error:
                checks_complete = False
                checks = ()
                payload["bcs_trade_checks"] = []
                payload["data_failures"].append(str(error))
            try:
                rate_report = BondRateModel(
                    RateScenarioPolicy.from_toml(POLICY_FILE)
                ).analyze(bonds, CbrKeyRateClient().fetch())
                payload["rate_model"] = rate_report.as_dict()
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
                additions = _render_recommendation_additions(checks, rate_report)
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


def _build_manager_report(snapshot):
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
    ).screen(snapshot, bonds)
    report = PortfolioManager(
        investment_policy,
        ManagerPolicy.from_toml(POLICY_FILE),
    ).recommend(audit, bonds, credit, universe)
    return investment_policy, bonds, report


def _render_recommendation_additions(checks, rate_report) -> str:
    lines = ["", "", "Проверка БКС:"]
    if not checks:
        lines.append("- нет действий с положительной суммой для точной проверки")
    for check in checks:
        status = (
            "доступен в справочнике"
            if check.broker_catalog_available
            else "заблокирован"
        )
        lines.append(
            f"- {check.side.value} {check.isin}: {status}; "
            f"лот {check.lot_size or 'н/д'}; сессия "
            f"{'открыта' if check.trading_is_open else 'закрыта'}"
        )
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


if __name__ == "__main__":
    raise SystemExit(main())
