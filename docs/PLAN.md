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
| `docs/PLAN-fingerprint.md`   | sharded    | Acoustic fingerprinting & archival identity (F0–F8): the identity triple, ingest identification, `audit` integrity pass. |
| `docs/PLAN-naming.md`        | pre-shard  | Library-wide dir/file-naming unification. The multi-medium S0 substrate it depended on has landed; still depends on `PLAN-fingerprint.md`'s identity layer. |

## Completed plans

- **`PLAN-multimedium.md`** (S0–S9, complete) — multi-medium-correct path construction + library
  maintenance: cross-medium work-group aggregation (C-S0 substrate the naming plan builds on),
  concerto-soloist path promotion accumulated across media (C-S4), and the journal-fragmentation
  `audit`/`regroup` cycle (C-S8).  Invariants in `docs/NOTES.md` (cross-medium aggregation,
  concerto-soloist accumulation, the `regrouped` journal obligation) plus the codebase-audit handoff
  brief; the concerto-like-soloist editorial allowlist follow-on is in `docs/BACKLOG.md`.
- **`PLAN-leafnumber.md`** (L0–L5, complete) — leaf = per-group track index (`CWP_MOVT_NUM`),
  per-group intermediate sibling index (`CWP_INTER_INDEX_{i}`), dead `_dedup_plan_entries`/`.dd`
  machinery retired, retroactive `repath` maintenance mode.  Invariants in `docs/NOTES.md`; the two
  deferred follow-ons (L2 depth normalisation; PL=0 orphan resolution) are in `docs/BACKLOG.md`.

## Reference

| File                | Role                                                                            |
|---------------------|---------------------------------------------------------------------------------|
| `docs/NOTES.md`     | Durable design invariants (path-is-a-handle, journal-detects-tag-adjudicates, CE anchor, …). |
| `docs/BACKLOG.md`   | Cross-cutting / external-dependency items not yet in any plan (source adapters, disc-ID submission, editorial deferrals, musicbrainzngs2 track). |

---

## Cross-plan dependencies

```
PLAN-multimedium.md  (S0 cross-medium substrate) ── COMPLETE; C-S0 frozen
        │
        ├──────────────► PLAN-naming.md      (library-wide unification consumes C-S0 — now available)
        │
PLAN-fingerprint.md  (F0 identity substrate, audit machinery)
        │
        └──────────────► PLAN-naming.md      (same-work/recording detection uses the identity tag + audit)
```

- `PLAN-multimedium.md` is **complete**: its C-S0 cross-medium substrate is frozen and available to
  the downstream naming plan.
- `PLAN-fingerprint.md` was **independent** of `PLAN-multimedium.md` except for one soft dependency:
  F5 (medium-sequence corroboration) gains a cross-medium-span generalisation now that S0 has landed,
  but its medium-scoped form does not require it.
- `PLAN-naming.md` is the downstream consumer of both; one of its two substrates (multimedium C-S0)
  has landed, so it stays pre-shard only until `PLAN-fingerprint.md`'s identity layer lands.

Each sharded plan runs its own `/run-plan` chain against its own ledger; they do not share a ledger.
