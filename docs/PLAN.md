# music-annotator — Plan Index

The project's forward work is split into several **independent** plans, each self-contained with its
own scope and (where sharded) its own `/run-plan` Progress ledger.  This file is the index; it holds
no plan content itself.

Conventions (see `~/.config/opencode/multi-session-planning.md`):
- A **sharded plan** is decomposed into commit-shaped sessions with a session list, cross-session
  contracts, and a progress ledger — directly executable by `/run-plan`.
- A **pre-shard plan** is described at sub-track granularity because its sessions are crisply known
  only after its substrate lands.  It is re-written into sharded form when ready.
- **`docs/NOTES.md`** holds the durable design invariants every plan refracts through.
- **`docs/BACKLOG.md`** holds cross-cutting items with no committed substrate yet.

---

## Active plans

| Plan                         | Status     | Scope                                                                 |
|------------------------------|------------|-----------------------------------------------------------------------|
| `docs/PLAN-multimedium.md`   | sharded    | Multi-medium-correct path construction + library maintenance (S0–S9). The substrate other plans build on. |
| `docs/PLAN-leafnumber.md`    | sharded    | Leaf-numbering & hierarchy-depth correctness (L0–L5): per-group track index as the leaf, depth uniformity, retroactive `repath`. Self-contained; consumes the post-S0 substrate. |
| `docs/PLAN-fingerprint.md`   | sharded    | Acoustic fingerprinting & archival identity (F0–F8): the identity triple, ingest identification, `audit` integrity pass. |
| `docs/PLAN-naming.md`        | pre-shard  | Library-wide dir/file-naming unification. Depends on `PLAN-multimedium.md` S0 and `PLAN-fingerprint.md`'s identity layer. |

## Reference

| File                | Role                                                                            |
|---------------------|---------------------------------------------------------------------------------|
| `docs/NOTES.md`     | Durable design invariants (path-is-a-handle, journal-detects-tag-adjudicates, CE anchor, …). |
| `docs/BACKLOG.md`   | Cross-cutting / external-dependency items not yet in any plan (source adapters, disc-ID submission, editorial deferrals, musicbrainzngs2 track). |

---

## Cross-plan dependencies

```
PLAN-multimedium.md  (S0 cross-medium substrate)
        │
        ├──────────────► PLAN-naming.md      (library-wide unification consumes C-S0)
        │
PLAN-fingerprint.md  (F0 identity substrate, audit machinery)
        │
        └──────────────► PLAN-naming.md      (same-work/recording detection uses the identity tag + audit)
```

- `PLAN-fingerprint.md` is **independent** of `PLAN-multimedium.md` except for one soft dependency:
  F5 (medium-sequence corroboration) gains a cross-medium-span generalisation once S0 lands, but its
  medium-scoped form does not require it.
- `PLAN-naming.md` is the downstream consumer of both and stays pre-shard until both substrates land.

Each sharded plan runs its own `/run-plan` chain against its own ledger; they do not share a ledger.
