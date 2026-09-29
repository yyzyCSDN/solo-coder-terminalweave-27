from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from terminalweave.core.clock import ensure_utc
from terminalweave.core.errors import ValidationError, ConflictError
from terminalweave.core.observable import Observable

@dataclass(frozen=True)
class ManifestItem:
    container_id: str
    bill_of_lading: str
    consignee: str
    iso_type: str
    gross_weight_kg: float
    seal_number: str | None = None

    def __post_init__(self) -> None:
        if self.gross_weight_kg <= 0:
            raise ValidationError('gross weight must be positive')
        if not self.bill_of_lading.strip():
            raise ValidationError('bill of lading required')

@dataclass(frozen=True)
class ManifestVersion:
    version: int
    submitted_at: datetime
    items: tuple[ManifestItem, ...]
    submitter: str

    def __post_init__(self) -> None:
        object.__setattr__(self, 'submitted_at', ensure_utc(self.submitted_at))
        if self.version < 1:
            raise ValidationError('manifest version must be positive')
        ids = [i.container_id for i in self.items]
        if len(ids) != len(set(ids)):
            raise ValidationError('manifest contains duplicate container')

class ManifestLedger(Observable):

    def __init__(self):
        super().__init__()
        self.versions: list[ManifestVersion] = []

    def submit(self, version: ManifestVersion) -> None:
        expected = self.versions[-1].version + 1 if self.versions else 1
        if version.version != expected:
            raise ConflictError(f'expected manifest version {expected}')
        if self.versions and version.submitted_at <= self.versions[-1].submitted_at:
            raise ValidationError('submission time must increase')
        self.versions.append(version)
        prior = self.versions[-2] if len(self.versions) >= 2 else None
        changes = self.changes(prior.version, version.version) if prior else {'added': {i.container_id for i in version.items}, 'removed': set(), 'changed': set()}
        self.notify('manifest_submitted', {'version': version.version, 'submitted_at': version.submitted_at, 'added': tuple(sorted(changes['added'])), 'removed': tuple(sorted(changes['removed'])), 'changed': tuple(sorted(changes['changed']))})

    def lookup(self, container_id: str) -> ManifestItem | None:
        latest = self.latest()
        if not latest:
            return None
        return next((x for x in latest.items if x.container_id == container_id), None)

    def latest(self) -> ManifestVersion | None:
        return self.versions[-1] if self.versions else None

    def changes(self, older: int, newer: int) -> dict[str, set[str]]:
        a = next((v for v in self.versions if v.version == older))
        b = next((v for v in self.versions if v.version == newer))
        amap = {x.container_id: x for x in a.items}
        bmap = {x.container_id: x for x in b.items}
        return {'added': set(bmap) - set(amap), 'removed': set(amap) - set(bmap), 'changed': {cid for cid in set(amap) & set(bmap) if amap[cid] != bmap[cid]}}

    def verify_weight(self, container_id: str, observed_kg: float, tolerance_kg: float) -> bool:
        latest = self.latest()
        if not latest:
            raise ValidationError('no manifest submitted')
        item = next((x for x in latest.items if x.container_id == container_id), None)
        if not item:
            raise ValidationError('container absent from manifest')
        return abs(item.gross_weight_kg - observed_kg) <= tolerance_kg
