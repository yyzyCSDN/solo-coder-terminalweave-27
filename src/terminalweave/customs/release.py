from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from threading import RLock

from terminalweave.core.clock import ensure_utc, utc_now
from terminalweave.core.errors import (
    ConflictError,
    NotFoundError,
    StateTransitionError,
    ValidationError,
)
from terminalweave.customs.authority import AuthorityInbox, AuthorityMessage
from terminalweave.customs.holds import CustomsLedger, HoldRecord, HoldState
from terminalweave.customs.inspection import (
    Inspection,
    InspectionScheduler,
    InspectionState,
)
from terminalweave.customs.manifest import ManifestLedger, ManifestVersion
from terminalweave.customs.release_queue import ReleaseQueue, ReleaseRequest
from terminalweave.customs.risk import CustomsRiskEngine, RiskInput, RiskResult


class ReleaseStatus(str, Enum):
    ALLOWED = 'allowed'
    BLOCKED = 'blocked'


@dataclass(frozen=True)
class RiskAssessment:
    result: RiskResult
    assessed_at: datetime
    source: str = 'risk_engine'

    def __post_init__(self) -> None:
        object.__setattr__(self, 'assessed_at', ensure_utc(self.assessed_at))


@dataclass(frozen=True)
class ReleaseBasis:
    kind: str
    status: str
    at: datetime
    details: dict[str, object] = field(default_factory=dict)
    source: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'at', ensure_utc(self.at))


@dataclass(frozen=True)
class ReleaseDecision:
    request_id: str
    container_id: str
    status: ReleaseStatus
    evaluated_at: datetime
    manifest_version: int | None
    basis: tuple[ReleaseBasis, ...]
    missing_basis: tuple[str, ...]
    blocking_reasons: tuple[str, ...]
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, 'evaluated_at', ensure_utc(self.evaluated_at))


@dataclass(frozen=True)
class ExecutedMovement:
    movement_id: str
    request_id: str
    container_id: str
    movement_type: str
    destination: str
    executed_at: datetime
    actor: str
    decision: ReleaseDecision

    def __post_init__(self) -> None:
        object.__setattr__(self, 'executed_at', ensure_utc(self.executed_at))


class UnifiedReleaseService:
    """Combine customs evidence sources into one fail-closed gate decision."""

    DEFAULT_RELEASE_MESSAGE_TYPES = frozenset({
        'release',
        'released',
        'customs_release',
        'clearance',
        'customs_clearance',
        'gate_release',
    })

    def __init__(
        self,
        holds: CustomsLedger | None = None,
        risk_engine: CustomsRiskEngine | None = None,
        inspections: InspectionScheduler | None = None,
        manifests: ManifestLedger | None = None,
        queue: ReleaseQueue | None = None,
        inbox: AuthorityInbox | None = None,
        *,
        release_message_types: set[str] | frozenset[str] | tuple[str, ...] | None = None,
        require_official_release: bool = False,
    ):
        self.holds = holds or CustomsLedger()
        self.risk_engine = risk_engine or CustomsRiskEngine()
        self.inspections = inspections or InspectionScheduler([])
        self.manifests = manifests or ManifestLedger()
        self.queue = queue or ReleaseQueue()
        self.inbox = inbox or AuthorityInbox()
        self.release_message_types = frozenset(
            x.lower() for x in (release_message_types or self.DEFAULT_RELEASE_MESSAGE_TYPES)
        )
        self.require_official_release = require_official_release
        self._risk_inputs: dict[str, RiskInput] = {}
        self._risk_input_times: dict[str, datetime] = {}
        self._risk_results: dict[str, RiskAssessment] = {}
        self._executed: dict[str, ExecutedMovement] = {}
        self._executed_by_request: dict[str, ExecutedMovement] = {}
        self._latest_decisions: dict[str, ReleaseDecision] = {}
        self._lock = RLock()

    def submit_request(self, request: ReleaseRequest) -> None:
        with self._lock:
            self.queue.submit(request)
            self._recalculate_locked(request.submitted_at)

    def submit_manifest(self, version: ManifestVersion) -> tuple[ReleaseDecision, ...]:
        with self._lock:
            self.manifests.submit(version)
            return self._recalculate_locked(version.submitted_at)

    def ingest_authority_message(self, message: AuthorityMessage) -> AuthorityMessage:
        with self._lock:
            self.inbox.ingest(message)
            if self._is_release_message(message):
                self._release_matching_holds(message)
            self._recalculate_locked(message.issued_at)
            return message

    def place_hold(self, hold: HoldRecord) -> HoldRecord:
        with self._lock:
            record = self.holds.place_hold(hold)
            self._recalculate_locked(hold.created_at)
            return record

    def release_hold(
        self,
        hold_id: str,
        at: datetime,
        actor: str,
        request_token: str,
    ) -> HoldRecord:
        at = ensure_utc(at)
        with self._lock:
            record = self.holds.release(hold_id, at, actor, request_token)
            self._recalculate_locked(at)
            return record

    def cancel_hold(self, hold_id: str, actor: str, at: datetime | None = None) -> HoldRecord:
        with self._lock:
            record = self.holds.cancel(hold_id, actor)
            self._recalculate_locked(ensure_utc(at or utc_now()))
            return record

    def request_inspection(self, inspection: Inspection) -> None:
        with self._lock:
            self.inspections.request(inspection)
            self._recalculate_locked(inspection.requested_at)

    def schedule_inspection(
        self,
        inspection_id: str,
        at: datetime,
        requires_xray: bool = False,
    ) -> Inspection:
        with self._lock:
            item = self.inspections.schedule(inspection_id, at, requires_xray)
            self._recalculate_locked(at)
            return item

    def start_inspection(self, inspection_id: str, at: datetime | None = None) -> Inspection:
        at = ensure_utc(at or utc_now())
        with self._lock:
            item = self.inspections.start(inspection_id)
            self._recalculate_locked(at)
            return item

    def finish_inspection(
        self,
        inspection_id: str,
        cleared: bool,
        at: datetime | None = None,
    ) -> Inspection:
        at = ensure_utc(at or utc_now())
        with self._lock:
            item = self.inspections.finish(inspection_id, cleared)
            self._recalculate_locked(at)
            return item

    def record_risk_input(
        self,
        container_id: str,
        risk_input: RiskInput,
        assessed_at: datetime | None = None,
    ) -> RiskAssessment:
        if not container_id.strip():
            raise ValidationError('container id required')
        with self._lock:
            result = self.risk_engine.evaluate(risk_input)
            assessed_at = ensure_utc(assessed_at or utc_now())
            assessment = RiskAssessment(result, assessed_at)
            self._risk_inputs[container_id] = risk_input
            self._risk_input_times[container_id] = assessed_at
            self._risk_results[container_id] = assessment
            self._recalculate_locked(assessment.assessed_at)
            return deepcopy(assessment)

    def record_risk_result(
        self,
        container_id: str,
        result: RiskResult,
        assessed_at: datetime | None = None,
        source: str = 'external',
    ) -> RiskAssessment:
        if not container_id.strip():
            raise ValidationError('container id required')
        assessment = RiskAssessment(result, ensure_utc(assessed_at or utc_now()), source)
        with self._lock:
            self._risk_results[container_id] = assessment
            self._recalculate_locked(assessment.assessed_at)
        return deepcopy(assessment)

    def risk_assessment(self, container_id: str) -> RiskAssessment | None:
        with self._lock:
            assessment = self._risk_results.get(container_id)
            return deepcopy(assessment) if assessment else None

    def pending_decisions(
        self,
        at: datetime | None = None,
    ) -> tuple[ReleaseDecision, ...]:
        at = ensure_utc(at or utc_now())
        with self._lock:
            return self._recalculate_locked(at)

    def recalculate_pending(
        self,
        at: datetime | None = None,
    ) -> tuple[ReleaseDecision, ...]:
        return self.pending_decisions(at)

    def _recalculate_locked(self, at: datetime | None = None) -> tuple[ReleaseDecision, ...]:
        at = ensure_utc(at or utc_now())
        decisions = tuple(
            self._evaluate_request(request, at)
            for request in self.queue.open_requests()
            if request.request_id not in self._executed_by_request
        )
        self._latest_decisions = {decision.request_id: deepcopy(decision) for decision in decisions}
        return deepcopy(decisions)

    def latest_decisions(self) -> tuple[ReleaseDecision, ...]:
        with self._lock:
            return deepcopy(tuple(self._latest_decisions.values()))

    def request_view(self, request_id: str, at: datetime | None = None) -> ReleaseDecision:
        at = ensure_utc(at or utc_now())
        with self._lock:
            executed = self._executed_by_request.get(request_id)
            if executed is not None:
                return self._copy_decision(executed.decision)
            request = self.queue.get(request_id)
            if request is None:
                raise NotFoundError('unknown release request')
            decision = self._evaluate_request(request, at)
            self._latest_decisions[request_id] = deepcopy(decision)
            return deepcopy(decision)

    def views(self, at: datetime | None = None) -> tuple[ReleaseDecision, ...]:
        return self.pending_decisions(at)

    def allowed_views(self, at: datetime | None = None) -> tuple[ReleaseDecision, ...]:
        return tuple(x for x in self.views(at) if x.status == ReleaseStatus.ALLOWED)

    def blocked_views(self, at: datetime | None = None) -> tuple[ReleaseDecision, ...]:
        return tuple(x for x in self.views(at) if x.status == ReleaseStatus.BLOCKED)

    def evaluate(self, request_id: str, at: datetime | None = None) -> ReleaseDecision:
        return self.request_view(request_id, at)

    def decision_for_request(self, request_id: str, at: datetime | None = None) -> ReleaseDecision:
        return self.request_view(request_id, at)

    def execute(
        self,
        movement_id: str,
        request_id: str,
        at: datetime | None = None,
        *,
        movement_type: str = 'gate_out',
        destination: str = 'outside_terminal',
        actor: str = 'release_service',
        mark_processed: bool = True,
    ) -> ExecutedMovement:
        return self.execute_movement(
            movement_id,
            request_id,
            at,
            movement_type=movement_type,
            destination=destination,
            actor=actor,
            mark_processed=mark_processed,
        )

    def executed_movement(self, movement_id: str) -> ExecutedMovement:
        with self._lock:
            movement = self._executed.get(movement_id)
            if movement is None:
                raise NotFoundError('unknown executed movement')
            return self._copy_movement(movement)

    def executed_movement_for_request(self, request_id: str) -> ExecutedMovement | None:
        with self._lock:
            movement = self._executed_by_request.get(request_id)
            return self._copy_movement(movement) if movement else None

    def executed_movements(self) -> tuple[ExecutedMovement, ...]:
        with self._lock:
            return tuple(self._copy_movement(x) for x in self._executed.values())

    def execute_movement(
        self,
        movement_id: str,
        request_id: str,
        at: datetime | None = None,
        *,
        movement_type: str = 'gate_out',
        destination: str = 'outside_terminal',
        actor: str = 'release_service',
        mark_processed: bool = True,
    ) -> ExecutedMovement:
        at = ensure_utc(at or utc_now())
        if not movement_id.strip():
            raise ValidationError('movement id required')
        if not destination.strip():
            raise ValidationError('destination required')
        with self._lock:
            if movement_id in self._executed:
                raise ConflictError('movement id already executed')
            request = self.queue.get(request_id)
            if request is None:
                raise NotFoundError('unknown release request')
            if request_id in self._executed_by_request:
                raise ConflictError('request already executed')
            decision = self._evaluate_request(request, at)
            if decision.status != ReleaseStatus.ALLOWED:
                raise StateTransitionError('release request is blocked')
            movement = ExecutedMovement(
                movement_id,
                request_id,
                request.container_id,
                movement_type,
                destination,
                at,
                actor,
                decision,
            )
            self._executed[movement_id] = movement
            self._executed_by_request[request_id] = movement
            if mark_processed:
                self.queue.mark_processed(request_id)
            return self._copy_movement(movement)

    def _evaluate_request(self, request: ReleaseRequest, at: datetime) -> ReleaseDecision:
        basis: list[ReleaseBasis] = []
        missing: list[str] = []
        blocking: list[str] = []
        positive: list[str] = []

        self._evaluate_queue(request, at, basis, missing, blocking, positive)
        manifest = self._evaluate_manifest(request, at, basis, missing, blocking, positive)
        assessment = self._evaluate_risk(request, at, basis, missing, blocking, positive)
        official_release = self._evaluate_authority(
            request, at, basis, missing, blocking, positive
        )
        self._evaluate_holds(request, at, basis, missing, blocking, positive)
        self._evaluate_inspections(
            request, at, assessment, official_release, basis, missing, blocking, positive
        )

        status = ReleaseStatus.ALLOWED if not missing and not blocking else ReleaseStatus.BLOCKED
        return ReleaseDecision(
            request.request_id,
            request.container_id,
            status,
            at,
            manifest.version if manifest else None,
            tuple(basis),
            tuple(missing),
            tuple(blocking),
            tuple(positive),
        )

    def _evaluate_queue(
        self,
        request: ReleaseRequest,
        at: datetime,
        basis: list[ReleaseBasis],
        missing: list[str],
        blocking: list[str],
        positive: list[str],
    ) -> None:
        executed = request.request_id in self._executed_by_request
        if not request.complete_documents:
            missing.append('complete release documents')
            blocking.append('release documents incomplete')
            status = 'incomplete_documents'
        elif self.queue.is_processed(request.request_id) and not executed:
            status = 'processed'
            positive.append('release request processed')
        else:
            status = 'executed' if executed else 'open'
            positive.append(
                'release decision retained for executed movement'
                if executed
                else 'release request open with complete documents'
            )
        basis.append(
            ReleaseBasis(
                'release_queue',
                status,
                at,
                {
                    'request_id': request.request_id,
                    'priority': request.priority,
                    'submitted_at': request.submitted_at,
                    'complete_documents': request.complete_documents,
                    'declared_risk_band': request.risk_band,
                },
            )
        )

    def _evaluate_manifest(
        self,
        request: ReleaseRequest,
        at: datetime,
        basis: list[ReleaseBasis],
        missing: list[str],
        blocking: list[str],
        positive: list[str],
    ) -> ManifestVersion | None:
        latest = self.manifests.version_at(at)
        if latest is None:
            missing.append('manifest')
            blocking.append('manifest missing')
            basis.append(ReleaseBasis('manifest', 'missing', at))
            return None
        item = next((x for x in latest.items if x.container_id == request.container_id), None)
        if item is None:
            missing.append('manifest item')
            blocking.append('container absent from manifest')
            basis.append(
                ReleaseBasis(
                    'manifest',
                    'item_missing',
                    latest.submitted_at,
                    {'version': latest.version},
                    latest.submitter,
                )
            )
            return latest
        positive.append('container listed on current manifest')
        basis.append(
            ReleaseBasis(
                'manifest',
                'present',
                latest.submitted_at,
                {
                    'version': latest.version,
                    'bill_of_lading': item.bill_of_lading,
                    'consignee': item.consignee,
                    'iso_type': item.iso_type,
                    'gross_weight_kg': item.gross_weight_kg,
                    'seal_number': item.seal_number,
                },
                latest.submitter,
            )
        )
        return latest

    def _evaluate_risk(
        self,
        request: ReleaseRequest,
        at: datetime,
        basis: list[ReleaseBasis],
        missing: list[str],
        blocking: list[str],
        positive: list[str],
    ) -> RiskAssessment | None:
        assessment = self._risk_results.get(request.container_id)
        if assessment is None:
            band = request.risk_band.lower()
            if band not in {'low', 'medium', 'high'}:
                missing.append('risk assessment')
                blocking.append('risk assessment missing')
                basis.append(ReleaseBasis('risk', 'missing', at))
                return None
            score = {'low': 0.0, 'medium': 50.0, 'high': 100.0}[band]
            assessment = RiskAssessment(
                RiskResult(score, band, ('declared in release request',)),
                request.submitted_at,
                'release_queue',
            )
        if assessment.assessed_at > at:
            missing.append('current risk assessment')
            blocking.append('risk assessment missing')
            basis.append(ReleaseBasis('risk', 'missing', at))
            return None
        if assessment.result.band == 'low' or assessment.result.band == 'medium':
            positive.append(f"{assessment.result.band} customs risk")
        basis.append(
            ReleaseBasis(
                'risk',
                assessment.result.band,
                assessment.assessed_at,
                {
                    'score': assessment.result.score,
                    'reasons': assessment.result.reasons,
                    'source': assessment.source,
                },
                assessment.source,
            )
        )
        return assessment

    def _evaluate_authority(
        self,
        request: ReleaseRequest,
        at: datetime,
        basis: list[ReleaseBasis],
        missing: list[str],
        blocking: list[str],
        positive: list[str],
    ) -> AuthorityMessage | None:
        messages = tuple(m for m in self.inbox.effective(request.container_id, at) if m.issued_at <= at)
        releases = tuple(m for m in messages if self._is_release_message(m))
        latest_release = max(releases, key=lambda x: x.issued_at, default=None)

        for message in messages:
            kind = 'official_release' if self._is_release_message(message) else 'authority_message'
            basis.append(
                ReleaseBasis(
                    kind,
                    'received',
                    message.issued_at,
                    {
                        'message_id': message.message_id,
                        'authority': message.authority,
                        'message_type': message.message_type,
                        'supersedes': message.supersedes,
                    },
                    message.authority,
                )
            )

        if latest_release is not None:
            positive.append(f"official release received from {latest_release.authority}")
        elif self.require_official_release:
            missing.append('official release')
            blocking.append('official release missing')
            basis.append(ReleaseBasis('official_release', 'missing', at))
        return latest_release

    def _evaluate_holds(
        self,
        request: ReleaseRequest,
        at: datetime,
        basis: list[ReleaseBasis],
        missing: list[str],
        blocking: list[str],
        positive: list[str],
    ) -> None:
        all_holds = [hold for hold in self.holds.holds_for_container(request.container_id) if hold.created_at <= at]
        active = [
            hold for hold in all_holds
            if hold.state == HoldState.ACTIVE or (hold.released_at and hold.released_at > at)
        ]
        if not active:
            positive.append('no active customs holds')
        for hold in all_holds:
            effective_state = (
                HoldState.ACTIVE
                if hold.state == HoldState.ACTIVE or (hold.released_at and hold.released_at > at)
                else hold.state
            )
            if effective_state == HoldState.ACTIVE:
                blocking.append(f"active hold {hold.hold_id}: {hold.reason_code}")
            basis.append(
                ReleaseBasis(
                    'hold',
                    effective_state.value,
                    hold.released_at or hold.created_at,
                    {
                        'hold_id': hold.hold_id,
                        'authority': hold.authority,
                        'reason_code': hold.reason_code,
                        'revision': hold.revision,
                        'released_by': hold.released_by,
                    },
                    hold.authority,
                )
            )
        if not all_holds:
            basis.append(ReleaseBasis('holds', 'clear', at))

    def _evaluate_inspections(
        self,
        request: ReleaseRequest,
        at: datetime,
        assessment: RiskAssessment | None,
        official_release: AuthorityMessage | None,
        basis: list[ReleaseBasis],
        missing: list[str],
        blocking: list[str],
        positive: list[str],
    ) -> None:
        inspections = [
            x for x in self.inspections.items.values()
            if x.container_id == request.container_id and x.requested_at <= at
        ]
        latest = max(inspections, key=lambda x: (x.scheduled_at or x.requested_at, x.requested_at), default=None)

        if latest is None:
            if assessment is not None and assessment.result.band == 'high' and official_release is None:
                missing.append('high-risk inspection clearance')
                blocking.append('high-risk inspection clearance missing')
                basis.append(ReleaseBasis('inspection', 'missing', at))
            else:
                positive.append('no inspection required')
                basis.append(ReleaseBasis('inspection', 'not_required', at))
            return

        if latest.state == InspectionState.CLEARED:
            positive.append(f"inspection {latest.inspection_id} cleared")
            status = InspectionState.CLEARED.value
        elif official_release is not None:
            positive.append(f"official release supersedes inspection {latest.inspection_id}")
            status = f'{latest.state.value}_released'
        else:
            blocking.append(f"inspection {latest.inspection_id} {latest.state.value}")
            status = latest.state.value

        basis.append(
            ReleaseBasis(
                'inspection',
                status,
                latest.scheduled_at or latest.requested_at,
                {
                    'inspection_id': latest.inspection_id,
                    'state': latest.state.value,
                    'bay_id': latest.bay_id,
                    'requested_at': latest.requested_at,
                    'scheduled_at': latest.scheduled_at,
                },
            )
        )

    def _is_release_message(self, message: AuthorityMessage) -> bool:
        return message.message_type.lower() in self.release_message_types

    def _release_matching_holds(self, message: AuthorityMessage) -> None:
        for hold in self.holds.active_holds(message.container_id):
            if hold.authority != message.authority or message.issued_at < hold.created_at:
                continue
            self.holds.release(
                hold.hold_id,
                message.issued_at,
                message.authority,
                f'{message.message_id}:{hold.hold_id}',
            )

    def _copy_decision(self, decision: ReleaseDecision) -> ReleaseDecision:
        return deepcopy(decision)

    def _copy_movement(self, movement: ExecutedMovement) -> ExecutedMovement:
        return deepcopy(movement)


UnifiedReleaseView = UnifiedReleaseService
CustomsReleaseService = UnifiedReleaseService
ReleaseView = ReleaseDecision
ReleaseViewDecision = ReleaseDecision
