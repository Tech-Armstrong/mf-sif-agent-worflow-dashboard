from dataclasses import dataclass, field, asdict
from typing import Any

@dataclass
class EvaluationIssue:
    rule_id: str
    severity: str
    message: str
    category: str | None = None
    fund_name: str | None = None
    amfi_code: str | None = None
    month: str | None = None
    instrument: str | None = None
    field: str | None = None
    expected: Any = None
    actual: Any = None
    source_reference: dict | None = None

    def to_dict(self):
        return asdict(self)

@dataclass
class EvaluationResult:
    evaluator: str
    status: str
    score: float | None
    total_checks: int
    passed_checks: int
    failed_checks: int
    issues: list[EvaluationIssue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self):
        result = asdict(self)
        result["issues"] = [x.to_dict() if hasattr(x, "to_dict") else x for x in self.issues]
        return result
