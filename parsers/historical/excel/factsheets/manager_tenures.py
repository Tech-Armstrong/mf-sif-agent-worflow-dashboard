"""Fund manager tenures from output/factsheet_fund_managers.csv, shared by the merged JSON and the Neo4j loader."""
import csv
import os
from collections import defaultdict
from datetime import datetime

# factsheets cover these funds, but they are outside the categories we track
EXCLUDED_CATEGORIES = {"Large and Mid Cap"}

EXITS_FILE = "factsheet_manager_exits.csv"


def month_year(value):
    """'2024-11-04' or '2024-11' -> 'Nov-2024'."""
    return datetime.strptime(value[:7], "%Y-%m").strftime("%b-%Y")


def load_exits(csv_path):
    """{(scheme_code, manager): [printed exit dates, oldest first]} from the exits file beside csv_path, if there is one."""
    exits = defaultdict(list)
    path = os.path.join(os.path.dirname(os.path.abspath(csv_path)), EXITS_FILE)

    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8", newline="") as file:
            for row in csv.DictReader(file):
                exits[(row["scheme_code"], row["fund_manager"])].append(row["exit_date"])

    return {key: sorted(dates) for key, dates in exits.items()}


def load_tenures(csv_path, wanted_categories=None):
    """
    {scheme_code: {"fundName", "category", "tenures": [{"name", "startDate", "endDate"}]}}.

    A tenure is an unbroken run of the fund's factsheets listing the manager, so someone who leaves and comes back
    has two. Dates are only what the factsheets print, never worked out from the months a manager is listed in.
    startDate is the date printed during the tenure ('Nov-2024', the earliest if the print changed; None when none
    prints one). endDate is 'present' when the fund's latest factsheet still lists them, otherwise the exit date
    printed on the fund's pages (manager_exits.py), None when none prints one.
    """
    funds = {}
    exits = load_exits(csv_path)

    with open(csv_path, "r", encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            if row["category"] in EXCLUDED_CATEGORIES:
                continue

            if wanted_categories and row["category"].strip().lower() not in wanted_categories:
                continue

            fund = funds.setdefault(
                row["scheme_code"],
                {"name": row["scheme_name"], "category": row["category"], "months": set(), "managers": defaultdict(dict)},
            )
            fund["months"].add(row["portfolio_month"])

            if row["fund_manager"]:
                fund["managers"][row["fund_manager"]][row["portfolio_month"]] = row["managing_since"]

    result = {}

    for code, fund in funds.items():
        months = sorted(fund["months"])
        tenures = []

        for name, listed in fund["managers"].items():
            run = []

            for index, month in enumerate(months):
                if month in listed:
                    run.append(month)

                if run and (month not in listed or index == len(months) - 1):
                    since = min((listed[m] for m in run if listed[m]), default=None)
                    left = next((d for d in exits.get((code, name), []) if d[:7] >= run[-1]), None)
                    tenures.append({
                        "name": name,
                        "startDate": month_year(since) if since else None,
                        "endDate": "present" if run[-1] == months[-1] else month_year(left) if left else None,
                    })
                    run = []

        result[code] = {"fundName": fund["name"], "category": fund["category"], "tenures": tenures}

    return result
