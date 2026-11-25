"""Files the user uploaded through the web UI, plus the LangChain tools that let
the agent list, read and push them to Google Drive.

Uploads live in uploads/<thread_id>/ so each chat only sees its own files.
Composio tools run on Composio's servers and cannot see this disk, so the Drive
upload stages the file to Composio storage first (SDK auto-upload, restricted to
UPLOAD_ROOT) and then runs GOOGLEDRIVE_UPLOAD_FILE with that reference.
"""
import json
import mimetypes
import os
import re
import shutil
import uuid

mimetypes.add_type("text/csv", ".csv")
mimetypes.add_type("text/markdown", ".md")
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

load_dotenv()

from pipeline import drive_targets, parse_jobs  # noqa: E402

UPLOAD_ROOT = Path(os.getenv("UPLOAD_DIR", Path(__file__).resolve().parents[1] / "uploads")).resolve()
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "100"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
SIMPLE_UPLOAD_MAX = 5 * 1024 * 1024  # GOOGLEDRIVE_UPLOAD_FILE limit; bigger files use the resumable upload
RESUMABLE_SLUG = "GOOGLEDRIVE_RESUMABLE_UPLOAD"
CHUNK_BYTES = 8 * 1024 * 1024  # must be a multiple of 256 KB
MAX_READ_CHARS = 20_000
DRIVE_UPLOAD_SLUG = "GOOGLEDRIVE_UPLOAD_FILE"

# Tool names, used by agent.py's approval gate.
LOCAL_READ_TOOLS = {"list_uploaded_files", "read_uploaded_file"}
LOCAL_WRITE_TOOLS = {"upload_file_to_drive"}


# ---------- storage ----------

def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", value)[:64] or "default"


def thread_dir(thread_id: str) -> Path:
    d = UPLOAD_ROOT / _safe_id(thread_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_filename(name: str) -> str:
    name = Path(name.replace("\\", "/")).name  # strip any client-side directory
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:180] or "file"


def save_upload(thread_id: str, filename: str, data: bytes) -> dict:
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"{filename}: {len(data) / 1048576:.1f} MB is over the {MAX_UPLOAD_MB} MB upload limit")
    d = thread_dir(thread_id)
    name = safe_filename(filename)
    path = d / name
    stem, suffix, n = path.stem, path.suffix, 1
    while path.exists():
        path = d / f"{stem} ({n}){suffix}"
        n += 1
    path.write_bytes(data)
    return describe(path)


def describe(path: Path) -> dict:
    return {
        "file_id": path.name,
        "name": path.name,
        "size_bytes": path.stat().st_size,
        "mimetype": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    }


def list_files(thread_id: str) -> list[dict]:
    return [describe(p) for p in sorted(thread_dir(thread_id).iterdir()) if p.is_file()]


def resolve(thread_id: str, file_id: str) -> Path:
    d = thread_dir(thread_id)
    path = (d / safe_filename(file_id)).resolve()
    if path.parent != d.resolve() or not path.is_file():
        names = [f["file_id"] for f in list_files(thread_id)]
        raise FileNotFoundError(f"No uploaded file '{file_id}'. Available: {names or 'none'}")
    return path


def delete_file(thread_id: str, file_id: str) -> None:
    resolve(thread_id, file_id).unlink()


# ---------- reading ----------

TEXT_EXT = {".txt", ".csv", ".tsv", ".md", ".json", ".xml", ".html", ".htm", ".log", ".yaml", ".yml"}


def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in TEXT_EXT:
        return path.read_text(encoding="utf-8", errors="replace")
    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = [f"--- page {i + 1} ---\n{p.extract_text() or ''}" for i, p in enumerate(reader.pages)]
        text = "\n".join(pages)
        return text if text.strip(" -\npage0123456789") else "(PDF has no extractable text; it may be a scan.)"
    if ext in {".xlsx", ".xlsm"}:
        from openpyxl import load_workbook

        wb = load_workbook(str(path), read_only=True, data_only=True)
        out = []
        try:
            for ws in wb.worksheets:
                out.append(f"--- sheet: {ws.title} ---")
                for row in ws.iter_rows(values_only=True):
                    if any(v is not None for v in row):
                        out.append("\t".join("" if v is None else str(v) for v in row))
        finally:
            wb.close()  # read_only mode keeps the file open (locks it on Windows)
        return "\n".join(out)
    if ext == ".docx":
        from docx import Document

        doc = Document(str(path))
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.append("\t".join(c.text for c in row.cells))
        return "\n".join(parts)
    return f"(Cannot read {ext or 'this'} files as text; it can still be uploaded to Drive.)"


# ---------- Drive upload ----------

@lru_cache(maxsize=1)
def _uploader():
    """Separate Composio client that may read local files, but only inside UPLOAD_ROOT."""
    from composio import Composio

    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    return Composio(
        dangerously_allow_auto_upload_download_files=True,
        file_upload_dirs=[str(UPLOAD_ROOT)],
    )


def upload_to_drive(user_id: str, path: Path, folder_id: str | None = None) -> dict:
    """Small files: GOOGLEDRIVE_UPLOAD_FILE (5 MB cap). Larger: chunked GOOGLEDRIVE_RESUMABLE_UPLOAD."""
    args = {"file_to_upload": str(path)}
    if folder_id:
        args["folder_to_upload_to"] = folder_id
    slug = DRIVE_UPLOAD_SLUG
    if path.stat().st_size > SIMPLE_UPLOAD_MAX:
        slug = RESUMABLE_SLUG
        args["chunkSize"] = CHUNK_BYTES
        args["metadata"] = {"name": path.name, **({"parents": [folder_id]} if folder_id else {})}
    res = _uploader().tools.execute(slug, args, user_id=user_id, dangerously_skip_version_check=True)
    return res if isinstance(res, dict) else dict(res)


# ---------- LangChain tools ----------

def _thread(config: RunnableConfig) -> str:
    return str((config.get("configurable") or {}).get("thread_id", "default"))


def make_local_tools(user_id: str) -> list:
    drive_targets.configure(user_id)

    @tool
    def list_uploaded_files(config: RunnableConfig) -> str:
        """List the files the user uploaded in this chat (file_id, name, size, mimetype)."""
        return json.dumps(list_files(_thread(config)))

    @tool
    def read_uploaded_file(file_id: str, config: RunnableConfig) -> str:
        """Read the text content of a file the user uploaded in this chat.
        Supports txt/csv/md/json, PDF, Excel (.xlsx) and Word (.docx)."""
        try:
            text = extract_text(resolve(_thread(config), file_id))
        except Exception as e:  # report, don't crash the graph
            return f"Error reading {file_id}: {e}"
        if len(text) > MAX_READ_CHARS:
            text = text[:MAX_READ_CHARS] + f"\n... [truncated, {len(text)} chars total]"
        return text

    @tool
    def upload_file_to_drive(file_id: str, doc_type: str, amc: str, year: int, month: str,
                             config: RunnableConfig) -> str:
        """Upload ONE file the user uploaded in this chat to the Drive folder for one AMC or SIF.
        doc_type: "Monthly Portfolio Disclosure", "Factsheet", "SIF Monthly Portfolio Disclosure" or "SIF Factsheet".
        amc: an AMC name (MF types) or SIF name (SIF types) from the page's list.
        year: e.g. 2026. month: JAN..DEC. The destination folder and factsheet name are found automatically
        (portfolio -> <root>/<year>/<month>/<AMC or SIF folder>/, factsheet -> <root>/<year>/<month>/<name>.pdf)."""
        try:
            path = resolve(_thread(config), file_id)
        except Exception as e:
            return f"Upload of {file_id} failed: {e}"
        p = drive_targets.plan(doc_type, amc, year, month, path.name)
        if not p["ok"]:
            return f"Not uploaded ({file_id} -> {amc}): {p['error']}"
        staged = path
        try:
            if p["upload_name"] != path.name:  # Drive takes the name from the file, so stage a renamed copy
                staged = UPLOAD_ROOT / "_staging" / uuid.uuid4().hex / safe_filename(p["upload_name"])
                staged.parent.mkdir(parents=True)
                shutil.copyfile(path, staged)
            res = upload_to_drive(user_id, staged, p["folder_id"])
            ok = res.get("successful", True)
            # Also keep a copy in Azure storage (and parse it); never fails the Drive upload.
            stored = parse_jobs.archive(path, doc_type, amc, year, month, p["upload_name"]) if ok else {}
        except Exception as e:
            return f"Upload of {file_id} failed: {e}"
        finally:
            if staged != path:
                shutil.rmtree(staged.parent, ignore_errors=True)
        drive_targets.forget_folder(p["folder_id"])
        if not ok:
            return f"Upload of {file_id} failed: {res.get('error')}"
        data = res.get("data") or {}
        link = (data.get("webViewLink") or (data.get("response_data") or {}).get("webViewLink")
                or data.get("display_url"))
        return json.dumps({"uploaded": file_id, "saved_as": f"{p['path']}/{p['upload_name']}", "link": link,
                           **stored}, default=str)

    return [list_uploaded_files, read_uploaded_file, upload_file_to_drive]
