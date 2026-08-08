"""Local tools invoked by Codex. They are not a separate user interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from invest_agent.brokers.bcs import BcsApiError, BcsReadClient
from invest_agent.portfolio import (
    BcsPortfolioNormalizer,
    PortfolioContractError,
    render_portfolio_json,
    render_portfolio_text,
)
from invest_agent.reader import PortfolioReader
from invest_agent.secrets import (
    EphemeralFileRefreshTokenStore,
    SecretStoreError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invest-agent")
    commands = parser.add_subparsers(dest="command", required=True)

    portfolio = commands.add_parser("portfolio", help="Read and normalize the BCS portfolio")
    portfolio.add_argument("--format", choices=("text", "json"), default="text")
    portfolio.add_argument(
        "--token-file",
        type=Path,
        required=True,
        help="Private mode-600 refresh-token file inside /private/tmp",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "portfolio":
            snapshot = PortfolioReader(
                client=BcsReadClient(),
                token_store=EphemeralFileRefreshTokenStore(args.token_file),
                normalizer=BcsPortfolioNormalizer(),
                is_iis=True,
            ).refresh()
            renderer = render_portfolio_json if args.format == "json" else render_portfolio_text
            print(renderer(snapshot))
            return 0
    except (BcsApiError, PortfolioContractError, SecretStoreError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())
