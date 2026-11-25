# Composio connected-apps agent (LangGraph)

Gmail + Google Drive (or any Composio toolkit) for a LangGraph agent, with a human approval step before any send/share/delete/edit.

## Repository layout
```
Dockerfile, requirements.txt   the web app's image (Azure's auto-deploy builds ./Dockerfile)
app/                           the web app
  server.py                    FastAPI backend: upload, chat, approve, Coverage and Parsed tabs
  static/index.html            the page
  agent.py                     LangGraph agent with the approval step
  composio_setup.py            Composio client and session
  local_files.py               upload storage, file reading, local -> Drive upload tools
pipeline/                      shared by the app and the parse jobs
  drive_targets.py             Drive destinations: doc types, AMCs, SIFs, coverage
  blob_store.py                Azure Blob Storage (or local archive/) and the queues
  parse_jobs.py                archive -> parse -> parsed JSON, Parsed-tab listing, merged JSON
  worker.py                    entry point of the parse Container Apps Jobs
parsers/                       parsers copied from their own repos (see each SOURCE.md)
  historical/                  MF: Historical-Scheme-Portfolio
  sif/                         SIF: sif-parser
  factsheet_exits.py           exit dates in one factsheet, for either parser
jobs/                          Dockerfiles, requirements and build step of the two parse jobs
scripts/                       command-line tools: connect, chat, seed_factsheets
.github/workflows/             app auto-deploy, parse-job images
```
Run everything from the repo root: the app as `python -m app.server`, a tool as `python -m scripts.<name>`.
`uploads/` and `archive/` (local runs) are created at the repo root and are not in git.

## Setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # fill COMPOSIO_API_KEY + Azure OpenAI vars
```

## 1. Connect a user's Google account (once per user)
```bash
python -m scripts.connect techops gmail googledrive
```
Open each printed link, sign in, and the script waits until the connection is ACTIVE.
`techops` is *your* id for the user; Composio stores their tokens under it.

## 2. Chat
```bash
python -m scripts.chat techops
```
Example: `find the latest email from HDFC AMC and save its PDF attachment to Drive`
Reads run directly; the upload pauses and asks `Approve? [y/N]`.

## 3. Web UI: upload files → agent reads them → saves to Drive
```bash
python -m app.server       # uses AGENT_USER_ID from .env (default: techops)
```
Open http://127.0.0.1:8000, drop in your files (PDF, Excel, Word, CSV, text; max 100 MB each, set MAX_UPLOAD_MB in .env), then ask e.g.
`Read both files and upload them to my 'Reports' folder in Drive`. Each Drive upload shows an **Approve / Decline** card.

Why this needed new code: Composio tools run on Composio's servers, so they cannot see files on this machine.
`app/local_files.py` gives the agent three local tools:

| Tool | Approval |
|---|---|
| `list_uploaded_files` | no |
| `read_uploaded_file(file_id)`: text from txt/csv/md/json, PDF, .xlsx, .docx | no |
| `upload_file_to_drive(file_id, folder_id?)`: stages the file to Composio storage, then runs `GOOGLEDRIVE_UPLOAD_FILE` | **yes** |

Staging uses a separate Composio client with `dangerously_allow_auto_upload_download_files=True` and
`file_upload_dirs=[uploads/]`, so it can only read files the user uploaded. If the model tries `GOOGLEDRIVE_UPLOAD_FILE`
directly with a local filename, `app/agent.py` sends it back to use `upload_file_to_drive` instead.

Uploads are kept in `uploads/<chat id>/` (set `UPLOAD_DIR` to move them). Chat memory is in-process, so restarting the server starts new chats.

### Drive destination (Type / AMC / Year / Month)
The left panel picks where files go. `pipeline/drive_targets.py` finds the folder in code (the agent doesn't search Drive):

| Type | Destination | Name in Drive |
|---|---|---|
| Monthly Portfolio Disclosure | `Historical MF Monthly Portfolio/<YYYY>/<MON>/<AMC folder>/`, each file to its own AMC | as uploaded |
| Factsheet | `Historical MF Factsheets/<YYYY>/<MON>/`, one file per AMC | `<AMC>.pdf` |
| SIF Monthly Portfolio Disclosure | `Historical SIF Monthly Portfolio/<YYYY>/<MON>/<SIF folder>/`, each file to its own SIF | as uploaded |
| SIF Factsheet | `Historical SIF Factsheet/<YYYY>/<MON>/`, one file per SIF | `<SIF> Factsheet.pdf` (e.g. `Altiva Factsheet.pdf`) |

- The page picks **Fund** (MF / SIF) and **Type** (Monthly Portfolio / Factsheet). MF types use the 21 AMCs (`AMCS`), SIF types the 15 SIFs (`SIFS`); Altiva matches "Edelweiss …" file names only under SIF types.
- SIF coverage columns are strategies (Equity Long-Short, Ex-Top 100, Hybrid, Active Asset Allocator, …); files whose name doesn't say the strategy (e.g. `SIF Platinum Monthly Portfolio.xlsx`) count as "Other". The `2025-old` / `2026-old` folders are ignored.

- Each file goes to ONE selected AMC, matched by file name or contents; the agent asks when unsure. If a file name names a different AMC than the one chosen, the upload is sent back before approval.
- AMC folders are matched by keyword (`AMCS` in `pipeline/drive_targets.py`), so `HDFC Mutual fund ` and `Canara Robeco` still match.
- Folders are never created. If a Year/Month/AMC folder is missing, the upload stops and names the missing folder.
- The approval card shows the exact Drive path, and warns if a file with that name or AMC is already there.
- Edit `AMCS` or `DOC_TYPES` in `pipeline/drive_targets.py` to add AMCs or types.

### Coverage tab (what's uploaded / missing)
Read-only view of Drive for any Year/Month:
- **Monthly Portfolio Disclosure:** AMC × fund-category grid (Large, Mid, Small, Flexi, Focused, Value/Contra,
  Balanced Advantage, Multi Asset, Business Cycle, Conservative Hybrid). A category counts as *expected* if the AMC
  had it in any of the previous 3 months (`LOOKBACK_MONTHS`); otherwise it shows as not offered.
- **Factsheet:** one tile per AMC, uploaded or missing.
- Categories come from file names (`CATEGORIES` in `pipeline/drive_targets.py`, including common typos). Files that match no
  category are listed under the grid.
- First load of a month takes about 20 s; results are cached for 5 minutes (**Refresh** re-reads Drive).

### Parsed tab (Azure storage + parsed JSON)
Every file uploaded to Drive is also copied to Azure Blob Storage, with the same layout:
`files/<Drive root>/<YYYY>/<MON>/<AMC>/<file>`. A failed copy never fails the Drive upload; the agent's tool
result says `archive_error` instead.

The MF parsers from Historical-Scheme-Portfolio are copied unchanged into `parsers/historical/`, and the SIF parsers from
sif-parser into `parsers/sif/` (see each folder's `SOURCE.md`):

| Type | Parser | Result in `parsed/<same path>.json` | Time |
|---|---|---|---|
| Monthly Portfolio Disclosure | `historical/.../mf_portfolio_parser.py` | holdings (`holdings.json` schema), scheme snapshot, 100%-of-NAV and unclassified-row checks | ~20 s per file |
| Factsheet (MF) | `historical/.../factsheet_parser.py`: docling plus the Azure OpenAI agent | fund managers, AUM (month end and average), data date, printed manager exits | a few minutes per PDF |
| SIF Monthly Portfolio Disclosure | `sif/sif_parser.py` (Excel only) | holdings in the same schema, keyed by SIF code (`SIF-62`); the check is market value reconciled with the printed total | a few seconds per file |
| SIF Factsheet | `sif/factsheets/factsheet_parser.py`: docling plus the same agent | fund managers, AUM, data date, printed manager exits, per strategy | a few minutes per PDF |

A factsheet is parsed **only for the funds you uploaded portfolios for**. An AMC has one fund per SEBI category, so the
app passes `--category` for each category with a parsed Monthly Portfolio Disclosure of that AMC. It looks in the
factsheet's month first, then the month before. The parser then skips every other fund in the index. If a
fund's portfolio arrives later, that AMC's factsheet is parsed again by itself; docling's pages are cached. With no
portfolio uploaded yet, the factsheet waits as "failed: no fund to look for". **Parse all funds** parses every fund in the index.

Funds left without a manager or AUM by the first pass get a **second pass**: the LLM is asked again
(`--force-llm`, not its cached answer) with more reasoning effort, `FACTSHEET_RETRY_REASONING_EFFORT` (default
`high`; the first pass uses `FACTSHEET_LLM_REASONING_EFFORT`, default `low`). A fund's rows are replaced only if
the second pass found more. **Parse again → Factsheet** always asks the LLM again. Funds outside the parser's 12
categories (for example Aggressive Hybrid, which the portfolio parser files as "Others") get no managers or AUM.

**SIF.** SIF files use the same queues and jobs as MF: the file's path picks the parser. They differ from MF in four ways:
- A SIF factsheet is always parsed for **every** strategy of that SIF in `sif_scheme_master.xlsx`. It has 1 to 4, and some SIFs
  publish no portfolio file. So it never waits for a portfolio, and a new portfolio never triggers a re-parse.
- A SIF portfolio's factsheet is `<SIF short> Factsheet.pdf` in the same month folder or the next one (`Dyna Factsheet.pdf` for DynaSIF).
- SIF portfolio PDFs (for example `Magnum SIF Oct.pdf`) are stored but not parsed: the SIF parser reads Excel only.
- SIF factsheet rows are kept in `state/sif_factsheets.json`, apart from MF's, and the SIF merge (`parsers/sif/merge_portfolio_factsheet.py`)
  joins them. The month merge is `/api/merged?fund=SIF&year=&month=`, cached in `merged/SIF/<YEAR>/<MON>.json`.

To bring in SIF factsheets parsed before the app existed: `python -m scripts.seed_factsheets --sif "<sif-parser>/factsheets/output"`.

**Merged.** `merge_portfolio_factsheet.py` joins a month's holdings with fund-manager tenures and AUM, giving the
`merged_*.json` schema. Tenures come from every factsheet so far, so each parsed factsheet's rows are also kept in
`state/factsheets.json`. The app builds the merged JSON when you open it and caches it in `merged/<YEAR>/<MON>.json`
until a portfolio file of that month or any factsheet is parsed again. AUM for September comes from the factsheet
whose data is as on 30 September, which is usually in the OCT folder.

**Parsed tab.** It lists a month's stored files with their status, and shows holdings per fund or managers and AUM per
fund. **Merged** shows the month's merged data. Every fund folds up, and the tab has **Collapse all**, **Close**,
**Raw JSON**, **Download JSON** and **Parse again**.

### Where the parsers run (`PARSE_RUNNER`)
- `inline` (default, local): a background thread in the app, one file at a time. Factsheets need docling, which the
  app doesn't install. Set `FACTSHEET_PYTHON` to a Python that has it, for example the Historical-Scheme-Portfolio
  venv.
- `queue` (Azure): the app puts `{"source": "files/..."}` on an Azure Storage Queue. Two **Container Apps Jobs** take
  it from there (`pipeline/worker.py`) and write the result back to storage. The app itself stays small.

| Job | Image | Queue | Size |
|---|---|---|---|
| `parse-portfolio` (MF and SIF) | `jobs/portfolio.Dockerfile` | `parse-portfolio` | 1 vCPU / 2 GiB |
| `parse-factsheet` (MF and SIF) | `jobs/factsheet.Dockerfile` (docling, CPU torch, models baked in; about 3 GB) | `parse-factsheet` | 4 vCPU / 8 GiB |

`.github/workflows/parse-jobs.yml` builds both images in ACR and updates the jobs on every push that changes the
parsers or job code. The jobs themselves are created once, as shown below.

#### One-time Azure setup (portal)
1. **Container App Jobs → Create**, once for each job:
   - Name `parse-portfolio` (then `parse-factsheet`), resource group `MF-SIF-DATA-EXTRACTION`, and the same Container Apps environment as the app.
   - Trigger type **Event-driven**. Replica timeout `1800` (portfolio) or `3600` (factsheet). Retry limit `0`. Polling interval `30`. Max executions `1`.
   - Container: choose "Use quickstart image" for now; the workflow replaces it. CPU and memory as in the table above.
2. Open each job:
   - **Secrets**: add `storage-conn` (the full storage connection string). For `parse-factsheet`, also add `openai-key` (the Azure OpenAI key).
   - **Containers → Edit**: set the registry to `mfsifagentdashboard.azurecr.io` (admin credentials or the identity).
     Add these environment variables:
     `AZURE_STORAGE_CONNECTION_STRING` → secret `storage-conn` and `AZURE_STORAGE_CONTAINER=mf-documents`.
     For factsheets, also add `AZURE_OPENAI_API_KEY` → secret `openai-key`, `AZURE_OPENAI_ENDPOINT`, and `FACTSHEET_DEPLOYMENT`
     (the deployment the factsheet agent uses; defaults to `AZURE_OPENAI_DEPLOYMENT`).
   - **Event-driven scaling → Add rule**: type `azure-queue`. Metadata `queueName=parse-portfolio` (or `parse-factsheet`),
     `queueLength=1`, `accountName=mfsifdocuments`. Authentication: secret `storage-conn` → trigger parameter `connection`.
3. On the **app** (`mf-sif-agent-dashboard`), add the environment variable `PARSE_RUNNER=queue`.
4. Push, or run the **Build and deploy the parse jobs** workflow by hand. The first factsheet image build takes about 15 minutes.

The queues are created on first use. A message that crashes a job 3 times is dropped and the file is marked failed.

Settings:
- `AZURE_STORAGE_CONNECTION_STRING`: the storage account's connection string.
- `AZURE_STORAGE_CONTAINER`: the container name, default `mf-documents`. It is created if missing.
- `PARSE_RUNNER` (`inline`/`queue`), `PORTFOLIO_QUEUE`, `FACTSHEET_QUEUE`, `PARSE_TIMEOUT_S` (600), `FACTSHEET_TIMEOUT_S` (3000),
  `FACTSHEET_PYTHON`, and `FACTSHEET_DEPLOYMENT`.

Without a connection string, files go to a local `archive/` folder instead (`ARCHIVE_DIR`), which is handy for local runs.

## How it works
| File | Role |
|---|---|
| `app/composio_setup.py` | `Composio(provider=LangchainProvider())` → `create(user_id, toolkits, manage_connections=False, sandbox off)` |
| `scripts/connect.py` | `session.authorize(toolkit)` → `redirect_url` → `wait_for_connection()` |
| `app/agent.py` | `agent → review → tools` graph. `review` inspects every `COMPOSIO_MULTI_EXECUTE_TOOL` call and `interrupt()`s on non-read actions |
| `scripts/chat.py` | CLI that resumes the graph with `Command(resume={"approve": bool, "feedback": str})` |
| `app/local_files.py` | upload storage, file reading, local → Drive upload tools |
| `app/server.py` + `app/static/index.html` | FastAPI backend and the upload / chat / approve page |
| `pipeline/blob_store.py` | Azure Blob Storage (or local folder) for uploaded files and parsed JSON |
| `pipeline/parse_jobs.py` | archives each Drive upload, queues or runs its parser, month index for the Parsed tab, merged JSON |
| `pipeline/worker.py` | entry point of the parse Container Apps Jobs (reads the queue) |
| `jobs/` | Dockerfiles and requirements of the two parse jobs |

The model gets Composio's meta tools (`COMPOSIO_SEARCH_TOOLS`, `COMPOSIO_MULTI_EXECUTE_TOOL`, …), not 400 Gmail/Drive tools, so context stays small. Same pattern Nod uses.

**Read vs write rule** (`app/agent.py`): the verb after the toolkit prefix decides it.
`GMAIL_FETCH_EMAILS`, `GOOGLEDRIVE_FIND_FILE` → run. `GMAIL_SEND_EMAIL`, `GOOGLEDRIVE_UPLOAD_FILE` → ask.
Add exact slugs to `AUTO_APPROVE` to skip asking (e.g. `GMAIL_CREATE_EMAIL_DRAFT`).

## Add more apps
Add slugs to `COMPOSIO_TOOLKITS` (e.g. `gmail,googledrive,googlecalendar,googlesheets,slack,notion`), then run `python -m scripts.connect` for the new ones.

## Taking it to production
- **Web sign-in:** call `session.authorize(toolkit, callback_url="https://your.app/composio/callback")` from a FastAPI route and send the user to `redirect_url`.
- **Persistent threads:** swap `InMemorySaver` for `langgraph-checkpoint-postgres` so approvals survive restarts.
- **Your own Google consent screen:** by default users see Composio's OAuth app. To show Armstrong's name, create an auth config in Composio with your Google OAuth client and pass `auth_configs={"gmail": "<auth_config_id>"}` to `create()`. External users + Gmail read scopes then need Google verification.
- **Token security:** Google tokens live in Composio, never in your DB; you only keep `user_id`.
