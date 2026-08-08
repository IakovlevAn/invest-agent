"""Protected storage for BCS refresh tokens on macOS.

The token is never passed in process arguments. Writes use a pseudo-terminal with
echo disabled because the macOS `security` tool documents `-w <password>` as
insecure and supports a hidden prompt when `-w` is the final argument.
"""

from __future__ import annotations

import os
import pty
import subprocess
import sys
import termios
from typing import Protocol

KEYCHAIN_SERVICE = "com.iakovlevan.invest-agent.bcs-readonly-refresh-token"
KEYCHAIN_ACCOUNT = "bcs-readonly"
SECURITY_COMMAND = "/usr/bin/security"


class SecretStoreError(RuntimeError):
    """Secret storage failed without including secret material in the message."""


class SecretNotFoundError(SecretStoreError):
    """The read-only BCS refresh token has not been configured yet."""


class RefreshTokenStore(Protocol):
    def get(self) -> str: ...

    def set(self, token: str) -> None: ...


class MacOSKeychainRefreshTokenStore:
    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        self._timeout_seconds = timeout_seconds

    def get(self) -> str:
        self._require_macos()
        result = subprocess.run(
            [
                SECURITY_COMMAND,
                "find-generic-password",
                "-a",
                KEYCHAIN_ACCOUNT,
                "-s",
                KEYCHAIN_SERVICE,
                "-w",
            ],
            check=False,
            capture_output=True,
            timeout=self._timeout_seconds,
        )
        if result.returncode != 0:
            raise SecretNotFoundError("Read-only BCS token is not configured in macOS Keychain")
        token = result.stdout.decode("utf-8").rstrip("\r\n")
        if not token:
            raise SecretStoreError("Stored read-only BCS token is empty")
        return token

    def set(self, token: str) -> None:
        self._require_macos()
        if not token:
            raise ValueError("token cannot be empty")
        command = self.keychain_write_command()
        master_fd, slave_fd = pty.openpty()
        try:
            attributes = termios.tcgetattr(slave_fd)
            attributes[3] &= ~termios.ECHO
            termios.tcsetattr(slave_fd, termios.TCSANOW, attributes)
            process = subprocess.Popen(
                command,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
            os.close(slave_fd)
            slave_fd = -1
            os.write(master_fd, token.encode("utf-8") + b"\n")
            try:
                return_code = process.wait(timeout=self._timeout_seconds)
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.wait()
                raise SecretStoreError("macOS Keychain write timed out") from error
            if return_code != 0:
                raise SecretStoreError(f"macOS Keychain write failed with status {return_code}")
        finally:
            if slave_fd >= 0:
                os.close(slave_fd)
            os.close(master_fd)

    @staticmethod
    def keychain_write_command() -> tuple[str, ...]:
        return (
            SECURITY_COMMAND,
            "add-generic-password",
            "-U",
            "-a",
            KEYCHAIN_ACCOUNT,
            "-s",
            KEYCHAIN_SERVICE,
            "-w",
        )

    @staticmethod
    def _require_macos() -> None:
        if sys.platform != "darwin":
            raise SecretStoreError("The MVP Keychain backend requires macOS")
