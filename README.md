# Composio connected-apps agent (LangGraph)

Gmail + Google Drive (or any Composio toolkit) for a LangGraph agent, with a human approval step before any send/share/delete/edit.

## Setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # fill COMPOSIO_API_KEY + Azure OpenAI vars
```

## 1. Connect a user's Google account (once per user)
```bash
python connect.py techops gmail googledrive
```
Open each printed link, sign in, and the script waits until the connection is ACTIVE.
`techops` is *your* id for the user; Composio stores their tokens under it.

## 2. Chat
```bash
python chat.py techops
```
Example: `find the latest email from HDFC AMC and save its PDF attachment to Drive`
Reads run directly; the upload pauses and asks `Approve? [y/N]`.

## 3. Web UI: upload files → agent reads them → saves to Drive
```bash
python server.py            # uses AGENT_USER_ID from .env (default: techops)
```
Open http://127.0.0.1:8000, drop in your files (PDF, Excel, Word, CSV, text; max 100 MB each, set MAX_UPLOAD_MB in .env), then ask e.g.
`Read both files and upload them to my 'Reports' folder in Drive`. Each Drive upload shows an **Approve / Decline** card.

Why this needed new code: Composio tools run on Composio's servers, so they cannot see files on this machine.
`local_files.py` gives the agent three local tools:

| Tool | Approval |
|---|---|
| `list_uploaded_files` | no |
| `read_uploaded_file(file_id)`: text from txt/csv/md/json, PDF, .xlsx, .docx | no |
| `upload_file_to_drive(file_id, folder_id?)`: stages the file to Composio storage, then runs `GOOGLEDRIVE_UPLOAD_FILE` | **yes** |

Staging uses a separate Composio client with `dangerously_allow_auto_upload_download_files=True` and
`file_upload_dirs=[uploads/]`, so it can only read files the user uploaded. If the model tries `GOOGLEDRIVE_UPLOAD_FILE`
directly with a local filename, `agent.py` sends it back to use `upload_file_to_drive` instead.

Uploads are kept in `uploads/<chat id>/` (set `UPLOAD_DIR` to move them). Chat memory is in-process, so restarting the server starts new chats.

### Drive destination (Type / AMC / Year / Month)
The left panel picks where files go. `drive_targets.py` finds the folder in code (the agent doesn't search Drive):

| Type | Destination | Name in Drive |
|---|---|---|
| Monthly Portfolio Disclosure | `Historical MF Monthly Portfolio/<YYYY>/<MON>/<AMC folder>/`, each file to its own AMC | as uploaded |
| Factsheet | `Historical MF Factsheets/<YYYY>/<MON>/`, one file per AMC | `<AMC>.pdf` |
| SIF Monthly Portfolio Disclosure | `Historical SIF Monthly Portfolio/<YYYY>/<MON>/<SIF folder>/`, each file to its own SIF | as uploaded |
| SIF Factsheet | `Historical SIF Factsheet/<YYYY>/<MON>/`, one file per SIF | `<SIF> Factsheet.pdf` (e.g. `Altiva Factsheet.pdf`) |

- The page picks **Fund** (MF / SIF) and **Type** (Monthly Portfolio / Factsheet). MF types use the 21 AMCs (`AMCS`), SIF types the 15 SIFs (`SIFS`); Altiva matches "Edelweiss …" file names only under SIF types.
- SIF coverage columns are strategies (Equity Long-Short, Ex-Top 100, Hybrid, Active Asset Allocator, …); files whose name doesn't say the strategy (e.g. `SIF Platinum Monthly Portfolio.xlsx`) count as "Other". The `2025-old` / `2026-old` folders are ignored.

- Each file goes to ONE selected AMC, matched by file name or contents; the agent asks when unsure. If a file name names a different AMC than the one chosen, the upload is sent back before approval.
- AMC folders are matched by keyword (`AMCS` in `drive_targets.py`), so `HDFC Mutual fund ` and `Canara Robeco` still match.
- Folders are never created. If a Year/Month/AMC folder is missing, the upload stops and names the missing folder.
- The approval card shows the exact Drive path, and warns if a file with that name or AMC is already there.
- Edit `AMCS` or `DOC_TYPES` in `drive_targets.py` to add AMCs or types.

### Coverage tab (what's uploaded / missing)
Read-only view of Drive for any Year/Month:
- **Monthly Portfolio Disclosure:** AMC × fund-category grid (Large, Mid, Small, Flexi, Focused, Value/Contra,
  Balanced Advantage, Multi Asset, Business Cycle, Conservative Hybrid). A category counts as *expected* if the AMC
  had it in any of the previous 3 months (`LOOKBACK_MONTHS`); otherwise it shows as not offered.
- **Factsheet:** one tile per AMC, uploaded or missing.
- Categories come from file names (`CATEGORIES` in `drive_targets.py`, including common typos). Files that match no
  category are listed under the grid.
- First load of a month takes about 20 s; results are cached for 5 minutes (**Refresh** re-reads Drive).

## How it works
| File | Role |
|---|---|
| `composio_setup.py` | `Composio(provider=LangchainProvider())` → `create(user_id, toolkits, manage_connections=False, sandbox off)` |
| `connect.py` | `session.authorize(toolkit)` → `redirect_url` → `wait_for_connection()` |
| `agent.py` | `agent → review → tools` graph. `review` inspects every `COMPOSIO_MULTI_EXECUTE_TOOL` call and `interrupt()`s on non-read actions |
| `chat.py` | CLI that resumes the graph with `Command(resume={"approve": bool, "feedback": str})` |
| `local_files.py` | upload storage, file reading, local → Drive upload tools |
| `server.py` + `static/index.html` | FastAPI backend and the upload / chat / approve page |

The model gets Composio's meta tools (`COMPOSIO_SEARCH_TOOLS`, `COMPOSIO_MULTI_EXECUTE_TOOL`, …), not 400 Gmail/Drive tools, so context stays small. Same pattern Nod uses.

**Read vs write rule** (`agent.py`): the verb after the toolkit prefix decides it.
`GMAIL_FETCH_EMAILS`, `GOOGLEDRIVE_FIND_FILE` → run. `GMAIL_SEND_EMAIL`, `GOOGLEDRIVE_UPLOAD_FILE` → ask.
Add exact slugs to `AUTO_APPROVE` to skip asking (e.g. `GMAIL_CREATE_EMAIL_DRAFT`).

## Add more apps
Add slugs to `COMPOSIO_TOOLKITS` (e.g. `gmail,googledrive,googlecalendar,googlesheets,slack,notion`), then run `connect.py` for the new ones.

## Taking it to production
- **Web sign-in:** call `session.authorize(toolkit, callback_url="https://your.app/composio/callback")` from a FastAPI route and send the user to `redirect_url`.
- **Persistent threads:** swap `InMemorySaver` for `langgraph-checkpoint-postgres` so approvals survive restarts.
- **Your own Google consent screen:** by default users see Composio's OAuth app. To show Armstrong's name, create an auth config in Composio with your Google OAuth client and pass `auth_configs={"gmail": "<auth_config_id>"}` to `create()`. External users + Gmail read scopes then need Google verification.
- **Token security:** Google tokens live in Composio, never in your DB; you only keep `user_id`.
