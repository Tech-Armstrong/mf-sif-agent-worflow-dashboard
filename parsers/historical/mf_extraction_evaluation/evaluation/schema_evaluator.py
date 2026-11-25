from .models import EvaluationIssue, EvaluationResult
from .utils import MONTHS, iter_fund_months

FUND_REQUIRED = ("fund_name", "scheme_amfi_code", "fund_manager")
HOLDING_REQUIRED = ("instrument", "isin", "asset_class", "industry", "sector", "holdings", "subType", "symbol", "market_cap")

def evaluate_schema(document):
    issues = []
    total = passed = 0

    if not isinstance(document, dict):
        issue = EvaluationIssue("SCH_001", "CRITICAL", "Root JSON must be an object")
        return EvaluationResult("schema", "FAIL", 0, 1, 0, 1, [issue])

    total += 1
    if document:
        passed += 1
    else:
        issues.append(EvaluationIssue("SCH_002", "CRITICAL", "Root JSON is empty"))

    for year, categories in document.items():
        total += 1
        if not isinstance(categories, dict):
            issues.append(EvaluationIssue("SCH_003", "CRITICAL", f"Year '{year}' must contain a category object"))
            continue
        passed += 1

        for category, funds in categories.items():
            total += 1
            if not isinstance(funds, list):
                issues.append(EvaluationIssue("SCH_004", "CRITICAL", f"Category '{category}' must contain a list of funds", category=category))
                continue
            passed += 1

            for fund in funds:
                if not isinstance(fund, dict):
                    total += 1
                    issues.append(EvaluationIssue("SCH_005", "CRITICAL", "Fund entry must be an object", category=category))
                    continue

                fund_name = fund.get("fund_name")
                amfi = fund.get("scheme_amfi_code")
                for key in FUND_REQUIRED:
                    total += 1
                    if key in fund and fund[key] is not None:
                        passed += 1
                    else:
                        issues.append(EvaluationIssue(
                            "SCH_006", "HIGH", f"Missing required fund field: {key}",
                            category=category, fund_name=fund_name, amfi_code=amfi, field=key
                        ))

                for month in MONTHS:
                    if month not in fund:
                        continue
                    record = fund[month]
                    total += 1
                    if isinstance(record, dict):
                        passed += 1
                    else:
                        issues.append(EvaluationIssue(
                            "SCH_007", "CRITICAL", f"Monthly record '{month}' must be an object",
                            category=category, fund_name=fund_name, amfi_code=amfi, month=month
                        ))
                        continue

                    total += 1
                    if "AUM" in record:
                        passed += 1
                    else:
                        issues.append(EvaluationIssue(
                            "SCH_008", "HIGH", "Monthly record is missing the AUM key",
                            category=category, fund_name=fund_name, amfi_code=amfi, month=month, field="AUM"
                        ))

                    total += 1
                    portfolio = record.get("portfolio")
                    if isinstance(portfolio, list):
                        passed += 1
                    else:
                        issues.append(EvaluationIssue(
                            "SCH_009", "CRITICAL", "Monthly record portfolio must be a list",
                            category=category, fund_name=fund_name, amfi_code=amfi, month=month, field="portfolio"
                        ))
                        continue

                    for holding in portfolio:
                        total += 1
                        if isinstance(holding, dict):
                            passed += 1
                        else:
                            issues.append(EvaluationIssue(
                                "SCH_010", "CRITICAL", "Portfolio holding must be an object",
                                category=category, fund_name=fund_name, amfi_code=amfi, month=month
                            ))
                            continue

                        for key in HOLDING_REQUIRED:
                            total += 1
                            if key in holding:
                                passed += 1
                            else:
                                issues.append(EvaluationIssue(
                                    "SCH_011", "HIGH", f"Holding is missing expected key: {key}",
                                    category=category, fund_name=fund_name, amfi_code=amfi,
                                    month=month, instrument=holding.get("instrument"), field=key
                                ))

                        total += 1
                        if isinstance(holding.get("holdings"), (int, float)) and not isinstance(holding.get("holdings"), bool):
                            passed += 1
                        else:
                            issues.append(EvaluationIssue(
                                "SCH_012", "HIGH", "Holding percentage must be numeric",
                                category=category, fund_name=fund_name, amfi_code=amfi,
                                month=month, instrument=holding.get("instrument"), field="holdings",
                                actual=holding.get("holdings")
                            ))

    failed = total - passed
    score = round((passed / total) * 100, 2) if total else None
    status = "FAIL" if any(i.severity == "CRITICAL" for i in issues) else ("WARNING" if issues else "PASS")
    return EvaluationResult("schema", status, score, total, passed, failed, issues)
