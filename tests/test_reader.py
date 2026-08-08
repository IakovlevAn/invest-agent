from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from invest_agent.brokers.bcs import BcsAccessToken, BcsTokenPair
from invest_agent.portfolio import BcsPortfolioNormalizer
from invest_agent.reader import PortfolioReader
from invest_agent.secrets import SecretStoreError

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


class FakeStore:
    def __init__(self, *, fail_on_set: bool = False) -> None:
        self.value = "old-refresh"
        self.fail_on_set = fail_on_set
        self.events: list[str] = []

    def get(self) -> str:
        self.events.append("get")
        return self.value

    def set(self, token: str) -> None:
        self.events.append("set")
        if self.fail_on_set:
            raise SecretStoreError("synthetic token-store failure")
        self.value = token


class FakeClient:
    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.fetch_called = False

    def exchange_read_only_refresh_token(self, refresh_token: str) -> BcsTokenPair:
        self.assert_refresh = refresh_token
        return BcsTokenPair(
            access_token=BcsAccessToken("access", NOW + timedelta(hours=1)),
            refresh_token="rotated-refresh",
            refresh_expires_at=NOW + timedelta(days=90),
        )

    def fetch_raw_portfolio(self, access_token: BcsAccessToken) -> dict[str, object]:
        self.fetch_called = True
        self.store.events.append("fetch")
        return json.loads((ROOT / "tests/fixtures/bcs_portfolio.json").read_text())


class PortfolioReaderTests(unittest.TestCase):
    def test_persists_rotated_token_before_fetch(self) -> None:
        store = FakeStore()
        client = FakeClient(store)
        reader = PortfolioReader(
            client=client,  # type: ignore[arg-type]
            token_store=store,
            normalizer=BcsPortfolioNormalizer(now=lambda: NOW),
        )

        snapshot = reader.refresh()

        self.assertEqual(client.assert_refresh, "old-refresh")
        self.assertEqual(store.value, "rotated-refresh")
        self.assertEqual(store.events, ["get", "set", "fetch"])
        self.assertEqual(str(snapshot.total_value_rub), "250000")

    def test_does_not_fetch_when_rotated_token_cannot_be_saved(self) -> None:
        store = FakeStore(fail_on_set=True)
        client = FakeClient(store)
        reader = PortfolioReader(
            client=client,  # type: ignore[arg-type]
            token_store=store,
            normalizer=BcsPortfolioNormalizer(now=lambda: NOW),
        )

        with self.assertRaisesRegex(SecretStoreError, "synthetic token-store failure"):
            reader.refresh()
        self.assertFalse(client.fetch_called)


if __name__ == "__main__":
    unittest.main()
