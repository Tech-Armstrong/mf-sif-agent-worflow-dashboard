from .models import EvaluationIssue, EvaluationResult
from .utils import iter_fund_months

def evaluate_classification(document, expected_classifications=None):
    """
    Compare classifications against a maintained reference mapping.
    expected_classifications maps ISIN or normalized instrument name to
    {"asset_class": "...", "subType": "..."}.
    No inferred taxonomy is imposed by default.
    """
    expected_classifications = expected_classifications or {}
    issues = []
    total = passed = 0

    for _, category, fund, month, record in iter_fund_months(document):
        for holding in record.get("portfolio", []) if isinstance(record.get("portfolio"), list) else []:
            if not isinstance(holding, dict):
                continue
            identifier = holding.get("isin") or holding.get("instrument")
            expected = expected_classifications.get(identifier)
            if expected is None:
                continue

            for field in ("asset_class", "subType"):
                if field not in expected:
                    continue
                total += 1
                actual = holding.get(field)
                if actual == expected[field]:
                    passed += 1
                else:
                    issues.append(EvaluationIssue(
                        "CLS_001", "HIGH", f"Classification mismatch: {field}",
                        category, fund.get("fund_name"), fund.get("scheme_amfi_code"), month,
                        instrument=holding.get("instrument"), field=field,
                        expected=expected[field], actual=actual
                    ))

    failed = total - passed
    score = round((passed / total) * 100, 2) if total else None
    status = "UNVERIFIED" if total == 0 else ("WARNING" if issues else "PASS")
    notes = ["No classification reference mapping supplied; no classification accuracy claim made."] if total == 0 else []
    return EvaluationResult("classification", status, score, total, passed, failed, issues, notes)
