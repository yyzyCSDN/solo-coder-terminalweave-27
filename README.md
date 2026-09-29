# TerminalWeave

TerminalWeave is a dependency-light Python engine for modelling day-to-day container-terminal operations. It covers marine access, berth use, quay cranes, yard placement, truck-gate appointments, customs holds, reefer monitoring, hazardous-goods segregation, equipment maintenance, rail blocks, demurrage billing and transshipment connections.

The package is intentionally usable without a database or web framework. Domain services expose ordinary Python APIs, while the persistence layer supplies an append-only event store and snapshots for embedding in larger systems.

## Quick start

```bash
python -m pip install -e ".[test]"
python -m terminalweave.cli demo
python -m pytest -q
```

Run the small HTTP adapter with `terminalweave serve`. It exposes `GET /health`,
`POST /tide/clearance`, and `POST /billing/demurrage`. The other packages are
library components intended to be composed by an embedding application.

## Design principles

- domain calculations are deterministic for identical inputs;
- time values are timezone-aware UTC datetimes;
- operational state transitions are explicit and validated;
- safety constraints fail closed when required information is absent;
- money uses `Decimal`;
- core domain modules depend only on the Python standard library.

## Main packages

`marine`, `cranes`, `yard`, `gate`, `customs`, `reefer`, `hazmat`, `equipment`, `rail`, `billing`, `transshipment`, `planning`, `persistence`, `reporting`, and `api`.

### Unified customs release view

The customs queues (holds, risk, inspection, manifest, release queue and the
authority inbox) each work independently. `customs.unified_release.UnifiedReleaseBoard`
fuses them into one releasable/blocked verdict per release request:

- every basis is checked; a missing basis blocks as *missing* (fail closed),
  alongside any actively blocking basis, each with a stable code and full
  per-basis reasons;
- pending requests are recomputed automatically when an official message
  arrives or the manifest changes (hold, risk and inspection updates
  recompute the affected container as well);
- an executed movement is frozen with its exact decision snapshot and the
  complete reasons at execution time, and is never recomputed or rewritten
  by later upstream changes;
- `view(...)` returns one row per request for the duty officer, filtered by
  `status`/`container`, and rows serialize with `core.serialization.to_primitive`.
