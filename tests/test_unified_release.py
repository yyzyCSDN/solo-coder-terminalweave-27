from datetime import datetime, timezone

import pytest

from terminalweave.core.errors import StateTransitionError
from terminalweave.customs.authority import AuthorityMessage
from terminalweave.customs.holds import HoldRecord, HoldState
from terminalweave.customs.inspection import (
    Inspection,
    InspectionBay,
    InspectionScheduler,
    InspectionState,
)
from terminalweave.customs.manifest import ManifestItem, ManifestLedger, ManifestVersion
from terminalweave.customs.release import (
    ExecutedMovement,
    ReleaseStatus,
    RiskAssessment,
    UnifiedReleaseService,
)
from terminalweave.customs.release_queue import ReleaseQueue, ReleaseRequest
from terminalweave.customs.risk import RiskInput, RiskResult


UTC = timezone.utc
D = datetime(2026, 2, 1, tzinfo=UTC)


def item(container='C1', weight=10000, seal='S1'):
    return ManifestItem(container, 'BOL1', 'ACME', '20GP', weight, seal)


def manifest(version=1, at=D, items=None):
    return ManifestVersion(version, at, tuple(items or [item()]), 'agent')


def request(rid='R1', container='C1', complete=True, band='low', at=D):
    return ReleaseRequest(rid, container, at, 10, complete, band)


def low_risk_input(seal_mismatch=False):
    return RiskInput('CA', '0001', 0.9, 0.0, seal_mismatch, 0)


def high_risk_input():
    return RiskInput('XX', '0001', 0.1, 20.0, True, 2)


def test_unified_view_blocks_until_every_required_basis_exists():
    service = UnifiedReleaseService(queue=ReleaseQueue())
    service.submit_request(request())

    decision = service.request_view('R1', D)

    assert decision.status == ReleaseStatus.BLOCKED
    assert decision.missing_basis == ('manifest',)
    assert 'manifest missing' in decision.blocking_reasons
    assert 'risk assessment missing' not in decision.blocking_reasons
    assert len(decision.basis) >= 3
    with pytest.raises(StateTransitionError):
        service.execute_movement('M1', 'R1', D)


def test_manifest_and_low_risk_basis_produce_allowed_view():
    service = UnifiedReleaseService()
    service.submit_request(request())
    service.submit_manifest(manifest())
    service.record_risk_input('C1', low_risk_input(), D)

    decision = service.request_view('R1', D)

    assert decision.status == ReleaseStatus.ALLOWED
    assert decision.manifest_version == 1
    assert decision.missing_basis == ()
    assert decision.blocking_reasons == ()
    assert 'container listed on current manifest' in decision.reasons
    assert 'low customs risk' in decision.reasons
    assert 'no active customs holds' in decision.reasons


def test_official_release_recalculates_open_request_and_does_not_change_execution():
    queue = ReleaseQueue()
    manifests = ManifestLedger()
    service = UnifiedReleaseService(queue=queue, manifests=manifests)
    service.submit_request(request())
    service.submit_manifest(manifest())
    service.record_risk_input('C1', high_risk_input(), D)
    service.place_hold(HoldRecord('H1', 'C1', 'CBP', 'EXAM', D))
    blocked = service.request_view('R1', D)
    assert blocked.status == ReleaseStatus.BLOCKED
    assert 'high-risk inspection clearance missing' in blocked.blocking_reasons

    service.ingest_authority_message(
        AuthorityMessage('A1', 'C1', 'CBP', 'customs_release', D)
    )
    recalculated = service.latest_decisions()[0]

    assert recalculated.status == ReleaseStatus.ALLOWED
    released_hold = service.holds.holds_for_container('C1')[0]
    assert released_hold.state == HoldState.RELEASED
    assert released_hold.released_by == 'CBP'
    movement = service.execute_movement('M1', 'R1', D, destination='GATE')

    service.submit_manifest(
        manifest(
            version=2,
            at=datetime(2026, 2, 2, tzinfo=UTC),
            items=[item(seal='S2')],
        )
    )
    service.ingest_authority_message(
        AuthorityMessage(
            'A2',
            'C1',
            'CBP',
            'hold',
            datetime(2026, 2, 2, tzinfo=UTC),
        )
    )

    stored = service.executed_movement('M1')
    assert stored == movement
    assert stored.decision.manifest_version == 1
    assert stored.decision.status == ReleaseStatus.ALLOWED
    assert any(x.kind == 'official_release' for x in stored.decision.basis)
    assert service.pending_decisions(datetime(2026, 2, 2, tzinfo=UTC)) == ()


def test_manifest_update_recalculates_remaining_open_requests():
    service = UnifiedReleaseService()
    service.submit_request(request('R1', 'C1'))
    service.submit_request(request('R2', 'C2'))
    service.submit_manifest(manifest(items=[item('C1')]))
    service.record_risk_input('C1', low_risk_input(), D)
    service.record_risk_input('C2', low_risk_input(), D)

    decisions = service.recalculate_pending(D)
    by_request = {x.request_id: x for x in decisions}
    assert by_request['R1'].status == ReleaseStatus.ALLOWED
    assert by_request['R2'].status == ReleaseStatus.BLOCKED
    assert 'container absent from manifest' in by_request['R2'].blocking_reasons

    updated_at = datetime(2026, 2, 1, 0, 0, 1, tzinfo=UTC)
    service.submit_manifest(
        manifest(
            version=2,
            at=updated_at,
            items=[item('C1'), item('C2')],
        )
    )
    decisions = service.latest_decisions()
    by_request = {x.request_id: x for x in decisions}
    assert by_request['R1'].manifest_version == 2
    assert by_request['R2'].manifest_version == 2
    assert by_request['R2'].status == ReleaseStatus.ALLOWED


def test_high_risk_container_requires_cleared_inspection_without_official_release():
    service = UnifiedReleaseService(
        inspections=InspectionScheduler([InspectionBay('B1', 10)])
    )
    service.submit_request(request(band='high'))
    service.submit_manifest(manifest())
    service.record_risk_result('C1', RiskResult(80.0, 'high', ('seal mismatch',)), D)
    service.request_inspection(Inspection('I1', 'C1', D))

    pending = service.request_view('R1', D)
    assert pending.status == ReleaseStatus.BLOCKED
    assert 'inspection I1 requested' in pending.blocking_reasons

    service.schedule_inspection('I1', D)
    service.start_inspection('I1')
    service.finish_inspection('I1', True)

    cleared = service.request_view('R1', D)
    assert cleared.status == ReleaseStatus.ALLOWED
    assert service.inspections.items['I1'].state == InspectionState.CLEARED


def test_executed_movement_preserves_complete_reason_snapshot():
    service = UnifiedReleaseService()
    service.submit_request(request())
    service.submit_manifest(manifest())
    service.record_risk_input('C1', low_risk_input(), D)
    service.execute_movement('M1', 'R1', D)

    service.record_risk_result(
        'C1',
        RiskResult(90.0, 'high', ('changed information',)),
        datetime(2026, 2, 2, tzinfo=UTC),
    )
    service.place_hold(
        HoldRecord(
            'H2',
            'C1',
            'CBP',
            'LATE_HOLD',
            datetime(2026, 2, 2, tzinfo=UTC),
        )
    )

    movement: ExecutedMovement = service.executed_movement_for_request('R1')
    assert movement.decision.evaluated_at == D
    assert movement.decision.blocking_reasons == ()
    risk_basis = next(x for x in movement.decision.basis if x.kind == 'risk')
    assert risk_basis.status == 'low'
    assert risk_basis.details['score'] == 0.0
    assert not any(x.kind == 'hold' for x in movement.decision.basis)
