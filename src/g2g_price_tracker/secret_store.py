from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path


class SecretStorageError(RuntimeError):
    """Raised when Windows cannot protect or recover a local secret."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob_from_bytes(value: bytes) -> tuple[_DataBlob, object]:
    buffer = ctypes.create_string_buffer(value)
    blob = _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return blob, buffer


def _protect(value: str) -> bytes:
    if sys.platform != "win32":
        raise SecretStorageError("Secure token storage is available in the Windows app.")
    source, source_buffer = _blob_from_bytes(value.encode("utf-8"))
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    result = crypt32.CryptProtectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(output),
    )
    del source_buffer
    if not result:
        raise SecretStorageError("Windows could not protect the Telegram bot token.")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def _unprotect(value: bytes) -> str:
    if sys.platform != "win32":
        raise SecretStorageError("Secure token storage is available in the Windows app.")
    source, source_buffer = _blob_from_bytes(value)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    result = crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(output),
    )
    del source_buffer
    if not result:
        raise SecretStorageError("Windows could not recover the saved Telegram bot token.")
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretStorageError("The saved Telegram bot token is invalid.") from exc
    finally:
        kernel32.LocalFree(output.pbData)


def load_secret(path: str | Path) -> str:
    secret_path = Path(path)
    if not secret_path.exists():
        return ""
    try:
        return _unprotect(secret_path.read_bytes())
    except OSError as exc:
        raise SecretStorageError("The saved Telegram bot token could not be read.") from exc


def save_secret(path: str | Path, value: str) -> None:
    secret_path = Path(path)
    if not value:
        try:
            secret_path.unlink()
        except FileNotFoundError:
            pass
        return
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = secret_path.with_suffix(".tmp")
    temporary.write_bytes(_protect(value))
    temporary.replace(secret_path)
