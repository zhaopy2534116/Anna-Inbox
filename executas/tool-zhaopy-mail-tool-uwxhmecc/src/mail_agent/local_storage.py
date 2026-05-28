"""Local JSON file storage that mirrors the APS StorageClient interface.

Usage in main.py:
    from mail_agent.local_storage import LocalStorageClient
    storage = LocalStorageClient(data_dir)
    init_storage_singleton(storage, storage, scope="user")
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any


class LocalStorageClient:
    """In-process JSON-file storage with the same async API as APS StorageClient."""

    def __init__(self, data_dir: str | Path, *, scope: str = "user"):
        self._root = Path(data_dir).resolve()
        self._scope = scope
        self._generation: int = 1
        self._lock = threading.Lock()

    # ── helpers ───────────────────────────────────────────────────

    def _key_path(self, key: str) -> Path:
        safe = key.replace("\\", "/").strip("/")
        return self._root / f"{safe}.json"

    def _read(self, key: str) -> tuple[Any, str, bool]:
        path = self._key_path(key)
        if not path.is_file():
            return None, "", False
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, OSError):
            return None, "", False
        if not isinstance(data, dict) or "value" not in data:
            return None, "", False
        return data["value"], data.get("etag", ""), True

    def _write(self, key: str, value: Any, if_match: str | None = None) -> dict:
        path = self._key_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            current_etag = ""
            if if_match and path.is_file():
                try:
                    raw = path.read_text(encoding="utf-8")
                    data = json.loads(raw)
                    current_etag = data.get("etag", "") if isinstance(data, dict) else ""
                except (json.JSONDecodeError, OSError):
                    pass
                if current_etag and current_etag != if_match:
                    from executa_sdk.storage import StorageError, STORAGE_ERR_PRECONDITION_FAILED
                    raise StorageError(STORAGE_ERR_PRECONDITION_FAILED, "etag mismatch")

            self._generation += 1
            etag = f"local_{self._generation}_{hashlib.md5(str(self._generation).encode()).hexdigest()[:6]}"
            payload = json.dumps({"value": value, "etag": etag}, ensure_ascii=False, default=str)
            try:
                size_bytes = len(payload.encode("utf-8"))
            except UnicodeEncodeError:
                # Replace lone surrogates (U+D800–U+DFFF) from LLM output
                payload = "".join(c if ord(c) < 0xD800 or ord(c) >= 0xE000 else "?" for c in payload)
                size_bytes = len(payload.encode("utf-8"))

            path.write_text(payload + "\n", encoding="utf-8")

        return {"etag": etag, "generation": self._generation, "size_bytes": size_bytes}

    # ── public API ────────────────────────────────────────────────

    async def get(self, key: str, *, scope: str | None = None, timeout: float = 30) -> dict:
        value, etag, exists = self._read(key)
        return {"value": value, "etag": etag, "exists": exists}

    async def set(
        self,
        key: str,
        value: Any,
        *,
        scope: str | None = None,
        if_match: str | None = None,
        ttl_seconds: int | None = None,
        timeout: float = 30,
    ) -> dict:
        return self._write(key, value, if_match=if_match)

    async def delete(
        self,
        key: str,
        *,
        scope: str | None = None,
        if_match: str | None = None,
        timeout: float = 30,
    ) -> dict:
        path = self._key_path(key)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return {"deleted": True}

    async def list(
        self,
        *,
        prefix: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
        kind: str | None = None,
        scope: str | None = None,
        timeout: float = 30,
    ) -> dict:
        prefix = (prefix or "").strip("/")

        # Walk the directory tree rooted at prefix (or root if no prefix)
        search_dir = self._root / prefix if prefix else self._root
        items: list[dict] = []

        if search_dir.is_dir():
            for dirpath, _dirs, files in os.walk(search_dir):
                for fname in files:
                    if not fname.endswith(".json"):
                        continue
                    full_path = Path(dirpath) / fname
                    rel = str(full_path.relative_to(self._root).with_suffix("")).replace("\\", "/")
                    if prefix and not rel.startswith(prefix):
                        continue
                    items.append({"key": rel})

        items.sort(key=lambda x: x["key"])

        if limit and len(items) > limit:
            items = items[:limit]

        return {"items": items, "next_cursor": None}

    # FilesClient stub — not used by storage_ops, needed for init()
    async def read(self, key: str, **kwargs: Any) -> Any:
        raise NotImplementedError("Local storage does not support file operations")

    async def write(self, key: str, data: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("Local storage does not support file operations")


def make_local_clients(data_dir: str | Path) -> tuple[LocalStorageClient, LocalStorageClient]:
    """Create local storage + files clients for standalone runs.

    Returns (storage, files) suitable for storage_client.init().
    """
    client = LocalStorageClient(data_dir)
    return client, client
