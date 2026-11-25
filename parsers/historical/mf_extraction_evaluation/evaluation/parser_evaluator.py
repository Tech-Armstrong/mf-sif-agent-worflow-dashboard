"""Rubrics for the Excel parser, one score dict per file and one for the run.

Each takes the parser's own scheme snapshot, so the checks stay in one place. The market value check is the strict
one: rows are compared with the grand total the AMC printed, in lakhs. The % total allows +/-0.5, which can hide a
counted subtotal or a missing row. A pass means the rows add up to the file's total, not that each row is right.
"""
FILE_RUBRICS = ("total_matches_source", "pct_total_ok", "no_dropped_rows", "scheme_code_found")
MAX_LISTED = 10
# scheme master category -> our category name: the funds we track, as CATEGORIES in excel/factsheets/factsheet_parser.py
TRACKED_CATEGORIES = {
    "LARGE CAP": "Large Cap", "MID CAP": "Mid Cap", "SMALL CAP": "Small Cap",
    "FLEXI CAP": "Flexi Cap", "FOCUSED": "Focused", "VALUE": "Value", "CONTRA": "Contra",
    "THEMATIC BUSINESS CYCLE": "Business Cycle", "BALANCED ADVANTAGE": "Balanced Advantage",
    "MULTI ASSET ALLOCATION": "Multi Asset", "CONSERVATIVE HYBRID": "Conservative Hybrid",
    "BALANCED HYBRID": "Balanced Hybrid",
}


def _score(passed, comment=None):
    return {"score": 1.0 if passed else 0.0, "comment": None if passed else comment}


def _dropped_row(row):
    value = row.get("market_value_lakhs")
    worth = f"{value:,.2f} lakhs" if isinstance(value, (int, float)) and value == value else "no value"
    return f"row {row.get('source_row')} {row.get('row_label')} ({worth})"


def evaluate_parsed_file(snapshot, dropped_rows=(), tracked=True):
    """
    snapshot: a row of scheme_snapshot.csv. dropped_rows: the rows the parser could not classify in this file.
    Returns {rubric: {"score": 0 or 1, "comment": why it failed}}. A file that prints no market values has nothing to
    compare, so total_matches_source is left out, not passed. tracked: whether the fund is in a category we track; only those
    need an AMFI code.
    """
    scores = {}
    status = snapshot.get("recon_status")
    if status in ("OK", "CHECK"):
        gap, total = snapshot["mv_reconciliation_diff_lakhs"], snapshot["net_assets_lakhs"]
        share = f" ({gap / total * 100:+.3f}% of net assets)" if total else ""
        scores["total_matches_source"] = _score(
            status == "OK",
            f"holdings sum to {snapshot['sum_market_value_lakhs']:,.2f} lakhs, {snapshot.get('grand_total_label')} is "
            f"{total:,.2f}: gap {gap:+,.2f}{share}")
    else:  # no printed total to compare with: the % total is the only check left (it allows +/-0.5, so it is the weaker one)
        scores["pct_total_ok"] = _score(snapshot.get("pct_total_status") == "OK",
                                        f"holdings sum to {snapshot.get('sum_pct_to_nav')}% of net assets")
    scores["no_dropped_rows"] = _score(not dropped_rows, "; ".join(_dropped_row(row) for row in list(dropped_rows)[:MAX_LISTED]))
    if tracked:
        code = snapshot.get("amc_scheme_code")
        scores["scheme_code_found"] = _score(code is not None and code == code,
                                             f"no AMFI code for {snapshot.get('scheme_name')!r}: the fund cannot be linked to its factsheet data")
    failed = [rubric for rubric in FILE_RUBRICS if rubric in scores and scores[rubric]["score"] < 1]
    scores["file_pass"] = _score(not failed, ", ".join(failed))
    return scores


def expected_funds(master_rows, first_month_by_amc, months, category=None):
    """
    The fund-months a run should contain. master_rows: {scheme_code, fund_house, category, scheme_name}. first_month_by_amc:
    {AMC name: (year, month)} from the disclosure folders, so an AMC is expected only from the month it was first added.
    months: the (year, month) the run covers. category: limit to one of our category names. Returns one dict per fund-month.
    """
    expected = []
    for row in master_rows:
        ours = TRACKED_CATEGORIES.get(row["category"])
        first = first_month_by_amc.get(f"{row['fund_house']} Mutual Fund")
        if ours is None or first is None or (category and ours != category):
            continue
        for month in sorted(months):
            if month >= first:
                expected.append({"year": month[0], "month": month[1], "scheme_code": str(row["scheme_code"]),
                                 "scheme_name": row["scheme_name"], "amc": f"{row['fund_house']} Mutual Fund"})
    return expected


def evaluate_fund_coverage(expected, found):
    """expected: from expected_funds. found: {(year, month, scheme code)} parsed. A fund that never arrived has no file to fail on."""
    missing = [fund for fund in expected if (fund["year"], fund["month"], fund["scheme_code"]) not in found]
    listed = "; ".join(f"{fund['year']}-{fund['month']:02d} {fund['scheme_name']}" for fund in missing[:MAX_LISTED])
    comment = f"{len(missing)} of {len(expected)} expected fund-months missing: {listed}" if missing else None
    return {"expected_funds_found": {"score": 1 - len(missing) / len(expected) if expected else 1.0, "comment": comment}}, missing


def trace_scores(scores):
    """What goes to LangSmith for a file: the checks themselves. file_pass only repeats them, so it stays in parser_rubrics.json."""
    return {rubric: value for rubric, value in scores.items() if rubric != "file_pass"}


def evaluate_parser_run(file_scores, skipped=()):
    """
    One headline for the run, files_pass, naming each failed file and the checks it failed, beside files_parsed. A
    share per check would only repeat the files' own scores.
    file_scores: {source_file: the file's scores}. skipped: files that failed to parse, as {source_file, error_type, error}.
    """
    total = len(file_scores) + len(skipped)
    scores = {"files_parsed": {
        "score": len(file_scores) / total if total else 0.0,
        "comment": "; ".join(f"{s['source_file']}: {s['error_type']}" for s in list(skipped)[:MAX_LISTED]) or None,
    }}
    if file_scores:
        failed = {source: s["file_pass"]["comment"] for source, s in file_scores.items() if s["file_pass"]["score"] < 1}
        listed = "; ".join(f"{source} ({why})" for source, why in list(failed.items())[:MAX_LISTED])
        scores["files_pass"] = {"score": 1 - len(failed) / len(file_scores),
                                "comment": f"{len(failed)} of {len(file_scores)} failed: {listed}" if failed else None}
    return scores
