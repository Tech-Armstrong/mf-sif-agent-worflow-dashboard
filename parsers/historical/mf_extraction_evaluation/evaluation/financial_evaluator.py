from collections import Counter
from .models import EvaluationIssue, EvaluationResult
from .utils import iter_fund_months

def evaluate_financials(document, percentage_tolerance=0.5, expected_total_by_fund_month=None):
    """
    Percentage check defaults to 100% +/- tolerance.
    If source-reported expected totals are available, supply a mapping keyed by
    (AMFI code, month) -> expected total and those values take precedence.
    """
    issues = []
    total = passed = 0

    for _, category, fund, month, record in iter_fund_months(document):
        portfolio = record.get("portfolio")
        if not isinstance(portfolio, list) or not portfolio:
            continue

        values = [h.get("holdings") for h in portfolio if isinstance(h, dict)]
        numeric_values = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
        total += 1
        if len(numeric_values) == len(values):
            actual_total = sum(numeric_values)
            key = (str(fund.get("scheme_amfi_code")), month)
            expected = (expected_total_by_fund_month or {}).get(key, 100.0)
            deviation = abs(actual_total - expected)
            if deviation <= percentage_tolerance:
                passed += 1
            else:
                issues.append(EvaluationIssue(
                    "FIN_001", "HIGH", "Holdings percentage total is outside configured tolerance",
                    category, fund.get("fund_name"), fund.get("scheme_amfi_code"), month,
                    field="holdings", expected=expected, actual=round(actual_total, 6)
                ))
        else:
            issues.append(EvaluationIssue(
                "FIN_002", "HIGH", "One or more holding percentages are non-numeric",
                category, fund.get("fund_name"), fund.get("scheme_amfi_code"), month,
                field="holdings"
            ))

        for holding in portfolio:
            if not isinstance(holding, dict):
                continue
            value = holding.get("holdings")
            subtype = str(holding.get("subType") or "").casefold()
            instrument_name = str(holding.get("instrument") or "").casefold()
            is_derivative = (
                "derivative" in subtype
                or "future" in instrument_name
                or "option" in instrument_name
            )
            total += 1
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if value >= 0 or is_derivative:
                    # Negative derivative exposure can be legitimate; do not flag it as a generic error.
                    passed += 1
                else:
                    issues.append(EvaluationIssue(
                        "FIN_003", "WARNING", "Negative holding percentage on a non-derivative instrument; verify against source",
                        category, fund.get("fund_name"), fund.get("scheme_amfi_code"), month,
                        instrument=holding.get("instrument"), field="holdings", actual=value
                    ))
            else:
                issues.append(EvaluationIssue(
                    "FIN_003", "HIGH", "Non-numeric holding percentage detected",
                    category, fund.get("fund_name"), fund.get("scheme_amfi_code"), month,
                    instrument=holding.get("instrument"), field="holdings", actual=value
                ))

        isin_counts = Counter(
            h.get("isin") for h in portfolio
            if isinstance(h, dict) and h.get("isin")
        )
        for isin, count in isin_counts.items():
            if count > 1:
                total += 1
                issues.append(EvaluationIssue(
                    "FIN_004", "WARNING", f"Duplicate ISIN occurs {count} times in this fund-month",
                    category, fund.get("fund_name"), fund.get("scheme_amfi_code"), month,
                    field="isin", expected="Unique within fund-month", actual=isin
                ))
            else:
                total += 1
                passed += 1

    failed = total - passed
    score = round((passed / total) * 100, 2) if total else None
    status = "WARNING" if issues else "PASS"
    return EvaluationResult("financial", status, score, total, passed, failed, issues)
