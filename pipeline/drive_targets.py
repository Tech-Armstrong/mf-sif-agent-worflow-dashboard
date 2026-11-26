"""Where each document type lives in Google Drive, and how to find the exact folder.

Layouts (as they exist in Drive):
  Monthly Portfolio Disclosure:     Historical MF Monthly Portfolio  / <YYYY> / <MON> / <AMC folder> / <file as uploaded>
  Factsheet:                        Historical MF Factsheets         / <YYYY> / <MON> / <AMC name>.pdf
  SIF Monthly Portfolio Disclosure: Historical SIF Monthly Portfolio / <YYYY> / <MON> / <SIF folder> / <file as uploaded>
  SIF Factsheet:                    Historical SIF Factsheet         / <YYYY> / <MON> / <SIF short name> Factsheet.pdf

Folders are never created here: if one is missing, planning fails and the user is told.
Folder names drift between months ("HDFC Mutual fund ", "DynaSIF", ...), so AMCs and SIFs
are matched by keywords rather than exact names. MF types use AMCS, SIF types use SIFS.
Edit AMCS / SIFS / DOC_TYPES to change the setup.
"""
import re
import threading
import time
from datetime import date
from pathlib import Path

FOLDER_MIME = "application/vnd.google-apps.folder"
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
FIRST_YEAR = 2020

DOC_TYPES = {
    "Monthly Portfolio Disclosure": {
        "fund": "MF", "kind": "Portfolio", "managers": "mf",
        "root": "Historical MF Monthly Portfolio",
        "root_id": "1DYCpYHPv9bNjkhQ3Tk6KpIXsSsghH_aE",
        "layout": "amc_folder",
    },
    "Factsheet": {
        "fund": "MF", "kind": "Factsheet", "managers": "mf",
        "root": "Historical MF Factsheets",
        "root_id": "14a9PjymdNtZVr2cwJA-u1qN4t3BTCMeO",
        "layout": "amc_file", "file_name": "{name}",
    },
    "SIF Monthly Portfolio Disclosure": {
        "fund": "SIF", "kind": "Portfolio", "managers": "sif",
        "root": "Historical SIF Monthly Portfolio",
        "root_id": "1SyuVA_BlYuIHFRZvKdUMCzo1HswxGR1P",
        "layout": "amc_folder",
    },
    "SIF Factsheet": {
        "fund": "SIF", "kind": "Factsheet", "managers": "sif",
        "root": "Historical SIF Factsheet",
        "root_id": "1-ywVvwHTUoSCQl0BEEuL5q_BTDsYon86",
        "layout": "amc_file", "file_name": "{short} Factsheet",
    },
}

# Display name (also the factsheet file name) -> keywords that identify the AMC in a Drive name.
AMCS: dict[str, list[str]] = {
    "Aditya Birla Mutual Fund": ["aditya birla", "adithya birla", "absl"],
    "Axis Mutual Fund": ["axis"],
    "Baroda BNP Paribas Mutual Fund": ["baroda", "bnp"],
    "Canara Robeco Mutual Fund": ["canara"],
    "DSP Mutual Fund": ["dsp"],
    "Edelweiss Mutual Fund": ["edelweiss"],
    "HDFC Mutual Fund": ["hdfc"],
    "HSBC Mutual Fund": ["hsbc"],
    "ICICI Prudential Mutual Fund": ["icici"],
    "Invesco Mutual Fund": ["invesco"],
    "Kotak Mahindra Mutual Fund": ["kotak"],
    "Mirae Asset Mutual Fund": ["mirae"],
    "Motilal Oswal Mutual Fund": ["motilal"],
    "Nippon India Mutual Fund": ["nippon"],
    "PGIM Mutual Fund": ["pgim"],
    "Parag Parikh Mutual Fund": ["parag parikh", "ppfas"],
    "SBI Mutual Fund": ["sbi"],
    "Sundaram Mutual Fund": ["sundaram"],
    "Tata Mutual Fund": ["tata"],
    "Union Mutual Fund": ["union"],
    "WhiteOak Mutual Fund": ["whiteoak", "white oak"],
}

# SIF display name -> keywords. Altiva is Edelweiss's SIF and its files are named "Edelweiss ...".
SIFS: dict[str, list[str]] = {
    "Altiva SIF": ["altiva", "edelweiss"],
    "Apex SIF": ["apex"],
    "Arthaya SIF": ["arthaya"],
    "Arudha SIF": ["arudha"],
    "Diviniti SIF": ["diviniti"],
    "DynaSIF": ["dyna", "dynasif"],
    "Infinity SIF": ["infinity"],
    "Magnum SIF": ["magnum"],
    "Platinum SIF": ["platinum"],
    "Prism SIF": ["prism"],
    "RedHex SIF": ["redhex", "red hex"],
    "Sapphire SIF": ["sapphire"],
    "Summit SIF": ["summit"],
    "WSIF": ["wsif"],
    "iSIF": ["isif"],
}
# Short name used in SIF factsheet file names ("<short> Factsheet.pdf").
SIF_SHORT = {s: ("Dyna" if s == "DynaSIF" else s.removesuffix(" SIF")) for s in SIFS}

MANAGERS = {"mf": AMCS, "sif": SIFS}
_KEYWORDS = {**AMCS, **SIFS}

_USER_ID: str | None = None  # set by configure()
_CACHE: dict[tuple, tuple[float, list]] = {}
_CACHE_TTL = 300.0  # uploads through this app clear their folder at once; use refresh for outside changes
_lock = threading.Lock()


def configure(user_id: str) -> None:
    global _USER_ID
    _USER_ID = user_id


def options() -> dict:
    today = date.today()
    prev = date(today.year - (today.month == 1), (today.month - 2) % 12 + 1, 1)  # previous month
    return {
        "doc_types": list(DOC_TYPES),
        "types": {t: {"fund": c["fund"], "kind": c["kind"], "root": c["root"], "layout": c["layout"],
                      "managers": list(managers_for(t)), "manager_label": "SIF" if c["managers"] == "sif" else "AMC",
                      "file_name": c.get("file_name")} for t, c in DOC_TYPES.items()},
        "amcs": list(AMCS),
        "months": MONTHS,
        "years": list(range(today.year, FIRST_YEAR - 1, -1)),
        "default": {"year": prev.year, "month": MONTHS[prev.month - 1], "doc_type": "Monthly Portfolio Disclosure"},
    }


# ---------- matching ----------

def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def managers_for(doc_type: str | None) -> dict[str, list[str]]:
    """AMCs for MF types, SIFs for SIF types (both when the type is unknown)."""
    cfg = DOC_TYPES.get(doc_type or "")
    return MANAGERS[cfg["managers"]] if cfg else _KEYWORDS


def amc_matches(amc: str, drive_name: str) -> bool:
    n = f" {_norm(Path(drive_name).stem if '.' in drive_name else drive_name)} "
    return any(f" {k} " in n for k in _KEYWORDS[amc])


def amcs_in_name(filename: str, doc_type: str | None = None) -> list[str]:
    """AMCs/SIFs a file name points to (empty if the name doesn't say)."""
    return [a for a in managers_for(doc_type) if amc_matches(a, filename)]


def name_conflict(amc: str, filename: str, doc_type: str | None = None) -> str | None:
    """Error text if the file name clearly names a different AMC/SIF than `amc`."""
    chosen = canonical_amc(amc or "", doc_type)
    named = amcs_in_name(filename, doc_type)
    if chosen and named and chosen not in named:
        return f"'{filename}' looks like a {' / '.join(named)} file, not {chosen}."
    return None


def canonical_amc(value: str, doc_type: str | None = None) -> str | None:
    pool = managers_for(doc_type)
    v = _norm(value)
    for amc in pool:
        if _norm(amc) == v:
            return amc
    hits = [a for a in pool if amc_matches(a, value)]
    return hits[0] if len(hits) == 1 else None


# ---------- Drive reads (read-only, cached) ----------

def _children(parent_id: str) -> list[dict]:
    key = ("children", parent_id)
    with _lock:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < _CACHE_TTL:
            return hit[1]
    from app.composio_setup import client

    files, token = [], None
    while True:
        args = {
            "q": f"'{parent_id}' in parents and trashed=false",
            "fields": "files(id,name,mimeType),nextPageToken",
            "pageSize": 200,
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if token:
            args["pageToken"] = token
        res = client().tools.execute(
            "GOOGLEDRIVE_LIST_FILES", args, user_id=_USER_ID, dangerously_skip_version_check=True
        )
        if not res.get("successful"):
            raise RuntimeError(f"Could not list Drive folder: {res.get('error')}")
        data = res.get("data") or {}
        files += data.get("files", [])
        token = data.get("nextPageToken")
        if not token:
            break
    with _lock:
        _CACHE[key] = (time.time(), files)
    return files


def clear_cache() -> None:
    with _lock:
        _CACHE.clear()


def forget_folder(folder_id: str) -> None:
    """Drop the cached listing after an upload, so duplicate warnings stay accurate."""
    with _lock:
        _CACHE.pop(("children", folder_id), None)


def _child_folder(parent_id: str, name: str) -> dict | None:
    target = _norm(name)
    return next(
        (f for f in _children(parent_id) if f["mimeType"] == FOLDER_MIME and _norm(f["name"]) == target), None
    )


# ---------- planning ----------

def plan(doc_type: str, amc: str, year, month: str, filename: str) -> dict:
    """Resolve the Drive destination for one file. Never writes to Drive.
    Returns {ok, folder_id, path, upload_name, warning?} or {ok: False, error}."""
    if doc_type not in DOC_TYPES:
        return {"ok": False, "error": f"Unknown type '{doc_type}'. Use one of {list(DOC_TYPES)}."}
    amc_name = canonical_amc(amc or "", doc_type)
    if not amc_name:
        label = "SIF" if DOC_TYPES[doc_type]["managers"] == "sif" else "AMC"
        return {"ok": False, "error": f"Unknown {label} '{amc}' for {doc_type}. Use one of: {list(managers_for(doc_type))}."}
    mon = str(month or "").strip().upper()[:3]
    if mon not in MONTHS:
        return {"ok": False, "error": f"Unknown month '{month}'."}
    try:
        yr = str(int(year))
    except (TypeError, ValueError):
        return {"ok": False, "error": f"Unknown year '{year}'."}

    cfg = DOC_TYPES[doc_type]
    path = [cfg["root"]]
    try:
        node = cfg["root_id"]
        for part in (yr, mon):
            f = _child_folder(node, part)
            if not f:
                return {"ok": False, "error": f"Folder not found in Drive: {'/'.join(path + [part])}. "
                                              "Create it in Drive, then try again."}
            node, path = f["id"], path + [f["name"].strip()]

        if cfg["layout"] == "amc_folder":
            hits = [f for f in _children(node) if f["mimeType"] == FOLDER_MIME and amc_matches(amc_name, f["name"])]
            if not hits:
                return {"ok": False, "error": f"No folder for {amc_name} in Drive: {'/'.join(path)}. "
                                              "Create it in Drive, then try again."}
            if len(hits) > 1:
                return {"ok": False, "error": f"More than one folder matches {amc_name} in {'/'.join(path)}: "
                                              f"{[h['name'].strip() for h in hits]}. Remove the extra one."}
            node, path = hits[0]["id"], path + [hits[0]["name"].strip()]
            upload_name = filename
            same = [f["name"] for f in _children(node) if _norm(f["name"]) == _norm(filename)]
        else:  # amc_file: factsheet named after the AMC/SIF, directly in the month folder
            stem = cfg["file_name"].format(name=amc_name, short=SIF_SHORT.get(amc_name, amc_name))
            upload_name = stem + (Path(filename).suffix.lower() or ".pdf")
            same = [f["name"].strip() for f in _children(node)
                    if f["mimeType"] != FOLDER_MIME and amc_matches(amc_name, f["name"])]
    except Exception as e:
        return {"ok": False, "error": str(e)}

    out = {"ok": True, "folder_id": node, "path": "/".join(path), "upload_name": upload_name}
    if same:
        out["warning"] = f"Already in this folder: {same}. Uploading adds another copy."
    return out


# ---------- coverage (what's uploaded / missing for a month) ----------

# Fund categories tracked per fund family. Each entry: (name, file-name keywords). SIF categories are named
# exactly as in sif_scheme_master.xlsx, and a parsed SIF file's own categories come first (_parsed_categories):
# the keywords only place files not parsed yet.
# "order" tries the more specific names first ("dynamic asset allocation" before "asset allocation").
MF_CATEGORIES: list[tuple[str, list[str]]] = [
    ("Large Cap", ["large cap", "largecap", "bluechip"]),
    ("Mid Cap", ["mid cap", "midcap"]),
    ("Small Cap", ["small cap", "smallcap"]),
    ("Flexi Cap", ["flexi cap", "flexicap"]),
    ("Focused", ["focused", "focus"]),
    ("Value / Contra", ["value", "contra"]),
    ("Balanced Advantage", ["balanced advantage", "dynamic asset allocation"]),
    ("Multi Asset", ["multi asset", "muilti asset", "multi allocation", "asset allocation", "asset alocation"]),
    ("Business Cycle", ["business cycle", "business cycles"]),
    ("Conservative Hybrid", ["conservative hybrid", "debt hybrid"]),
]
# The official SIF investment strategies, in their official order (equity, debt, hybrid). Use these names exactly.
SIF_CATEGORIES: list[tuple[str, list[str]]] = [
    ("Equity Long-Short", ["equity long short", "equity longshort", "long short", "longshort"]),
    ("Equity Ex-Top 100 Long-Short", ["ex top 100", "extop 100", "ex top100", "extop100"]),
    ("Sector Rotation Long-Short", ["sector rotation"]),
    ("Debt Long-Short", ["debt long short", "debt longshort"]),
    ("Sectoral Debt Long-Short", ["sectoral debt"]),
    ("Active Asset Allocator Long-Short", ["active asset allocator", "asset allocator"]),
    ("Hybrid Long-Short", ["hybrid"]),
]
SIF_GROUPS = [("Equity", ["Equity Long-Short", "Equity Ex-Top 100 Long-Short", "Sector Rotation Long-Short"]),
              ("Debt", ["Debt Long-Short", "Sectoral Debt Long-Short"]),
              ("Hybrid", ["Active Asset Allocator Long-Short", "Hybrid Long-Short"])]
CATEGORY_SETS = {
    "mf": {"categories": MF_CATEGORIES, "fallback": None,
           "order": ["Business Cycle", "Conservative Hybrid", "Balanced Advantage", "Multi Asset",
                     "Flexi Cap", "Focused", "Value / Contra", "Large Cap", "Mid Cap", "Small Cap"]},
    "sif": {"categories": SIF_CATEGORIES, "fallback": None,
            # file-name matching tries the more specific names first ("sectoral debt" before "debt")
            "order": ["Equity Ex-Top 100 Long-Short", "Sector Rotation Long-Short", "Sectoral Debt Long-Short",
                      "Debt Long-Short", "Active Asset Allocator Long-Short", "Hybrid Long-Short", "Equity Long-Short"]},
}
CATEGORIES = MF_CATEGORIES  # kept for older imports
LOOKBACK_MONTHS = 3  # a category is "expected" if the AMC/SIF had it in any of these previous months


def _cat_set(doc_type: str) -> dict:
    return CATEGORY_SETS[DOC_TYPES[doc_type]["managers"]]


def category_of(filename: str, doc_type: str = "Monthly Portfolio Disclosure") -> str | None:
    cs = _cat_set(doc_type)
    n = f" {_norm(Path(filename).stem if Path(filename).suffix.lower() in {'.xlsx', '.xls', '.csv', '.pdf'} else filename)} "
    keys = dict(cs["categories"])
    for cat in cs["order"]:
        if any(f" {k} " in n for k in keys[cat]):
            return cat
    return cs["fallback"]


def _prev_months(year: int, mon: str, n: int) -> list[tuple[int, str]]:
    i = year * 12 + MONTHS.index(mon)
    return [((i - k) // 12, MONTHS[(i - k) % 12]) for k in range(1, n + 1)]


def _month_folder(doc_type: str, year: int, mon: str) -> dict | None:
    y = _child_folder(DOC_TYPES[doc_type]["root_id"], str(year))
    return _child_folder(y["id"], mon) if y else None


def _parsed_categories(doc_type: str, year: int, mon: str) -> dict[tuple[str, str], list[str]]:
    """{(amc, file name): categories} of the month's parsed files: the parser's exact categories (from the scheme
    master / AMFI scheme), so a file named "prism october.xlsx" still lands in Hybrid Long-Short."""
    from pipeline import blob_store

    known = {c for c, _ in _cat_set(doc_type)["categories"]}
    try:
        idx = blob_store.get_json(f"{blob_store.PARSED}/{DOC_TYPES[doc_type]['root']}/{year}/{mon}/_index.json") or {}
    except Exception:
        return {}
    out = {}
    for src, e in idx.items():
        parts = src.split("/")
        cats = [c for c in (e or {}).get("categories") or [] if c in known]  # not the parser's "Others"
        if len(parts) >= 6 and e.get("status") == "done" and cats:
            out[(parts[-2], parts[-1])] = cats
    return out


def _portfolio_month(doc_type: str, year: int, mon: str) -> dict | None:
    """{amc: {"folder": drive name, "files": {category: [file names]}, "other": [...]}} or None if no month folder."""
    from concurrent.futures import ThreadPoolExecutor

    m = _month_folder(doc_type, year, mon)
    if not m:
        return None
    parsed = _parsed_categories(doc_type, year, mon) if DOC_TYPES[doc_type]["managers"] == "sif" else {}
    pool = managers_for(doc_type)
    folders = [f for f in _children(m["id"]) if f["mimeType"] == FOLDER_MIME]
    owner = {}
    for f in folders:
        hits = [a for a in pool if amc_matches(a, f["name"])]
        if len(hits) == 1:
            owner[f["id"]] = hits[0]
    with ThreadPoolExecutor(8) as ex:
        listings = dict(zip(owner, ex.map(_children, owner)))
    out = {}
    for f in folders:
        amc = owner.get(f["id"])
        if not amc:
            continue
        files, other = {}, []
        for c in listings[f["id"]]:
            if c["mimeType"] == FOLDER_MIME:
                continue
            name = c["name"].strip()
            cats = parsed.get((amc, name)) or [cat for cat in [category_of(name, doc_type)] if cat]
            for cat in cats:
                files.setdefault(cat, []).append(name)
            if not cats:
                other.append(name)
        out[amc] = {"folder": f["name"].strip(), "files": files, "other": other}
    return out


def coverage(doc_type: str, year, month: str) -> dict:
    """Read-only summary of what is in Drive for one month."""
    mon, yr = str(month).upper()[:3], int(year)
    cfg = DOC_TYPES[doc_type]
    pool = managers_for(doc_type)
    base = {"doc_type": doc_type, "year": yr, "month": mon, "layout": cfg["layout"],
            "manager_label": "SIF" if cfg["managers"] == "sif" else "AMC",
            "path": f"{cfg['root']}/{yr}/{mon}"}
    if cfg["layout"] == "amc_file":
        m = _month_folder(doc_type, yr, mon)
        kids = _children(m["id"]) if m else []
        rows = []
        for amc in pool:
            files = [f["name"].strip() for f in kids if f["mimeType"] != FOLDER_MIME and amc_matches(amc, f["name"])]
            rows.append({"amc": amc, "status": "uploaded" if files else "missing", "files": files})
        return {**base, "month_exists": bool(m), "rows": rows}

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(LOOKBACK_MONTHS + 1) as ex:
        months = list(ex.map(lambda ym: _portfolio_month(doc_type, *ym),
                             [(yr, mon), *_prev_months(yr, mon, LOOKBACK_MONTHS)]))
    cur, history = months[0], [h for h in months[1:] if h]
    all_cats = [c for c, _ in _cat_set(doc_type)["categories"]]
    rows = []
    for amc in pool:
        now = (cur or {}).get(amc)
        expected = {c for h in history for c in (h.get(amc) or {}).get("files", {})}
        present = set((now or {}).get("files", {}))
        cells = {}
        for c in all_cats:
            if c in present:
                cells[c] = {"status": "uploaded", "files": now["files"][c]}
            elif c in expected:
                cells[c] = {"status": "missing"}
            else:
                cells[c] = {"status": "na"}
        rows.append({
            "amc": amc,
            "folder": (now or {}).get("folder"),
            "folder_missing": cur is not None and now is None,
            "cells": cells,
            "uploaded": len(present),
            "expected": len(expected | present),
            "other": (now or {}).get("other", []),
        })
    groups = None
    if cfg["managers"] == "sif":
        # every official SIF strategy, under its group, also those no SIF runs yet
        cats, groups = all_cats, [{"name": g, "categories": cs} for g, cs in SIF_GROUPS]
    else:
        # Hide columns nobody has.
        cats = [c for c in all_cats if any(r["cells"][c]["status"] != "na" for r in rows)] or all_cats
    return {**base, "month_exists": cur is not None, "categories": cats, "groups": groups, "rows": rows,
            "lookback": [f"{m} {y}" for y, m in _prev_months(yr, mon, LOOKBACK_MONTHS)]}
