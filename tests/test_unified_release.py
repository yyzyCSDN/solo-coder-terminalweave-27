from datetime import datetime, timedelta, timezone

import pytest

from terminalweave.customs.authority import AuthorityInbox, AuthorityMessage
from terminalweave.customs.holds import CustomsLedger, HoldRecord
from terminalweave.customs.inspection import (
    Inspection,
    InspectionBay,
    InspectionScheduler,
)
from terminalweave.customs.manifest import (
    ManifestItem,
    ManifestLedger,
    ManifestVersion,
)
from terminalweave.customs.release_queue import ReleaseQueue, ReleaseRequest
from terminalweave.customs.risk import (
    RiskAssessment,
    RiskAssessmentBook,
    RiskResult,
)
from terminalweave.customs.unified_release import (
    BasisStatus,
    BasisType,
    DecisionStatus,
    UnifiedReleaseBoard,
)
from terminalweave.core.errors import (
    NotFoundError,
    StateTransitionError,
    ValidationError,
)

UTC = timezone.utc
D = datetime(2026, 9, 28, tzinfo=UTC)


def make_item(container_id='CONT1', bol='BL1', weight=12000.0):
    return ManifestItem(container_id, bol, 'ACME', '22G1', weight)


def make_board(**kwargs):
    queue = ReleaseQueue()
    holds = CustomsLedger()
    risk = RiskAssessmentBook()
    inspections = InspectionScheduler([InspectionBay('B1', 20)])
    manifests = ManifestLedger()
    inbox = AuthorityInbox()
    board = UnifiedReleaseBoard(queue, holds, risk, inspections, manifests, inbox, **kwargs)
    return board, queue, holds, risk, inspections, manifests, inbox


def clear_all(queue, risk, manifests, inbox, container_id='CONT1', at=D, bol='BL1'):
    """Supply every satisfied basis for a low-risk container."""
    risk.record(RiskAssessment(f'A-{container_id}', container_id, at, RiskResult(2.0, 'low', ())))
    manifests.submit(ManifestVersion(manifests.latest().version + 1 if manifests.latest() else 1,
                                     at + timedelta(minutes=1), (make_item(container_id, bol),), 'agent'))
    inbox.ingest(AuthorityMessage(f'M-{container_id}', container_id, 'CBP', 'clearance',
                                  at + timedelta(minutes=2)))


# --------------------------------------------------------------- missing evidence

def test_missing_any_basis_blocks_fail_closed():
    board, queue, *_ = make_board()
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, True, 'low'))
    decision = board.latest_decision('R1')
    assert decision.status == DecisionStatus.BLOCKED.value
    assert decision.has_missing_basis
    assert set(decision.blocking_codes) == {
        'risk_assessment_missing', 'manifest_missing', 'authority_clearance_missing'}
    statuses = {b.basis: b.status for b in decision.bases}
    assert statuses[BasisType.RISK.value] == BasisStatus.MISSING.value
    assert statuses[BasisType.MANIFEST.value] == BasisStatus.MISSING.value
    assert statuses[BasisType.AUTHORITY.value] == BasisStatus.MISSING.value


def test_container_absent_from_manifest_is_missing_not_cleared():
    board, queue, holds, risk, inspections, manifests, inbox = make_board()
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, True, 'low'))
    risk.record(RiskAssessment('A1', 'CONT1', D, RiskResult(2.0, 'low', ())))
    manifests.submit(ManifestVersion(1, D, (make_item('OTHER', 'BL9'),), 'agent'))
    inbox.ingest(AuthorityMessage('M1', 'CONT1', 'CBP', 'clearance', D + timedelta(minutes=1)))
    decision = board.evaluate('R1')
    manifest = decision.basis(BasisType.MANIFEST)
    assert manifest.status == BasisStatus.MISSING.value
    assert manifest.code == 'container_not_on_manifest'
    assert not decision.releasable


def test_cannot_evaluate_unknown_request():
    board, *_ = make_board()
    with pytest.raises(NotFoundError):
        board.evaluate('NOPE')


# ------------------------------------------------------------------ releasable path

def test_all_bases_satisfied_yields_releasable_with_full_reasons():
    board, queue, *_ = make_board()
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, True, 'low'))
    clear_all(queue, board.risk_book, board.manifests, board.inbox)
    decision = board.latest_decision('R1')
    assert decision.releasable
    assert decision.blocking_codes == ()
    assert {b.basis for b in decision.bases} == {b.value for b in BasisType}
    assert all(b.satisfied for b in decision.bases)
    # full reasons include the satisfied bases, not only the blockers
    assert any('no_active_hold' in line for line in decision.reason_lines)
    assert any('authority_clearance ' in line or 'authority_clearance:' in line
               for line in decision.reason_lines)


def test_incomplete_documents_block_even_when_all_clearances_present():
    board, queue, *_ = make_board()
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, False, 'low'))
    clear_all(queue, board.risk_book, board.manifests, board.inbox)
    decision = board.latest_decision('R1')
    assert decision.blocking_codes == ('documents_incomplete',)
    assert decision.basis(BasisType.QUEUE).status == BasisStatus.BLOCKING.value


# ------------------------------------------------------------------------- holds

def test_active_hold_blocks_and_released_hold_clears():
    board, queue, holds, *_ = make_board()
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, True, 'low'))
    clear_all(queue, board.risk_book, board.manifests, board.inbox)
    holds.place_hold(HoldRecord('H1', 'CONT1', 'CBP', 'EXAM', D + timedelta(minutes=3)))
    decision = board.latest_decision('R1')
    hold_basis = decision.basis(BasisType.HOLD)
    assert hold_basis.status == BasisStatus.BLOCKING.value
    assert hold_basis.code == 'customs_hold_active'
    assert hold_basis.references == ('H1',)
    assert hold_basis.details['H1']['reason_code'] == 'EXAM'

    holds.release('H1', D + timedelta(minutes=10), 'officer', 'TOKEN-1')
    decision = board.latest_decision('R1')
    assert decision.releasable
    assert decision.basis(BasisType.HOLD).code == 'no_active_hold'


# -------------------------------------------------------------------- inspections

def test_high_risk_without_inspection_is_missing_and_cannot_leave():
    board, queue, *_ = make_board()
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, True, 'high'))
    board.risk_book.record(RiskAssessment('A1', 'CONT1', D, RiskResult(80.0, 'high', ('origin',))))
    board.manifests.submit(ManifestVersion(1, D, (make_item(),), 'agent'))
    board.inbox.ingest(AuthorityMessage('M1', 'CONT1', 'CBP', 'release', D + timedelta(minutes=1)))
    decision = board.latest_decision('R1')
    assert decision.basis(BasisType.INSPECTION).code == 'inspection_missing'
    assert decision.basis(BasisType.INSPECTION).status == BasisStatus.MISSING.value
    assert not decision.releasable


def test_inspection_lifecycle_drives_recompute_and_clears():
    board, queue, *_ = make_board()
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, True, 'high'))
    board.risk_book.record(RiskAssessment('A1', 'CONT1', D, RiskResult(80.0, 'high', ('origin',))))
    board.manifests.submit(ManifestVersion(1, D, (make_item(),), 'agent'))
    board.inbox.ingest(AuthorityMessage('M1', 'CONT1', 'CBP', 'release', D + timedelta(minutes=1)))
    assert not board.latest_decision('R1').releasable

    board.inspections.request(Inspection('I1', 'CONT1', D + timedelta(minutes=2)))
    assert board.latest_decision('R1').basis(BasisType.INSPECTION).code == 'inspection_pending'
    board.inspections.schedule('I1', D + timedelta(minutes=10))
    board.inspections.start('I1')
    assert board.latest_decision('R1').basis(BasisType.INSPECTION).code == 'inspection_pending'
    board.inspections.finish('I1', True)
    decision = board.latest_decision('R1')
    assert decision.releasable
    assert decision.basis(BasisType.INSPECTION).code == 'inspection_cleared'


def test_referred_inspection_blocks_despite_clearance_message():
    board, queue, *_ = make_board()
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, True, 'high'))
    board.risk_book.record(RiskAssessment('A1', 'CONT1', D, RiskResult(80.0, 'high', ())))
    board.manifests.submit(ManifestVersion(1, D, (make_item(),), 'agent'))
    board.inbox.ingest(AuthorityMessage('M1', 'CONT1', 'CBP', 'release', D + timedelta(minutes=1)))
    board.inspections.request(Inspection('I1', 'CONT1', D + timedelta(minutes=2)))
    board.inspections.schedule('I1', D + timedelta(minutes=3))
    board.inspections.start('I1')
    board.inspections.finish('I1', False)
    decision = board.latest_decision('R1')
    assert decision.blocking_codes == ('inspection_referred',)


def test_policy_can_require_inspection_for_medium_risk():
    board, queue, *_ = make_board(inspect_medium_risk=True)
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, True, 'medium'))
    board.risk_book.record(RiskAssessment('A1', 'CONT1', D, RiskResult(40.0, 'medium', ())))
    board.manifests.submit(ManifestVersion(1, D, (make_item(),), 'agent'))
    board.inbox.ingest(AuthorityMessage('M1', 'CONT1', 'CBP', 'clearance', D + timedelta(minutes=1)))
    assert board.latest_decision('R1').basis(BasisType.INSPECTION).code == 'inspection_missing'


def test_latest_risk_band_governs_inspection_requirement():
    board, queue, *_ = make_board()
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, True, 'low'))
    board.risk_book.record(RiskAssessment('A0', 'CONT1', D - timedelta(minutes=5), RiskResult(5.0, 'low', ())))
    board.manifests.submit(ManifestVersion(1, D, (make_item(),), 'agent'))
    board.inbox.ingest(AuthorityMessage('M1', 'CONT1', 'CBP', 'clearance', D + timedelta(minutes=1)))
    assert board.latest_decision('R1').releasable
    # re-screened high -> inspection now required
    board.risk_book.record(RiskAssessment('A1', 'CONT1', D + timedelta(minutes=5), RiskResult(90.0, 'high', ('origin',))))
    assert board.latest_decision('R1').basis(BasisType.INSPECTION).code == 'inspection_missing'


# ------------------------------------------------------------- authority messages

def test_blocking_authority_message_overrides_release_message():
    board, queue, *_ = make_board()
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, True, 'low'))
    clear_all(queue, board.risk_book, board.manifests, board.inbox)
    assert board.latest_decision('R1').releasable
    board.inbox.ingest(AuthorityMessage('M2', 'CONT1', 'CBP', 'detention', D + timedelta(minutes=9)))
    basis = board.latest_decision('R1').basis(BasisType.AUTHORITY)
    assert basis.code == 'authority_block'
    assert 'M2' in basis.references

    # a later release does not lift an outstanding detention
    board.inbox.ingest(AuthorityMessage('M3', 'CONT1', 'CBP', 'release', D + timedelta(minutes=10)))
    assert board.latest_decision('R1').basis(BasisType.AUTHORITY).code == 'authority_block'


def test_superseding_block_message_lifts_block():
    board, queue, *_ = make_board()
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, True, 'low'))
    clear_all(queue, board.risk_book, board.manifests, board.inbox, at=D - timedelta(minutes=3))
    block = AuthorityMessage('M-B', 'CONT1', 'CBP', 'detention', D + timedelta(minutes=5))
    board.inbox.ingest(block)
    assert not board.latest_decision('R1').releasable
    board.inbox.ingest(AuthorityMessage('M-R', 'CONT1', 'CBP', 'release',
                                        D + timedelta(minutes=8), supersedes='M-B'))
    assert board.latest_decision('R1').releasable


def test_required_authority_must_issue_clearance():
    board, queue, *_ = make_board(required_authority='CBP')
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, True, 'low'))
    board.risk_book.record(RiskAssessment('A1', 'CONT1', D, RiskResult(2.0, 'low', ())))
    board.manifests.submit(ManifestVersion(1, D, (make_item(),), 'agent'))
    board.inbox.ingest(AuthorityMessage('M1', 'CONT1', 'PORT_POLICE', 'release', D + timedelta(minutes=1)))
    basis = board.latest_decision('R1').basis(BasisType.AUTHORITY)
    assert basis.status == BasisStatus.MISSING.value
    assert basis.details['required_authority'] == 'CBP'
    board.inbox.ingest(AuthorityMessage('M2', 'CONT1', 'CBP', 'release', D + timedelta(minutes=2)))
    assert board.latest_decision('R1').releasable


# ------------------------------------------------------------ recompute semantics

def test_manifest_change_recomputes_only_affected_pending_requests():
    board, queue, holds, risk, inspections, manifests, inbox = make_board()
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, True, 'low'))
    queue.submit(ReleaseRequest('R2', 'CONT2', D, 5, True, 'low'))
    for cid in ('CONT1', 'CONT2'):
        risk.record(RiskAssessment(f'A-{cid}', cid, D, RiskResult(2.0, 'low', ())))
        inbox.ingest(AuthorityMessage(f'M-{cid}', cid, 'CBP', 'clearance', D + timedelta(minutes=1)))
    manifests.submit(ManifestVersion(1, D + timedelta(minutes=1),
                                     (make_item('CONT1', 'BL1'),), 'agent'))
    # CONT2 is still missing from the manifest; CONT1 already releasable
    assert board.latest_decision('R1').releasable
    r2_before = board.latest_decision('R2')
    rev2_before = r2_before.revision
    # a manifest update only touching CONT1 must not recompute CONT2
    manifests.submit(ManifestVersion(2, D + timedelta(minutes=3),
                                     (make_item('CONT1', 'BL1', 12500.0),), 'agent'))
    assert board.latest_decision('R2').revision == rev2_before
    # adding CONT2 recomputes and releases it
    manifests.submit(ManifestVersion(3, D + timedelta(minutes=4),
                                     (make_item('CONT1', 'BL1', 12500.0),
                                      make_item('CONT2', 'BL2')), 'agent'))
    assert board.latest_decision('R2').releasable


def test_recompute_skips_executed_requests():
    board, queue, *_ = make_board()
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, True, 'low'))
    clear_all(queue, board.risk_book, board.manifests, board.inbox)
    movement = board.execute('R1', movement_id='MV1', actor='dispatcher', at=D + timedelta(minutes=4))
    decisions = board.recompute_pending()
    assert decisions == ()
    # hostile updates never recompute the executed request either
    board.holds.place_hold(HoldRecord('H1', 'CONT1', 'CBP', 'X', D + timedelta(minutes=8)))
    board.recompute_pending()
    assert board.movement('R1').decision.revision == movement.decision.revision


# ------------------------------------------------------------- executed movement

def test_execute_fails_closed_when_blocked():
    board, queue, *_ = make_board()
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, True, 'low'))
    with pytest.raises(StateTransitionError):
        board.execute('R1', movement_id='MV1', actor='dispatcher')


def test_execute_freeze_keeps_decision_snapshot_and_full_reasons():
    board, queue, *_ = make_board()
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, True, 'low'))
    clear_all(queue, board.risk_book, board.manifests, board.inbox)
    movement = board.execute('R1', movement_id='MV1', actor='dispatcher',
                             at=D + timedelta(minutes=4), movement_type='rail_out')
    assert movement.movement_type == 'rail_out'
    assert queue.is_processed('R1')

    # everything that authorised the move is captured at that instant
    snapshot = movement.decision
    assert snapshot.status == DecisionStatus.RELEASABLE.value
    assert len(snapshot.bases) == len(BasisType)
    assert snapshot.basis(BasisType.MANIFEST).details['manifest_version'] == 1
    assert len(snapshot.reason_lines) == len(BasisType)

    # upstream world changes completely after the move
    board.holds.place_hold(HoldRecord('H1', 'CONT1', 'CBP', 'RECHECK', D + timedelta(minutes=10)))
    board.manifests.submit(ManifestVersion(2, D + timedelta(minutes=11), (), 'agent'))
    board.inbox.ingest(AuthorityMessage('M-BAD', 'CONT1', 'CBP', 'seizure', D + timedelta(minutes=12)))
    board.recompute_pending()

    row = board.view(container_id='CONT1')[0]
    assert row.status == DecisionStatus.EXECUTED.value
    assert row.movement_id == 'MV1'
    assert row.executed_by == 'dispatcher'
    # the frozen decision is byte-for-byte the same narrative
    assert board.movement('R1').decision == snapshot
    assert board.latest_decision('R1') == snapshot
    frozen_manifest = snapshot.basis(BasisType.MANIFEST)
    assert frozen_manifest.code == 'manifest_listed'
    assert frozen_manifest.details['bill_of_lading'] == 'BL1'
    frozen_authority = snapshot.basis(BasisType.AUTHORITY)
    assert frozen_authority.code == 'authority_clearance'
    assert 'M-CONT1' in frozen_authority.references


def test_double_execute_rejected():
    board, queue, *_ = make_board()
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, True, 'low'))
    clear_all(queue, board.risk_book, board.manifests, board.inbox)
    board.execute('R1', movement_id='MV1', actor='dispatcher', at=D + timedelta(minutes=4))
    with pytest.raises(StateTransitionError):
        board.execute('R1', movement_id='MV2', actor='dispatcher')
    with pytest.raises(ValidationError):
        # duplicate movement id even on another request
        queue.submit(ReleaseRequest('R2', 'CONT2', D, 5, True, 'low'))
        clear_all(queue, board.risk_book, board.manifests, board.inbox,
                  at=D + timedelta(minutes=2), bol='BL2')
        board.execute('R2', movement_id='MV1', actor='dispatcher', at=D + timedelta(minutes=10))


def test_execute_requires_actor_and_movement_id():
    board, queue, *_ = make_board()
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, True, 'low'))
    clear_all(queue, board.risk_book, board.manifests, board.inbox)
    with pytest.raises(ValidationError):
        board.execute('R1', movement_id='', actor='dispatcher')
    with pytest.raises(ValidationError):
        board.execute('R1', movement_id='MV1', actor='  ')


# ------------------------------------------------------------------------ views

def test_view_separates_blocked_releasable_and_executed():
    board, queue, *_ = make_board()
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, True, 'low'))
    queue.submit(ReleaseRequest('R2', 'CONT2', D, 5, True, 'low'))
    queue.submit(ReleaseRequest('R3', 'CONT3', D, 5, False, 'low'))
    for cid in ('CONT1', 'CONT2', 'CONT3'):
        board.risk_book.record(RiskAssessment(f'A-{cid}', cid, D, RiskResult(2.0, 'low', ())))
        board.inbox.ingest(AuthorityMessage(f'M-{cid}', cid, 'CBP', 'clearance', D + timedelta(minutes=1)))
    board.manifests.submit(ManifestVersion(
        1, D + timedelta(minutes=1),
        (make_item('CONT1', 'BL1'), make_item('CONT2', 'BL2'), make_item('CONT3', 'BL3')), 'agent'))
    board.execute('R1', movement_id='MV1', actor='a', at=D + timedelta(minutes=5))
    # R2 releasable pending; R3 blocked on documents
    rows = {r.request_id: r for r in board.view()}
    assert rows['R1'].status == DecisionStatus.EXECUTED.value
    assert rows['R2'].status == DecisionStatus.RELEASABLE.value
    assert rows['R3'].status == DecisionStatus.BLOCKED.value
    assert rows['R3'].blocking_codes == ('documents_incomplete',)
    assert {r.request_id for r in board.view(status=DecisionStatus.EXECUTED)} == {'R1'}
    assert {r.request_id for r in board.view(status=DecisionStatus.RELEASABLE)} == {'R2'}
    assert {r.request_id for r in board.view(status=DecisionStatus.BLOCKED)} == {'R3'}
    assert len(board.view(include_executed=False)) == 2


def test_history_is_append_only_per_request():
    board, queue, *_ = make_board()
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, True, 'low'))
    clear_all(queue, board.risk_book, board.manifests, board.inbox)
    revisions = [d.revision for d in board.history('R1')]
    assert revisions == sorted(revisions) and len(revisions) >= 2
    executed = board.execute('R1', movement_id='MV1', actor='a', at=D + timedelta(minutes=5))
    history_after = board.history('R1')
    assert history_after[-1] == executed.decision
    board.holds.place_hold(HoldRecord('H1', 'CONT1', 'CBP', 'X', D + timedelta(minutes=9)))
    board.recompute_pending()
    # history does not grow for the executed request
    assert board.history('R1') == history_after
    with pytest.raises(NotFoundError):
        board.history('UNKNOWN')


# ------------------------------------------------------------------ notifications

def test_board_emits_movement_event_and_sources_emit_events():
    board, queue, holds, risk, inspections, manifests, inbox = make_board(auto_recompute=False)
    events = []
    board.subscribe(lambda event, payload: events.append((event, payload)))
    manifests.subscribe(lambda event, payload: events.append((event, payload)))

    manifests.submit(ManifestVersion(1, D, (make_item(),), 'agent'))
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, True, 'low'))
    risk.record(RiskAssessment('A1', 'CONT1', D, RiskResult(2.0, 'low', ())))
    inbox.ingest(AuthorityMessage('M1', 'CONT1', 'CBP', 'clearance', D + timedelta(minutes=1)))
    board.evaluate('R1')
    board.execute('R1', movement_id='MV1', actor='a', at=D + timedelta(minutes=3))

    event_names = {e for e, _ in events}
    assert 'manifest_submitted' in event_names
    assert 'movement_executed' in event_names
    move_event = next(p for e, p in events if e == 'movement_executed')
    assert move_event['movement_id'] == 'MV1'
    assert move_event['container_id'] == 'CONT1'


def test_manual_board_without_autobind_stays_stale_until_recompute():
    board, queue, *_ = make_board(auto_recompute=False)
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, True, 'low'))
    # no auto evaluation on submit: nothing computed yet
    assert board.latest_decision('R1') is None
    clear_all(queue, board.risk_book, board.manifests, board.inbox)
    # still stale because no binding
    assert board.latest_decision('R1') is None
    decision = board.recompute_pending()[0]
    assert decision.releasable


def test_unified_view_and_executed_movement_are_json_serializable():
    import json
    from terminalweave.core.serialization import to_primitive

    board, queue, *_ = make_board()
    queue.submit(ReleaseRequest('R1', 'CONT1', D, 5, True, 'low'))
    clear_all(queue, board.risk_book, board.manifests, board.inbox)
    board.execute('R1', movement_id='MV1', actor='dispatcher', at=D + timedelta(minutes=5))
    payload = json.loads(json.dumps(to_primitive(board.view())))
    row = payload[0]
    assert row['status'] == DecisionStatus.EXECUTED.value
    assert row['executed_at'].endswith('Z')
    assert all(isinstance(b['details'], dict) for b in row['bases'])
