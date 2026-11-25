from .schema_evaluator import evaluate_schema
from .models import EvaluationResult
from .completeness_evaluator import evaluate_completeness
from .financial_evaluator import evaluate_financials
from .classification_evaluator import evaluate_classification
from .provenance_evaluator import evaluate_provenance

def run_evaluation(document, expected_months_by_amfi=None,
                   expected_classifications=None, require_provenance=False,
                   percentage_tolerance=0.5):
    """Run all evaluators supported by the current merged JSON structure."""
    results = [
        evaluate_schema(document),
        evaluate_completeness(document, expected_months_by_amfi),
        evaluate_financials(document, percentage_tolerance=percentage_tolerance),
        # Accuracy requires a trusted reference portfolio for each fund-month.
        EvaluationResult(
            "accuracy", "UNVERIFIED", None, 0, 0, 0, [],
            ["No verified golden/reference portfolio was supplied to this run."]
        ),
        evaluate_classification(document, expected_classifications),
        evaluate_provenance(document, require_provenance=require_provenance),
    ]

    all_issues = []
    for result in results:
        all_issues.extend(issue.to_dict() for issue in result.issues)

    scored = [r for r in results if r.score is not None]
    partial_score = (
        round(sum(r.score for r in scored) / len(scored), 2)
        if scored else None
    )
    # Do not label a partial score as the overall quality score while important
    # evaluators (accuracy/classification/provenance) remain unverified.
    overall_score = partial_score if len(scored) == len(results) else None

    critical_failure = any(
        issue["severity"] == "CRITICAL" for issue in all_issues
    )
    high_failure = any(
        issue["severity"] == "HIGH" for issue in all_issues
    )

    if critical_failure:
        overall_status = "FAIL"
    elif high_failure:
        overall_status = "WARNING"
    elif any(r.status == "UNVERIFIED" for r in results):
        overall_status = "UNVERIFIED"
    elif all(r.status == "PASS" for r in results):
        overall_status = "PASS"
    else:
        overall_status = "WARNING"

    return {
        "overall_status": overall_status,
        "overall_score": overall_score,
        "partial_score_available_checks_only": partial_score,
        "score_note": "Partial score covers only evaluators with measurable results; it is not a complete extraction-accuracy score.",
        "evaluator_results": [r.to_dict() for r in results],
        "issue_count": len(all_issues),
        "issues": all_issues,
    }
