"""Unified customs release view.

The five customs work queues each answer their own question: the release
queue carries the move request, the hold ledger records customs freezes,
the risk book stores screening outcomes, the inspection scheduler tracks
examinations, the manifest ledger lists cargo, and the authority inbox
carries official messages.  None of them alone says whether a container
may actually leave the terminal.

``UnifiedReleaseBoard`` evaluates every basis together.  The rule is
deliberately strict: a basis that is satisfied clears, a basis that is
actively negative blocks, and a basis for which no evidence exists blocks
as *missing* (fail closed).  Pending requests are re-evaluated whenever an
official message arrives or the manifest changes (hold, risk and
inspection updates trigger the same recomputation).  An executed move is
frozen with the exact decision snapshot and the complete per-basis
reasons that authorised it; later upstream changes never rewrite it.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from terminalweave.core.clock import ensure_utc, utc_now, iso_z
from terminalweave.core.errors import ValidationError, StateTransitionError, NotFoundError
from terminalweave.core.observable import Observable
from terminalweave.customs.holds import CustomsLedger
from terminalweave.customs.inspection import InspectionScheduler, InspectionState
from terminalweave.customs.manifest import ManifestLedger
from terminalweave.customs.release_queue import ReleaseQueue
from terminalweave.customs.risk import RiskAssessmentBook
from terminalweave.customs.authority import AuthorityInbox


class BasisType(str, Enum):
    QUEUE = 'queue'
    HOLD = 'hold'
    RISK = 'risk'
    INSPECTION = 'inspection'
    MANIFEST = 'manifest'
    AUTHORITY = 'authority'


class BasisStatus(str, Enum):
    SATISFIED = 'satisfied'
    BLOCKING = 'blocking'
    MISSING = 'missing'


class DecisionStatus(str, Enum):
    BLOCKED = 'blocked'
    RELEASABLE = 'releasable'
    EXECUTED = 'executed'


#: message types that amount to an official stop order
DEFAULT_BLOCKING_MESSAGE_TYPES = frozenset({
    'hold', 'detention', 'seizure', 'do_not_load', 'inspection_order',
})

#: message types that amount to an official release / clearance
DEFAULT_RELEASE_MESSAGE_TYPES = frozenset({
    'release', 'clearance', 'release_order',
})


@dataclass(frozen=True)
class BasisEvidence:
    """The result of checking one release basis."""
    basis: str
    status: str
    code: str
    summary: str
    references: tuple[str, ...] = ()
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.details, MappingProxyType):
            object.__setattr__(self, 'details', MappingProxyType(dict(self.details)))

    @property
    def satisfied(self) -> bool:
        return self.status == BasisStatus.SATISFIED.value


@dataclass(frozen=True)
class ReleaseDecision:
    request_id: str
    container_id: str
    revision: int
    status: str
    bases: tuple[BasisEvidence, ...]
    blocking_codes: tuple[str, ...]
    has_missing_basis: bool
    computed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, 'computed_at', ensure_utc(self.computed_at))

    @property
    def releasable(self) -> bool:
        return self.status == DecisionStatus.RELEASABLE.value

    def basis(self, basis: BasisType | str) -> BasisEvidence:
        key = basis.value if isinstance(basis, BasisType) else str(basis)
        return next(b for b in self.bases if b.basis == key)

    @property
    def reason_lines(self) -> tuple[str, ...]:
        """Full audit narrative, including every satisfied basis."""
        return tuple(f'[{b.basis}] {b.status}: {b.code} - {b.summary}' for b in self.bases)


@dataclass(frozen=True)
class ExecutedMovement:
    movement_id: str
    request_id: str
    container_id: str
    movement_type: str
    actor: str
    executed_at: datetime
    decision: ReleaseDecision

    def __post_init__(self) -> None:
        object.__setattr__(self, 'executed_at', ensure_utc(self.executed_at))


@dataclass(frozen=True)
class ReleaseViewRow:
    request_id: str
    container_id: str
    priority: int
    submitted_at: datetime
    status: str
    has_missing_basis: bool
    blocking_codes: tuple[str, ...]
    bases: tuple[BasisEvidence, ...]
    reason_lines: tuple[str, ...]
    decision_revision: int
    computed_at: datetime
    movement_id: str | None = None
    movement_type: str | None = None
    executed_at: datetime | None = None
    executed_by: str | None = None


class UnifiedReleaseBoard(Observable):

    def __init__(self,
                 queue: ReleaseQueue,
                 holds: CustomsLedger,
                 risk_book: RiskAssessmentBook,
                 inspections: InspectionScheduler,
                 manifests: ManifestLedger,
                 inbox: AuthorityInbox,
                 *,
                 movement_type: str = 'gate_out',
                 required_authority: str | None = None,
                 release_message_types: frozenset[str] = DEFAULT_RELEASE_MESSAGE_TYPES,
                 blocking_message_types: frozenset[str] = DEFAULT_BLOCKING_MESSAGE_TYPES,
                 inspect_high_risk: bool = True,
                 inspect_medium_risk: bool = False,
                 inspect_low_risk: bool = False,
                 auto_recompute: bool = True):
        super().__init__()
        self.queue = queue
        self.holds = holds
        self.risk_book = risk_book
        self.inspections = inspections
        self.manifests = manifests
        self.inbox = inbox
        self.movement_type = movement_type
        self.required_authority = required_authority
        self.release_message_types = frozenset(release_message_types)
        self.blocking_message_types = frozenset(blocking_message_types)
        self._inspect_bands = {
            'high': inspect_high_risk,
            'medium': inspect_medium_risk,
            'low': inspect_low_risk,
        }
        self._revisions: dict[str, int] = {}
        self._history: dict[str, list[ReleaseDecision]] = {}
        self._movements: dict[str, ExecutedMovement] = {}
        if auto_recompute:
            self._bind()

    # ------------------------------------------------------------------ wiring

    def _bind(self) -> None:
        self.queue.subscribe(self._on_request_submitted)
        if hasattr(self.holds, 'subscribe'):
            self.holds.subscribe(self._on_container_event)
        if hasattr(self.risk_book, 'subscribe'):
            self.risk_book.subscribe(self._on_container_event)
        if hasattr(self.inspections, 'subscribe'):
            self.inspections.subscribe(self._on_container_event)
        if hasattr(self.inbox, 'subscribe'):
            self.inbox.subscribe(self._on_container_event)
        if hasattr(self.manifests, 'subscribe'):
            self.manifests.subscribe(self._on_manifest_event)

    def _on_request_submitted(self, event: str, payload: object) -> None:
        self.evaluate(payload['request_id'])

    def _on_container_event(self, event: str, payload: object) -> None:
        self.recompute_pending({payload['container_id']})

    def _on_manifest_event(self, event: str, payload: object) -> None:
        affected = set(payload['added']) | set(payload['removed']) | set(payload['changed'])
        if affected:
            self.recompute_pending(affected)

    # -------------------------------------------------------------- evaluation

    def evaluate(self, request_id: str, at: datetime | None = None) -> ReleaseDecision:
        request = self.queue.get(request_id)
        if request is None:
            raise NotFoundError(f'unknown release request {request_id}')
        if request_id in self._movements:
            return self._movements[request_id].decision
        at = ensure_utc(at) if at else utc_now()
        bases = (
            self._check_queue(request),
            self._check_holds(request.container_id),
            self._check_risk(request),
            self._check_inspection(request),
            self._check_manifest(request.container_id),
            self._check_authority(request.container_id),
        )
        blocking = tuple(b.code for b in bases if not b.satisfied)
        missing = any(b.status == BasisStatus.MISSING.value for b in bases)
        status = DecisionStatus.RELEASABLE.value if not blocking else DecisionStatus.BLOCKED.value
        revision = self._revisions.get(request_id, 0) + 1
        self._revisions[request_id] = revision
        decision = ReleaseDecision(request_id, request.container_id, revision, status, bases, blocking, missing, at)
        self._history.setdefault(request_id, []).append(decision)
        return decision

    def recompute_pending(self, containers: str | set[str] | None = None) -> tuple[ReleaseDecision, ...]:
        """Re-evaluate every request still awaiting a move.

        Executed requests are never recomputed: their decision snapshot is
        permanent.  When ``containers`` is given only those containers are
        refreshed.
        """
        if containers is None:
            scope = None
        elif isinstance(containers, str):
            scope = {containers}
        else:
            scope = set(containers)
        out = []
        for request in self.queue.all():
            if request.request_id in self._movements:
                continue
            if scope is not None and request.container_id not in scope:
                continue
            out.append(self.evaluate(request.request_id))
        return tuple(out)

    def _check_queue(self, request) -> BasisEvidence:
        if request.complete_documents:
            return BasisEvidence(BasisType.QUEUE.value, BasisStatus.SATISFIED.value,
                                 'documents_complete', 'release request documents complete',
                                 (request.request_id,),
                                 {'priority': request.priority, 'declared_risk_band': request.risk_band})
        return BasisEvidence(BasisType.QUEUE.value, BasisStatus.BLOCKING.value,
                             'documents_incomplete', 'release request documents incomplete',
                             (request.request_id,), {'priority': request.priority})

    def _check_holds(self, container_id: str) -> BasisEvidence:
        active = self.holds.active_holds(container_id)
        if not active:
            return BasisEvidence(BasisType.HOLD.value, BasisStatus.SATISFIED.value,
                                 'no_active_hold', 'no active customs freeze')
        refs = tuple(sorted(h.hold_id for h in active))
        detail = {h.hold_id: {'authority': h.authority, 'reason_code': h.reason_code,
                              'placed_at': iso_z(h.created_at), 'revision': h.revision}
                  for h in active}
        return BasisEvidence(BasisType.HOLD.value, BasisStatus.BLOCKING.value,
                             'customs_hold_active', f'{len(active)} active customs freeze(s)',
                             refs, detail)

    def _check_risk(self, request) -> BasisEvidence:
        assessment = self.risk_book.latest(request.container_id)
        if assessment is None:
            return BasisEvidence(BasisType.RISK.value, BasisStatus.MISSING.value,
                                 'risk_assessment_missing', 'container has never been risk screened')
        result = assessment.result
        return BasisEvidence(BasisType.RISK.value, BasisStatus.SATISFIED.value,
                             'risk_screened', f'risk band {result.band} ({result.score})',
                             (assessment.assessment_id,),
                             {'band': result.band, 'score': result.score,
                              'reasons': result.reasons, 'evaluated_at': iso_z(assessment.evaluated_at)})

    def _current_risk_band(self, request) -> str:
        assessment = self.risk_book.latest(request.container_id)
        return assessment.result.band if assessment else request.risk_band

    def _check_inspection(self, request) -> BasisEvidence:
        band = self._current_risk_band(request)
        required = self._inspect_bands.get(band, True)
        inspection = self.inspections.latest_for(request.container_id)
        if inspection is None:
            if required:
                return BasisEvidence(BasisType.INSPECTION.value, BasisStatus.MISSING.value,
                                     'inspection_missing', f'{band} risk requires an inspection, none on record',
                                     (), {'risk_band': band, 'inspection_required': True})
            return BasisEvidence(BasisType.INSPECTION.value, BasisStatus.SATISFIED.value,
                                 'inspection_not_required', f'{band} risk does not require inspection',
                                 (), {'risk_band': band, 'inspection_required': False})
        refs = (inspection.inspection_id,)
        detail = {'risk_band': band, 'inspection_required': required, 'state': inspection.state.value}
        if inspection.state == InspectionState.CLEARED:
            return BasisEvidence(BasisType.INSPECTION.value, BasisStatus.SATISFIED.value,
                                 'inspection_cleared', 'customs inspection cleared',
                                 refs, detail)
        if inspection.state == InspectionState.REFERRED:
            return BasisEvidence(BasisType.INSPECTION.value, BasisStatus.BLOCKING.value,
                                 'inspection_referred', 'customs inspection referred, not cleared',
                                 refs, detail)
        code = 'inspection_pending' if required else 'inspection_open'
        summary = f'inspection {inspection.state.value}, no clearance yet'
        return BasisEvidence(BasisType.INSPECTION.value, BasisStatus.BLOCKING.value,
                             code, summary, refs, detail)

    def _check_manifest(self, container_id: str) -> BasisEvidence:
        version = self.manifests.latest()
        if version is None:
            return BasisEvidence(BasisType.MANIFEST.value, BasisStatus.MISSING.value,
                                 'manifest_missing', 'no manifest has been submitted')
        item = next((x for x in version.items if x.container_id == container_id), None)
        if item is None:
            return BasisEvidence(BasisType.MANIFEST.value, BasisStatus.MISSING.value,
                                 'container_not_on_manifest', 'container absent from latest manifest',
                                 (), {'manifest_version': version.version})
        return BasisEvidence(BasisType.MANIFEST.value, BasisStatus.SATISFIED.value,
                             'manifest_listed', 'container listed on latest manifest',
                             (), {'manifest_version': version.version,
                                  'bill_of_lading': item.bill_of_lading,
                                  'consignee': item.consignee,
                                  'gross_weight_kg': item.gross_weight_kg})

    def _check_authority(self, container_id: str) -> BasisEvidence:
        messages = self.inbox.effective(container_id)
        if not messages:
            return BasisEvidence(BasisType.AUTHORITY.value, BasisStatus.MISSING.value,
                                 'authority_clearance_missing', 'no effective official message on record')
        blocks = [m for m in messages if m.message_type in self.blocking_message_types]
        if blocks:
            refs = tuple(m.message_id for m in blocks)
            detail = {m.message_id: {'authority': m.authority, 'type': m.message_type,
                                     'issued_at': iso_z(m.issued_at)} for m in blocks}
            return BasisEvidence(BasisType.AUTHORITY.value, BasisStatus.BLOCKING.value,
                                 'authority_block', f'{len(blocks)} official blocking message(s)',
                                 refs, detail)
        releases = [m for m in messages if m.message_type in self.release_message_types]
        if self.required_authority is not None:
            releases = [m for m in releases if m.authority == self.required_authority]
        if not releases:
            detail: dict[str, object] = {'effective_types': tuple(sorted({m.message_type for m in messages}))}
            if self.required_authority is not None:
                detail['required_authority'] = self.required_authority
            return BasisEvidence(BasisType.AUTHORITY.value, BasisStatus.MISSING.value,
                                 'authority_clearance_missing', 'no official release or clearance message',
                                 (), detail)
        latest_release = max(releases, key=lambda m: m.issued_at)
        return BasisEvidence(BasisType.AUTHORITY.value, BasisStatus.SATISFIED.value,
                             'authority_clearance', f'official {latest_release.message_type} from {latest_release.authority}',
                             tuple(m.message_id for m in releases),
                             {'authority': latest_release.authority, 'type': latest_release.message_type,
                              'issued_at': iso_z(latest_release.issued_at)})

    # ----------------------------------------------------------------- movement

    def execute(self, request_id: str, *, movement_id: str, actor: str,
                at: datetime | None = None, movement_type: str | None = None) -> ExecutedMovement:
        if not actor or not actor.strip():
            raise ValidationError('actor required')
        if not movement_id or not movement_id.strip():
            raise ValidationError('movement id required')
        if request_id in self._movements:
            raise StateTransitionError(f'movement already executed for request {request_id}')
        if any(m.movement_id == movement_id for m in self._movements.values()):
            raise ValidationError('duplicate movement id')
        at = ensure_utc(at) if at else utc_now()
        decision = self.evaluate(request_id, at)
        if not decision.releasable:
            raise StateTransitionError(f'request {request_id} still blocked: {", ".join(decision.blocking_codes)}')
        movement = ExecutedMovement(movement_id, request_id, decision.container_id,
                                    movement_type or self.movement_type, actor, at, decision)
        self._movements[request_id] = movement
        self.queue.mark_processed(request_id)
        self.notify('movement_executed', {'request_id': request_id, 'container_id': decision.container_id,
                                          'movement_id': movement_id, 'executed_at': iso_z(at),
                                          'decision_revision': decision.revision})
        return movement

    def is_executed(self, request_id: str) -> bool:
        return request_id in self._movements

    def movement(self, request_id: str) -> ExecutedMovement | None:
        return self._movements.get(request_id)

    def movements(self) -> tuple[ExecutedMovement, ...]:
        return tuple(sorted(self._movements.values(), key=lambda m: (m.executed_at, m.movement_id)))

    # -------------------------------------------------------------------- views

    def latest_decision(self, request_id: str) -> ReleaseDecision | None:
        if request_id in self._movements:
            return self._movements[request_id].decision
        history = self._history.get(request_id)
        return history[-1] if history else None

    def history(self, request_id: str) -> tuple[ReleaseDecision, ...]:
        request = self.queue.get(request_id)
        if request is None:
            raise NotFoundError(f'unknown release request {request_id}')
        return tuple(self._history.get(request_id, ()))

    def view(self, *, status: DecisionStatus | str | None = None,
             container_id: str | None = None,
             include_executed: bool = True) -> tuple[ReleaseViewRow, ...]:
        wanted = status.value if isinstance(status, DecisionStatus) else status
        rows = []
        for request in self.queue.all():
            if container_id is not None and request.container_id != container_id:
                continue
            movement = self._movements.get(request.request_id)
            decision = movement.decision if movement else self.latest_decision(request.request_id)
            if decision is None:
                decision = self.evaluate(request.request_id)
            row_status = DecisionStatus.EXECUTED.value if movement else decision.status
            if wanted is not None and row_status != wanted:
                continue
            if movement and not include_executed:
                continue
            rows.append(ReleaseViewRow(
                request_id=request.request_id,
                container_id=request.container_id,
                priority=request.priority,
                submitted_at=request.submitted_at,
                status=row_status,
                has_missing_basis=decision.has_missing_basis,
                blocking_codes=decision.blocking_codes,
                bases=decision.bases,
                reason_lines=decision.reason_lines,
                decision_revision=decision.revision,
                computed_at=decision.computed_at,
                movement_id=movement.movement_id if movement else None,
                movement_type=movement.movement_type if movement else None,
                executed_at=movement.executed_at if movement else None,
                executed_by=movement.actor if movement else None,
            ))
        return tuple(sorted(rows, key=lambda r: (r.submitted_at, r.request_id)))
