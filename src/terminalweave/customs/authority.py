from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from terminalweave.core.clock import ensure_utc
from terminalweave.core.errors import ValidationError

@dataclass(frozen=True)
class AuthorityMessage:
    message_id: str
    container_id: str
    authority: str
    message_type: str
    issued_at: datetime
    supersedes: str | None = None

    def __post_init__(self):
        object.__setattr__(self, 'issued_at', ensure_utc(self.issued_at))

class AuthorityInbox:

    def __init__(self):
        self.messages: dict[str, AuthorityMessage] = {}

    def ingest(self, m: AuthorityMessage) -> None:
        if m.message_id in self.messages:
            raise ValidationError('duplicate authority message')
        if m.supersedes and m.supersedes not in self.messages:
            raise ValidationError('superseded message not found')
        if m.supersedes:
            old = self.messages[m.supersedes]
            if old.container_id != m.container_id or old.authority != m.authority:
                raise ValidationError('supersession scope mismatch')
            if m.issued_at <= old.issued_at:
                raise ValidationError('superseding message must be newer')
        self.messages[m.message_id] = m

    def effective(self, container_id: str, at: datetime | None = None) -> tuple[AuthorityMessage, ...]:
        if at is not None:
            at = ensure_utc(at)
        superseded = {
            m.supersedes
            for m in self.messages.values()
            if m.supersedes and (at is None or m.issued_at <= at)
        }
        values = [
            m for m in self.messages.values()
            if m.container_id == container_id
            and m.message_id not in superseded
            and (at is None or m.issued_at <= at)
        ]
        return tuple(sorted(values, key=lambda x: (x.authority, x.issued_at, x.message_id)))

    def latest(self, container_id: str, authority: str, message_type: str) -> AuthorityMessage | None:
        values = [m for m in self.effective(container_id) if m.authority == authority and m.message_type == message_type]
        return max(values, key=lambda x: x.issued_at) if values else None
