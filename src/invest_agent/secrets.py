"""Ephemeral file storage for BCS refresh tokens."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Protocol

MAX_TOKEN_BYTES = 16 * 1024
TEMP_ROOT = Path("/private/tmp").resolve()


class SecretStoreError(RuntimeError):
    """Secret storage failed without including secret material in the message."""


class SecretNotFoundError(SecretStoreError):
    """The read-only BCS refresh token file has not been configured."""


class RefreshTokenStore(Protocol):
    def get(self) -> str: ...

    def set(self, token: str) -> None: ...


class EphemeralFileRefreshTokenStore:
    """Reads and atomically rotates a mode-600 token file under /private/tmp."""

    def __init__(self, path: Path) -> None:
        try:
            resolved = path.expanduser().resolve(strict=True)
        except OSError as error:
            raise SecretNotFoundError("Временный файл с read-only токеном не найден") from error
        if TEMP_ROOT not in resolved.parents:
            raise SecretStoreError("Файл с токеном должен находиться внутри /private/tmp")
        self.path = resolved

    def get(self) -> str:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags)
        except OSError as error:
            raise SecretNotFoundError("Временный файл с read-only токеном не найден") from error

        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise SecretStoreError("Файл с токеном должен быть обычным файлом")
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise SecretStoreError("Права временного файла с токеном должны быть 600")
            payload = os.read(descriptor, MAX_TOKEN_BYTES + 1)
        except OSError as error:
            raise SecretStoreError("Не удалось прочитать временный файл с токеном") from error
        finally:
            os.close(descriptor)

        if len(payload) > MAX_TOKEN_BYTES:
            raise SecretStoreError("Файл с токеном неожиданно большой")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SecretStoreError("Файл с токеном должен быть в UTF-8") from error

        token = text.rstrip("\r\n")
        suffix = text[len(token) :]
        if not token:
            raise SecretStoreError("Временный файл с токеном пуст")
        if any(character.isspace() for character in token):
            raise SecretStoreError("Токен содержит пробел или перенос строки внутри значения")
        if suffix not in ("", "\n", "\r\n"):
            raise SecretStoreError("После токена допускается только один перенос строки")
        return token

    def set(self, token: str) -> None:
        if not token or any(character.isspace() for character in token):
            raise SecretStoreError("Нельзя сохранить пустой токен или токен с пробелами")
        encoded = token.encode("utf-8")
        if len(encoded) > MAX_TOKEN_BYTES:
            raise SecretStoreError("Токен неожиданно большой")

        temporary_path: Path | None = None
        descriptor = -1
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                dir=self.path.parent,
            )
            temporary_path = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            file = os.fdopen(descriptor, "wb", closefd=True)
            descriptor = -1
            with file:
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
            self.path.chmod(0o600)
        except OSError as error:
            raise SecretStoreError("Не удалось обновить временный файл с токеном") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
