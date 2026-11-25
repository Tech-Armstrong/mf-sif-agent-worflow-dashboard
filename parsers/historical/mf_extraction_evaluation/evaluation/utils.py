MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december"
]

def iter_fund_months(document):
    """Yield (year, category, fund, month, monthly_record) from merged_2026-style JSON."""
    if not isinstance(document, dict):
        return
    for year, categories in document.items():
        if not isinstance(categories, dict):
            continue
        for category, funds in categories.items():
            if not isinstance(funds, list):
                continue
            for fund in funds:
                if not isinstance(fund, dict):
                    continue
                for month in MONTHS:
                    if month in fund:
                        yield year, category, fund, month, fund[month]

def normalized_name(value):
    if value is None:
        return ""
    return " ".join(str(value).casefold().replace(".", " ").replace(",", " ").split())
