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
    PrivateFileRefreshTokenStore,
    SecretStoreError,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCAL_ENV_FILE = PROJECT_ROOT / ".env"
TOKEN_FILE_ENV_KEY = "INVEST_AGENT_TOKEN_FILE"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invest-agent")
    commands = parser.add_subparsers(dest="command", required=True)

    portfolio = commands.add_parser("portfolio", help="Read and normalize the BCS portfolio")
    portfolio.add_argument("--format", choices=("text", "json"), default="text")
    portfolio.add_argument(
        "--token-file",
        type=Path,
        help="Private mode-600 refresh-token file; defaults to INVEST_AGENT_TOKEN_FILE in .env",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "portfolio":
            token_file = args.token_file or _token_file_from_local_env(LOCAL_ENV_FILE)
            snapshot = PortfolioReader(
                client=BcsReadClient(),
                token_store=PrivateFileRefreshTokenStore(token_file),
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
