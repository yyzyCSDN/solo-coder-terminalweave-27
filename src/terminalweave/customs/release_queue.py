from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from terminalweave.core.clock import ensure_utc
from terminalweave.core.errors import ValidationError
from terminalweave.core.observable import Observable

@dataclass(frozen=True)
class ReleaseRequest:
    request_id: str
    container_id: str
    submitted_at: datetime
    priority: int
    complete_documents: bool
    risk_band: str

    def __post_init__(self):
        object.__setattr__(self, 'submitted_at', ensure_utc(self.submitted_at))

class ReleaseQueue(Observable):

    def __init__(self):
        super().__init__()
        self._requests: dict[str, ReleaseRequest] = {}
        self._processed: set[str] = set()

    def submit(self, r: ReleaseRequest) -> None:
        if r.request_id in self._requests:
            raise ValidationError('duplicate release request')
        self._requests[r.request_id] = r
        self.notify('release_request_submitted', {'request_id': r.request_id, 'container_id': r.container_id})

    def get(self, request_id: str) -> ReleaseRequest | None:
        return self._requests.get(request_id)

    def all(self) -> tuple[ReleaseRequest, ...]:
        return tuple(sorted(self._requests.values(), key=lambda r: (r.submitted_at, r.request_id)))

    def is_processed(self, request_id: str) -> bool:
        return request_id in self._processed

    def ready(self) -> list[ReleaseRequest]:
        band = {'low': 0, 'medium': 1, 'high': 2}
        values = [r for r in self._requests.values() if r.request_id not in self._processed and r.complete_documents]
        return sorted(values, key=lambda r: (band.get(r.risk_band, 9), -r.priority, r.submitted_at, r.request_id))

    def next(self) -> ReleaseRequest | None:
        values = self.ready()
        return values[0] if values else None

    def mark_processed(self, request_id: str) -> None:
        if request_id not in self._requests:
            raise ValidationError('unknown request')
        if request_id not in self._processed:
            self._processed.add(request_id)
            self.notify('release_request_processed', {'request_id': request_id, 'container_id': self._requests[request_id].container_id})
