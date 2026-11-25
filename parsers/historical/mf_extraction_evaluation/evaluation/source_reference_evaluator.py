from collections import Counter
from .models import EvaluationIssue, EvaluationResult

def _normalize_name(value):
    if value is None:
        return ""
    import re
    value = str(value).casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9 ]", " ", value)
    tokens = ["ltd" if token in {"limited", "ltd"} else token for token in value.split()]
    return " ".join(tokens)

def compare_against_golden_reference(extracted_document, reference_dataset, percentage_tolerance=0.01):
    """
    Compare merged_2026-style extraction JSON against a source-backed reference dataset.

    Match key: (scheme_amfi_code, month, ISIN).
    Percentage tolerance is expressed in percentage points, not a relative percentage.
    Source rows whose percentage was text/unreadable are not scored for percentage accuracy.
    """
    issues = []
    total = passed = 0
    fund_lookup = {}
    for categories in extracted_document.values():
        if not isinstance(categories, dict):
            continue
        for funds in categories.values():
            if not isinstance(funds, list):
                continue
            for fund in funds:
                if isinstance(fund, dict):
                    fund_lookup[str(fund.get("scheme_amfi_code"))] = fund

    per_fund_month = []
    for fm in reference_dataset.get("fund_months", []):
        amfi = str(fm["scheme_amfi_code"])
        month = fm["month"].lower()
        fund = fund_lookup.get(amfi)
        actual_portfolio = (fund or {}).get(month, {}).get("portfolio", [])
        if not isinstance(actual_portfolio, list):
            actual_portfolio = []

        expected_by_isin = {}
        for h in fm.get("source_holdings", []):
            expected_by_isin.setdefault(str(h["isin"]).upper(), []).append(h)

        actual_by_isin = {}
        non_isin_output_records = []
        import re
        for h in actual_portfolio:
            isin = h.get("isin")
            normalized_isin = str(isin).strip().upper() if isin else ""
            # Only valid ISIN-shaped identifiers participate in source ISIN matching.
            # Synthetic identifiers such as CASHANDEQ or NIFTY-IO-... are evaluated separately.
            if re.fullmatch(r"IN[A-Z0-9]{10}", normalized_isin):
                actual_by_isin.setdefault(normalized_isin, []).append(h)
            else:
                non_isin_output_records.append({
                    "instrument": h.get("instrument"),
                    "identifier": isin,
                    "asset_class": h.get("asset_class"),
                    "subType": h.get("subType")
                })

        before = len(issues)
        local_total = local_passed = 0

        for isin, expected_rows in expected_by_isin.items():
            if len(expected_rows) != 1:
                issues.append(EvaluationIssue(
                    "ACC_REF_DUP_SOURCE", "HIGH", "Duplicate ISIN in golden source reference",
                    fm.get("category"), fm.get("fund_name"), amfi, month, field="isin", actual=isin
                ))
                continue
            expected = expected_rows[0]
            actual_rows = actual_by_isin.get(isin, [])

            local_total += 1
            total += 1
            if len(actual_rows) == 1:
                local_passed += 1
                passed += 1
            elif len(actual_rows) == 0:
                issues.append(EvaluationIssue(
                    "ACC_001", "HIGH", "Source holding is missing from extracted output",
                    fm.get("category"), fm.get("fund_name"), amfi, month,
                    instrument=expected.get("instrument"), field="isin",
                    expected=isin, actual=None, source_reference=expected.get("source_reference")
                ))
                continue
            else:
                issues.append(EvaluationIssue(
                    "ACC_REF_DUP_OUTPUT", "HIGH", "ISIN occurs more than once in extracted fund-month",
                    fm.get("category"), fm.get("fund_name"), amfi, month,
                    instrument=expected.get("instrument"), field="isin",
                    expected="one record", actual=len(actual_rows), source_reference=expected.get("source_reference")
                ))
                continue

            actual = actual_rows[0]

            local_total += 1
            total += 1
            if _normalize_name(expected.get("instrument")) == _normalize_name(actual.get("instrument")):
                local_passed += 1
                passed += 1
            else:
                issues.append(EvaluationIssue(
                    "ACC_002", "HIGH", "Instrument name mismatch after legal-suffix normalization",
                    fm.get("category"), fm.get("fund_name"), amfi, month,
                    instrument=expected.get("instrument"), field="instrument",
                    expected=expected.get("instrument"), actual=actual.get("instrument"),
                    source_reference=expected.get("source_reference")
                ))

            # Compare industry only when the source column contains an industry,
            # not a credit rating / sovereign label from the combined Industry / Rating column.
            if expected.get("industry_comparable"):
                local_total += 1
                total += 1
                expected_industry = expected.get("industry_or_rating")
                actual_industry = actual.get("industry")
                if _normalize_name(expected_industry) == _normalize_name(actual_industry):
                    local_passed += 1
                    passed += 1
                else:
                    issues.append(EvaluationIssue(
                        "ACC_005", "HIGH", "Industry classification differs from comparable source industry",
                        fm.get("category"), fm.get("fund_name"), amfi, month,
                        instrument=expected.get("instrument"), field="industry",
                        expected=expected_industry, actual=actual_industry,
                        source_reference=expected.get("source_reference")
                    ))

            expected_pct = expected.get("expected_percentage")
            if expected_pct is not None:
                local_total += 1
                total += 1
                actual_pct = actual.get("holdings")
                if isinstance(actual_pct, (int, float)) and abs(float(expected_pct) - float(actual_pct)) <= percentage_tolerance:
                    local_passed += 1
                    passed += 1
                else:
                    issues.append(EvaluationIssue(
                        "ACC_003", "HIGH", "Holding percentage differs from source beyond configured tolerance",
                        fm.get("category"), fm.get("fund_name"), amfi, month,
                        instrument=expected.get("instrument"), field="holdings",
                        expected=expected_pct, actual=actual_pct,
                        source_reference=expected.get("source_reference")
                    ))

        for isin, actual_rows in actual_by_isin.items():
            if isin not in expected_by_isin:
                for actual in actual_rows:
                    local_total += 1
                    total += 1
                    issues.append(EvaluationIssue(
                        "ACC_004", "HIGH", "Extracted ISIN is absent from source reference",
                        fm.get("category"), fm.get("fund_name"), amfi, month,
                        instrument=actual.get("instrument"), field="isin",
                        expected=None, actual=isin
                    ))

        local_failed = local_total - local_passed
        per_fund_month.append({
            "non_isin_output_records": non_isin_output_records,
            "amc": fm.get("amc"),
            "fund_name": fm.get("fund_name"),
            "scheme_amfi_code": amfi,
            "month": month,
            "checks": local_total,
            "passed": local_passed,
            "failed": local_failed,
            "issues": len(issues) - before
        })

    failed = total - passed
    score = round((passed / total) * 100, 4) if total else None
    status = "FAIL" if any(i.severity == "CRITICAL" for i in issues) else ("WARNING" if issues else "PASS")
    result = EvaluationResult("source_accuracy", status, score, total, passed, failed, issues)
    return {
        "result": result.to_dict(),
        "fund_month_results": per_fund_month,
        "non_isin_output_record_count": sum(len(x["non_isin_output_records"]) for x in per_fund_month),
        "non_isin_output_records": [
            {"amc": x["amc"], "fund_name": x["fund_name"], "month": x["month"], **record}
            for x in per_fund_month for record in x["non_isin_output_records"]
        ],
        "notes": [
            "Only valid ISIN-shaped identifiers are included in ISIN-based matching.",
            "Cash and derivative rows with synthetic identifiers are excluded from this ISIN accuracy score and should receive a separate name/type-based evaluator."
        ]
    }
