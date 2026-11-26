"""Archive Drive uploads to Azure storage, parse them into JSON, and merge holdings with factsheets.

After a file is uploaded to Drive it is also copied to the archive (blob_store) under the same
type/year/month/AMC layout, and queued for its parser:

    Monthly Portfolio Disclosure      -> Historical-Scheme-Portfolio mf_portfolio_parser.py (holdings)
    Factsheet (MF)                    -> Historical-Scheme-Portfolio factsheet_parser.py (managers, AUM)
    SIF Monthly Portfolio Disclosure  -> sif-parser sif_parser.py (holdings)
    SIF Factsheet                     -> sif-parser factsheets/factsheet_parser.py (managers, AUM)

The parsers live unchanged in parsers/historical (MF) and parsers/sif (SIF) and run in their own process.
MF and SIF share the portfolio and factsheet queues and jobs; the file's path says which parser it gets.
SIF factsheet rows are kept apart from MF's (state/sif_factsheets.json), as is the SIF merge. Where they run is set by
PARSE_RUNNER:
    inline  (default, local)  a background thread in this app, one file at a time
    queue   (Azure)           a message on a Storage Queue; a Container Apps Job per parser picks it up
                              (worker.py) and writes the result back to storage

The result is stored as parsed/<same path>.json. Each month folder keeps an index
(parsed/<root>/<YEAR>/<MON>/_index.json) of every file's status, so the UI lists a month with one read.

Factsheet rows are also kept in state/factsheets.json (one entry per factsheet), because manager tenures
are worked out from every factsheet so far. merged(year, month) runs merge_portfolio_factsheet.py over a
month's parsed holdings and those rows, and caches the result in merged/<YEAR>/<MON>.json until either
input changes.
"""
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from pipeline import blob_store, drive_targets

HERE = Path(__file__).resolve().parents[1]  # repo root
HIST = HERE / "parsers" / "historical"
PORTFOLIO_SCRIPT = HIST / "excel" / "standardized_format" / "mf_portfolio_parser.py"
FACTSHEET_DIR = HIST / "excel" / "factsheets"
MERGE_SCRIPT = HIST / "excel" / "merge_portfolio_factsheet.py"
EXITS_SCRIPT = HERE / "parsers" / "factsheet_exits.py"
SIF_DIR = HERE / "parsers" / "sif"
SIF_PORTFOLIO_SCRIPT = SIF_DIR / "sif_parser.py"
SIF_MERGE_SCRIPT = SIF_DIR / "merge_portfolio_factsheet.py"

PORTFOLIO, FACTSHEET = "portfolio", "factsheet"
KIND = {"Monthly Portfolio Disclosure": PORTFOLIO, "Factsheet": FACTSHEET,  # doc types with a parser
        "SIF Monthly Portfolio Disclosure": PORTFOLIO, "SIF Factsheet": FACTSHEET}
PARSEABLE = set(KIND)
FACTSHEET_TYPE = {"Monthly Portfolio Disclosure": "Factsheet", "SIF Monthly Portfolio Disclosure": "SIF Factsheet"}
PORTFOLIO_TYPE = {"MF": "Monthly Portfolio Disclosure", "SIF": "SIF Monthly Portfolio Disclosure"}
EXCEL = (".xls", ".xlsx", ".xlsm", ".xlsb")
# The parser looks up the AMC by its folder name (AMC_STD in mf_portfolio_parser.py). Names that differ:
PARSER_AMC_FOLDER = {"Motilal Oswal Mutual Fund": "Motilal Mutual Fund"}

RUNNER = os.getenv("PARSE_RUNNER", "inline").strip().lower()
QUEUES = {PORTFOLIO: os.getenv("PORTFOLIO_QUEUE", "parse-portfolio"),
          FACTSHEET: os.getenv("FACTSHEET_QUEUE", "parse-factsheet")}
TIMEOUT_S = {PORTFOLIO: int(os.getenv("PARSE_TIMEOUT_S", "600")),
             FACTSHEET: int(os.getenv("FACTSHEET_TIMEOUT_S", "3000"))}
QUEUED_STALE_S = int(os.getenv("QUEUED_STALE_S", str(6 * 3600)))  # queue mode: a job should have started by then
# The factsheet parser needs docling (a few GB). Locally, point this at a Python that has it, e.g. the
# Historical-Scheme-Portfolio venv. In the factsheet job image it is the image's own Python.
FACTSHEET_PYTHON = os.getenv("FACTSHEET_PYTHON") or sys.executable
FACTSHEET_STATE = f"{blob_store.STATE}/factsheets.json"
SIF_FACTSHEET_STATE = f"{blob_store.STATE}/sif_factsheets.json"
MAX_LIST_ROWS = 300  # unclassified rows kept in the result


def _fund(doc_type: str) -> str:
    """"MF" or "SIF" (DOC_TYPES[...]["fund"])."""
    return drive_targets.DOC_TYPES[doc_type].get("fund", "MF")


def fs_state(fund: str) -> str:
    """Where every factsheet's rows of one fund kind are kept (manager tenures use all of them)."""
    return SIF_FACTSHEET_STATE if fund == "SIF" else FACTSHEET_STATE


def fs_edits(fund: str) -> str:
    """Hand edits of factsheet AUM / fund managers, {"<factsheet source>|<scheme code>": edit}: see edit_factsheet."""
    return f"{blob_store.STATE}/{'sif_' if fund == 'SIF' else ''}factsheet_edits.json"

# fund_details.csv columns (factsheet_parser.py), so the merge's CSV has a header even with no factsheets yet
FS_COLUMNS = ["year", "factsheet_month", "as_on_date", "as_on_source", "portfolio_month", "amc", "fund_house",
              "category", "scheme_code", "scheme_name", "factsheet_fund_name", "index_page", "pdf_pages",
              "aum_month_end_cr", "aum_monthly_avg_cr", "aum_check", "notes", "fund_manager", "managing_since",
              "managing_since_raw", "manager_role"]
EXIT_COLUMNS = ["scheme_code", "scheme_name", "fund_manager", "exit_date", "factsheet_month", "pdf_pages", "as_printed"]

_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="parse")  # inline: one at a time, parsers are heavy
_merge_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- paths ----------

def _root_to_type() -> dict[str, str]:
    return {cfg["root"]: t for t, cfg in drive_targets.DOC_TYPES.items()}


def describe(source: str) -> dict:
    """files/<root>/<YEAR>/<MON>/[<AMC>/]<file> -> {doc_type, kind, year, month, amc, name}."""
    parts = PurePosixPath(blob_store.clean_path(source)).parts
    if len(parts) < 5 or parts[0] != blob_store.FILES:
        raise ValueError(f"Not an archived file: {source}")
    doc_type = _root_to_type().get(parts[1])
    if not doc_type:
        raise ValueError(f"Unknown type folder: {parts[1]}")
    amc_folder = drive_targets.DOC_TYPES[doc_type]["layout"] == "amc_folder"
    return {"doc_type": doc_type, "kind": KIND.get(doc_type), "fund": _fund(doc_type),
            "year": int(parts[2]), "month": parts[3],
            "amc": parts[4] if amc_folder and len(parts) > 5 else None, "name": parts[-1]}


def month_prefix(doc_type: str, year, month: str) -> str:
    return f"{drive_targets.DOC_TYPES[doc_type]['root']}/{int(year)}/{month.upper()[:3]}"


def _index_path(source: str) -> str:
    parts = PurePosixPath(blob_store.clean_path(source)).parts
    return "/".join([blob_store.PARSED, *parts[1:4], "_index.json"])


def _update_index(source: str, entry: dict) -> None:
    blob_store.update_json(_index_path(source), lambda idx: {**(idx or {}), source: entry})


def _summary(result: dict) -> dict:
    keys = ("status", "kind", "started_at", "finished_at", "seconds", "error", "warning", "funds",
            "holdings_count", "pct_status", "managers", "portfolio_month", "scope", "categories",
            "missing_manager", "missing_aum", "retried")
    return {k: result[k] for k in keys if result.get(k) is not None}


# ---------- archive / queue ----------

def archive(local: Path, doc_type: str, amc: str, year, month: str, name: str) -> dict:
    """Copy one uploaded file to the archive and queue it for parsing. Never raises."""
    try:
        amc_name = drive_targets.canonical_amc(amc or "", doc_type) or amc
        layout_amc = amc_name if drive_targets.DOC_TYPES[doc_type]["layout"] == "amc_folder" else None
        source = blob_store.file_path(drive_targets.DOC_TYPES[doc_type]["root"], year, month, layout_amc, name)
        blob_store.put_file(source, local)
    except Exception as e:
        return {"archive_error": f"{type(e).__name__}: {e}"}
    out = {"archived": source}
    if doc_type in PARSEABLE:
        try:
            queue(source)
            out["parsing"] = "queued"
        except Exception as e:
            out["parse_error"] = f"{type(e).__name__}: {e}"
    return out


def queue(source: str, scope: str = "uploaded", force: bool = False, only: list[str] | None = None) -> dict:
    """Queue one archived file for its parser.

    scope (factsheets): "uploaded" parses only the funds whose Monthly Portfolio Disclosure has been parsed
    for that AMC and month (by category, see _uploaded_categories); "all" every fund the index lists.
    force (factsheets): ask the LLM again instead of reusing its cached answers (a re-parse the user asked for).
    only (factsheets): re-parse just these categories (one portfolio file's funds). The other funds' rows are kept
    from the last result, and while it runs the other portfolio files keep showing that result ("previous").
    """
    info = describe(source)
    if not info["kind"]:
        raise ValueError(f"No parser for {info['doc_type']} yet.")
    scope = "all" if scope == "all" else "uploaded"
    only = sorted(set(only)) if only and info["kind"] == FACTSHEET else None
    entry = {"status": "queued", "kind": info["kind"], "started_at": _now(), "scope": scope}
    if only:
        last = (blob_store.get_json(_index_path(source)) or {}).get(source) or {}
        last = last.get("previous") if last.get("status") in ("queued", "running") else last
        entry["only"] = only
        if last and last.get("status") == "done":
            entry["previous"] = {k: v for k, v in last.items() if k != "previous"}
    _update_index(source, entry)
    if RUNNER == "queue":
        blob_store.queue_client(QUEUES[info["kind"]]).send_message(
            json.dumps({"source": source, "scope": scope, "force": bool(force), "only": only}))
    else:
        _pool.submit(run, source, scope, force, only)
    return entry


# ---------- parse (inline thread, or worker.py in a Container Apps Job) ----------

def run(source: str, scope: str = "uploaded", force: bool = False, only: list[str] | None = None) -> dict:
    """Parse one archived file and store the result. Never raises: a failure is stored as the result."""
    try:
        return _run(source, scope, force, only)
    except Exception as e:
        result = {"source": source, "status": "failed", "finished_at": _now(), "error": f"{type(e).__name__}: {e}"}
        try:
            result["kind"] = describe(source)["kind"]
            if only and _keep_previous(source, only, result):
                return result
            blob_store.put_json(blob_store.parsed_path(source), result)
            _update_index(source, _summary(result))
        except Exception:
            pass
        return result


def _keep_previous(source: str, only: list[str], failed: dict) -> bool:
    """A re-parse of some funds (only) failed: keep the last good result for every fund, with a warning.
    False when there is no good result to keep."""
    prev = blob_store.get_json(blob_store.parsed_path(source)) or {}
    if prev.get("status") != "done":
        return False
    prev["warning"] = f"Re-parse of {', '.join(only)} failed ({failed.get('error')}); the last result is kept."
    blob_store.put_json(blob_store.parsed_path(source), prev)
    _update_index(source, _summary(prev))
    return True


def _read_csv(path: Path, limit: int | None = None) -> list[dict]:
    if not path.is_file():
        return []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return rows[:limit] if limit else rows


def _exec(cmd: list, cwd: Path, timeout: int, env: dict, hide: list[Path]) -> tuple[str, int]:
    """Run a parser; its log with the temp folder removed from paths, and the return code."""
    env = {**os.environ, "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false",
           "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1", **env}
    try:
        proc = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=env, cwd=cwd)
        log, rc = (proc.stdout or "") + (proc.stderr or ""), proc.returncode
    except subprocess.TimeoutExpired:
        log, rc = f"Parser took longer than {timeout} s and was stopped.", -1
    for h in hide:
        for p in {str(h) + os.sep, str(h.resolve()) + os.sep}:
            log = log.replace(p, "")
    return log, rc


def _tail(log: str, n: int = 40) -> str:
    return "\n".join(log.strip().splitlines()[-n:])


def _run(source: str, scope: str = "uploaded", force: bool = False, only: list[str] | None = None) -> dict:
    info = describe(source)
    if not info["kind"]:
        raise ValueError(f"No parser for {info['doc_type']} yet.")
    only = only if info["kind"] == FACTSHEET and only else None
    started, t0 = _now(), time.monotonic()
    blob_store.update_json(_index_path(source), lambda idx: {**(idx or {}), source: {
        **{k: v for k, v in ((idx or {}).get(source) or {}).items() if only and k in ("only", "previous")},
        "status": "running", "kind": info["kind"], "started_at": started}})
    result = {"source": source, **info, "started_at": started}
    with tempfile.TemporaryDirectory(prefix="parse-") as tmp:
        if info["kind"] == PORTFOLIO:
            _parse_portfolio(source, info, result, Path(tmp))
        else:
            _parse_factsheet(source, info, result, Path(tmp), scope, force, only)
    result.update(finished_at=_now(), seconds=round(time.monotonic() - t0, 1))
    if only and result.get("status") != "done" and _keep_previous(source, only, result):
        return result
    blob_store.put_json(blob_store.parsed_path(source), result)
    _update_index(source, _summary(result))
    if info["kind"] == PORTFOLIO and result.get("status") == "done":
        _refresh_factsheets(info, result)
    return result


# ---------- which factsheet funds to parse ----------

FACTSHEET_CATEGORIES = {"Large Cap", "Mid Cap", "Small Cap", "Flexi Cap", "Focused", "Value", "Contra",
                        "Business Cycle", "Balanced Advantage", "Multi Asset", "Conservative Hybrid",
                        "Balanced Hybrid"}  # factsheet_parser.CATEGORIES values
SIF_FACTSHEET_CATEGORIES = {"Equity Long-Short", "Hybrid Long-Short", "Equity Ex-Top 100 Long-Short",
                            "Active Asset Allocator Long-Short", "Sector Rotation Long-Short"}  # parsers/sif's CATEGORIES


def factsheet_categories(fund: str) -> set[str]:
    """Categories that fund kind's factsheet parser reads managers and AUM for."""
    return SIF_FACTSHEET_CATEGORIES if fund == "SIF" else FACTSHEET_CATEGORIES


_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def _shift(year: int, mon: str, by: int) -> tuple[int, str]:
    i = _MONTHS.index(mon) + by
    return year + i // 12, _MONTHS[i % 12]


def _categories_in(year: int, mon: str, amc: str) -> list[str]:
    """Categories of the parsed Monthly Portfolio Disclosures of one AMC in one month folder."""
    prefix = month_prefix("Monthly Portfolio Disclosure", year, mon)
    idx = blob_store.get_json(f"{blob_store.PARSED}/{prefix}/_index.json") or {}
    cats = set()
    for src in idx:
        parts = PurePosixPath(src).parts
        if len(parts) > 5 and parts[4] == amc:
            # the stored result, not the index: a file being parsed again still has its last result
            r = blob_store.get_json(blob_store.parsed_path(src)) or {}
            if r.get("status") == "done":
                for by_cat in (r.get("holdings") or {}).values():
                    cats |= set(by_cat) & FACTSHEET_CATEGORIES
    return sorted(cats)


def _uploaded_categories(amc: str, year: int, mon: str) -> tuple[list[str], str | None]:
    """The funds (by category: an AMC has one fund per SEBI category) whose portfolio was uploaded for the
    factsheet's month, or for the month before when that has none (an ABSL JUN factsheet has May data)."""
    for y, m in ((year, mon), _shift(year, mon, -1)):
        cats = _categories_in(y, m, amc)
        if cats:
            return cats, f"{m} {y}"
    return [], None


def _refresh_factsheets(info: dict, result: dict) -> None:
    """A new fund's portfolio arrived after its AMC's factsheet was parsed: parse the factsheet again
    (docling's pages are cached, so only the new fund's pages cost anything).
    MF only: a SIF factsheet is always parsed for every strategy, so a new portfolio needs no re-parse."""
    if info.get("fund") != "MF":
        return
    amc = info["amc"]
    new = {c for by_cat in (result.get("holdings") or {}).values() for c in by_cat} & FACTSHEET_CATEGORIES
    if not amc or not new:
        return
    for y, m in ((info["year"], info["month"]), _shift(info["year"], info["month"], 1)):
        fs = blob_store.file_path(drive_targets.DOC_TYPES["Factsheet"]["root"], y, m, None, f"{amc}.pdf")
        entry = (blob_store.get_json(_index_path(fs)) or {}).get(fs)
        # queued: it reads the fund list when it starts, so it will include this one. running: it started
        # with the old list, so it has to run again.
        if not entry or entry.get("status") == "queued" or entry.get("scope") == "all":
            continue
        if entry.get("status") == "running" or not new <= set(entry.get("categories") or []):
            try:
                queue(fs)
            except Exception:
                pass


def _parse_portfolio(source: str, info: dict, result: dict, tmp: Path) -> None:
    sif = info["fund"] == "SIF"
    if sif:
        # sif_parser.py reads <root>/<YEAR>/<MON>/<SIF>/*.xls*, the SIF folder named as its fund_house in
        # sif_scheme_master.xlsx, which is the app's SIF name (drive_targets.SIFS)
        result["parser"] = "sif-parser sif_parser"
        if not info["name"].lower().endswith(EXCEL):
            result.update(status="failed", error="The SIF parser reads Excel disclosures only, so this file is stored "
                                                 "but not parsed. Upload the SIF's Excel portfolio for its holdings.")
            return
        script, folder = SIF_PORTFOLIO_SCRIPT, info["amc"]
    else:
        result["parser"] = "Historical-Scheme-Portfolio mf_portfolio_parser"
        script, folder = PORTFOLIO_SCRIPT, PARSER_AMC_FOLDER.get(info["amc"], info["amc"])
    inp = tmp / "in"
    out = tmp / "out"
    out.mkdir()
    if not blob_store.download(source, inp / str(info["year"]) / info["month"] / folder / info["name"]):
        raise FileNotFoundError(f"{source} is no longer in storage")
    log, rc = _exec([sys.executable, script, inp, out], tmp, TIMEOUT_S[PORTFOLIO], {}, [inp])

    holdings_file = out / "holdings.json"
    skipped = [ln for ln in log.splitlines() if ln.startswith(("SKIPPED FILE", "ERROR:"))]
    if rc != 0 or skipped or not holdings_file.is_file():
        error = " ".join(skipped[-1:]) or ("No portfolio sheet in this workbook could be parsed."
                                           if "No files were parsed successfully" in log else f"Parser exited with code {rc}.")
        result.update(status="failed", error=error, log_tail=_tail(log))
        return
    holdings = json.loads(holdings_file.read_text(encoding="utf-8"))
    snapshot = _read_csv(out / "scheme_snapshot.csv")
    funds = [f for cats in holdings.values() for fl in cats.values() for f in fl]
    n_rows = sum(len(v) for f in funds for v in f.values() if isinstance(v, list))
    # MF: sum of % to NAV; SIF: market value reconciled with the printed total (sif_parser's recon_status)
    status_col = "recon_status" if sif else "pct_total_status"
    statuses = {s.get(status_col) for s in snapshot if s.get(status_col)}
    result.update(
        status="done", funds=len(funds), holdings_count=n_rows,
        categories=sorted({c for by_cat in holdings.values() for c in by_cat}),
        pct_status="OK" if statuses == {"OK"} else ", ".join(sorted(statuses)) or None,
        snapshot=snapshot,
        checks={"pct_total": _read_csv(out / "pct_total_check.csv"),
                "unclassified_rows": _read_csv(out / "unclassified_rows.csv", MAX_LIST_ROWS),
                "amc_reported_totals": _read_csv(out / "amc_reported_totals.csv", MAX_LIST_ROWS)},
        holdings=holdings,
    )


FIRST_EFFORT = os.getenv("FACTSHEET_LLM_REASONING_EFFORT", "low")    # GPT-5 reasoning effort, first pass
RETRY_EFFORT = os.getenv("FACTSHEET_RETRY_REASONING_EFFORT", "high")  # second pass, funds still missing data


def _missing(rows: list[dict]) -> set[str]:
    """Funds with no manager or no month-end AUM."""
    codes = dict.fromkeys(r["scheme_code"] for r in rows)
    return {c for c in codes
            if not any(r.get("fund_manager") for r in rows if r["scheme_code"] == c)
            or not next(r for r in rows if r["scheme_code"] == c).get("aum_month_end_cr")}


def _retry_missing(rows, parser, details, tmp, hide, time_left):
    """Second pass for funds the first pass left without a manager or AUM: the LLM is asked again
    (--force-llm, not its cached answer) with more reasoning effort, for those funds' categories only.
    A fund's rows are replaced only when the second pass found more."""
    missing = _missing(rows)
    if not missing or time_left < 120:
        return rows, None
    cats = sorted({r["category"] for r in rows if r["scheme_code"] in missing})
    log, rc = _exec(parser + [a for c in cats for a in ("--category", c)] + ["--force-llm"], tmp, int(time_left),
                    {**_factsheet_env(), "FACTSHEET_LLM_REASONING_EFFORT": RETRY_EFFORT}, hide)
    again = _read_csv(details) if rc == 0 else []
    score = lambda rs: (any(r.get("fund_manager") for r in rs), bool(rs and rs[0].get("aum_month_end_cr")))
    fixed = []
    for code in missing:
        old = [r for r in rows if r["scheme_code"] == code]
        new = [r for r in again if r["scheme_code"] == code]
        if new and sum(score(new)) > sum(score(old)):
            rows = [r for r in rows if r["scheme_code"] != code] + new
            fixed.append(new[0]["scheme_name"])
    names = sorted({r["scheme_name"] for r in rows if r["scheme_code"] in missing})
    return rows, {"funds": names, "fixed": sorted(fixed), "effort": RETRY_EFFORT, "ok": rc == 0}


def _factsheet_env() -> dict:
    """factsheet_parser.py reads API_KEY / endpoint / deployment_name; map the app's Azure OpenAI settings."""
    env = {}
    for theirs, ours in (("API_KEY", "AZURE_OPENAI_API_KEY"), ("endpoint", "AZURE_OPENAI_ENDPOINT"),
                         ("deployment_name", "FACTSHEET_DEPLOYMENT")):
        val = os.getenv(theirs) or os.getenv(ours) or (os.getenv("AZURE_OPENAI_DEPLOYMENT") if theirs == "deployment_name" else None)
        if val:
            env[theirs] = val
    return env


def sif_house(stem: str) -> str | None:
    """'Magnum Factsheet' / 'Dyna Factsheet' -> the SIF ('Magnum SIF' / 'DynaSIF'), as the SIF factsheet parser's
    factsheet_house() reads the file name. The app's SIF names are the scheme master's fund_house names."""
    short = " ".join(stem.split())
    short = short[:-len(" Factsheet")] if short.lower().endswith(" factsheet") else short
    for s in drive_targets.SIFS:
        if short.lower() in (s.lower(), drive_targets.SIF_SHORT[s].lower()):
            return s
    return drive_targets.canonical_amc(short, "SIF Factsheet")


def _parse_factsheet(source: str, info: dict, result: dict, tmp: Path, scope: str = "uploaded",
                     force: bool = False, only: list[str] | None = None) -> None:
    sif = info["fund"] == "SIF"
    result["parser"] = "sif-parser factsheet_parser" if sif else "Historical-Scheme-Portfolio factsheet_parser"
    name = info["name"]
    if not name.lower().endswith(".pdf"):
        result.update(status="failed", error="Factsheets must be PDF files.")
        return
    stem, year, mon = Path(name).stem, str(info["year"]), info["month"]
    house = sif_house(stem) if sif else stem
    if not house:
        result.update(status="failed", error=f"No SIF matches the file name {name}: SIF factsheets are named "
                                             "'<SIF> Factsheet.pdf', e.g. 'Magnum Factsheet.pdf'.")
        return
    key = f"{year}/{mon}/{house}"  # as the parser's output folder, so seeded entries (seed_factsheets.py) are replaced
    # A SIF factsheet has 1-4 strategies and some SIFs publish no portfolio file, so it is read in full.
    scope = "all" if sif else scope
    result["scope"] = scope
    cat_args = []
    prev = (blob_store.get_json(blob_store.parsed_path(source)) or {}) if only else {}
    prev = prev if prev.get("status") == "done" else {}
    if only:
        # one portfolio file's funds; the other funds' rows come from the last result (below)
        if prev.get("categories"):
            result.update(categories=sorted(set(prev["categories"]) | set(only)), categories_from=prev.get("categories_from"))
        result["only"] = only
        cat_args = [a for c in only for a in ("--category", c)]
    elif scope != "all":
        cats, from_month = _uploaded_categories(stem, info["year"], mon)
        if not cats:
            result.update(status="failed", categories=[],
                          error=f"No Monthly Portfolio Disclosure of {stem} has been parsed for {mon} {year} (or the month "
                                f"before) yet, so there is no fund to look for. Upload those first (this factsheet is then "
                                f"parsed again by itself), or use Parse all funds.")
            return
        result.update(categories=cats, categories_from=from_month)
        cat_args = [a for c in cats for a in ("--category", c)]

    # The parser reads its PDFs from a folder inside its own repo and writes <its folder>/output/..., so run a copy.
    #   MF:  excel/factsheets/<YEAR>/<MON>/<AMC>.pdf          -> excel/factsheets/output/<YEAR>/<MON>/<AMC>/
    #   SIF: data/factsheets/<YEAR>/<MON>/<SIF> Factsheet.pdf -> factsheets/output/<YEAR>/<MON>/<SIF fund_house>/
    repo = tmp / "repo"
    if sif:
        shutil.copytree(SIF_DIR, repo, ignore=shutil.ignore_patterns(
            "__pycache__", "output", "data", "archive", "market_cap_mapping", "ingestion-pipeline"))
        fdir = repo / "factsheets"
        pdf = repo / "data" / "factsheets" / year / mon / name
        exits_dir = SIF_DIR / "factsheets"
    else:
        shutil.copytree(HIST, repo, ignore=shutil.ignore_patterns("__pycache__", "output"))
        fdir = repo / "excel" / "factsheets"
        pdf = fdir / year / mon / name
        exits_dir = HIST / "excel" / "factsheets"
    if not blob_store.download(source, pdf):
        raise FileNotFoundError(f"{source} is no longer in storage")
    outdir = fdir / "output" / year / mon / house
    cache = tmp / "cache.zip"
    if blob_store.download(blob_store.cache_path(source), cache):  # docling pages + LLM answers from a previous run
        with zipfile.ZipFile(cache) as z:
            z.extractall(outdir)

    parser = [FACTSHEET_PYTHON, fdir / "factsheet_parser.py", "--year", year, "--month", mon]
    hide = [repo / "excel" / "factsheets", repo]
    t_start = time.monotonic()
    log, rc = _exec(parser + cat_args + (["--force-llm"] if force else []), tmp, TIMEOUT_S[FACTSHEET],
                    {**_factsheet_env(), "FACTSHEET_LLM_REASONING_EFFORT": FIRST_EFFORT}, hide)
    details = outdir / "fund_details.csv"
    failed = [ln.strip() for ln in log.splitlines() if "! FAILED" in ln]
    if rc != 0 or failed or not details.is_file():
        if "No module named 'docling'" in log or "No module named 'pypdfium2'" in log:
            error = "This Python has no docling: set FACTSHEET_PYTHON locally, or parse factsheets in the Azure job."
        elif "no fund house of the SIF scheme master matches" in log:
            error = (f"The SIF factsheet parser matched no SIF in sif_scheme_master.xlsx to {name}. Add the SIF to the "
                     "scheme master (parsers/sif/scheme_master_mapping) or rename the file '<SIF> Factsheet.pdf'.")
        else:
            error = (failed[-1:] or [f"Parser exited with code {rc}."])[0]
        result.update(status="failed", error=error, log_tail=_tail(log))
        return
    rows = _read_csv(details)
    model_off = next((ln.strip() for ln in log.splitlines() if "model off" in ln), None)
    if rows and not model_off:
        rows, retried = _retry_missing(rows, parser, details, tmp, hide, TIMEOUT_S[FACTSHEET] - (time.monotonic() - t_start))
        if retried:
            result["retried"] = retried
    index_md = (outdir / "index.md").read_text(encoding="utf-8") if (outdir / "index.md").is_file() else ""
    if only and rows:
        # keep the other funds' rows from the last result
        new_codes = {r["scheme_code"] for r in rows}
        rows = [r for r in prev.get("rows") or [] if r.get("category") not in only and r["scheme_code"] not in new_codes] + rows
    if rows:
        rows = _apply_edits(source, info["fund"], rows)  # hand edits outlive a re-parse
    if not rows:
        if sif:
            error = (f"No strategy of {house} in the SIF scheme master was found in this factsheet (not launched yet, "
                     "or printed under another name: add it to factsheet_scheme_alias.csv). See index_md.")
        else:
            error = ("None of the funds asked for were found in this factsheet's index"
                     + (f" ({', '.join(result['categories'])})." if result.get("categories") else "."))
        result.update(status="failed", error=error, index_md=index_md[:20000], log_tail=_tail(log))
        return

    # managers each fund has listed in any factsheet: an exit note can name someone from an earlier month
    state_path = fs_state(info["fund"])
    state = blob_store.get_json(state_path) or {}
    managers: dict[str, set] = {}
    for k, v in state.items():
        if k != key:
            for r in v.get("rows", []):
                if r.get("fund_manager"):
                    managers.setdefault(r["scheme_code"], set()).add(r["fund_manager"])
    (tmp / "exits_in.json").write_text(json.dumps(
        {"year": year, "rows": rows, "managers": {c: sorted(m) for c, m in managers.items()}}), encoding="utf-8")
    elog, erc = _exec([FACTSHEET_PYTHON, EXITS_SCRIPT, pdf, tmp / "exits_in.json", tmp / "exits_out.json", exits_dir],
                      tmp, 300, {}, [repo])
    exits = json.loads((tmp / "exits_out.json").read_text(encoding="utf-8")) if erc == 0 else []

    blob_store.update_json(state_path, lambda s: {**(s or {}), key: {
        "source": source, "parsed_at": _now(), "rows": rows, "exits": exits}})

    buf = tmp / "cache_out.zip"
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for sub in ("pages", "llm"):
            for f in (outdir / sub).rglob("*") if (outdir / sub).is_dir() else []:
                if f.is_file():
                    z.write(f, f.relative_to(outdir).as_posix())
    try:
        blob_store.put_file(blob_store.cache_path(source), buf)
    except Exception:
        pass  # only a cache

    codes = list(dict.fromkeys(r["scheme_code"] for r in rows))
    first = {c: next(r for r in rows if r["scheme_code"] == c) for c in codes}
    result.update(
        status="done", funds=len(codes), managers=sum(1 for r in rows if r.get("fund_manager")),
        as_on=rows[0].get("as_on_date") or None, portfolio_month=rows[0].get("portfolio_month") or None,
        missing_manager=[first[c]["scheme_name"] for c in codes if not any(r.get("fund_manager") for r in rows if r["scheme_code"] == c)],
        missing_aum=[first[c]["scheme_name"] for c in codes if not first[c].get("aum_month_end_cr")],
        aum_review=[first[c]["scheme_name"] for c in codes if first[c].get("aum_check") not in ("ok", "missing", None, "")],
        warning=(model_off.lstrip("! ") if model_off else None) or (f"Exit dates not read: {_tail(elog, 3)}" if erc else None),
        rows=rows, exits=exits, index_md=index_md[:20000],
        edited=sorted({r["scheme_code"] for r in rows if r.get("aum_check") == "edited"}),
    )


# ---------- read ----------

def _fresh(source: str, entry: dict | None) -> dict | None:
    """Mark a queued/running entry as failed if it can no longer finish (app or job restarted)."""
    if not entry or entry.get("status") not in ("queued", "running"):
        return entry
    kind = entry.get("kind") or KIND.get(describe(source)["doc_type"]) or PORTFOLIO
    limit = TIMEOUT_S[kind] + 300
    if entry["status"] == "queued" and RUNNER == "queue":
        limit = QUEUED_STALE_S
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(entry["started_at"])).total_seconds()
    except (KeyError, ValueError):
        age = 0
    if age > limit:
        return {**entry, "status": "failed", "error": "Interrupted (the parser restarted). Parse it again."}
    return entry


def _factsheet_candidates(info: dict) -> list[str]:
    """Where a portfolio's AMC (or SIF) factsheet would be stored: the same month folder, then the next one
    (a factsheet is often filed a month after the data it reports). Named as drive_targets names uploads:
    MF '<AMC>.pdf', SIF '<SIF short> Factsheet.pdf'."""
    fs_type = FACTSHEET_TYPE.get(info["doc_type"])
    if not fs_type or not info["amc"]:
        return []
    cfg, amc = drive_targets.DOC_TYPES[fs_type], info["amc"]
    name = cfg["file_name"].format(name=amc, short=drive_targets.SIF_SHORT.get(amc, amc)) + ".pdf"
    return [blob_store.file_path(cfg["root"], y, m, None, name)
            for y, m in ((info["year"], info["month"]), _shift(info["year"], info["month"], 1))]


def factsheet_for(source: str) -> str | None:
    """The stored factsheet of this portfolio file's AMC, if there is one."""
    return next((fs for fs in _factsheet_candidates(describe(source)) if blob_store.version(fs)), None)


def reparse(source: str, what: str) -> dict:
    """Parse again what the user picked for one portfolio file: "portfolio", "factsheet" (its AMC's) or "both"."""
    if what not in ("portfolio", "factsheet", "both"):
        raise ValueError("Choose portfolio, factsheet or both.")
    fs = factsheet_for(source) if what != "portfolio" else None
    if what != "portfolio" and not fs:
        info = describe(source)
        ny, nm = _shift(info["year"], info["month"], 1)
        raise ValueError(f"No factsheet of {info['amc']} is stored for {info['month']} {info['year']} or {nm} {ny}. "
                         f"Upload it from the Upload tab ({FACTSHEET_TYPE.get(info['doc_type'], 'Factsheet')}) first.")
    out = {}
    if what != "factsheet":
        out["portfolio"] = queue(source)
    if fs:
        # only this file's funds: the factsheet's other funds (other portfolio files) are left as they are
        only = _file_categories(source) or None
        out["factsheet"] = {**queue(fs, force=True, only=only), "path": fs}  # asked for: don't reuse cached LLM answers
    return out


def _file_categories(source: str) -> list[str]:
    """Categories of one parsed portfolio file that its factsheet parser reads (empty if not parsed yet)."""
    r = blob_store.get_json(blob_store.parsed_path(source)) or {}
    cats = {c for by_cat in (r.get("holdings") or {}).values() for c in by_cat}
    return sorted(cats & factsheet_categories(describe(source)["fund"]))


def month_listing(doc_type: str, year, month: str) -> dict:
    prefix = month_prefix(doc_type, year, month)
    files = blob_store.list_files(prefix)
    idx = blob_store.get_json(f"{blob_store.PARSED}/{prefix}/_index.json") or {}
    fs_idx: dict[str, dict] = {}  # factsheet month indexes, read once per month
    rows = []
    for f in files:
        info = describe(f["path"])
        row = {**f, "amc": info["amc"], "parse": _fresh(f["path"], idx.get(f["path"]))}
        p = row["parse"]
        if p and p.get("status") == "done" and info["kind"] == PORTFOLIO and "categories" not in p:
            # parsed before categories were recorded: read them once and keep them in the index
            r = blob_store.get_json(blob_store.parsed_path(f["path"])) or {}
            p["categories"] = sorted({c for by_cat in (r.get("holdings") or {}).values() for c in by_cat})
            try:
                _update_index(f["path"], p)
            except Exception:
                pass
        if p and info["kind"] == PORTFOLIO:
            p["factsheet_categories"] = sorted(set(p.get("categories") or []) & factsheet_categories(info["fund"]))
        cands = _factsheet_candidates(info)
        if cands:
            row["factsheet"] = None
            for fs in cands:
                ip = _index_path(fs)
                if ip not in fs_idx:
                    fs_idx[ip] = blob_store.get_json(ip) or {}
                if fs in fs_idx[ip]:
                    entry = _fresh(fs, fs_idx[ip][fs]) or {}
                    # re-parsing another file's funds only: this file keeps the last result
                    if (entry.get("status") in ("queued", "running") and entry.get("only") and entry.get("previous")
                            and not (set((p or {}).get("factsheet_categories") or [])
                                     or set(_file_categories(f["path"]))) & set(entry["only"])):
                        entry = entry["previous"]
                    row["factsheet"] = {"path": fs, **entry}
                    break
            if row["factsheet"] is None:  # stored but never parsed (or not stored at all)
                fs = next((c for c in cands if blob_store.version(c)), None)
                row["factsheet"] = {"path": fs, "status": "stored"} if fs else None
        rows.append(row)
    return {"prefix": prefix, "backend": blob_store.enabled_backend(), "runner": RUNNER,
            "parseable": doc_type in PARSEABLE, "kind": KIND.get(doc_type), "fund": _fund(doc_type),
            "factsheet_reads": sorted(factsheet_categories(_fund(doc_type))), "files": rows}


def result(source: str) -> dict | None:
    r = blob_store.get_json(blob_store.parsed_path(source))
    idx = (blob_store.get_json(_index_path(source)) or {}).get(source)
    if idx and idx.get("status") in ("queued", "running"):  # parsing again: show that, not the old result
        return _fresh(source, idx)
    if r is None:
        return _fresh(source, idx)
    return r


# ---------- hand edits of factsheet AUM and fund managers ----------
#
# An edit replaces one fund's AUM and manager list in one factsheet. It is written to three places:
#   state/[sif_]factsheet_edits.json   the edit itself, with the parsed rows it replaced (for "revert")
#   parsed/<factsheet>.json            the factsheet's result, so the JSON shows the edited values
#   state/[sif_]factsheets.json        what the merge reads: its new version rebuilds the merged JSON
# A later parse of the same factsheet applies the edit again (_apply_edits), so it is not lost.

def _since(v: str) -> str:
    v = str(v or "").strip()
    try:
        datetime.strptime(v, "%Y-%m-%d" if len(v) == 10 else "%Y-%m")
    except ValueError:
        raise ValueError(f"'Managing since' must be YYYY-MM or YYYY-MM-DD, not '{v}'.")
    return v


def _amount(v) -> str:
    if v is None or str(v).strip() == "":
        return ""
    try:
        x = float(str(v).replace(",", "").strip())
    except ValueError:
        raise ValueError(f"AUM must be a number in crore, not '{v}'.")
    if x < 0:
        raise ValueError("AUM cannot be negative.")
    return f"{x:.2f}"


def _edited_rows(template: dict, edit: dict) -> list[dict]:
    """One fund's factsheet rows (one per manager) with the edit's AUM and managers."""
    info = describe(edit["source"])
    month = f"{info['year']}-{MONTH_NUM[info['month']]:02d}"  # the factsheet's month, when the parser read no date
    base = {**template, "aum_month_end_cr": edit["aum_month_end_cr"], "aum_monthly_avg_cr": edit["aum_monthly_avg_cr"],
            "aum_check": "edited", "notes": f"edited by hand {edit['edited_at'][:10]}",
            "year": template.get("year") or str(info["year"]), "factsheet_month": template.get("factsheet_month") or info["month"],
            "portfolio_month": template.get("portfolio_month") or month}
    if not edit["managers"]:
        return [{**base, "fund_manager": "", "managing_since": "", "managing_since_raw": "", "manager_role": ""}]
    return [{**base, "fund_manager": m["name"], "managing_since": m["since"], "managing_since_raw": "edited by hand",
             "manager_role": m.get("role", "")} for m in edit["managers"]]


def _apply_edits(source: str, fund: str, rows: list[dict]) -> list[dict]:
    """A freshly parsed factsheet's rows with the hand edits made to it; each edit keeps the new parse for revert."""
    edits = {k: v for k, v in (blob_store.get_json(fs_edits(fund)) or {}).items() if k.startswith(source + "|")}
    if not edits:
        return rows
    out = list(rows)
    for k, e in edits.items():
        code = e["scheme_code"]
        parsed = [r for r in out if r.get("scheme_code") == code and r.get("aum_check") != "edited"]
        e["parsed_rows"] = parsed or e.get("parsed_rows") or []
        template = (parsed or e["parsed_rows"] or [e["template"]])[0]
        out = [r for r in out if r.get("scheme_code") != code] + _edited_rows(template, e)
    blob_store.update_json(fs_edits(fund), lambda s: {**(s or {}), **edits})
    return out


def _store_factsheet_rows(source: str, r: dict, rows: list[dict]) -> dict:
    """Write a factsheet's (edited) rows to its parsed JSON, the month index and the merge's state."""
    info = describe(source)
    codes = list(dict.fromkeys(x["scheme_code"] for x in rows))
    first = {c: next(x for x in rows if x["scheme_code"] == c) for c in codes}
    r.update(rows=rows, funds=len(codes), managers=sum(1 for x in rows if x.get("fund_manager")),
             missing_manager=[first[c]["scheme_name"] for c in codes
                              if not any(x.get("fund_manager") for x in rows if x["scheme_code"] == c)],
             missing_aum=[first[c]["scheme_name"] for c in codes if not first[c].get("aum_month_end_cr")],
             edited=sorted({x["scheme_code"] for x in rows if x.get("aum_check") == "edited"}))
    blob_store.put_json(blob_store.parsed_path(source), r)
    _update_index(source, _summary(r))
    house = sif_house(Path(info["name"]).stem) if info["fund"] == "SIF" else Path(info["name"]).stem
    key = f"{info['year']}/{info['month']}/{house}"
    blob_store.update_json(fs_state(info["fund"]), lambda s: {**(s or {}), key: {
        **((s or {}).get(key) or {"source": source, "exits": r.get("exits") or []}), "rows": rows}})
    return r


def _editable(source: str) -> tuple[dict, dict]:
    info = describe(source)
    if info["kind"] != FACTSHEET:
        raise ValueError("Only factsheet data (AUM and fund managers) can be edited.")
    idx = (blob_store.get_json(_index_path(source)) or {}).get(source) or {}
    if idx.get("status") in ("queued", "running"):
        raise ValueError("This factsheet is being parsed. Edit it when the parse has finished.")
    r = blob_store.get_json(blob_store.parsed_path(source))
    if not r or r.get("status") != "done":
        raise ValueError("This factsheet has no parsed data yet. Parse it first.")
    return info, r


def edit_factsheet(source: str, scheme_code: str, aum_month_end_cr=None, aum_monthly_avg_cr=None,
                   managers: list[dict] | None = None, scheme_name: str = "", category: str = "",
                   edited_by: str = "") -> dict:
    """Set one fund's AUM and fund managers in one parsed factsheet by hand. Returns the factsheet's result.
    managers: [{"name", "since": YYYY-MM[-DD], "role"}]. A fund the parser did not read is added (scheme_name,
    category from its portfolio)."""
    info, r = _editable(source)
    code = str(scheme_code or "").strip()
    if not code:
        raise ValueError("Which fund? scheme_code is missing.")
    mgrs, seen = [], set()
    for m in managers or []:
        name = " ".join(str(m.get("name") or "").split())
        if not name:
            continue
        if name.lower() in seen:
            raise ValueError(f"{name} is listed twice.")
        seen.add(name.lower())
        mgrs.append({"name": name, "since": _since(m.get("since")), "role": str(m.get("role") or "").strip()})
    rows = r.get("rows") or []
    key = f"{source}|{code}"
    old = (blob_store.get_json(fs_edits(info["fund"])) or {}).get(key) or {}
    parsed = old.get("parsed_rows") if old else [x for x in rows if x.get("scheme_code") == code
                                                  and x.get("aum_check") != "edited"]
    template = (parsed or [x for x in rows if x.get("scheme_code") == code] or [None])[0]
    if template is None:
        if not scheme_name:
            raise ValueError(f"{code} is not in this factsheet: give the fund's name and category to add it.")
        any_row = rows[0] if rows else {}
        template = {**{c: any_row.get(c, "") for c in ("year", "factsheet_month", "as_on_date", "as_on_source",
                                                        "portfolio_month", "amc", "fund_house", "factsheet_file")},
                    "category": category, "scheme_code": code, "scheme_name": scheme_name,
                    "factsheet_fund_name": scheme_name, "pdf_pages": ""}
    edit = {"source": source, "scheme_code": code, "scheme_name": template.get("scheme_name") or scheme_name,
            "aum_month_end_cr": _amount(aum_month_end_cr), "aum_monthly_avg_cr": _amount(aum_monthly_avg_cr),
            "managers": mgrs, "edited_at": _now(), "edited_by": edited_by,
            "parsed_rows": parsed or [], "template": template}
    blob_store.update_json(fs_edits(info["fund"]), lambda s: {**(s or {}), key: edit})
    rows = [x for x in rows if x.get("scheme_code") != code] + _edited_rows(template, edit)
    return _store_factsheet_rows(source, r, rows)


def revert_factsheet_edit(source: str, scheme_code: str) -> dict:
    """Undo the hand edit of one fund: back to what the parser read."""
    info, r = _editable(source)
    key = f"{source}|{scheme_code}"
    edit = (blob_store.get_json(fs_edits(info["fund"])) or {}).get(key)
    if not edit:
        raise ValueError("This fund has no hand edit.")
    rows = [x for x in r.get("rows") or [] if x.get("scheme_code") != scheme_code] + (edit.get("parsed_rows") or [])
    blob_store.update_json(fs_edits(info["fund"]), lambda s: {k: v for k, v in (s or {}).items() if k != key})
    return _store_factsheet_rows(source, r, rows)


def factsheet_edits(source: str) -> dict:
    """{scheme_code: edit} of one factsheet (without the stored parsed rows)."""
    fund = describe(source)["fund"]
    return {v["scheme_code"]: {k: x for k, x in v.items() if k not in ("parsed_rows", "template")}
            for k, v in (blob_store.get_json(fs_edits(fund)) or {}).items() if k.startswith(source + "|")}


# ---------- merged: holdings + fund managers + AUM ----------

def _combine_holdings(parts: list[dict]) -> dict:
    """Several holdings.json ({year: {category: [fund]}}) as one; a fund in two files keeps both months."""
    out: dict = {}
    for h in parts:
        for year, cats in (h or {}).items():
            for cat, funds in cats.items():
                lst = out.setdefault(year, {}).setdefault(cat, [])
                for f in funds:
                    same = next((x for x in lst if f.get("scheme_amfi_code") and
                                 str(x.get("scheme_amfi_code")) == str(f.get("scheme_amfi_code"))), None)
                    if same is None:
                        lst.append(dict(f))
                    else:
                        for k, v in f.items():
                            if isinstance(v, list) and isinstance(same.get(k), list):
                                same[k] = same[k] + v
                            else:
                                same.setdefault(k, v)
    return out


def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    extra = [c for r in rows for c in r if c not in columns]
    cols = columns + list(dict.fromkeys(extra))
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _merge_run(holdings: dict, year: int, month: str | None, fund: str = "MF") -> dict:
    """merge_portfolio_factsheet.py (MF's, or parsers/sif's for SIF) over these holdings and every factsheet's rows
    of that fund kind. {"status": done|empty|failed, "merged", "coverage", "error", "log_tail"}"""
    state = blob_store.get_json(fs_state(fund)) or {}
    rows = [r for v in state.values() for r in v.get("rows", [])]
    exits = list({(e["scheme_code"], e["fund_manager"], e["exit_date"]): e
                  for v in state.values() for e in v.get("exits", [])}.values())
    with tempfile.TemporaryDirectory(prefix="merge-") as tmp:
        tmp = Path(tmp)
        (tmp / "holdings.json").write_text(json.dumps(holdings, ensure_ascii=False), encoding="utf-8")
        (tmp / "fs").mkdir()
        _write_csv(tmp / "fs" / "factsheet_fund_managers.csv", FS_COLUMNS, rows)
        _write_csv(tmp / "fs" / "factsheet_manager_exits.csv", EXIT_COLUMNS, exits)
        cmd = [sys.executable, SIF_MERGE_SCRIPT if fund == "SIF" else MERGE_SCRIPT, "--holdings", tmp / "holdings.json",
               "--factsheets", tmp / "fs" / "factsheet_fund_managers.csv", "--year", str(year),
               "--output", tmp / "out" / "merged.json"]
        if month:
            cmd += ["--month", month]
        log, rc = _exec(cmd, tmp, 300, {}, [tmp])
        if rc != 0:
            empty = "No holdings or factsheet data" in log
            return {"status": "empty" if empty else "failed",
                    "error": None if empty else f"Merge exited with code {rc}.", "log_tail": None if empty else _tail(log)}
        return {"status": "done", "merged": json.loads((tmp / "out" / "merged.json").read_text(encoding="utf-8")),
                "coverage": _read_csv(next((tmp / "out").glob("coverage*.csv"), tmp / "none"))}


def file_merged(source: str) -> dict | None:
    """One parsed portfolio file in the merged schema: its funds with fund managers and AUM (cached).

    Only this file's funds are kept (the merge also adds factsheet funds that have no disclosure)."""
    r = result(source)
    if not r or r.get("status") != "done" or r.get("kind", PORTFOLIO) != PORTFOLIO:
        return None
    holdings = r.get("holdings") or {}
    fund = describe(source)["fund"]
    cache_path = blob_store.parsed_path(source)[:-len(".json")] + ".merged.json"
    fingerprint = json.dumps([r.get("finished_at"), blob_store.version(fs_state(fund))])
    cached = blob_store.get_json(cache_path)
    if cached and cached.get("fingerprint") == fingerprint:
        return cached

    codes = {str(f.get("scheme_amfi_code")) for cats in holdings.values() for fs in cats.values() for f in fs}
    months = {m for cats in holdings.values() for fs in cats.values() for f in fs for m, v in f.items() if isinstance(v, list)}
    out = {"source": source, "fingerprint": fingerprint, "built_at": _now(), "status": "empty", "merged": {}}
    for year in holdings:
        res = _merge_run(holdings, int(year), next(iter(months)) if len(months) == 1 else None, fund)
        if res["status"] == "failed":
            out.update(status="failed", error=res["error"], log_tail=res["log_tail"])
            break
        for y, cats in (res.get("merged") or {}).items():
            for cat, fs in cats.items():
                keep = [f for f in fs if str(f.get("scheme_amfi_code")) in codes]
                if keep:
                    out["merged"].setdefault(y, {}).setdefault(cat, []).extend(keep)
                    out["status"] = "done"
    blob_store.put_json(cache_path, out)
    return out


def merged(year, month: str, rebuild: bool = False, fund: str = "MF") -> dict:
    """Holdings for one month joined with every factsheet's managers and that month's AUM (cached).
    fund "MF" -> merged/<YEAR>/<MON>.json, "SIF" -> merged/SIF/<YEAR>/<MON>.json."""
    fund = "SIF" if str(fund).upper() == "SIF" else "MF"
    year, mon = int(year), month.upper()[:3]
    pf_prefix = month_prefix(PORTFOLIO_TYPE[fund], year, mon)
    out_path = f"{blob_store.MERGED}/{'SIF/' if fund == 'SIF' else ''}{year}/{mon}.json"
    idx = blob_store.get_json(f"{blob_store.PARSED}/{pf_prefix}/_index.json") or {}
    done = sorted((s, e.get("finished_at")) for s, e in idx.items() if e.get("status") == "done")
    fingerprint = json.dumps({"portfolio": done, "factsheets": blob_store.version(fs_state(fund))})
    cached = blob_store.get_json(out_path)
    if cached and cached.get("fingerprint") == fingerprint and not rebuild:
        return cached
    with _merge_lock:
        holdings = []
        for s, _ in done:
            r = blob_store.get_json(blob_store.parsed_path(s))
            if r and r.get("status") == "done":
                holdings.append(r.get("holdings") or {})
        res = {"year": year, "month": mon, "fund": fund, "fingerprint": fingerprint, "built_at": _now(),
               "sources": {"portfolio_files": [s for s, _ in done]},
               **_merge_run(_combine_holdings(holdings), year, mon, fund)}
        blob_store.put_json(out_path, res)
        return res


MONTH_NUM = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                                             "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
