from __future__ import annotations

import unittest

from invest_agent.secrets import MacOSKeychainRefreshTokenStore


class KeychainCommandTests(unittest.TestCase):
    def test_write_command_prompts_instead_of_accepting_secret_argument(self) -> None:
        command = MacOSKeychainRefreshTokenStore.keychain_write_command()
        self.assertEqual(command[-1], "-w")
        self.assertNotIn("secret", " ".join(command).lower())


if __name__ == "__main__":
    unittest.main()
