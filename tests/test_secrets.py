from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from invest_agent.secrets import EphemeralFileRefreshTokenStore, SecretStoreError


class EphemeralFileStoreTests(unittest.TestCase):
    def token_file(self, payload: bytes, *, mode: int = 0o600) -> Path:
        descriptor, raw_path = tempfile.mkstemp(prefix="invest-agent-token.", dir="/private/tmp")
        path = Path(raw_path)
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        path.chmod(mode)
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_reads_single_line_private_token(self) -> None:
        store = EphemeralFileRefreshTokenStore(self.token_file(b"refresh-token\n"))

        self.assertEqual(store.get(), "refresh-token")

    def test_rejects_whitespace_inside_token(self) -> None:
        store = EphemeralFileRefreshTokenStore(self.token_file(b"partial token\n"))

        with self.assertRaisesRegex(SecretStoreError, "пробел"):
            store.get()

    def test_rejects_broad_permissions(self) -> None:
        store = EphemeralFileRefreshTokenStore(self.token_file(b"secret", mode=0o644))

        with self.assertRaisesRegex(SecretStoreError, "600"):
            store.get()

    def test_atomically_rotates_token(self) -> None:
        path = self.token_file(b"old-refresh")
        store = EphemeralFileRefreshTokenStore(path)

        store.set("rotated-refresh")

        self.assertEqual(store.get(), "rotated-refresh")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_rejects_file_outside_private_tmp(self) -> None:
        with self.assertRaisesRegex(SecretStoreError, "/private/tmp"):
            EphemeralFileRefreshTokenStore(Path(__file__))


if __name__ == "__main__":
    unittest.main()
