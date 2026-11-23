"""Web UI: upload files, chat with the agent, approve Drive uploads.

    python server.py                # user id from AGENT_USER_ID (default: techops)
    open http://127.0.0.1:8000
"""
import os
import threading
import uuid
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel

import drive_targets
import local_files
from agent import make_agent

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
