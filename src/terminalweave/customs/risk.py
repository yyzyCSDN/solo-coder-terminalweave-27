from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from terminalweave.core.clock import ensure_utc
from terminalweave.core.errors import ValidationError
from terminalweave.core.observable import Observable

@dataclass(frozen=True)
class RiskInput:
    origin_country: str
    commodity_code: str
    shipper_score: float
    weight_deviation_pct: float
    seal_mismatch: bool
    prior_exams: int

@dataclass(frozen=True)
class RiskResult:
    score: float
    band: str
    reasons: tuple[str, ...]

class CustomsRiskEngine:

    def __init__(self, high_risk_origins: set[str] | None=None):
        self.high_risk_origins = set(high_risk_origins or set())

    def evaluate(self, x: RiskInput) -> RiskResult:
        if not 0 <= x.shipper_score <= 1 or x.prior_exams < 0:
            raise ValidationError('invalid risk input')
        score = 0.0
        reasons = []
        if x.origin_country in self.high_risk_origins:
            score += 25
            reasons.append('origin')
        if x.shipper_score < 0.4:
            score += (0.4 - x.shipper_score) * 50
            reasons.append('shipper history')
        if abs(x.weight_deviation_pct) > 5:
            score += min(abs(x.weight_deviation_pct), 30)
            reasons.append('weight variance')
        if x.seal_mismatch:
            score += 35
            reasons.append('seal mismatch')
        if x.prior_exams:
            score += min(x.prior_exams * 3, 15)
            reasons.append('prior examinations')
        score = min(100.0, score)
        band = 'high' if score >= 60 else 'medium' if score >= 30 else 'low'
        return RiskResult(round(score, 2), band, tuple(reasons))


@dataclass(frozen=True)
class RiskAssessment:
    assessment_id: str
    container_id: str
    evaluated_at: datetime
    result: RiskResult

    def __post_init__(self):
        object.__setattr__(self, 'evaluated_at', ensure_utc(self.evaluated_at))


class RiskAssessmentBook(Observable):
    """Records risk outcomes per container so the release view can fail
    closed when a container has never been screened."""

    def __init__(self, engine: CustomsRiskEngine | None = None):
        super().__init__()
        self.engine = engine
        self._assessments: dict[str, list[RiskAssessment]] = {}

    def record(self, assessment: RiskAssessment) -> RiskAssessment:
        prior = self.latest(assessment.container_id)
        if prior and assessment.evaluated_at < prior.evaluated_at:
            raise ValidationError('risk assessment must be newer than the current one')
        self._assessments.setdefault(assessment.container_id, []).append(assessment)
        self.notify('risk_assessed', {'container_id': assessment.container_id, 'assessment_id': assessment.assessment_id, 'band': assessment.result.band, 'score': assessment.result.score})
        return assessment

    def evaluate(self, assessment_id: str, container_id: str, x: RiskInput, at: datetime) -> RiskAssessment:
        if self.engine is None:
            raise ValidationError('risk book has no engine configured')
        return self.record(RiskAssessment(assessment_id, container_id, at, self.engine.evaluate(x)))

    def latest(self, container_id: str) -> RiskAssessment | None:
        values = self._assessments.get(container_id)
        if not values:
            return None
        return max(values, key=lambda a: (a.evaluated_at, a.assessment_id))
