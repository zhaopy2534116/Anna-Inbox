"""Shared storage singleton for the mail agent.

main.py initialises the singleton during startup; all other modules import
``get_storage`` / ``get_files`` to access the configured persistence backend
without circular imports. The default backend is local JSON storage.
"""

from __future__ import annotations

from typing import Any

try:
    from executa_sdk.storage import StorageClient, FilesClient, StorageError  # noqa: E402
except ImportError:  # pragma: no cover — executa_sdk not available outside Anna runtime
    StorageClient = None  # type: ignore[assignment]
    FilesClient = None  # type: ignore[assignment]
    StorageError = RuntimeError  # type: ignore[assignment]

_storage: Any = None
_files: Any = None
_scope: str = "user"


def init(storage: StorageClient, files: FilesClient, *, scope: str = "user") -> None:
    """Called once by main.py after creating the clients."""
    global _storage, _files, _scope
    _storage = storage
    _files = files
    _scope = scope


def get_storage() -> StorageClient:
    """Return the shared StorageClient singleton."""
    if _storage is None:
        raise RuntimeError("StorageClient not initialised — call storage_client.init() first")
    return _storage


def get_files() -> FilesClient:
    """Return the shared FilesClient singleton."""
    if _files is None:
        raise RuntimeError("FilesClient not initialised — call storage_client.init() first")
    return _files


def scope() -> str:
    """Return the default storage scope."""
    return _scope


def is_ready() -> bool:
    return _storage is not None and _files is not None
