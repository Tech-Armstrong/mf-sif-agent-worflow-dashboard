"""LangGraph agent with Composio tools and a human approval gate before writes."""
import os
from typing import Literal

from langchain_core.messages import SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt

from langchain_core.runnables import RunnableConfig

from pipeline import drive_targets
from app.local_files import LOCAL_READ_TOOLS, LOCAL_WRITE_TOOLS, list_files, make_local_tools

SYSTEM = """You are an assistant with access to the user's connected apps (Gmail, Google Drive) via Composio.
- For Gmail/Drive actions, first call COMPOSIO_SEARCH_TOOLS to find the right tool and its schema, then run it with COMPOSIO_MULTI_EXECUTE_TOOL.
- Keep searches narrow (sender, subject, date, small max_results). Fetch full bodies only for likely matches.
- Never invent email content, file contents or recipients. Say what you could not read.
- Sending, sharing, deleting, editing or uploading needs the user's approval. The system asks them automatically, so do NOT
  ask for confirmation in chat: just call the tool. If declined, stop and ask what to change.
- If an app is not connected, tell the user to connect it (run connect.py) instead of retrying.
- Files the user uploaded in this chat are local: use list_uploaded_files and read_uploaded_file to see them.
- list_uploaded_files, read_uploaded_file and upload_file_to_drive are your own direct tools: call them directly.
  Never search for them with COMPOSIO_SEARCH_TOOLS or run them through COMPOSIO_MULTI_EXECUTE_TOOL.
- To put an uploaded file in Google Drive, ALWAYS use upload_file_to_drive (never GOOGLEDRIVE_UPLOAD_FILE through
  COMPOSIO_MULTI_EXECUTE_TOOL: Composio cannot see local files). Do not search Drive for folders: the tool finds the
  destination from doc_type, amc, year and month.
- The user picks these on the page; each message starts with [Upload settings: ...]. Use exactly those values.
  There are four types: "Monthly Portfolio Disclosure" and "Factsheet" (mutual funds, one per AMC) and
  "SIF Monthly Portfolio Disclosure" and "SIF Factsheet" (Specialised Investment Funds, one per SIF such as
  "Altiva SIF", "iSIF", "DynaSIF"). For SIF types, pass the SIF name as the amc argument.
  * Every file (portfolio disclosure or factsheet) belongs to exactly ONE of the selected AMCs/SIFs: one call per
    file. With one selected, all files go to it. Otherwise match each file from the file name (e.g.
    "BNP Value Fund.xlsx" -> Baroda BNP Paribas, "Adithya Birla Value Fund" -> Aditya Birla, "Edelweiss Hybrid
    Long Short Fund.xlsx" -> Altiva SIF, "SIF Platinum Monthly Portfolio.xlsx" -> Platinum SIF) or, if the name
    doesn't say, by reading the file. If a file can't be matched with confidence, or it matches one that isn't
    selected, upload the files you are sure of and ask the user about the rest instead of guessing.
  * Portfolio disclosures keep the file name. MF Factsheet is saved as "<AMC>.pdf"; SIF Factsheet as
    "<SIF short name> Factsheet.pdf" (e.g. "Altiva Factsheet.pdf"). The tool names them; one factsheet each.
  * If none is selected, ask the user to pick AMCs/SIFs on the page.
- If an upload reports a missing folder, tell the user exactly which folder is missing and that they need to create it
  in Drive. Never create, rename or move Drive folders yourself, and do not offer to.
- When asked to read files, summarise what is actually in them and quote figures exactly."""

# Composio meta tools that only look things up.
SAFE_META = {"COMPOSIO_SEARCH_TOOLS", "COMPOSIO_GET_TOOL_SCHEMAS"} | LOCAL_READ_TOOLS
MULTI_EXECUTE = "COMPOSIO_MULTI_EXECUTE_TOOL"
# Verb right after the toolkit prefix, e.g. GMAIL_[FETCH]_EMAILS, GOOGLEDRIVE_[FIND]_FILE.
READ_VERBS = {"FETCH", "GET", "LIST", "SEARCH", "FIND", "READ", "DOWNLOAD", "QUERY", "CHECK"}
# Exact app tools you are happy to run without asking (e.g. drafts). Empty = ask for every write.
AUTO_APPROVE: set[str] = set()


def is_read_only(slug: str) -> bool:
    parts = slug.upper().split("_")
    return slug.upper() in AUTO_APPROVE or (len(parts) > 1 and parts[1] in READ_VERBS)


def pending_writes(tool_call: dict) -> list[dict]:
    """App actions inside one LLM tool call that need approval."""
    name, args = tool_call["name"], tool_call.get("args") or {}
    if name in SAFE_META:
        return []
    if name == MULTI_EXECUTE:
        items = args.get("tools") or []
        writes = [
            {"tool": i.get("tool_slug", "?"), "arguments": i.get("arguments", {})}
            for i in items
            if not is_read_only(str(i.get("tool_slug", "")))
        ]
        return writes if items else [{"tool": name, "arguments": args}]
    return [{"tool": name, "arguments": args}]  # unknown meta tool: always ask


def local_tools_inside_multi_execute(tool_call: dict) -> set[str]:
    """The model sometimes wraps our local tools in COMPOSIO_MULTI_EXECUTE_TOOL, which can only fail."""
    if tool_call["name"] != MULTI_EXECUTE:
        return set()
    slugs = {str(i.get("tool_slug", "")) for i in (tool_call.get("args") or {}).get("tools") or []}
    return {s for s in slugs if s.lower() in LOCAL_READ_TOOLS | LOCAL_WRITE_TOOLS}


def misrouted_uploads(tool_call: dict, local_names: set[str]) -> list[str]:
    """GOOGLEDRIVE_UPLOAD_FILE calls that point at a locally uploaded file (or have no real
    s3key). Composio cannot reach those, so they would only fail: they are bounced back to
    the model instead of being shown for approval. Real keys come from earlier tool results,
    e.g. a Gmail attachment: '47563/gmail/GET_ATTACHMENT/response/123'."""
    if tool_call["name"] != MULTI_EXECUTE:
        return []
    bad = []
    for item in (tool_call.get("args") or {}).get("tools") or []:
        if str(item.get("tool_slug", "")).upper() != "GOOGLEDRIVE_UPLOAD_FILE":
            continue
        f = (item.get("arguments") or {}).get("file_to_upload")
        key = str(f.get("s3key", "")) if isinstance(f, dict) else str(f or "")
        name = str(f.get("name", "")) if isinstance(f, dict) else ""
        tail = key.replace("\\", "/").rsplit("/", 1)[-1]
        if "/" not in key or tail in local_names or (name in local_names and tail == name):
            bad.append(name or key)
    return bad


def build_graph(llm, tools, checkpointer=None):
    model = llm.bind_tools(tools)

    def agent(state: MessagesState):
        return {"messages": [model.invoke([SystemMessage(SYSTEM), *state["messages"]])]}

    def route(state: MessagesState) -> Literal["review", "__end__"]:
        return "review" if getattr(state["messages"][-1], "tool_calls", None) else END

    def review(state: MessagesState, config: RunnableConfig) -> Command[Literal["tools", "agent"]]:
        calls = state["messages"][-1].tool_calls
        thread_id = str((config.get("configurable") or {}).get("thread_id", "default"))
        local = {f["file_id"] for f in list_files(thread_id)}
        errors, previews = {}, {}
        for tc in calls:
            bad = misrouted_uploads(tc, local)
            wrapped = sorted(local_tools_inside_multi_execute(tc))
            if wrapped:
                errors[tc["id"]] = (f"Not run: {wrapped} are your own direct tools, not Composio tools. "
                                    "Call them directly (e.g. upload_file_to_drive(...)), not via COMPOSIO_MULTI_EXECUTE_TOOL.")
            elif bad:
                errors[tc["id"]] = (f"Not run: {bad} are files the user uploaded locally; Composio cannot reach "
                                    "them. Call upload_file_to_drive for each one instead.")
            elif tc["name"] == "upload_file_to_drive":
                a = tc.get("args") or {}
                fid = str(a.get("file_id", ""))
                if fid not in local:
                    errors[tc["id"]] = f"Not uploaded: no uploaded file '{fid}'. Available: {sorted(local)}"
                    continue
                conflict = drive_targets.name_conflict(str(a.get("amc", "")), fid, a.get("doc_type"))
                if conflict:
                    errors[tc["id"]] = (f"Not uploaded: {conflict} Send it to the matching selected AMC, "
                                        "or ask the user if that AMC isn't selected.")
                    continue
                p = drive_targets.plan(a.get("doc_type"), a.get("amc"), a.get("year"), a.get("month"), fid)
                if not p["ok"]:
                    errors[tc["id"]] = f"Not uploaded ({fid} -> {a.get('amc')}): {p['error']} Tell the user; do not retry."
                else:
                    previews[tc["id"]] = {"file": fid, "save to": f"{p['path']}/{p['upload_name']}",
                                          **({"warning": p["warning"]} if p.get("warning") else {})}
        if errors:  # every tool call needs an answer, so the valid ones are sent back to be re-issued
            skipped = "Not run because another action in this step failed. Run it again on its own."
            return Command(
                goto="agent",
                update={"messages": [ToolMessage(content=errors.get(tc["id"], skipped), tool_call_id=tc["id"])
                                     for tc in calls]},
            )
        writes = [w for tc in calls for w in
                  ([{"tool": "Upload to Drive", "arguments": previews[tc["id"]]}] if tc["id"] in previews
                   else pending_writes(tc))]
        if not writes:
            return Command(goto="tools")
        decision = interrupt({"actions": writes})  # pauses; resume with Command(resume={"approve": bool})
        if isinstance(decision, dict) and decision.get("approve"):
            return Command(goto="tools")
        note = (decision or {}).get("feedback") if isinstance(decision, dict) else None
        msg = "The user declined this action. Do not retry it." + (f" User says: {note}" if note else "")
        return Command(
            goto="agent",
            update={"messages": [ToolMessage(content=msg, tool_call_id=tc["id"]) for tc in calls]},
        )

    g = StateGraph(MessagesState)
    g.add_node("agent", agent)
    g.add_node("review", review)
    g.add_node("tools", ToolNode(tools))
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route)
    g.add_edge("tools", "agent")
    return g.compile(checkpointer=checkpointer or InMemorySaver())


def default_llm():
    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]
    if "/openai/v1" in endpoint:  # new Azure v1 API: OpenAI-compatible, no api-version
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            base_url=endpoint, api_key=os.environ["AZURE_OPENAI_API_KEY"], model=deployment
        )
    from langchain_openai import AzureChatOpenAI

    return AzureChatOpenAI(azure_endpoint=endpoint, azure_deployment=deployment)


def make_agent(user_id: str, checkpointer=None):
    from composio_setup import get_session

    tools = get_session(user_id).tools()  # LangChain StructuredTools (Composio meta tools)
    tools = [*tools, *make_local_tools(user_id)]  # uploaded-file tools (local disk -> Drive)
    return build_graph(default_llm(), tools, checkpointer)