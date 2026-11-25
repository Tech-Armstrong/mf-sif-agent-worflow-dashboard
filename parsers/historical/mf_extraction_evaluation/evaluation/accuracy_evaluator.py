from .models import EvaluationIssue, EvaluationResult
from .utils import normalized_name

def evaluate_fund_month_accuracy(expected_portfolio, actual_portfolio, category=None,
                                 fund_name=None, amfi_code=None, month=None,
                                 percentage_tolerance=0.01):
    """
    Compare one verified reference portfolio with one extracted portfolio.
    Match by ISIN first; use normalized instrument name only when ISIN is absent.
    This evaluator requires a trusted reference, not raw unverified OCR.
    """
    issues = []
    total = passed = 0

    def key_for(h):
        isin = h.get("isin")
        if isin:
            return ("isin", str(isin).strip().upper())
        return ("name", normalized_name(h.get("instrument")))

    expected_map = {key_for(h): h for h in expected_portfolio if isinstance(h, dict)}
    actual_map = {key_for(h): h for h in actual_portfolio if isinstance(h, dict)}

    for key, expected in expected_map.items():
        instrument = expected.get("instrument")
        actual = actual_map.get(key)

        total += 1
        if actual is None:
            issues.append(EvaluationIssue(
                "ACC_001", "HIGH", "Reference holding is missing from extracted output",
                category, fund_name, amfi_code, month, instrument=instrument
            ))
            continue
        passed += 1

        for field in ("isin", "instrument", "asset_class", "industry", "sector", "subType", "symbol", "market_cap"):
            exp, act = expected.get(field), actual.get(field)
            total += 1
            if exp == act:
                passed += 1
            else:
                issues.append(EvaluationIssue(
                    "ACC_002", "WARNING" if field in ("industry", "sector", "symbol", "market_cap") else "HIGH",
                    f"Field mismatch: {field}", category, fund_name, amfi_code, month,
                    instrument=instrument, field=field, expected=exp, actual=act
                ))

        exp_pct, act_pct = expected.get("holdings"), actual.get("holdings")
        total += 1
        if isinstance(exp_pct, (int, float)) and isinstance(act_pct, (int, float)) and abs(exp_pct - act_pct) <= percentage_tolerance:
            passed += 1
        else:
            issues.append(EvaluationIssue(
                "ACC_003", "HIGH", "Holding percentage mismatch",
                category, fund_name, amfi_code, month, instrument=instrument,
                field="holdings", expected=exp_pct, actual=act_pct
            ))

    for key, actual in actual_map.items():
        if key not in expected_map:
            total += 1
            issues.append(EvaluationIssue(
                "ACC_004", "HIGH", "Extracted holding is not present in the verified reference",
                category, fund_name, amfi_code, month, instrument=actual.get("instrument")
            ))

    failed = total - passed
    score = round((passed / total) * 100, 2) if total else None
    status = "WARNING" if issues else "PASS"
    return EvaluationResult("accuracy", status, score, total, passed, failed, issues)
