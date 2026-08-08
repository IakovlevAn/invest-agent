"""Local tools invoked by Codex. They are not a separate user interface."""

from __future__ import annotations

import argparse
import getpass
import sys

from invest_agent.brokers.bcs import BcsApiError, BcsReadClient
from invest_agent.portfolio import (
    BcsPortfolioNormalizer,
    PortfolioContractError,
    render_portfolio_json,
    render_portfolio_text,
)
from invest_agent.reader import PortfolioReader
from invest_agent.secrets import (
    MacOSKeychainRefreshTokenStore,
    SecretNotFoundError,
    SecretStoreError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invest-agent")
    commands = parser.add_subparsers(dest="command", required=True)

    token = commands.add_parser("token", help="Manage the read-only BCS token")
    token_commands = token.add_subparsers(dest="token_command", required=True)
    token_commands.add_parser("set", help="Store a token through a hidden local prompt")
    token_commands.add_parser("status", help="Check whether a token is configured")

    portfolio = commands.add_parser("portfolio", help="Read and normalize the BCS portfolio")
    portfolio.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = MacOSKeychainRefreshTokenStore()
    try:
        if args.command == "token":
            return _token_command(args.token_command, store)
        if args.command == "portfolio":
            snapshot = PortfolioReader(
                client=BcsReadClient(),
                token_store=store,
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


def _token_command(command: str, store: MacOSKeychainRefreshTokenStore) -> int:
    if command == "set":
        token = getpass.getpass("Вставьте read-only refresh-токен БКС: ")
        try:
            store.set(token)
        finally:
            token = ""
        print("Read-only токен сохранён в macOS Keychain.")
        return 0
    if command == "status":
        try:
            store.get()
        except SecretNotFoundError:
            print("Read-only токен не настроен.")
            return 1
        print("Read-only токен настроен.")
        return 0
    raise AssertionError("unreachable token command")


if __name__ == "__main__":
    raise SystemExit(main())
