"""Broker interfaces available to the analytical process."""

from __future__ import annotations

from typing import Protocol

from invest_agent.domain import PortfolioSnapshot


class BrokerReadPort(Protocol):
    """The only broker capability exposed to the analytical application."""

    def fetch_portfolio(self) -> PortfolioSnapshot:
        """Return a normalized point-in-time portfolio snapshot."""

