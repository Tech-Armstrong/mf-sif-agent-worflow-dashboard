"""Checks on the holdings themselves, which the parser's totals cannot see: a holding typed differently from how other
funds type it, an equity ISIN that is missing or not real, a holding that changed type since last month, and factsheet
AUM that disagrees with the portfolio's own net assets.

Each found the kind of error it is named for in 2026 data: Nippon's Vedanta shares typed as REIT units, Kotak's Malco
Energy with a temporary code typed as foreign, DSP Value's foreign stocks typed as stock futures in July. A check for
holdings that vanished since last month was tried and left out: at 2% and over it flagged 22-47 a month, mostly real sales.
Plain functions on lists of rows, no LangSmith.
"""
import calendar
import re

MONTHS = [calendar.month_name[number].lower() for number in range(1, 13)]
MAX_LISTED = 10
DERIVATIVE = re.compile(r"^derivative|futures|options", re.I)
SUFFIX = re.compile(r"-[A-Z]{1,4}$")  # the parser's own suffixes for partly paid, preference, rights, covered calls


def rows_for_month(document, year, month):
    """Rows of {year: {category: [fund]}} for one month, as flat dicts. A month is a list of holdings or {"portfolio": [...]}."""
    rows = []
    for category, funds in document.get(str(year), {}).items():
        for fund in funds:
            value = fund.get(month)
            holdings = value.get("portfolio") if isinstance(value, dict) else value
            for h in holdings or []:
                rows.append({"fund": fund.get("fund_name"), "scheme_code": str(fund.get("scheme_amfi_code") or ""), "category": category,
                             "instrument": h.get("instrument"), "isin": h.get("isin") or "", "type": h.get("subType") or "",
                             "asset_class": h.get("asset_class"), "holdings": h.get("holdings") or 0})
    return rows


def previous_month(year, month):
    i = MONTHS.index(month)
    return (year, MONTHS[i - 1]) if i else (year - 1, MONTHS[11])


def is_derivative(row):
    return bool(DERIVATIVE.search(row["type"]))


def isin_valid(code):
    """Shape and check digit of an ISIN, after the parser's own suffix (-PP, -PS, -CC ...)."""
    code = SUFFIX.sub("", code)
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", code):
        return False
    digits = "".join(str(int(c, 36)) for c in code[:-1])
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch) * (2 if i % 2 == 0 else 1)
        total += n // 10 + n % 10
    return (10 - total % 10) % 10 == int(code[-1])


def _isin_core(code):
    return SUFFIX.sub("", code)


def type_mismatches(scope, reference):
    """Holdings in scope typed differently from how most funds in reference type the same ISIN."""
    counts = {}
    for row in reference:
        if row["isin"] and not is_derivative(row):
            counts.setdefault(row["isin"], {}).setdefault(row["type"], set()).add(row["fund"])
    flagged = []
    for row in scope:
        types = counts.get(row["isin"])
        if not types or is_derivative(row) or len(types) < 2:
            continue
        usual = max(types, key=lambda t: len(types[t]))
        if row["type"] != usual and len(types[usual]) > len(types.get(row["type"], ())):
            flagged.append({**row, "usual_type": usual, "why": f"{row['instrument']} is {row['type']}; {len(types[usual])} funds have it as {usual}"})
    return flagged


def bad_isins(scope):
    """
    Equity holdings (not derivatives) whose ISIN is missing, a temporary code, or not a valid ISIN. Unlisted equity is left
    out: a defunct or private company often has no ISIN at all (Magnasound, held by DSP and ABSL every month).
    """
    flagged = []
    for row in scope:
        if row["asset_class"] not in ("Equity", "REITs & InvITs") or is_derivative(row) or row["type"] == "Unlisted Equity":
            continue
        code = row["isin"]
        problem = ("no ISIN" if not code else "temporary code" if "DUMMY" in code or code.startswith("DU")
                   else None if isin_valid(code) else "not a valid ISIN")
        if problem:
            flagged.append({**row, "why": f"{row['instrument']}: {problem}" + (f" ({code})" if code else "")})
    return flagged


def type_changes(now, before):
    """A holding a fund had last month that it has this month under a different type (DSP Value July: foreign stocks as futures)."""
    last = {}
    for row in before:
        if row["isin"] and not is_derivative(row):
            last.setdefault((row["scheme_code"] or row["fund"], _isin_core(row["isin"])), set()).add(row["type"])
    current, first = {}, {}
    for row in now:
        if row["isin"]:
            key = (row["scheme_code"] or row["fund"], _isin_core(row["isin"]))
            current.setdefault(key, set()).add(row["type"])
            first.setdefault(key, row)
    flagged = []
    for key, types_now in current.items():
        if key in last and not types_now & last[key]:
            row = first[key]
            flagged.append({**row, "why": f"{row['instrument']}: {'/'.join(sorted(last[key]))} last month, {'/'.join(sorted(types_now))} now"})
    return flagged


def aum_mismatches(aum_crore, net_assets_crore, tolerance_pct):
    """{scheme code: (fund, AUM)} from the factsheet vs {scheme code: net assets} the portfolio file printed, both in Rs crore."""
    flagged = []
    for code, (fund, aum) in aum_crore.items():
        net = net_assets_crore.get(code)
        if aum and net and abs(aum - net) / net * 100 > tolerance_pct:
            flagged.append({"fund": fund, "scheme_code": code, "why": f"{fund}: factsheet AUM {aum:,.2f} cr, portfolio net assets {net:,.2f} cr"})
    return flagged


def score(flagged, funds_checked):
    """Share of checked funds with nothing flagged; the comment names what was flagged."""
    funds = {row["fund"] for row in flagged}
    comment = "; ".join(row["why"] if row["fund"] in row["why"] else f"{row['fund']}: {row['why']}" for row in flagged[:MAX_LISTED])
    more = f" (+{len(flagged) - MAX_LISTED} more)" if len(flagged) > MAX_LISTED else ""
    return {"score": 1 - len(funds) / funds_checked if funds_checked else 1.0, "comment": (comment + more) if flagged else None}
