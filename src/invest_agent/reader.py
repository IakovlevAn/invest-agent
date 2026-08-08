"""End-to-end read-only portfolio refresh."""

from __future__ import annotations

from invest_agent.brokers.bcs import BcsReadClient
from invest_agent.domain import PortfolioSnapshot
from invest_agent.portfolio import BcsPortfolioNormalizer
from invest_agent.secrets import RefreshTokenStore


class PortfolioReader:
    def __init__(
        self,
        *,
        client: BcsReadClient,
        token_store: RefreshTokenStore,
        normalizer: BcsPortfolioNormalizer,
        is_iis: bool = True,
    ) -> None:
        self._client = client
        self._token_store = token_store
        self._normalizer = normalizer
        self._is_iis = is_iis

    def refresh(self) -> PortfolioSnapshot:
        refresh_token = self._token_store.get()
        token_pair = self._client.exchange_read_only_refresh_token(refresh_token)
        # BCS returns a new refresh token. Persist it before the next API call so
        # a crash cannot silently leave the user with a stale credential.
        self._token_store.set(token_pair.refresh_token)
        raw_portfolio = self._client.fetch_raw_portfolio(token_pair.access_token)
        return self._normalizer.normalize(raw_portfolio, is_iis=self._is_iis)
