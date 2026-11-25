"""Archive of uploaded files, their parsed JSON, and the parse queues.

Azure Blob Storage when AZURE_STORAGE_CONNECTION_STRING is set (container AZURE_STORAGE_CONTAINER,
default "mf-documents"); otherwise a local folder (ARCHIVE_DIR, default ./archive) with the same paths,
so the app runs the same way locally.

Paths:
    files/<Drive root>/<YEAR>/<MON>/<AMC>/<file>          the uploaded file
    parsed/<Drive root>/<YEAR>/<MON>/<AMC>/<file>.json    the parser's result for it
    cache/<Drive root>/<YEAR>/<MON>/<file>.zip            factsheet parser cache (docling pages, LLM answers)
    state/factsheets.json                                 every factsheet's fund/manager rows, for the merge
    merged/<YEAR>/<MON>.json                              holdings + managers + AUM for one month
"""
import json
import os
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath

from dotenv import load_dotenv

load_dotenv()

CONTAINER = os.getenv("AZURE_STORAGE_CONTAINER", "mf-documents")
LOCAL_ROOT = Path(os.getenv("ARCHIVE_DIR", Path(__file__).resolve().parents[1] / "archive")).resolve()
FILES, PARSED, CACHE, STATE, MERGED = "files", "parsed", "cache", "state", "merged"

_local_lock = threading.Lock()  # local backend: one process, so a lock is enough for update_json


def enabled_backend() -> str:
    return "azure" if os.getenv("AZURE_STORAGE_CONNECTION_STRING") else "local"


def clean_path(path: str) -> str:
    """Reject absolute paths and '..' so a request can't reach outside the archive."""
    p = PurePosixPath(str(path).replace("\\", "/"))
    if p.is_absolute() or not p.parts or any(part in ("..", ".", "") for part in p.parts):
        raise ValueError(f"Bad archive path: {path!r}")
    return str(p)


def file_path(root: str, year, month: str, amc: str | None, name: str) -> str:
    parts = [FILES, root.strip(), str(year), month.upper()[:3]] + ([amc.strip()] if amc else []) + [name]
    return clean_path("/".join(parts))


def _swap_root(source: str, root: str, suffix: str) -> str:
    src = clean_path(source)
    if not src.startswith(FILES + "/"):
        raise ValueError(f"Not an archived file: {source!r}")
    return root + src[len(FILES):] + suffix


def parsed_path(source: str) -> str:
    return _swap_root(source, PARSED, ".json")


def cache_path(source: str) -> str:
    return _swap_root(source, CACHE, ".zip")


@lru_cache(maxsize=1)
def _container():
    from azure.storage.blob import BlobServiceClient

    svc = BlobServiceClient.from_connection_string(os.environ["AZURE_STORAGE_CONNECTION_STRING"])
    c = svc.get_container_client(CONTAINER)
    if not c.exists():
        c.create_container()
    return c


def put_file(path: str, local: Path) -> None:
    path = clean_path(path)
    if enabled_backend() == "azure":
        with open(local, "rb") as fh:
            _container().upload_blob(path, fh, overwrite=True)
    else:
        dest = LOCAL_ROOT / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(Path(local).read_bytes())


def download(path: str, dest: Path) -> bool:
    """Copy a stored file to dest. False if there is none."""
    path = clean_path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if enabled_backend() == "azure":
        from azure.core.exceptions import ResourceNotFoundError

        try:
            data = _container().download_blob(path).readall()
        except ResourceNotFoundError:
            return False
        dest.write_bytes(data)
        return True
    src = LOCAL_ROOT / path
    if not src.is_file():
        return False
    dest.write_bytes(src.read_bytes())
    return True


def read_bytes(path: str) -> bytes | None:
    """A stored file's bytes, or None if there is none."""
    path = clean_path(path)
    if enabled_backend() == "azure":
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return _container().download_blob(path).readall()
        except ResourceNotFoundError:
            return None
    f = LOCAL_ROOT / path
    return f.read_bytes() if f.is_file() else None


def _encode(data) -> bytes:
    return json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")


def put_json(path: str, data) -> None:
    path = clean_path(path)
    if enabled_backend() == "azure":
        from azure.storage.blob import ContentSettings

        _container().upload_blob(path, _encode(data), overwrite=True,
                                 content_settings=ContentSettings(content_type="application/json"))
    else:
        dest = LOCAL_ROOT / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_bytes(_encode(data))
        os.replace(tmp, dest)


def get_json(path: str):
    path = clean_path(path)
    if enabled_backend() == "azure":
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return json.loads(_container().download_blob(path).readall())
        except ResourceNotFoundError:
            return None
    try:
        return json.loads((LOCAL_ROOT / path).read_bytes())
    except FileNotFoundError:
        return None


def update_json(path: str, change, retries: int = 10):
    """Read-modify-write one JSON blob safely while the app and the parse jobs write to it at once.

    change(current or None) returns the new value. Azure: optimistic concurrency on the blob's ETag,
    retried when another writer got in first.
    """
    path = clean_path(path)
    if enabled_backend() != "azure":
        with _local_lock:
            new = change(get_json(path))
            put_json(path, new)
            return new

    from azure.core import MatchConditions
    from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
    from azure.storage.blob import ContentSettings

    blob = _container().get_blob_client(path)
    for attempt in range(retries):
        try:
            dl = blob.download_blob()
            current, etag = json.loads(dl.readall()), dl.properties.etag
        except ResourceNotFoundError:
            current, etag = None, None
        new = change(current)
        try:
            settings = ContentSettings(content_type="application/json")
            if etag:
                blob.upload_blob(_encode(new), overwrite=True, content_settings=settings,
                                 etag=etag, match_condition=MatchConditions.IfNotModified)
            else:
                blob.upload_blob(_encode(new), overwrite=False, content_settings=settings)
            return new
        except (ResourceModifiedError, ResourceExistsError):
            time.sleep(0.2 * (attempt + 1))
    raise RuntimeError(f"Could not update {path}: too many concurrent writers")


def version(path: str) -> str | None:
    """Changes whenever the blob changes (ETag / file mtime). None if it doesn't exist."""
    path = clean_path(path)
    if enabled_backend() == "azure":
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return _container().get_blob_client(path).get_blob_properties().etag
        except ResourceNotFoundError:
            return None
    f = LOCAL_ROOT / path
    return str(f.stat().st_mtime_ns) if f.is_file() else None


def list_paths(prefix: str) -> list[dict]:
    """Everything stored under <prefix>/: [{path, name, size_bytes, modified}]."""
    base = clean_path(prefix)
    out = []
    if enabled_backend() == "azure":
        for b in _container().list_blobs(name_starts_with=base + "/"):
            out.append({"path": b.name, "name": PurePosixPath(b.name).name, "size_bytes": b.size,
                        "modified": b.last_modified.isoformat() if b.last_modified else None})
    else:
        root = LOCAL_ROOT / base
        if root.is_dir():
            for f in root.rglob("*"):
                if f.is_file() and not f.name.endswith(".tmp"):
                    st = f.stat()
                    out.append({"path": f.relative_to(LOCAL_ROOT).as_posix(), "name": f.name,
                                "size_bytes": st.st_size,
                                "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()})
    return sorted(out, key=lambda r: r["path"])


def list_files(prefix: str) -> list[dict]:
    """Archived files under files/<prefix>."""
    return list_paths(f"{FILES}/{prefix}")


# ---------- parse queues (Azure Storage Queues in the same account) ----------

@lru_cache(maxsize=None)
def queue_client(name: str):
    from azure.core.exceptions import ResourceExistsError
    from azure.storage.queue import QueueClient

    q = QueueClient.from_connection_string(os.environ["AZURE_STORAGE_CONNECTION_STRING"], name)
    try:
        q.create_queue()
    except ResourceExistsError:
        pass
    return q
