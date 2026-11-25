from .models import EvaluationIssue, EvaluationResult
from .utils import iter_fund_months, MONTHS

def evaluate_completeness(document, expected_months_by_amfi=None):
    """
    Checks fields and fund-month coverage.
    expected_months_by_amfi is optional, e.g. {"12345": ["may", "june"]}.
    Do not infer a fund's inception date from missing records.
    """
    issues = []
    total = passed = 0

    for year, category, fund, month, record in iter_fund_months(document):
        name, amfi = fund.get("fund_name"), fund.get("scheme_amfi_code")

        total += 1
        if record.get("AUM") is not None:
            passed += 1
        else:
            issues.append(EvaluationIssue(
                "COMP_001", "HIGH", "AUM is null or missing",
                category, name, amfi, month, field="AUM", actual=record.get("AUM")
            ))

        portfolio = record.get("portfolio")
        total += 1
        if isinstance(portfolio, list) and len(portfolio) > 0:
            passed += 1
        else:
            issues.append(EvaluationIssue(
                "COMP_002", "HIGH", "Portfolio is empty or unavailable",
                category, name, amfi, month, field="portfolio"
            ))
            continue

        for holding in portfolio:
            instrument = holding.get("instrument") if isinstance(holding, dict) else None

            total += 1
            if instrument and str(instrument).strip():
                passed += 1
            else:
                issues.append(EvaluationIssue(
                    "COMP_003", "HIGH", "Instrument name is missing",
                    category, name, amfi, month, instrument=instrument, field="instrument"
                ))

            # A null ISIN is recorded as a warning, not automatically an extraction failure.
            total += 1
            if holding.get("isin") is not None and str(holding.get("isin")).strip():
                passed += 1
            else:
                issues.append(EvaluationIssue(
                    "COMP_004", "WARNING", "ISIN is missing; verify whether it exists in the source",
                    category, name, amfi, month, instrument=instrument, field="isin"
                ))

    # Coverage check only when caller supplies an expected-month schedule.
    if expected_months_by_amfi:
        found = {}
        for _, _, fund, month, _ in iter_fund_months(document):
            code = str(fund.get("scheme_amfi_code"))
            found.setdefault(code, set()).add(month)

        for amfi, expected_months in expected_months_by_amfi.items():
            actual = found.get(str(amfi), set())
            for month in expected_months:
                total += 1
                month = month.lower()
                if month in actual:
                    passed += 1
                else:
                    issues.append(EvaluationIssue(
                        "COMP_005", "HIGH", "Expected fund-month record is missing",
                        amfi_code=str(amfi), month=month
                    ))

    failed = total - passed
    score = round((passed / total) * 100, 2) if total else None
    status = "WARNING" if issues else "PASS"
    return EvaluationResult("completeness", status, score, total, passed, failed, issues)
