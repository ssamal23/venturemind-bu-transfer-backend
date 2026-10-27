"""File storage: local disk and Azure Blob Storage (tested against a fake Azure client)."""

import pytest

from app.config import settings
from app.core import storage
from app.core.storage import AzureBlobStore, LocalBlobStore, get_store


class ResourceNotFoundError(Exception):
    """Named like the Azure SDK's exception, which is how the store recognises it."""


class ResourceExistsError(Exception):
    pass


class FakeContainer:
    def __init__(self, service, name):
        self.service, self.name = service, name

    def upload_blob(self, path, data, overwrite=False):
        if self.name not in self.service.created:
            raise ResourceNotFoundError("container missing")
        blobs = self.service.blobs
        if (self.name, path) in blobs and not overwrite:
            raise ResourceExistsError("blob exists")
        blobs[(self.name, path)] = bytes(data)

    def create_container(self):
        self.service.created.add(self.name)

    def download_blob(self, path):
        data = self.service.blobs.get((self.name, path))
        if data is None:
            raise ResourceNotFoundError("no such blob")

        class Download:
            def readall(self_inner):
                return data

        return Download()


class FakeService:
    def __init__(self, existing_containers=()):
        self.created = set(existing_containers)
        self.blobs = {}

    def get_container_client(self, name):
        return FakeContainer(self, name)


# ---------------------------------------------------------------- local
def test_local_round_trip(tmp_path):
    s = LocalBlobStore(str(tmp_path))
    s.put("syllabi", "a/b.pdf", b"hello")
    assert s.get("syllabi", "a/b.pdf") == b"hello"


@pytest.mark.parametrize("bad", ["../evil", "a/../../evil", "/etc/passwd", "", "..\\evil"])
def test_unsafe_paths_are_refused_everywhere(tmp_path, bad):
    local = LocalBlobStore(str(tmp_path))
    azure = AzureBlobStore(service=FakeService({"syllabi"}))
    for store in (local, azure):
        with pytest.raises(ValueError):
            store.put("syllabi", bad, b"x")
        with pytest.raises(ValueError):
            store.get("syllabi", bad)


def test_local_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        LocalBlobStore(str(tmp_path)).get("syllabi", "nope.pdf")


# ---------------------------------------------------------------- azure
def test_azure_round_trip_in_an_existing_container():
    svc = FakeService({"transcripts"})
    s = AzureBlobStore(service=svc)
    s.put("transcripts", "app/doc.pdf", b"%PDF-1.4 data")
    assert s.get("transcripts", "app/doc.pdf") == b"%PDF-1.4 data"


def test_azure_creates_a_missing_container_on_first_upload():
    svc = FakeService()
    s = AzureBlobStore(service=svc)
    s.put("syllabi", "x/y.docx", b"data")
    assert "syllabi" in svc.created and s.get("syllabi", "x/y.docx") == b"data"


def test_azure_never_overwrites_an_existing_file():
    s = AzureBlobStore(service=FakeService({"syllabi"}))
    s.put("syllabi", "same.pdf", b"first")
    with pytest.raises(ResourceExistsError):
        s.put("syllabi", "same.pdf", b"second")
    assert s.get("syllabi", "same.pdf") == b"first"


def test_azure_missing_blob_looks_like_a_missing_file():
    with pytest.raises(FileNotFoundError, match="syllabi/missing.pdf"):
        AzureBlobStore(service=FakeService({"syllabi"})).get("syllabi", "missing.pdf")


def test_other_azure_errors_are_not_hidden():
    class Boom(FakeService):
        def get_container_client(self, name):
            raise RuntimeError("network down")

    with pytest.raises(RuntimeError, match="network down"):
        AzureBlobStore(service=Boom()).get("syllabi", "a.pdf")


def test_azure_needs_an_account_url_or_a_connection_string():
    with pytest.raises(ValueError, match="AZURE_STORAGE_ACCOUNT_URL"):
        AzureBlobStore()


# ---------------------------------------------------------------- choosing the backend
def test_backend_is_chosen_from_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    assert isinstance(get_store(), LocalBlobStore)

    fake = AzureBlobStore(service=FakeService())
    monkeypatch.setattr(settings, "storage_backend", "azure")
    monkeypatch.setattr(storage, "_azure_store", fake)
    assert get_store() is fake


def test_uploads_use_the_configured_store_end_to_end(client, student, monkeypatch):
    """An upload through the API lands in whatever store is configured (here, a fake Azure)."""
    svc = FakeService()
    monkeypatch.setattr(settings, "storage_backend", "azure")
    monkeypatch.setattr(storage, "_azure_store", AzureBlobStore(service=svc))

    data = client.get(f"/api/students/{student['user']['id']}/dashboard", headers=student["headers"]).json()
    cid = next(c for c in data["courses"] if c["sourceCode"] == "PSY 200")["id"]
    res = client.post(f"/api/transcript-courses/{cid}/syllabus", headers=student["headers"],
                      files={"file": ("psy.pdf", b"%PDF-1.4 content", "application/pdf")})
    assert res.status_code == 201
    assert [k[0] for k in svc.blobs] == ["syllabi"]
    ((_, blob_path), stored), = svc.blobs.items()
    assert stored == b"%PDF-1.4 content" and blob_path.startswith(cid)
