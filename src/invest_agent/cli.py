"""Local tools invoked by Codex. They are not a separate user interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from invest_agent.audit import PortfolioAuditor, render_audit_json, render_audit_text
from invest_agent.bond_report import (
    BondPortfolioEnricher,
    render_bond_report_json,
    render_bond_report_text,
)
from invest_agent.brokers.bcs import BcsApiError, BcsReadClient
from invest_agent.credit import (
    CreditAnalysisPolicy,
    CreditPortfolioAnalyzer,
    render_credit_report_json,
    render_credit_report_text,
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
    render_manager_report_json,
    render_manager_report_text,
)
from invest_agent.market.moex import MoexIssClient
from invest_agent.policy import InvestmentPolicy
from invest_agent.portfolio import (
    BcsPortfolioNormalizer,
    PortfolioContractError,
    render_portfolio_json,
    render_portfolio_text,
)
from invest_agent.ratings.cbr import CbrRatingsClient
from invest_agent.reader import PortfolioReader
from invest_agent.secrets import (
    PrivateFileRefreshTokenStore,
    SecretStoreError,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCAL_ENV_FILE = PROJECT_ROOT / ".env"
TOKEN_FILE_ENV_KEY = "INVEST_AGENT_TOKEN_FILE"
POLICY_FILE = PROJECT_ROOT / "config" / "investment_policy.toml"


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
        if args.command in {
            "portfolio",
            "audit",
            "bonds",
            "credit",
            "fundamentals",
            "recommend",
        }:
            token_file = args.token_file or _token_file_from_local_env(LOCAL_ENV_FILE)
            snapshot = PortfolioReader(
                client=BcsReadClient(),
                token_store=PrivateFileRefreshTokenStore(token_file),
                normalizer=BcsPortfolioNormalizer(),
                is_iis=True,
            ).refresh()
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
        if args.command == "recommend":
            investment_policy = InvestmentPolicy.from_toml(POLICY_FILE)
            audit = PortfolioAuditor(investment_policy).audit(snapshot)
            bonds = BondPortfolioEnricher(MoexIssClient()).enrich(snapshot)
            credit = CreditPortfolioAnalyzer(
                CbrRatingsClient(),
                CreditAnalysisPolicy.from_toml(POLICY_FILE),
            ).analyze(bonds)
            report = PortfolioManager(
                investment_policy,
                ManagerPolicy.from_toml(POLICY_FILE),
            ).recommend(audit, bonds, credit)
            renderer = (
                render_manager_report_json
                if args.format == "json"
                else render_manager_report_text
            )
            print(renderer(report))
            return 0
    except (BcsApiError, PortfolioContractError, SecretStoreError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2
    raise AssertionError("unreachable command")


def _token_file_from_local_env(env_file: Path) -> Path:
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise SecretStoreError("Локальный .env с путём к read-only токену не найден") from error

    configured: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if separator and key.strip() == TOKEN_FILE_ENV_KEY:
            configured.append(value.strip().strip('"').strip("'"))
    if len(configured) != 1 or not configured[0]:
        raise SecretStoreError(f"В .env должен быть ровно один {TOKEN_FILE_ENV_KEY}")

    path = Path(configured[0]).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
