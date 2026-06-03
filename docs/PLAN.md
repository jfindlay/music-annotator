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

- **`PLAN-audit-action.md`** (Tracks Q/S/C, **sharded**, `/run-plan`-ready) — the action plan that
  implements the proposals adjudicated at `PLAN-audit.md`'s A4 HALT.  **Track Q** (conformance +
  `repath` safety parity) is independent, ships first, retires point-items 3+5.  **Track S** is the
  app-code chain: S1 extracts the shared move/verify/journal primitive (freezes **C-PROV**/**C-MOVE**)
  → S2 splits `_pipeline_maint.py` → S3 splits `_audit.py` → S4 test reorg + conftest hoist; S5
  (run() named passes; point-item 1) and S6 (`__init__` surface; point-item 2) are independent.
  **Track C** reworks the CLI taxonomy (split `audit` verb, unify dispatch, `rebuild --apply`).  User
  rulings frozen in-file: full module splits; hoist test factories to conftest.

## Completed plans

- **`PLAN-audit.md`** (A0–A4, investigation-only, complete) — structural-coherence audit across
  app-code / test-code / CLI axes.  `/style-audit` ran as A0 substrate; A1–A3 emitted findings +
  ranked proposals; A4 synthesised three ship-lanes and HALTed for the two values rulings.  The
  Findings register and proposal IDs (`P-A1.a`, …) remain the reference for `PLAN-audit-action.md`.
  Keep until the action plan completes, then both delete together (durable invariants C-PROV/C-MOVE
  migrate to `NOTES.md` / `AGENTS.md` at that point).

- **`PLAN-naming.md`** (W1a–W3a, complete; W3b deferred) — library-wide maintenance: regenerable
  cache (`rebuild` subcommand, `enrich --origin-time`), journal diff (`audit --diff`), performer-
  and composer-split unification (`unify` subcommand), arranger/finisher retroactive path credit,
  mechanical repath of stale path-fossils.  Frozen contracts: C-W1 (rebuild interface + origin_time
  field), C-W2 (unified-path policy, both performer and composer parts).  W3b (L2 depth
  normalisation) deferred to `docs/BACKLOG.md` (dedicated multisession).  Codebase-audit handoff
  items and execution learnings in `docs/BACKLOG.md`.
- **`PLAN-fingerprint.md`** (F0–F8, complete) — acoustic fingerprinting & archival identity: the identity triple (`audio_hash`,
  `chromaprint_fp`, `acoustid_id`) stored in tag + journal at ingest, ISRC rung, fuzzy-Chromaprint collision, idempotent
  `audit --enrich` retroactive backfill, medium-sequence corroboration, keyed `fetch_acoustid_lookup` + `--acoustid-key`,
  and the read-only `audit` integrity pass (journal-detects → tag-adjudicates → audio-anchor-confirms).  Invariants in
  `docs/NOTES.md` (archival identity section); two deferred follow-ons (AcoustID-seeded wholly-new-release-candidate
  resolution; `accuraterip` 4th dimension) in `docs/BACKLOG.md`.
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

The four feature plans are complete; the audit action plan is the only active work.  The frozen
substrate chain:

```
PLAN-multimedium.md  (C-S0 cross-medium substrate) ── COMPLETE
PLAN-fingerprint.md  (F0–F8 identity substrate)    ── COMPLETE
PLAN-naming.md       (W1–W3a; C-W1, C-W2 frozen)  ── COMPLETE (W3b deferred)
PLAN-leafnumber.md   (L0–L5)                       ── COMPLETE (L2/PL=0 deferred)
PLAN-audit-action.md (Tracks Q/S/C; C-PROV, C-MOVE) ── ACTIVE (refactor-only; no behaviour change)
```

Each sharded plan ran its own `/run-plan` chain against its own ledger; they do not share a ledger.
`PLAN-audit-action.md` is refactor-only — it consumes the existing frozen substrate (C-S0, C-W1/2,
fingerprint identity) without altering it, and adds C-PROV/C-MOVE as its own internal contracts.
