"""Rubrics for the AUM / fund manager extraction, one score dict per fund or factsheet.

There is no verified reference for these fields yet, so a pass means nothing looks wrong, not that the value is
right. A score is 0-1; a rubric that cannot apply (no monthly average to compare with) is left out, not passed.
Scores named info_* only show how an answer was reached (agent or rules, agreement with the rules): they are not
failures and are not alerts.
"""
import re

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
MAX_END_TO_AVERAGE = 1.43  # same limits as the agent's own check and aum_check: month-end vs average AUM
MIN_END_TO_AVERAGE = 0.7
MAX_LISTED = 10


def _score(passed, comment=None):
    return {"score": 1.0 if passed else 0.0, "comment": None if passed else comment}


def _in_window(date, factsheet_month):
    """A factsheet reports its own month or the one or two before (ABSL's JAN factsheet is as on 31-Dec)."""
    folder = re.fullmatch(r"(\w{3})\w*\s+(\d{4})", (factsheet_month or "").strip())
    seen = re.match(r"(\d{4})-(\d{2})", date or "")
    if not folder or not seen or folder.group(1).lower() not in MONTHS:
        return None
    folder_index = int(folder.group(2)) * 12 + MONTHS.index(folder.group(1).lower())
    seen_index = int(seen.group(1)) * 12 + int(seen.group(2)) - 1
    return folder_index - 2 <= seen_index <= folder_index


def evaluate_fund_answer(answer, cache_entry=None, factsheet_month=None):
    """
    answer: FundDetails as a dict. cache_entry: the fund's llm/<scheme_code>.json (how the answer was reached).
    Returns {rubric: {"score": 0-1, "comment": why it failed}}.
    """
    entry = cache_entry or {}
    managers = answer.get("fund_managers") or []
    month_end, average = answer.get("aum_month_end_cr"), answer.get("aum_monthly_average_cr")
    scores = {
        "fund_found": _score(answer.get("fund_found"), "the agent found no page for this fund"),
        "manager_present": _score(bool(managers), "no fund manager"),
        "aum_present": _score(month_end is not None, "no month-end AUM"),
    }

    date = answer.get("aum_as_on_date")
    in_window = _in_window(date, factsheet_month)
    if date is None:
        scores["aum_date_in_window"] = _score(False, "no AUM date")
    elif in_window is not None:
        scores["aum_date_in_window"] = _score(in_window, f"AUM date {date} is outside factsheet month {factsheet_month}")

    if month_end and average:
        ratio = month_end / average
        scores["aum_end_vs_average"] = _score(MIN_END_TO_AVERAGE <= ratio <= MAX_END_TO_AVERAGE,
                                              f"month-end {month_end} vs average {average}, ratio {ratio:.2f}")

    source = entry.get("source")
    if source:
        pending = entry.get("model_pending")
        scores["info_answered_by_agent"] = _score(source != "rules", f"code rules only: {pending or 'model not used'}")
    if source == "agent":
        unresolved = entry.get("unresolved") or []
        # the agent's own checks still failed after its last try: an alert, not information
        scores["agent_checks_clean"] = _score(not unresolved, " | ".join(i[:120] for i in unresolved[:3]))
        disagreements = entry.get("disagreements") or {}
        scores["info_rules_agree"] = _score(not disagreements, "; ".join(
            f"{field}: rules {values[0]}, agent {values[1]}" for field, values in disagreements.items()))
    return scores


def evaluate_factsheet(rows, missing=()):
    """
    One headline per factsheet beside coverage: funds_complete, the funds with both a manager and a month-end AUM.
    rows: one per fund manager, as fund_details.csv. missing: (scheme, reason) the scheme master expected.
    """
    funds = {}
    for row in rows:
        fund = funds.setdefault(row["scheme_code"], {"manager": False, "aum": False})
        fund["manager"] = fund["manager"] or bool(row.get("fund_manager"))
        fund["aum"] = fund["aum"] or row.get("aum_month_end_cr") is not None

    expected = len(funds) + len(missing)
    scores = {"fund_coverage": {
        "score": len(funds) / expected if expected else 0.0,
        "comment": "not found: " + "; ".join(str(m[0]) for m in list(missing)[:MAX_LISTED]) if missing else None,
    }}
    if funds:
        incomplete = [f"{code} ({', '.join(k for k in ('manager', 'aum') if not fund[k])} missing)"
                      for code, fund in funds.items() if not (fund["manager"] and fund["aum"])]
        scores["funds_complete"] = {"score": 1 - len(incomplete) / len(funds),
                                    "comment": "; ".join(incomplete[:MAX_LISTED]) or None}
    return scores
