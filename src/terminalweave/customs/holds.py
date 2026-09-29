from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from terminalweave.core.clock import ensure_utc
from terminalweave.core.errors import ValidationError, StateTransitionError
from terminalweave.core.observable import Observable

class HoldState(str, Enum):
    ACTIVE = 'active'
    RELEASED = 'released'
    CANCELLED = 'cancelled'

@dataclass
class HoldRecord:
    hold_id: str
    container_id: str
    authority: str
    reason_code: str
    created_at: datetime
    state: HoldState = HoldState.ACTIVE
    released_at: datetime | None = None
    released_by: str | None = None
    revision: int = 1
    history: list[tuple[int, str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.created_at = ensure_utc(self.created_at)
        self.history.append((self.revision, self.state.value, 'created'))

class CustomsLedger(Observable):

    def __init__(self):
        super().__init__()
        self._holds: dict[str, HoldRecord] = {}
        self._release_tokens: dict[str, tuple[str, int]] = {}

    def place_hold(self, record: HoldRecord) -> HoldRecord:
        if record.hold_id in self._holds:
            existing = self._holds[record.hold_id]
            if (existing.container_id, existing.authority, existing.reason_code) == (record.container_id, record.authority, record.reason_code):
                return existing
            raise ValidationError('hold id collision')
        self._holds[record.hold_id] = record
        self.notify('hold_placed', {'container_id': record.container_id, 'hold_id': record.hold_id, 'authority': record.authority, 'reason_code': record.reason_code})
        return record

    def active_holds(self, container_id: str) -> tuple[HoldRecord, ...]:
        return tuple((h for h in self._holds.values() if h.container_id == container_id and h.state == HoldState.ACTIVE))

    def is_blocked(self, container_id: str) -> bool:
        return bool(self.active_holds(container_id))

    def release(self, hold_id: str, at: datetime, actor: str, request_token: str) -> HoldRecord:
        at = ensure_utc(at)
        if not request_token.strip():
            raise ValidationError('release request token is required')
        if request_token in self._release_tokens:
            prior_hold, prior_revision = self._release_tokens[request_token]
            if prior_hold != hold_id:
                raise ValidationError('release token already used for another hold')
            return self._holds[hold_id]
        record = self._holds[hold_id]
        if record.state == HoldState.RELEASED:
            raise StateTransitionError('hold already released with a different request')
        if record.state != HoldState.ACTIVE:
            raise StateTransitionError('only active holds can be released')
        if at < record.created_at:
            raise ValidationError('release cannot precede hold creation')
        record.state = HoldState.RELEASED
        record.released_at = at
        record.released_by = actor
        record.revision += 1
        record.history.append((record.revision, record.state.value, actor))
        self._release_tokens[request_token] = (hold_id, record.revision)
        self.notify('hold_released', {'container_id': record.container_id, 'hold_id': hold_id, 'released_by': actor, 'revision': record.revision})
        return record

    def cancel(self, hold_id: str, actor: str) -> HoldRecord:
        record = self._holds[hold_id]
        if record.state != HoldState.ACTIVE:
            raise StateTransitionError('only active holds can be cancelled')
        record.state = HoldState.CANCELLED
        record.revision += 1
        record.history.append((record.revision, record.state.value, actor))
        self.notify('hold_cancelled', {'container_id': record.container_id, 'hold_id': hold_id, 'cancelled_by': actor, 'revision': record.revision})
        return record

    def movement_allowed(self, container_id: str, movement: str) -> bool:
        active = self.active_holds(container_id)
        if not active:
            return True
        movement = movement.lower()
        if movement in {'yard_rehandle', 'inspection_move'}:
            return True
        return False
