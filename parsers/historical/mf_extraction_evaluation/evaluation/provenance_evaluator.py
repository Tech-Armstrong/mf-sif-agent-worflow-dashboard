from .models import EvaluationIssue, EvaluationResult
from .utils import iter_fund_months

def evaluate_provenance(document, require_provenance=False):
    """
    The current merged JSON schema does not contain source references.
    Default mode reports this as unverified rather than failing all records.
    Enable require_provenance after the extraction schema stores source_reference.
    """
    issues = []
    total = passed = 0

    for _, category, fund, month, record in iter_fund_months(document):
        portfolio = record.get("portfolio")
        if not isinstance(portfolio, list):
            continue
        for holding in portfolio:
            if not isinstance(holding, dict):
                continue
            total += 1
            ref = holding.get("source_reference")
            valid = isinstance(ref, dict) and bool(ref.get("file")) and (
                ref.get("page") is not None or ref.get("sheet") is not None or ref.get("cell_range")
            )
            if valid:
                passed += 1
            elif require_provenance:
                issues.append(EvaluationIssue(
                    "PROV_001", "HIGH", "Holding has no usable source reference",
                    category, fund.get("fund_name"), fund.get("scheme_amfi_code"), month,
                    instrument=holding.get("instrument"), field="source_reference"
                ))

    failed = total - passed
    if not require_provenance:
        return EvaluationResult(
            "provenance", "UNVERIFIED", None, total, 0, 0, [],
            ["Source references are not part of the current merged JSON schema. Add source_reference before enforcing this evaluator."]
        )

    score = round((passed / total) * 100, 2) if total else None
    status = "WARNING" if issues else "PASS"
    return EvaluationResult("provenance", status, score, total, passed, failed, issues)
