from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from invest_agent.cli import PROJECT_ROOT, _token_file_from_local_env
from invest_agent.secrets import SecretStoreError


class LocalEnvTests(unittest.TestCase):
    def test_reads_only_token_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "IGNORED=value\n"
                "INVEST_AGENT_TOKEN_FILE=.local/secrets/bcs-readonly-refresh-token\n"
            )

            path = _token_file_from_local_env(env_file)

            self.assertEqual(
                path,
                PROJECT_ROOT / ".local" / "secrets" / "bcs-readonly-refresh-token",
            )

    def test_rejects_missing_token_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("IGNORED=value\n")

            with self.assertRaisesRegex(SecretStoreError, "ровно один"):
                _token_file_from_local_env(env_file)
