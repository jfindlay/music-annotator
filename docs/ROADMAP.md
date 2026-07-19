# music-annotator — ROADMAP: library completion

The static-frame view (sub-track DAG) of the library-completion arc: **Act I full inclusion → Act II
naming-policy freeze → Act III-a one-pass re-derivation**.  Durable for the arc's life; reviewed at
sub-track boundaries, not per session.  Per-sub-track rolling detail lives in a `PLAN-*.md` derived at
each sub-track's start.  Full design context for every node **remains in `docs/BACKLOG.md`** (pointers
below) until that node's PLAN is derived — the roadmap holds structure, not exposition.  Mid-session
discoveries land in the appendix and are evaluated at the next sub-track boundary, never absorbed
mid-track.

`juncture-tier: opus` — all junctures, all sub-tracks (user decision 2026-07-18).  The strong inner
loop (enforced 100% branch coverage, strict mypy, pylint 10/10) licenses small commits throughout,
but provenance-chain and path-policy work keeps correctness-criticality high, so the adjudicator does
not opt down.

## Design intent (anchor — re-read at every sub-track boundary)

The north star (BACKLOG, 2026-07-17): **full inclusion** (the library is a catalog; nothing stays
outside `Original/`), **coverage before quality** (every source dir ingested at its best achievable
rung, rung persisted), **two lenses** (filesystem = catalog, playlists = reading room).  The
layer-routing rule governs every fix: a defect is fixed at the layer that owns it and kept visible
until then (renderer/policy = class A, MB data = class B, scholarship = class C).

**Done means:**

- **Act I** — `Original/` on hades is empty; every dir ingested at its best rung with the rung
  explicitly marked in the track+sidecars unit.
- **Act II** — the naming policy is frozen (taxonomy, depth rendering, editorial signals): juncture J2.
- **Act III-a** — the whole library re-derived once under the final heuristics; conventions spec
  finalised against them.
- **Act III-b** (the perpetual upgrade loop) then *begins*.  It is the steady state after this
  roadmap, not a node in it.

## Sub-track DAG

```
R0 census ──► J1 (adjudicate adapter order + rung ladder)
                │
R1 _net ────────┼──► R2 rung substrate ──► R3 adapters (J1-ordered) ──► R5 drain ──► J3 ──► R6 re-derivation
                │                                                                     ▲
R4 Act II convergence ──────────────► J2 (naming-policy freeze) ──────────────────────┘
```

Critical path: **R0 → J1 → R2 → R3 (binding adapter) → R5 → J3 → R6**.  R1 is near-critical (gates
R3).  R4 is parallel but J2 gates R6.  R5 is operator-paced, not agent-session-paced — the arc's
schedule is dominated by the user's drain rate, not AI throughput.

### R0 — Census of `Original/`  (Category B; ~1-2 sessions; UNBLOCKED 2026-07-18)

Scan and classify the remaining top-level dirs (~218 pre-prune) into the BACKLOG taxonomy (Bach
Edition remainder / Presto / whipper rip / not-in-MB / track-mismatch / non-classical–other).
Deliverable: a census artifact.  Ends at **J1**.  Also feeds R4a (inventory of the non-classical
corpus the taxonomy must admit).  → BACKLOG "Census of `Original/`".

### R1 — `_net` retrieval subpackage  (Category A substrate; ~3-5 sessions; can start now)

One retry/backoff core with structured (never string-scraped) retryable classification; CAA and
AcoustID leave musicbrainzngs' transport; one terminal-error choke point closes the lossless-principle
gap.  PLAN derivation must resolve the deferred AcoustID persisted-path failure policy (raise vs
logged-gap).  Adapters (R3) build on `_net` from day one — this is the sequencing pressure that puts
R1 first.  Produces the `_net` core interface contract.  → BACKLOG "Unified network-retrieval
subpackage (`_net`)".

### R2 — Provisional-rung substrate  (Category A substrate; ~2-4 sessions; after J1)

Finalise the rung ladder; persist the rung in the track+sidecars unit (present-state authority);
`audit` enumerates provisional entries; upgrade candidates discoverable.  The ladder's exact rungs are
J1 output (the census distribution shapes them).  Freezes the rung-marking contract every adapter
consumes.  → BACKLOG "Provisional-ingest mode — the rung ladder".

### R3 — Source adapters  (Category B; mutually orthogonal on R1+R2; ~2-4 sessions each; J1-ordered)

- **R3a** PrestoMusic downloads (ISRC-bearing).
- **R3b** whipper / MakeMKV rips (+ AccurateRip exposure — unblocks the reserved 4th archival
  dimension, which backfills via P-FP3 and stays in BACKLOG).
- **R3c** Discogs adapter (identity mapping, mid-ladder rung, no work tree).
- **R3d** Track-mismatch-tolerant ingest (declared-mismatch mode; strict default preserved).
- **R3e** Not-in-MB routing rule (mostly policy; per-release adjudication posture set at J1 review).

J1 decides order and may prune scope (an adapter with zero census population is dead weight).
→ BACKLOG "Source-adapter support", "Alternate metadata source: Discogs adapter",
"Track-mismatch-tolerant ingest", "Not-in-MB routing rule".

### R4 — Act II naming-policy convergence  (design-heavy; ~3-6 sessions; parallel; soft-depends R0)

- **R4a** Library-wide taxonomy + initial directory component (A-b): top-level class scheme admitting
  the full corpus; within-classical component edge cases.  Design can start now; finalises against the
  R0 census inventory.
- **R4b** Cross-medium fragmentation inventory (A-c): enumerate before designing; remedies may route
  to class B or III-b.
- **R4c** Concerto-like soloist editorial allowlist (small additive; substrate already in place).

Ends at **J2 — the naming-policy freeze** (uniform-ceiling/ragged-floor already converged; A-b/A-c
close here).  → BACKLOG Act II sections.

### R5 — Operational drain of `Original/`  (operator loop on hades; no agent sessions)

Bach Edition remainder is runnable today; each R3 adapter unlocks another census class.  Interleaves
with dev work throughout.  Exit condition: `Original/` empty — this is Act I's "done".

### R6 — Act III-a one-pass re-derivation  (gated on R5 exit + J2; ~5-8 sessions)

- **R6a** Hierarchy-depth normalisation (W3b/L2 uniform-ceiling clamp; re-run
  `scripts/scan_nonuniform_depth.py` against the complete library first — the 36-group census is
  stale by construction).
- **R6b** Catalogue-colon retro-fix (re-survey for `NN - NN` dirs and corrupt `CWP_PART_*`; repath).
- **R6c** AcoustID tag naming + semantics — Picard alignment (persisted-tag migration; decide the two
  sub-questions at PLAN time).
- **R6d** Full-library repath under the frozen heuristics — the "more like itself" pass.  Gated by
  **J3**.
- **R6e** Conventions-spec finalisation (integrative writeup; consistently under-scheduled — allocate
  a full session minimum).

→ BACKLOG Act III-a sections and "Public conventions spec".

## Junctures

| Juncture | When | Adjudicates |
|----------|------|-------------|
| **J1** | end of R0 | Census distribution → R3 order/pruning; rung-ladder shape for R2; not-in-MB default posture. |
| **J2** | end of R4 | Naming-policy freeze: taxonomy, depth policy, editorial signals.  Gates R6. |
| **J3** | before R6d | Go/no-go on the destructive-scale full-library repath: `Reference/` retention decision, journal capacity, dry-run evidence. |

Post-R3, the **structural-audit trigger** fires (BACKLOG "Codebase maintenance cadence"): review the
coherence of the new module boundaries (adapters, rung substrate, `_net`) once settled.  Trigger-based;
stays in BACKLOG, referenced here for timing.

## Cross-session contracts

**Consumed (frozen — any invalidation is a destructive-HALT):** C-PROV / C-MOVE (move/verify/journal
provenance), C-L0 / C-L1 (leaf/intermediate numbering), C-S0 (aggregation spans media; mutation does
not), the defensive-download and confirmation-provenance invariants (repo `AGENTS.md`).

**Produced:** R1 freezes the `_net` core interface; R2 freezes the rung-marking contract; R4/J2
freezes the naming policy (C-W3b graduates from provisional); R6 freezes the final path policy and
externalises it as the conventions spec.  Each sub-track PLAN names its own C-* contracts per existing
convention.

**Prose contracts:** the lossless principle (failure ≠ no-data), "path is a handle, not a manifest",
the layer-routing rule, "journal detects, tag adjudicates" — all in `docs/NOTES.md`; every PLAN
derivation re-reads them.

## Scope estimate (static frame; provisional until J1)

R0 1-2 · R1 3-5 · R2 2-4 · R3 8-16 · R4 3-6 · R6 5-8 → **~22-41 agent sessions**, plus the
operator-paced R5 drain.  The R3 range is the widest and is exactly what J1 tightens.

## Out of scope (stays in BACKLOG)

Act III-b (perpetual by definition); the **playlist library** (graduates to its own ROADMAP when Act I
nears completion — decided 2026-07-18); MB-upstream data edits and the editorial/scholarly track
(operator/research-paced); musicbrainzngs2 contributions (external repo, maintainer-paced); AcoustID
seeded-candidate extension, AccurateRip backfill, and misc items (trigger- or dependency-based).

## Discoveries appendix

(Empty.  Mid-session discoveries append here; evaluated at the next sub-track boundary.)
