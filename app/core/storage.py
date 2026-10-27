"""Blob storage behind one small interface.

  local  files under STORAGE_DIR. For development and tests.
  azure  Azure Blob Storage. Required when the API and the worker run as separate containers,
         because a container's own disk is private to it and lost on restart.

Containers are private. Nothing here ever builds a public URL.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from app.config import settings


class BlobStore(Protocol):
    def put(self, container: str, path: str, data: bytes) -> None: ...

    def get(self, container: str, path: str) -> bytes: ...


def _check_path(path: str) -> None:
    """Blob names come from our own ids, but refuse anything that could climb out of a folder."""
    parts = path.replace("\\", "/").split("/")
    if not path or path.startswith(("/", "\\")) or ".." in parts:
        raise ValueError("Unsafe blob path")


class LocalBlobStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()

    def _resolve(self, container: str, path: str) -> Path:
        _check_path(path)
        target = (self.root / container / path).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("Path escapes the storage root")
        return target

    def put(self, container: str, path: str, data: bytes) -> None:
        target = self._resolve(container, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def get(self, container: str, path: str) -> bytes:
        return self._resolve(container, path).read_bytes()


class AzureBlobStore:
    """Azure Blob Storage.

    Sign-in is by managed identity (DefaultAzureCredential) against AZURE_STORAGE_ACCOUNT_URL,
    so no storage key is stored anywhere. The identity needs the "Storage Blob Data Contributor"
    role on the storage account. A connection string is accepted for local testing only.

    `service` is for tests: pass an object shaped like BlobServiceClient.
    """

    def __init__(self, account_url: str = "", connection_string: str = "", service: Any = None) -> None:
        if service is None:
            from azure.storage.blob import BlobServiceClient

            if connection_string:
                service = BlobServiceClient.from_connection_string(connection_string)
            elif account_url:
                from azure.identity import DefaultAzureCredential

                service = BlobServiceClient(account_url, credential=DefaultAzureCredential())
            else:
                raise ValueError("Set AZURE_STORAGE_ACCOUNT_URL (or AZURE_STORAGE_CONNECTION_STRING for local testing)")
        self._service = service

    @staticmethod
    def _not_found(exc: Exception) -> bool:
        return type(exc).__name__ in {"ResourceNotFoundError", "ContainerNotFound", "BlobNotFound"}

    def put(self, container: str, path: str, data: bytes) -> None:
        _check_path(path)
        client = self._service.get_container_client(container)
        try:
            client.upload_blob(path, data, overwrite=False)  # never replace an existing file
        except Exception as exc:
            if not self._not_found(exc):
                raise
            client.create_container()  # first upload into a new container
            client.upload_blob(path, data, overwrite=False)

    def get(self, container: str, path: str) -> bytes:
        _check_path(path)
        try:
            return self._service.get_container_client(container).download_blob(path).readall()
        except Exception as exc:
            if self._not_found(exc):
                raise FileNotFoundError(f"{container}/{path}") from exc
            raise


_azure_store: AzureBlobStore | None = None


def get_store() -> BlobStore:
    global _azure_store
    if settings.storage_backend == "azure":
        if _azure_store is None:
            _azure_store = AzureBlobStore(settings.azure_storage_account_url, settings.azure_storage_connection_string)
        return _azure_store
    return LocalBlobStore(settings.storage_dir)
