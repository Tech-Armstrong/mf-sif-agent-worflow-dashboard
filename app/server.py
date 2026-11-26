"""Web UI: upload files, chat with the agent, approve Drive uploads.

    python server.py                # user id from AGENT_USER_ID (default: techops)
    open http://127.0.0.1:8000
"""
import io
import mimetypes
import os
import threading
import uuid
from urllib.parse import quote
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel

from app import local_files
from app.agent import make_agent
from pipeline import blob_store, drive_targets, parse_jobs

USER_ID = os.getenv("AGENT_USER_ID", "techops")  # Composio user whose Google account is connected
STATIC = Path(__file__).parent / "static"

drive_targets.configure(USER_ID)  # coverage can be opened before the agent is built
app = FastAPI(title="Drive upload agent")
_locks: dict[str, threading.Lock] = {}


@lru_cache(maxsize=1)
def graph():
    return make_agent(USER_ID)


def _lock(thread_id: str) -> threading.Lock:
    return _locks.setdefault(thread_id, threading.Lock())


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _reply(result: dict) -> dict:
    if result.get("__interrupt__"):
        return {"status": "approval", "actions": result["__interrupt__"][0].value["actions"]}
    content = result["messages"][-1].content
    if isinstance(content, list):
        content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return {"status": "done", "reply": content}


class Settings(BaseModel):
    doc_type: str = ""
    amcs: list[str] = []
    year: int | None = None
    month: str = ""


class ChatIn(BaseModel):
    thread_id: str
    message: str
    settings: Settings | None = None


def _with_settings(message: str, s: Settings | None) -> str:
    if not s:
        return message
    sif = drive_targets.DOC_TYPES.get(s.doc_type, {}).get("managers") == "sif"
    amcs = [a for a in s.amcs if a in drive_targets.managers_for(s.doc_type)]
    head = (f"[Upload settings: Type={s.doc_type or 'not chosen'}; Year={s.year or 'not chosen'}; "
            f"Month={s.month or 'not chosen'}; {'SIFs' if sif else 'AMCs'}={amcs or 'none selected'}]")
    return f"{head}\n{message}"


class ApproveIn(BaseModel):
    thread_id: str
    approve: bool
    feedback: str = ""


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/options")
def options():
    return drive_targets.options()


@app.get("/api/coverage")
def coverage(doc_type: str, year: int, month: str, refresh: bool = False):
    """Read-only: what is uploaded / missing in Drive for one month."""
    if doc_type not in drive_targets.DOC_TYPES or month.upper()[:3] not in drive_targets.MONTHS:
        raise HTTPException(400, "Unknown type or month.")
    if refresh:
        drive_targets.clear_cache()
    try:
        return drive_targets.coverage(doc_type, year, month)
    except Exception as e:
        raise HTTPException(502, f"Could not read Drive: {e}")


@app.get("/api/parsed")
def parsed_list(doc_type: str, year: int, month: str):
    """Files archived in Azure storage for one month, with their parse status."""
    if doc_type not in drive_targets.DOC_TYPES or month.upper()[:3] not in drive_targets.MONTHS:
        raise HTTPException(400, "Unknown type or month.")
    try:
        return parse_jobs.month_listing(doc_type, year, month)
    except Exception as e:
        raise HTTPException(502, f"Could not read storage: {e}")


@app.get("/api/parsed/result")
def parsed_result(path: str):
    try:
        r = parse_jobs.result(path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if r is None:
        raise HTTPException(404, "This file has not been parsed yet.")
    return r


@app.get("/api/parsed/download")
def parsed_download(path: str):
    """The parsed JSON as a file download."""
    r = parsed_result(path)
    name = path.rsplit("/", 1)[-1].rsplit(".", 1)[0] + ".json"
    return JSONResponse(r, headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _file_merged(path: str) -> dict:
    try:
        r = parse_jobs.file_merged(path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Could not build the merged data: {e}")
    if r is None:
        raise HTTPException(404, "This file has no parsed holdings yet.")
    return r


def _source_bytes(path: str) -> tuple[str, bytes]:
    try:
        p = blob_store.clean_path(path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not p.startswith(blob_store.FILES + "/"):
        raise HTTPException(400, "Only uploaded files can be shown.")
    data = blob_store.read_bytes(p)
    if data is None:
        raise HTTPException(404, "This file is no longer in storage.")
    return p, data


@app.get("/api/source/file")
def source_file(path: str):
    """An uploaded file as stored (the factsheet PDF opens in the browser's PDF viewer)."""
    p, data = _source_bytes(path)
    name = p.rsplit("/", 1)[-1]
    media = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return Response(data, media_type=media,
                    headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}"})


MAX_SHEET_ROWS, MAX_SHEET_COLS = 3000, 40


@app.get("/api/source/sheets")
def source_sheets(path: str):
    """An uploaded Excel file's sheets as rows of text, for the Excel view next to the parsed data."""
    import pandas as pd

    p, data = _source_bytes(path)
    ext = p.rsplit(".", 1)[-1].lower()
    engine = {"xls": "xlrd", "xlsb": "pyxlsb"}.get(ext, "openpyxl")
    try:
        book = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, engine=engine, dtype=object)
    except Exception as e:
        raise HTTPException(422, f"Could not read this workbook: {e}")
    sheets = []
    for name, df in book.items():
        df = df.dropna(how="all").dropna(axis=1, how="all")
        sub = df.iloc[:MAX_SHEET_ROWS, :MAX_SHEET_COLS].astype(object)
        rows = sub.where(sub.notna(), "").values.tolist()
        sheets.append({"name": str(name), "rows": [[_cell(v) for v in r] for r in rows],
                       "truncated": len(df) > MAX_SHEET_ROWS or df.shape[1] > MAX_SHEET_COLS})
    return {"path": p, "sheets": sheets}


def _cell(v) -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, float):
        return f"{v:,.4f}".rstrip("0").rstrip(".") if abs(v) < 1e15 else str(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()[:10]
    return str(v)


@app.get("/api/parsed/merged")
def parsed_merged(path: str):
    """One parsed portfolio file in the merged schema: holdings + fund managers + AUM."""
    return _file_merged(path)


@app.get("/api/parsed/merged/download")
def parsed_merged_download(path: str):
    r = _file_merged(path)
    name = path.rsplit("/", 1)[-1].rsplit(".", 1)[0] + ".json"
    return JSONResponse(r.get("merged") or {}, headers={"Content-Disposition": f'attachment; filename="{name}"'})


class ParseIn(BaseModel):
    path: str
    scope: str = "uploaded"  # factsheets: "uploaded" = only funds with a parsed portfolio, "all" = every fund
    what: str | None = None  # portfolio files: "portfolio", "factsheet" (its AMC's) or "both"


@app.post("/api/parsed/run")
def parsed_run(body: ParseIn):
    """Parse (or re-parse) one archived file in the background."""
    try:
        if body.what:
            return parse_jobs.reparse(body.path, body.what)
        return parse_jobs.queue(body.path, body.scope)
    except ValueError as e:
        raise HTTPException(400, str(e))


class ManagerIn(BaseModel):
    name: str
    since: str  # YYYY-MM or YYYY-MM-DD
    role: str = ""


class FactsheetEditIn(BaseModel):
    path: str  # the factsheet (files/...pdf)
    scheme_code: str
    aum_month_end_cr: float | str | None = None
    aum_monthly_avg_cr: float | str | None = None
    managers: list[ManagerIn] = []
    scheme_name: str = ""  # only to add a fund the parser did not read
    category: str = ""


class FactsheetRevertIn(BaseModel):
    path: str
    scheme_code: str


def _after_edit(path: str, r: dict) -> dict:
    """Rebuild the month's merged JSON in storage now, so it shows the edit without waiting for a reader."""
    info = parse_jobs.describe(path)
    try:
        parse_jobs.merged(info["year"], info["month"], fund=info["fund"])
    except Exception as e:
        r = {**r, "warning": f"Saved, but the month's merged JSON was not rebuilt: {e}"}
    return {**r, "hand_edits": parse_jobs.factsheet_edits(path)}


@app.get("/api/parsed/factsheet/edits")
def factsheet_edits(path: str):
    """The hand edits of one factsheet, {scheme_code: edit}."""
    try:
        return parse_jobs.factsheet_edits(path)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/parsed/factsheet/edit")
def factsheet_edit(body: FactsheetEditIn):
    """Set one fund's AUM and fund managers by hand: the factsheet's JSON, the merge's data and the merged JSON."""
    try:
        r = parse_jobs.edit_factsheet(body.path, body.scheme_code, body.aum_month_end_cr, body.aum_monthly_avg_cr,
                                      [m.model_dump() for m in body.managers], body.scheme_name, body.category,
                                      edited_by=USER_ID)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _after_edit(body.path, r)


@app.post("/api/parsed/factsheet/revert")
def factsheet_revert(body: FactsheetRevertIn):
    """Undo one fund's hand edit: back to what the parser read."""
    try:
        r = parse_jobs.revert_factsheet_edit(body.path, body.scheme_code)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _after_edit(body.path, r)


def _merged(year: int, month: str, rebuild: bool = False, fund: str = "MF") -> dict:
    if month.upper()[:3] not in drive_targets.MONTHS:
        raise HTTPException(400, "Unknown month.")
    if fund.upper() not in ("MF", "SIF"):
        raise HTTPException(400, "fund is MF or SIF.")
    try:
        return parse_jobs.merged(year, month, rebuild, fund)
    except Exception as e:
        raise HTTPException(502, f"Could not build the merged data: {e}")


@app.get("/api/merged")
def merged(year: int, month: str, rebuild: bool = False, fund: str = "MF"):
    """One month's holdings joined with fund managers and AUM from the factsheets (fund: MF or SIF)."""
    return _merged(year, month, rebuild, fund)


@app.get("/api/merged/download")
def merged_download(year: int, month: str, fund: str = "MF"):
    r = _merged(year, month, fund=fund)
    name = f"merged_{'SIF_' if fund.upper() == 'SIF' else ''}{year}_{month.upper()[:3]}.json"
    return JSONResponse(r.get("merged") or {}, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/session")
def new_session():
    return {"thread_id": uuid.uuid4().hex, "user_id": USER_ID}


@app.post("/api/upload")
async def upload(thread_id: str = Form(...), files: list[UploadFile] = File(...)):
    saved, errors = [], []
    for f in files:
        try:
            saved.append(local_files.save_upload(thread_id, f.filename or "file", await f.read()))
        except ValueError as e:
            errors.append(str(e))
    return {"saved": saved, "errors": errors, "files": local_files.list_files(thread_id)}


@app.get("/api/files")
def files(thread_id: str):
    return {"files": local_files.list_files(thread_id)}


@app.delete("/api/files")
def remove(thread_id: str, file_id: str):
    try:
        local_files.delete_file(thread_id, file_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return {"files": local_files.list_files(thread_id)}


@app.post("/api/chat")
def chat(body: ChatIn):
    cfg = _cfg(body.thread_id)
    with _lock(body.thread_id):
        if graph().get_state(cfg).interrupts:
            # A new message while an approval is open counts as "decline, do this instead";
            # otherwise the pending tool call is left unanswered and the thread breaks.
            text = _with_settings(body.message, body.settings)
            result = graph().invoke(Command(resume={"approve": False, "feedback": text}), cfg)
        else:
            text = _with_settings(body.message, body.settings)
            result = graph().invoke({"messages": [HumanMessage(text)]}, cfg)
        return _reply(result)


@app.post("/api/approve")
def approve(body: ApproveIn):
    with _lock(body.thread_id):
        if not graph().get_state(_cfg(body.thread_id)).interrupts:
            raise HTTPException(409, "Nothing is waiting for approval in this chat.")
        result = graph().invoke(
            Command(resume={"approve": body.approve, "feedback": body.feedback}), _cfg(body.thread_id)
        )
        return _reply(result)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")))
