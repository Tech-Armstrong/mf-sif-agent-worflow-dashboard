"""Fund manager / AUM extraction from AMC monthly factsheets (PDF) with docling and a LangGraph extraction agent.

Usage:
    python excel/factsheets/factsheet_parser.py [--year 2026] [--month JUN] [--amc DSP]
                                                [--category "Balanced Advantage"] [--force | --force-llm]

API_KEY, endpoint and deployment_name in the repo-root .env point the extraction agent at a GPT-5 deployment on
Azure OpenAI; without them the run carries on with the code rules alone (see fund_extractor.py).

Factsheets are read from excel/factsheets/<YEAR>/<MON>/<AMC>.pdf. A factsheet runs to 60-350 pages and
docling takes ~5-15 s per page on CPU, so docling only ever sees the pages we need:

  Pass 1 (index)  pypdfium2 finds the index pages from the PDF text layer (milliseconds); docling converts
                  just those pages. Index entries are matched to scheme_master.xlsx (same fund house) by exact
                  name to keep only the funds in our categories, and their printed page numbers resolved to PDF
                  pages (Canara prints "26" on PDF page 40). A factsheet that names a fund differently from the
                  master ('Regular Savings Fund' for Conservative Hybrid) gets a row in factsheet_scheme_alias.csv.
                  A scheme no index entry matches goes to the page printing its master name as a title; else it
                  is listed as missing -> output/<YEAR>/<MON>/<AMC>/index.md
  Pass 2 (funds)  the first page of every fund is copied into one small PDF and docling converts it in a single
                  call. A tool-calling LangGraph agent per fund (fund_extractor.py, run in parallel) reads every
                  fund and decides itself which further pages to read or search for the managers, their dates,
                  the AUM and its date (pass2_graph); our code rules (rules/) answer only when it cannot.
                  -> output/<YEAR>/<MON>/<AMC>/funds.md, fund_details.csv and
                     output/factsheet_fund_managers.csv (every factsheet, one row per fund manager)

Docling output is cached per PDF page under output/<YEAR>/<MON>/<AMC>/pages/ and the model's answers per fund
under .../llm/ (keyed on model, prompt version and page content), so a re-run costs nothing for unchanged
pages; --force redoes both, --force-llm only the LLM step.

The factsheet month is the folder name; the data date (as_on_date, portfolio_month) is read from the pages:
ABSL's JUN factsheet reports AUM "as on May 31, 2026", so it maps to the MAY monthly disclosure.

A person printed under several spellings gets one name through factsheet_manager_alias.csv.
"""
import argparse
import json
import operator
import os
import re
import statistics
import sys
from collections import Counter
from itertools import takewhile
from pathlib import Path
from typing import Annotated, TypedDict

# docling's models are already downloaded: without this every run spends ~3-6 min checking the Hugging Face hub
# before the first page (set HF_HUB_OFFLINE=0 once to fetch models on a new machine)
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import pandas as pd
import pypdfium2 as pdfium
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mf_extraction_evaluation"))
from evaluation.factsheet_evaluator import evaluate_factsheet, evaluate_fund_answer
from evaluation.langsmith_tracing import flush_traces, post_scores, traceable  # sets the LangSmith project

import fund_extractor as fx
import rules
from fund_extractor import Pages
from rules.common import DATE, NAME, NUM, TITLE, number, parse_date

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
MASTER = ROOT.parent / "scheme_master_mapping" / "scheme_master.xlsx"
ALIAS = ROOT / "factsheet_scheme_alias.csv"
MANAGER_ALIAS = ROOT / "factsheet_manager_alias.csv"

MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

# scheme_master category -> category name used by the monthly disclosure parser
CATEGORIES = {
    "LARGE CAP": "Large Cap", "MID CAP": "Mid Cap", "SMALL CAP": "Small Cap",
    "FLEXI CAP": "Flexi Cap", "FOCUSED": "Focused", "VALUE": "Value", "CONTRA": "Contra",
    "THEMATIC BUSINESS CYCLE": "Business Cycle", "BALANCED ADVANTAGE": "Balanced Advantage",
    "MULTI ASSET ALLOCATION": "Multi Asset", "CONSERVATIVE HYBRID": "Conservative Hybrid",
    "BALANCED HYBRID": "Balanced Hybrid",
}

MAX_FUND_PAGES = 3
MAX_OFFSET = 40  # most pages a factsheet puts before its printed page 1 (Canara: 14)
LLM_WORKERS = 4  # fund agents run in parallel (the model's rate limiter still caps requests per minute)
DOCLING_THREADS = 8  # ~30% faster per page than docling's default 4, same output (16 is no faster)


# ---------------------------------------------------------------- names
SYNONYMS = [(r"\bmidcap\b", "mid cap"), (r"\bsmallcap\b", "small cap"), (r"\blargecap\b", "large cap"),
            (r"\bmulticap\b", "multi cap"), (r"\bflexicap\b", "flexi cap"), (r"\bcycles\b", "cycle")]
DROP_WORDS = {"fund", "the", "scheme", "an", "open", "ended"}

def norm(s):
    """Lower case, no parentheticals / punctuation, & -> and, common spelling variants."""
    s = re.sub(r"\([^)]*\)?", " ", str(s).lower()).replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    for rx, rep in SYNONYMS:
        s = re.sub(rx, rep, s)
    return " ".join(s.split())


def core_name(name, brand_tokens):
    """Fund name without brand prefix and filler words: 'ADITYA BIRLA SUN LIFE MIDCAP FUND' -> 'mid cap'."""
    toks = norm(name).split()
    while toks and toks[0] in brand_tokens:
        toks.pop(0)
    return " ".join(t for t in toks if t not in DROP_WORDS)


def master_base_name(scheme_name):
    """
    'ABSL Large Cap Fund-Growth' / 'Axis Flexi Cap Fund - Regular - Growth' / 'WhiteOak Capital Flexi Cap Fund
    Regular-Growth' -> name without plan/option ('ICICI Prudential Regular Savings Fund' keeps its 'Regular').
    """
    # 'Motilal Oswal Large Cap Regular Growth': plan and option with no 'Fund' or hyphen before them
    return re.split(r"\s+-\s+|-(?=\s*(?:growth|regular|direct)\b)|(?<=\bfund)\s+(?=(?:regular|direct)\b)"
                    r"|\s+(?=(?:regular|direct)\s*-?\s*growth\s*$)",
                    scheme_name, maxsplit=1, flags=re.I)[0]


# factsheet file names that share no leading word with the scheme master's fund house
HOUSE_ALIASES = {"parag parikh": "PPFAS"}


def fund_house_for(amc_folder, houses):
    """Longest common word prefix: 'Aditya Birla Mutual Fund' -> 'Aditya Birla Sun Life'."""
    for prefix, house in HOUSE_ALIASES.items():
        if norm(amc_folder).startswith(prefix) and house in set(houses):
            return house
    a = norm(amc_folder).split()
    best, best_n = None, 0
    for h in houses:
        b = norm(h).split()
        n = 0
        while n < min(len(a), len(b)) and a[n] == b[n]:
            n += 1
        if n > best_n:
            best, best_n = h, n
    return best


# ---------------------------------------------------------------- docling
_CONVERTER = None


def converter():
    global _CONVERTER
    if _CONVERTER is None:
        from docling.datamodel.accelerator_options import AcceleratorOptions
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
        from docling.document_converter import DocumentConverter, PdfFormatOption

        opts = PdfPipelineOptions()
        opts.accelerator_options = AcceleratorOptions(num_threads=DOCLING_THREADS)
        opts.do_ocr = False  # factsheets carry a text layer
        opts.do_table_structure = True  # ABSL AUM, Baroda fund-manager tables
        opts.table_structure_options.mode = TableFormerMode.FAST
        _CONVERTER = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
    return _CONVERTER


def docling_pages(pdf, page_nos, cache_dir, force=False):
    """
    Markdown for the given 1-based PDF pages, docling-converted in one call on a small PDF holding only those
    pages. traverse_pictures keeps text docling files under a picture (Axis prints FUND MANAGER / AUM in an
    illustrated sidebar). Cached as pages/page_NNN.md.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = lambda p: cache_dir / f"page_{p:03d}.md"
    todo = sorted({p for p in page_nos if force or not path(p).exists()})
    if todo:
        src = pdfium.PdfDocument(str(pdf))
        mini = pdfium.PdfDocument.new()
        mini.import_pages(src, [p - 1 for p in todo])
        tmp = cache_dir / "_pages.pdf"
        mini.save(str(tmp))
        mini.close()
        src.close()
        try:
            doc = converter().convert(str(tmp)).document
            for k, p in enumerate(todo, start=1):
                md = doc.export_to_markdown(page_no=k, traverse_pictures=True)
                md = re.sub(r"[.…·]{4,}", " … ", md)  # dot leaders
                path(p).write_text(md, encoding="utf-8")
        finally:
            tmp.unlink(missing_ok=True)
    return {p: path(p).read_text(encoding="utf-8") for p in page_nos}


# ---------------------------------------------------------------- pass 1: index
# the heading may carry its column label on the same line (PPFAS: 'Index Page No.')
INDEX_HEADING = re.compile(r"^\s*(?:i\s*n\s*d\s*e\s*x|contents|table of contents)(?:\s+page\s*(?:no\.?|number))?\s*$", re.I | re.M)
INDEX_LINE = re.compile(r"\bfund\b.{0,120}?\b\d{1,3}\s*$", re.I | re.M)
FUND_PAGE = (re.compile(r"investment\s+objective", re.I), re.compile(r"date\s+of\s+allotment", re.I))


def find_index_pages(texts):
    """First run of pages (within the first 10) with an INDEX heading or 5+ 'fund ... <page>' lines."""
    pages = []
    for i, t in enumerate(texts[:10]):
        ok = bool(INDEX_HEADING.search(t)) or len(INDEX_LINE.findall(t)) >= 5
        # a fund's own page is never the index, however many 'fund ... <number>' lines it prints (360 ONE
        # Balanced Hybrid's first page, which has no real index, passed for one and its managers went unread)
        ok = ok and not all(rx.search(t) for rx in FUND_PAGE)
        if ok:
            pages.append(i + 1)
        elif pages:
            break
    return pages


CHUNK = re.compile(r"[A-Za-z][^|…~\n\r]*?\bfund\b", re.I)
PAGE_NO = re.compile(r"(?<![\w.,/%:-])(\d{1,3})(?:\s*-\s*\d{1,3})?(?![\w.,/%:-])")  # '14', or the first page of '5-6'


def pair_names_pages(text):
    """
    Each page number goes to the oldest fund name still waiting for one, which covers 'name ..... 8', a
    number on the next line (Baroda's text layer), and docling rows that merge two entries
    ('Axis Momentum Fund … Axis Services Opportunities Fund … | 23 24').
    """
    tokens = [(m.start(), "name", m.group(0)) for m in CHUNK.finditer(text)]
    taken = [(s, s + len(n)) for s, _, n in tokens]
    tokens += [(m.start(), "page", int(m.group(1))) for m in PAGE_NO.finditer(text)
               if not any(a <= m.start() < b for a, b in taken)]
    pending, out = [], []
    for _, kind, val in sorted(tokens):
        if kind == "name":
            pending.append(" ".join(val.split()))
        elif pending:
            out.append((pending.pop(0), val))
    return out


def index_entries(text):
    """
    (name, printed page) pairs from index text. A markdown table row holding both a fund name and a page
    number is paired on its own, so a stray name never takes the next row's number (Baroda JAN: 'Aqua Fund
    of Fund | 25' yields a second chunk 'of Fund' that shifted every later entry by a row). A row with more
    numbers than names hands the extra ones to the next row when that row has a name and no number (Sundaram
    MAR: '| Balanced Advantage Fund | 31-32 33-34 |' then '| Multi Asset Allocation Fund | |'; queued in
    the stream instead, Multi Asset took the IDCW page 55). Everything else, including rows whose number
    docling put elsewhere (Canara), is paired as one stream.
    """
    text = re.sub(r"\([^)]*\)", " ", text)  # '(Erstwhile known as DSP Top 100 Equity Fund)'
    out, loose, surplus = [], [], []
    for line in text.splitlines():
        table = line.lstrip().startswith("|")
        pairs = pair_names_pages(line) if table else []
        names = list(CHUNK.finditer(line)) if table else []
        nums = [int(n.group(1)) for n in PAGE_NO.finditer(line) if not any(c.start() <= n.start() < c.end() for c in names)]
        if names and not nums and surplus:
            pairs = [(" ".join(c.group(0).split()), p) for c, p in zip(names, surplus)]
        surplus = nums[len(pairs):] if names and pairs else []
        if not pairs:
            # 'ICICI Prudential Large Cap Fund Large Cap Fund An open ended ... 14' / 'Large Cap Fund Nippon India
            # Large Cap Fund 6': fund name and category column share the line's one page number; queued one by
            # one, the extra chunks would shift every later entry
            names, nums = list(CHUNK.finditer(line)), list(PAGE_NO.finditer(line))
            nums = [n for n in nums if not any(c.start() <= n.start() < c.end() for c in names)]
            if len(names) > 1 and len(nums) == 1 and nums[0].start() > names[-1].end():
                pairs = [(" ".join(c.group(0).split()), int(nums[0].group(1))) for c in names]
        if pairs:
            out += pairs
        else:
            loose.append(line)
    return out + pair_names_pages("\n".join(loose))


def match_targets(entries, house, master, alias, brand_tokens):
    """Index entries -> target schemes of this fund house: {scheme_code: entry dict}."""
    m = master[(master.fund_house == house) & master.category.isin(CATEGORIES)]
    by_core = {core_name(master_base_name(r.scheme_name), brand_tokens): r for r in m.itertuples()}
    al = {norm(r.factsheet_name): str(r.scheme_code) for r in alias[alias.fund_house == house].itertuples()}
    found = {}
    # a name carrying the brand first: 'Nippon India Large Cap Fund' over its category column 'Large Cap Fund',
    # which is too generic to find the fund's own page with
    for name, page in sorted(entries, key=lambda e: not set(norm(e[0]).split()) & brand_tokens):
        # 'Fund Baroda BNP Paribas Large and Mid Cap Fund': start at the brand when the chunk caught a stray word
        words = name.split()
        b = next((k for k, w in enumerate(words) if norm(w) in brand_tokens), 0)
        name = " ".join(words[b:])
        code = al.get(norm(name))
        row = next((r for r in m.itertuples() if str(r.scheme_code) == code), None) if code else by_core.get(core_name(name, brand_tokens))
        if row is not None and str(row.scheme_code) not in found:
            found[str(row.scheme_code)] = dict(scheme_code=str(row.scheme_code), scheme_name=master_base_name(row.scheme_name),
                                               category=CATEGORIES[row.category], index_name=name, index_page=page)
    return found


# ---------------------------------------------------------------- page resolution
def standalone_numbers(text):
    """Lines holding only a 1-3 digit number: the printed page number (ABSL prints it mid-text: '...Centers.\\n15\\nNAV as on')."""
    return {int(l.strip()) for l in text.splitlines() if re.fullmatch(r"\s*\d{1,3}\s*", l)}


def resolve_pages(targets, entries, texts, skip_pages, amc=""):
    """
    Printed index page -> PDF page. A page qualifies when it carries the fund's own name; it scores for
    printing the index page number at its top/bottom, for fund-manager/AUM wording, for printing the name as a
    title line and for being close to the printed number (shifted by the offset seen on confirmed matches).
    """
    ntexts = [norm(t) for t in texts]
    printed = sorted({p for _, p in entries})
    # first guess at the printed -> PDF offset: pages carrying both a fund's name and its printed number
    # (the offset is the front matter before printed page 1; Nippon prints no page numbers, and a performance page
    # far back that names a fund and holds its page number as a figure gave +291)
    seed = [i + 1 - t["index_page"] for t in targets.values() for i in range(len(texts))
            if abs(i + 1 - t["index_page"]) <= MAX_OFFSET and i + 1 not in skip_pages and norm(t["index_name"]) in ntexts[i] and t["index_page"] in standalone_numbers(texts[i])]
    offsets, unplaced = [], []
    for t in sorted(targets.values(), key=lambda t: t["index_page"]):
        key = norm(t["index_name"])
        guess = t["index_page"] + (statistics.median(seed) if seed else 0)
        cands = [i for i in range(len(texts)) if i + 1 not in skip_pages and key in ntexts[i]]
        nxt = [p for p in printed if p > t["index_page"]]
        t["span"] = max(1, min(MAX_FUND_PAGES, (nxt[0] - t["index_page"]) if nxt else 1))
        if not any(abs((i + 1) - guess) <= MAX_FUND_PAGES for i in cands):
            unplaced.append(t)  # only far hits (performance / SIP tables), or the page is an image
            continue
        def score(i):
            s = 0
            if t["index_page"] in standalone_numbers(texts[i]):
                s += 10
            low = ntexts[i]
            details = "manag" in low and re.search(r"\baum\b|\baaum\b|assets under", low)
            if details:
                s += 5
            if key in ntexts[i][: max(400, len(ntexts[i]) // 3)]:
                s += 3
            # the fund's own page, not a list of schemes naming it: worth as much as the printed number, which can
            # be wrong (SBI MAR's index sends Conservative Hybrid to printed page 31, a list of schemes; its own
            # page 42 prints 34). Not a cover (Baroda APR page 1) nor a summary of many funds (Union APR page 38: 23
            # fund titles; a fund's own page prints up to 3, PPFAS and SBI naming related schemes)
            if details and rules.title_line(texts[i].replace("\r\n", "\n"), t["index_name"], amc) \
                    and fund_titles(texts[i], amc) <= 4:
                s += 10
            # snapshot pages (ABSL 'Equity Snapshot') also name every fund with its manager and AUM
            return s - abs((i + 1) - guess) * 0.5
        t["pdf_page"] = max(cands, key=score) + 1
        offsets.append(t["pdf_page"] - t["index_page"])
    # a page with no readable fund name (Baroda JAN Multi Asset is an image): printed page + the offset the
    # other funds of this factsheet showed
    for t in unplaced:
        g = t["index_page"] + int(round(statistics.median(offsets))) if offsets else None
        t["pdf_page"] = g if g and 1 <= g <= len(texts) and g not in skip_pages else None
        t["by_page_number"] = True
    return targets


# ---------------------------------------------------------------- driver
def aum_check(r):
    """Flag for review: month-end and monthly-average AUM rarely differ by more than ~30%."""
    if r is None or r.aum_month_end_cr is None:
        return "missing"
    if r.aum_monthly_average_cr:
        ratio = r.aum_month_end_cr / r.aum_monthly_average_cr
        if not 0.7 <= ratio <= 1.43:
            return f"end/avg ratio {ratio:.2f}"
    return "ok"


def fund_titles(text, amc):
    """How many different funds the page prints as a title line: 1-2 on a fund's own page, 3+ on a summary page."""
    out = set()
    for line in text.replace("\r\n", "\n").split("\n"):
        line = " ".join(re.sub(r"\(.*?\)|\(.*$", " ", line).split()).strip(" .:-")
        if line and len(line) <= 80 and re.search(r"\b(?:fund|fof)$", line, re.I):
            out.add(rules.fund_core(line, amc))
    return len(out - {""})


def relocate(d, texts, amc, skip_pages):
    """
    The fund's own page when the one resolved from the index is another fund's (Kotak JAN 'Conservative Hybrid
    Fund' -> the Multi Asset page; PGIM BAF -> the Overnight Fund page): a page with the fund's name as a title
    line (not a mention in a list of schemes) and fund-manager and AUM labels. Of several, the one printing the
    fewest fund titles (Mirae SEP Flexi Cap: its own page 34 over the 'FUND FACTS - EQUITY' page 16), then the
    nearest the page first tried. None when there is no such page. Text layer only (milliseconds).
    """
    tried = set(d.get("tried", [])) | set(skip_pages)
    near = (d.get("tried") or [d["pdf_page"]])[0]
    found = []
    for i, t in enumerate(texts):
        # the AUM may sit on the next page (360 ONE Multi Asset: title and managers on one page, AUM over the page)
        aum_text = t + "\n" + (texts[i + 1] if i + 1 < len(texts) else "")
        if i + 1 in tried or not re.search(r"fund\s*manager", t, re.I) \
                or not re.search(r"\bA?AUM\b|assets\s+under\s+management|fund\s+size", aum_text, re.I):
            continue
        t = t.replace("\r\n", "\n")
        if rules.title_line(t, d["index_name"], amc) or rules.title_line(t, d["scheme_name"], amc):
            found.append((fund_titles(t, amc), abs(i + 1 - near), i + 1))
    return min(found)[2] if found else None


def summary_page(text, fund_name, amc):
    """A page naming this fund without being its own: no title line of it, or the titles of several funds."""
    text = text.replace("\r\n", "\n")
    return not rules.title_line(text, fund_name, amc) or fund_titles(text, amc) > 1


def place_by_title(targets, texts, amc, skip_pages):
    """
    A fund resolve_pages placed by page number alone (no page near the guess prints its name) goes to the page
    relocate() finds: the guess is another fund's page whenever the index is off (SBI JAN Small Cap -> a portfolio
    page, Kotak SEP Small Cap -> the Pioneer Fund page after a broken index row). A guess with no text layer stays:
    that is the fund's page printed as a picture (Baroda JAN Multi Asset).
    """
    for t in targets.values():
        g = t.get("pdf_page")
        if t.get("by_page_number") and not (g and len(texts[g - 1].strip()) < 200):
            p = relocate(dict(t, pdf_page=g or t["index_page"] or 1), texts, amc, skip_pages)
            if p:
                t.update(pdf_page=p, matched_by="index; page by its title line")
        elif g and summary_page(texts[g - 1], t["index_name"], amc):
            # the full name is only on a snapshot, the fund's own page has no brand in its title ('LARGE CAP FUND':
            # Mirae SEP Large Cap -> 'Equity Snapshot' page 10, PGIM Large Cap -> the reckoner, which has no average AUM)
            # only to a page the index points at (Mirae's print no page number: PDF page = index page), and not from a
            # titled page it points at: a fund on its own page can look like a summary (SBI AUG prints other schemes'
            # names as lines of their own)
            at_index = lambda q: q == t["index_page"] or t["index_page"] in standalone_numbers(texts[q - 1])
            p = relocate(dict(t, pdf_page=t["index_page"] or g), texts, amc, skip_pages)
            if p and p != g and fund_titles(texts[p - 1], amc) == 1 and at_index(p) \
                    and not (at_index(g) and rules.title_line(texts[g - 1].replace("\r\n", "\n"), t["index_name"], amc)):
                t.update(pdf_page=p, matched_by="index; own page by its title line")


MANAGED_SINCE = re.compile(rf"(?P<names>(?:\s*(?:,|&|\band\b)?\s*{TITLE}{NAME})+)\s+(?:has|have)\s+"
                           rf"been\s+managing\s+the\s+(?:fund|scheme)\s+since\s+(?P<since>{DATE})")


SCHEME_INCEPTION = re.compile(rf"Scheme\s+Inception\s+date\s+is\s+({DATE})")


def managed_since(segment):
    """{manager: date as printed} from 'Mr. A has been managing the fund since <date> and Ms. B & Mr. C have been ...'."""
    dates = {}
    for m in MANAGED_SINCE.finditer(segment):
        for n in re.split(rf"\s*(?:,|&|\band\b)?\s*{TITLE}", m.group("names")):
            if n.strip(" ,&"):
                dates[" ".join(n.strip(" ,&").split())] = m.group("since")
    return dates


def manager_dates_finder(texts, amc):
    """
    lookup(fund_name, fund_text) -> {manager: date as printed} from the factsheet's fund-manager pages, where Kotak
    prints 'Scheme Inception date is 27/07/2005. Ms. Shibani Kurian has been managing the fund since 09/05/2019.' and
    its fund pages print no dates. Matched on the fund page's 'Allotment Date' (the fund names around these lines move
    between layouts: Kotak AUG Contra took Conservative Hybrid's date), else on the fund's distinguishing name in a
    '<fund> - Growth' heading. A manager printed with two dates for one inception date is left out.
    """
    table = None

    def build():
        by_inception, by_name = {}, {}
        for t in texts:
            if not re.search(r"been\s+managing\s+the\s+(?:fund|scheme)\s+since", t):
                continue
            starts = list(SCHEME_INCEPTION.finditer(t))
            for s, nxt in zip(starts, starts[1:] + [None]):
                dates = by_inception.setdefault(parse_date(s.group(1)), {})
                for name, since in managed_since(t[s.end():nxt.start() if nxt else len(t)]).items():
                    dates[name] = since if parse_date(dates.get(name, since)) == parse_date(since) else None
            heads = list(re.finditer(r"^(?P<h>[A-Z][^\n]{3,200}?)\s*-\s*Growth\b", t, re.M))
            for h, nxt in zip(heads, heads[1:] + [None]):
                dates = managed_since(t[h.end():nxt.start() if nxt else len(t)])
                core = rules.fund_core(re.sub(r"\(.*?\)", " ", h.group("h")), amc)
                if dates and core:
                    by_name.setdefault(core, dates)
        return by_inception, by_name

    def lookup(fund_name, fund_text=""):
        nonlocal table
        if table is None:
            table = build()
        by_inception, by_name = table
        allot = re.search(rf"Allotment\s+Date\s*:?\s*({DATE})", fund_text, re.I)
        dates = by_inception.get(parse_date(allot.group(1)), {}) if allot else {}
        named = next((by_name[c] for c in rules.fund_cores(fund_name, amc) if c in by_name), {})
        dates = {n: d for n, d in dates.items() if d}
        ends = {(n.split()[0], n.split()[-1]) for n in dates}
        return {**{n: d for n, d in named.items() if (n.split()[0], n.split()[-1]) not in ends}, **dates}
    return lookup


def average_aum_finder(texts, amc):
    """
    lookup(fund_name) -> monthly average AUM from Mirae's 'Key Features' pages, the only place most of its factsheets
    print it: three funds side by side, 'Key Features Mirae Asset Large Cap Fund Mirae Asset Large & Midcap Fund ...'
    then 'Monthly Average AUM (₹ Cr.) 38,011.402 42,838.608 4,429.503'. A page is read only when names and figures pair
    one to one. Other factsheets print the average on the fund's page.
    """
    table = None

    def build():
        out = {}
        for t in texts if "mirae" in amc.lower() else []:
            t = " ".join(t.split())
            head = re.search(r"Key Features (.*?) Type of Scheme", t)
            figs = re.search(rf"Monthly Average AUM \([^)]*\) (?:as on {DATE} )?((?:(?:{NUM}) ?)+)", t)  # 'as on': JAN, FEB
            if head and figs:
                names = [n for n in re.split(r"(?=Mirae Asset )", head.group(1)) if n.strip()]
                values = figs.group(1).split()
                if len(names) == len(values):
                    out.update({rules.fund_core(n, amc): number(v) for n, v in zip(names, values)})
        return out

    def lookup(fund_name):
        nonlocal table
        if table is None:
            table = build()
        return next((table[c] for c in rules.fund_cores(fund_name, amc) if c in table), None)
    return lookup


class Pass2(TypedDict, total=False):
    results: Annotated[dict, operator.or_]          # fund key -> FundDetails (or None); merged across branches
    fs: fx.FactsheetDate


@traceable(name="fund_extraction", run_type="chain",
           process_inputs=lambda i: {k: i[k] for k in ("scheme_code", "scheme_name", "category", "factsheet_month")})
def traced_extract(scheme_code, scheme_name, category, factsheet_month, cache_file, extract):
    """One fund's extraction as a LangSmith run, scored with the rubrics. Only the fund's identity goes in as inputs."""
    result = extract()
    entry = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else None
    post_scores(evaluate_fund_answer(result.model_dump(), entry, factsheet_month))
    return result


def pass2_graph(pages: Pages, details, out_dir, factsheet_month, force, force_llm):
    """
    LangGraph for pass 2 of one factsheet:

        START -> first_pages -> Send(extract_fund) x N in parallel -> END
              \\-> factsheet_date -> END   (in parallel)

    first_pages makes one docling call for every fund's first page (docling's model load is per call, so batching
    is far cheaper than per fund) and renders pages printed as pictures; both run here, not in the parallel
    branches, because pdfium is not thread-safe. A fund's first pages are its resolved page plus the following
    pages of its index span that still print its name (Canara's last fund is followed by 'Scheme performance -
    fund manager wise' pages), as text for the rules and the agent. Each extract_fund branch is fund_extractor's
    agent for one fund: it reads further pages, searches the factsheet or moves to the fund's real page itself.
    A fund answered not-found (its page turned out to be another fund's) is moved once to the page relocate()
    finds and read again, by the agent or, without the model, by the rules.
    """
    pdf, texts = pages.pdf, pages.texts
    dates_lookup = manager_dates_finder(texts, pdf.stem)
    average_lookup = average_aum_finder(texts, pdf.stem)

    def first_pages(state: Pass2):
        for d in details.values():
            p = d["pdf_page"]
            d["pages"] = [p] + list(takewhile(lambda q: q <= len(texts) and q not in pages.skip and pages.names(q, d["index_name"]),
                                              range(p + 1, p + d.get("span", 1))))
        mds = docling_pages(pdf, sorted({d["pdf_page"] for d in details.values()}), out_dir / "pages", force)
        for d in details.values():
            d["md"] = f"<!-- PDF page {d['pdf_page']} -->\n\n" + mds[d["pdf_page"]]
            d["images"] = [pages.png(q) for q in d["pages"] if pages.is_image(q)]
        return {}

    def fan_out(state: Pass2):
        return [Send("extract_fund", {"key": k}) for k in details] or END

    def run(k, d, cache_name=None):
        raw = "\n\n".join(f"--- PDF page {q} ---\n{texts[q - 1]}" for q in d["pages"])
        cache_file = out_dir / "llm" / f"{cache_name or k}.json"
        try:
            return traced_extract(
                d["scheme_code"], d["scheme_name"], d["category"], factsheet_month, cache_file,
                lambda: fx.extract(d["index_name"], d["scheme_name"], d["category"], d["md"], raw, pages, d["pages"],
                                   factsheet_month=factsheet_month, images=d.get("images"),
                                   cache_file=cache_file, force=force_llm, amc=pdf.stem,
                                   manager_dates=dates_lookup, average_aum=average_lookup),
                langsmith_extra={"metadata": {"amc": pdf.stem, "factsheet_month": factsheet_month}})
        except Exception as e:  # one bad answer should not drop the rest of the factsheet
            if fx.auth_error(e):
                raise
            print(f"  ! {d['scheme_name']}: {type(e).__name__}: {str(e)[:200]}")
            return None

    def extract_fund(arg):
        k = arg["key"]
        d = details[k]
        res = run(k, d)
        # the page resolved from the index was another fund's (Sundaram MAR Multi Asset -> the IDCW pages; the
        # agent may answer not-found without searching): relocate() scans the text layer already in memory, so
        # this costs nothing unless it finds a page, then one more agent run. The retry has its own cache file so
        # the two answers do not evict each other on a re-run.
        if res is not None and not res.fund_found:
            d["tried"] = list(d["pages"])
            p = relocate(d, texts, pdf.stem, pages.skip)
            if p:
                print(f"  {d['scheme_name']}: PDF page {d['pdf_page']} is another fund's, reading page {p}", flush=True)
                more = takewhile(lambda q: q <= len(texts) and q not in pages.skip and pages.names(q, d["index_name"]),
                                 range(p + 1, p + MAX_FUND_PAGES))
                d.update(pdf_page=p, pages=[p, *more], md="", images=[])
                res = run(k, d, cache_name=f"{k}_page{p}")
        return {"results": {k: res}}

    def factsheet_date(state: Pass2):
        # data date read from the opening pages: used for funds whose page prints no AUM / NAV date
        # (ABSL JAN: 'AUM as on last day'); a fund's own printed date wins but is flagged when its month differs
        return {"fs": pages.factsheet_date()}

    g = StateGraph(Pass2)
    g.add_node("first_pages", first_pages)
    g.add_node("extract_fund", extract_fund)
    g.add_node("factsheet_date", factsheet_date)
    g.add_edge(START, "first_pages")
    g.add_edge(START, "factsheet_date")
    g.add_conditional_edges("first_pages", fan_out, ["extract_fund", END])
    g.add_edge("extract_fund", END)
    g.add_edge("factsheet_date", END)
    return g.compile()


@traceable(name="process_factsheet", run_type="chain",
           process_inputs=lambda i: {"pdf": i["pdf"].name, "year": i["year"], "month": i["mon"]})
def process_factsheet(pdf, year, mon, master, alias, manager_alias, categories, force, force_llm):
    amc = pdf.stem
    out_dir = OUT / str(year) / mon / amc
    out_dir.mkdir(parents=True, exist_ok=True)
    house = fund_house_for(amc, master.fund_house.unique())
    # 'absl' / 'pru': the scheme master's short brands ('ABSL Large Cap Fund', 'ICICI Pru Smallcap Fund')
    brand_tokens = (set(norm(house).split()) | set(norm(amc).split()) | {"absl", "pru"}) - {"mutual", "fund"}

    doc = pdfium.PdfDocument(str(pdf))
    texts = [doc[i].get_textpage().get_text_range() for i in range(len(doc))]
    doc.close()

    # ---- pass 1: index
    idx_pages = find_index_pages(texts)
    if idx_pages:
        idx_md = docling_pages(pdf, idx_pages, out_dir / "pages", force)
        idx_text = "\n\n".join(idx_md[p] for p in idx_pages)
        entries = index_entries(idx_text)
        # names docling split badly are recovered from the text layer; docling wins where both have a page
        seen = {norm(n) for n, _ in entries}
        entries += [(n, p) for n, p in index_entries("\n".join(texts[p - 1] for p in idx_pages)) if norm(n) not in seen]
    else:
        # 360 ONE prints no index page: every fund is then found by the page that prints its name as a title (below)
        print(f"  no index page found in {pdf.name}; looking for each fund's page title", flush=True)
        idx_text, entries = "", []
    targets = match_targets(entries, house, master, alias, brand_tokens)
    targets = {k: v for k, v in targets.items() if v["category"] in categories}
    resolve_pages(targets, entries, texts, set(idx_pages), amc)
    place_by_title(targets, texts, amc, idx_pages)
    pages = Pages(pdf, texts, skip=idx_pages, date_cache=out_dir / "llm" / "_factsheet_date.json", force=force_llm)

    # schemes no index entry matched: a page printing the scheme master's name (or an alias) as its title, else
    # reported missing - a factsheet that names the fund differently needs a row in factsheet_scheme_alias.csv
    expected = master[(master.fund_house == house) & master.category.map(CATEGORIES).isin(categories)]
    missing = []
    for r in expected.itertuples():
        code = str(r.scheme_code)
        if targets.get(code, {}).get("pdf_page"):
            continue
        t = targets.get(code) or dict(scheme_code=code, scheme_name=master_base_name(r.scheme_name),
                                      category=CATEGORIES[r.category], index_name=master_base_name(r.scheme_name), index_page=None)
        names = [t["index_name"], *alias[(alias.fund_house == house) & (alias.scheme_code.astype(str) == code)].factsheet_name]
        p = next(filter(None, (relocate(dict(t, index_name=n, pdf_page=1), texts, amc, idx_pages) for n in names)), None)
        if p:
            targets[code] = dict(t, pdf_page=p, span=MAX_FUND_PAGES, matched_by="scheme master name as a page title")
            print(f"  {t['scheme_name']}: not in the index, PDF page {p} prints it as a title", flush=True)
        else:
            missing.append((t["scheme_name"], "no index entry or page title matches the scheme master name or an alias "
                                              "(not in this factsheet, or add its printed name to factsheet_scheme_alias.csv)"))
    write_index_md(out_dir / "index.md", amc, house, year, mon, idx_pages, targets, missing, idx_text)

    # ---- pass 2: fund pages -> the per-fund extraction agents (graph below)
    details = {k: dict(t, pages=[], md="", result=None) for k, t in targets.items() if t.get("pdf_page")}
    out = pass2_graph(pages, details, out_dir, f"{mon.title()} {year}", force, force_llm).invoke(
        {"results": {}}, {"max_concurrency": LLM_WORKERS})
    for k, res in out["results"].items():
        details[k]["result"] = res
    fs = out["fs"]
    # the model read 2020-03-31 off Mirae's opening pages: not this factsheet's data month, so not used for any fund
    if fx.date_out_of_window(fs.data_as_on_date, f"{mon.title()} {year}"):
        fs = fx.FactsheetDate()
    page_dates = [d["result"].aum_as_on_date for d in details.values() if d["result"] and d["result"].aum_as_on_date]
    as_on = fs.data_as_on_date or (Counter(page_dates).most_common(1)[0][0] if page_dates else None)

    rows = []
    for d in sorted(details.values(), key=lambda d: d["pdf_page"]):
        r = d["result"]
        fund_as_on = (r and r.aum_as_on_date) or as_on
        source = "page" if r and r.aum_as_on_date else "factsheet"
        if source == "page" and fs.data_as_on_date and fund_as_on[:7] != fs.data_as_on_date[:7]:
            source = f"page; factsheet says {fs.data_as_on_date}"
        base = dict(year=year, factsheet_month=mon, as_on_date=fund_as_on, as_on_source=source,
                    portfolio_month=fund_as_on[:7] if fund_as_on else None, amc=amc, fund_house=house,
                    category=d["category"], scheme_code=d["scheme_code"], scheme_name=d["scheme_name"],
                    factsheet_fund_name=d["index_name"], index_page=d["index_page"],
                    pdf_pages=",".join(map(str, (r and r.fund_found and r.source_pages) or d["pages"])),
                    aum_month_end_cr=r and r.aum_month_end_cr, aum_monthly_avg_cr=r and r.aum_monthly_average_cr,
                    aum_check=aum_check(r), notes=r and r.notes)
        managers = [dict(fund_manager=manager_alias.get((house, m.name), m.name), managing_since=m.managing_since,
                         managing_since_raw=m.managing_since_as_printed, manager_role=m.role)
                    for m in (r.fund_managers if r else [])]
        for mgr in managers or [dict(fund_manager=None, managing_since=None, managing_since_raw=None, manager_role=None)]:
            rows.append(dict(base, **mgr))
        slug = re.sub(r"[^A-Za-z0-9]+", "_", d["scheme_name"]).strip("_")
        (out_dir / "funds").mkdir(exist_ok=True)
        (out_dir / "funds" / f"{d['scheme_code']}_{slug}.md").write_text(
            f"# {d['scheme_name']}\n\nscheme_code {d['scheme_code']} | {d['category']} | PDF pages {d['pages']}\n" + d["md"],
            encoding="utf-8")
    write_funds_md(out_dir / "funds.md", amc, year, mon, as_on, rows)
    pd.DataFrame(rows).to_csv(out_dir / "fund_details.csv", index=False)
    post_scores(evaluate_factsheet(rows, missing))
    return rows


def md_table(headers, rows):
    esc = lambda v: "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).replace("|", "/")
    return "\n".join(["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
                     + ["| " + " | ".join(esc(v) for v in r) + " |" for r in rows])


def write_index_md(path, amc, house, year, mon, idx_pages, targets, missing, idx_text):
    rows = [(t["category"], t["index_name"], t["scheme_code"], t["scheme_name"], t["index_page"], t.get("pdf_page") or "NOT FOUND",
             t.get("matched_by", "index name"))
            for t in sorted(targets.values(), key=lambda t: t.get("pdf_page") or t["index_page"] or 0)]
    parts = [f"# {amc} - {mon} {year} factsheet index", "",
             f"Fund house (scheme master): {house}  ",
             f"Index on PDF pages: {', '.join(map(str, idx_pages))}", "",
             "## Funds captured", "",
             md_table(["Category", "Name in factsheet", "Scheme code", "Scheme master name", "Printed page", "PDF page", "Matched by"], rows)]
    if missing:
        parts += ["", "## In scheme master but no page found in this factsheet", ""] + [f"- {m}: {why}" for m, why in missing]
    parts += ["", "## Docling output for the index pages", "", idx_text]
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def write_funds_md(path, amc, year, mon, as_on, rows):
    parts = [f"# {amc} - {mon} {year} factsheet: fund managers and AUM", "",
             f"Data as on: {as_on or 'unknown'}", ""]
    for code in dict.fromkeys(r["scheme_code"] for r in rows):
        rs = [r for r in rows if r["scheme_code"] == code]
        r0 = rs[0]
        parts += [f"## {r0['scheme_name']}", "",
                  f"- Category: {r0['category']}",
                  f"- Scheme code: {code}",
                  f"- Name in factsheet: {r0['factsheet_fund_name']}",
                  f"- PDF pages: {r0['pdf_pages']}",
                  f"- AUM as on {r0['as_on_date'] or '?'}: month end {r0['aum_month_end_cr'] if r0['aum_month_end_cr'] is not None else 'NOT FOUND'} Cr,"
                  f" monthly average {r0['aum_monthly_avg_cr'] if r0['aum_monthly_avg_cr'] is not None else 'NOT FOUND'} Cr"
                  f" (check: {r0['aum_check']})"]
        if r0["notes"]:
            parts.append(f"- Notes: {r0['notes']}")
        parts += ["", md_table(["Fund manager", "Role", "Managing since", "As printed"],
                               [(r["fund_manager"] or "NOT FOUND", r["manager_role"], r["managing_since"], r["managing_since_raw"]) for r in rs]), ""]
    path.write_text("\n".join(parts), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--year", help="Only this year folder, e.g. 2026")
    ap.add_argument("--month", help="Only this month folder, e.g. JUN")
    ap.add_argument("--amc", help="Only factsheets whose file name contains this text")
    ap.add_argument("--category", action="append", help=f"Only these categories (repeatable). Default: {', '.join(CATEGORIES.values())}")
    ap.add_argument("--force", action="store_true", help="Re-run docling and the LLM even when cached")
    ap.add_argument("--force-llm", action="store_true", help="Re-ask the LLM even when an answer is cached (docling cache kept)")
    args = ap.parse_args()

    problem = fx.missing_credentials()
    if problem:
        fx.disable_model(problem)  # the code rules still run; funds they cannot finish are flagged in notes

    master = pd.read_excel(MASTER, dtype={"scheme_code": str})
    alias = pd.read_csv(ALIAS, dtype={"scheme_code": str}) if ALIAS.exists() else pd.DataFrame(columns=["fund_house", "factsheet_name", "scheme_code"])
    manager_alias = {(r.fund_house, r.printed_name): r.manager_name for r in pd.read_csv(MANAGER_ALIAS).itertuples()}
    categories = set(args.category or CATEGORIES.values())
    unknown = categories - set(CATEGORIES.values())
    if unknown:
        ap.error(f"unknown category {sorted(unknown)}; choose from {sorted(CATEGORIES.values())}")

    pdfs = []
    for ydir in sorted(p for p in ROOT.iterdir() if p.is_dir() and re.fullmatch(r"\d{4}", p.name)):
        if args.year and ydir.name != str(args.year):
            continue
        for mdir in sorted((p for p in ydir.iterdir() if p.is_dir() and p.name.upper() in MONTHS), key=lambda p: MONTHS.index(p.name.upper())):
            if args.month and mdir.name.upper() != args.month.upper():
                continue
            pdfs += [(int(ydir.name), mdir.name.upper(), f) for f in sorted(mdir.glob("*.pdf"))
                     if not args.amc or args.amc.lower() in f.stem.lower()]

    for year, mon, pdf in pdfs:
        print(f"{year} {mon} {pdf.stem}", flush=True)
        try:
            rows = process_factsheet(pdf, year, mon, master, alias, manager_alias, categories, args.force, args.force or args.force_llm,
                                     langsmith_extra={"metadata": {"amc": pdf.stem, "year": year, "month": mon},
                                                      "tags": ["factsheet", f"{year}-{mon}"]})
        except Exception as e:  # one bad PDF should not stop the batch
            if fx.auth_error(e):
                raise SystemExit(f"Azure OpenAI rejected the request ({type(e).__name__}): check API_KEY, endpoint "
                                 "and deployment_name in the repo-root .env")
            print(f"  ! FAILED: {type(e).__name__}: {e}")
            continue
        funds = {r["scheme_code"] for r in rows}
        no_mgr = {r["scheme_code"] for r in rows if not r["fund_manager"]}
        no_aum = {r["scheme_code"] for r in rows if r["aum_month_end_cr"] is None}
        odd = {r["scheme_code"] for r in rows if r["aum_check"] not in ("ok", "missing")}
        print(f"  {len(funds)} funds, {len(rows)} manager rows; missing manager: {len(no_mgr)}, "
              f"missing AUM: {len(no_aum)}, AUM to review: {len(odd)}", flush=True)

    # combined file over every factsheet processed so far (not just this run); an output whose PDF is no longer in
    # <YEAR>/<MON>/ is left out, so a month an AMC did not publish is simply absent (remove or move its PDF)
    parts = [pd.read_csv(f, dtype={"scheme_code": str}) for f in sorted(OUT.glob("*/*/*/fund_details.csv"))
             if f.stat().st_size > 2  # a factsheet that yielded no funds writes an empty file
             and (ROOT / f.parts[-4] / f.parts[-3] / f"{f.parts[-2]}.pdf").exists()]
    parts = [p for p in parts if not p.empty]
    if parts:
        allrows = pd.concat(parts, ignore_index=True)
        allrows.to_csv(OUT / "factsheet_fund_managers.csv", index=False)
        print(f"\n{len(allrows)} rows -> {OUT / 'factsheet_fund_managers.csv'}")
    flush_traces()


if __name__ == "__main__":
    main()
