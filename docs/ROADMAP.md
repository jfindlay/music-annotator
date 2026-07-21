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
R1 _net ✓ ──────┼──► R2 tier substrate ✓ ─► R3 adapters (J1-ordered) ──► R5 drain ──► J3 ──► R6 re-derivation
                │                                                                     ▲
R4 Act II convergence ──────────────► J2 (naming-policy freeze) ──────────────────────┘
```

Critical path: **R0 → J1 → R2 → R3 (binding adapter) → R5 → J3 → R6**.  R1 is near-critical (gates
R3).  R4 is parallel but J2 gates R6.  R5 is operator-paced, not agent-session-paced — the arc's
schedule is dominated by the user's drain rate, not AI throughput.

### R0 — Census of `Original/`  (Category B; 2 sessions; DONE 2026-07-20)

Scan and classify the remaining top-level dirs (~218 pre-prune) into the BACKLOG taxonomy (Bach
Edition remainder / Presto / whipper rip / not-in-MB / track-mismatch / non-classical–other).
Deliverable: a census artifact.  Ends at **J1**.  Also feeds R4a (inventory of the non-classical
corpus the taxonomy must admit).  → BACKLOG "Census of `Original/`".

**DONE (commits `63c897b`–`6bb7b73`, PLAN R0 2/2 rows).**  147 dirs classified on a two-axis taxonomy
(provenance × MB-status; C-R0-TAX), zero `unknown` on axis 2.  Artifact: `docs/census-r0.{json,md}`.
Final distribution: **107 in-mb-clean · 18 in-mb-mismatch · 15 non-classical-other · 6 not-in-mb · 1
already-ingested**.  The corpus is substantially already in MB — this **tightens the R3 range** (the
scope estimate below) and reinforces the coverage-before-quality intent rather than challenging it (no
defocus).  J1 handoff digest is written to PLAN R0's action-frame digest; **J1 has not yet fired.**

### R1 — `_net` retrieval subpackage  (Category A substrate; ~3-5 sessions; DONE 2026-07-19)

One retry/backoff core with structured (never string-scraped) retryable classification; CAA and
AcoustID leave musicbrainzngs' transport; one terminal-error choke point closes the lossless-principle
gap.  PLAN derivation must resolve the deferred AcoustID persisted-path failure policy (raise vs
logged-gap).  Adapters (R3) build on `_net` from day one — this is the sequencing pressure that puts
R1 first.  Produces the `_net` core interface contract.  → BACKLOG "Unified network-retrieval
subpackage (`_net`)".

**DONE (commits `011668e`–`39dc90f`, PLAN R1 4/4 rows).**  `_net.py` ships `RetryDecision` /
`NetPolicy` / `retrieve()`; MB-data, CAA, and AcoustID all route through it with structured
classifiers; the universal terminal rule is applied (AcoustID cannot-determine now raises).
**Follow-on (BACKLOG-resident, sharded as PLAN R1-F — not a new DAG node):** two `_discover.py`
search/disc-ID calls (`_search_mb_releases` → `mb.search_releases`; `_toc_lookup_mb_releases` →
`mb.get_releases_by_discid`) were never enrolled in R1's session list and kept the legacy
`_mb_retry`/`_mb_call` path with the codebase's last `"404" in str(exc)` scrape.  Migrating them
completes the "uniformly on `_net`" property R3 leans on; freezes no new contract; consumes
C-NET-CORE / C-NET-TERM.  Sequence before any R3 adapter.

### Pre-R3 hardening  (Category A fix; 1 session; J1 additive-reshard; before any R3 adapter; DONE 2026-07-20)

**Added by J1 (2026-07-20).**  Fix the `_discover.py` `_parse_release_item` search-result track-count
bug (`len(track-list)` yields 0 for MB search results, which return `track-list: []` + `track-count:
N`) with a regression test.  Standalone, *not* folded into R2: the annotation-tier substrate's
`mb-search-resolved` tier keys on track-count reconciliation, so the count must be correct before the
tier contract freezes on it.  Sequence alongside/after PLAN R1-F, before any R3 adapter.  Distinct from
R1-F (transport routing) — this is response *parsing*.  → ROADMAP Discoveries appendix (R0 boundary).

**DONE (commit `ca75aaf`, PLAN pre-R3 1/1 row).**  `_parse_release_item` now uses `track-list` length
only when non-empty, else falls back to `track-count` — matching the census `_extract_track_count`
reference.  C-TIER's `mb-search-resolved` denominator is repaired for the search-resolved population;
R1-F (`e7370b7`) was the other prerequisite.  **All R3 gates are now clear.**  Froze no new contract
(scope-completeness on C-TIER's input path).  ◆ handed off to the R3b whipper adapter shard (this
reconciliation).

### R2 — Annotation-tier substrate  (Category A substrate; 3 sessions; DONE 2026-07-20)

Finalise the **annotation-tier ladder** (J1 output; renamed from "rung ladder" — see the two-ladder
note below); persist the tier in the track+sidecars unit (present-state authority) via an
`ANNOTATION_TIER` field on `ProvenanceSidecar`; `audit` enumerates provisional (below-full) entries as
a new pass; upgrade candidates discoverable.  Freezes **C-TIER**, the annotation-tier contract every
adapter consumes (over-specified per Category-A).  → BACKLOG "Provisional-ingest mode — the rung ladder".

**J1 annotation-tier ladder (5 tiers; census-tuned):** `full-mb-verified` (embedded MBID/TOC identity)
→ `mb-search-resolved` (search-reconciled, *lower confidence*, carries `needs-spot-check`) → `mb-partial`
(declared track/structure mismatch) → `alternate-source` (Discogs-style; **empty now, reserved, no
adapter**) → `source-tags-only` (no MB identity; provisional).  The `mb-search-resolved` tier is J1's
key census-driven addition: 99 of 107 clean dirs are search-resolved, not identity-confirmed, and
adjudication caught score-100-but-wrong matches, so search-resolution must be a distinct persisted tier.

**DONE (commits `d679394`–`dab0343`, PLAN R2 3/3 rows).**  C-TIER frozen at S1: `AnnotationTier`
StrEnum (5 tiers incl. reserved-empty `alternate-source`), `annotation_tier` + `needs_spot_check` on
`ProvenanceSidecar`, `classify_annotation_tier` helper, monotonic-upgrade carve-out on the written-once
idempotency invariant.  S2 added the `audit` tier-enumeration pass; S3 wired tier assignment into
`_pipeline.py` (kept out of `_tags.py` per layer-routing — tier is provenance, not tag-rendering).  ◆
boundary `still-on-intent`; no juncture fired (R2 hands to the R3 adapter shards).  **Note:** C-TIER's
`mb-search-resolved` denominator depends on the pre-R3 `_parse_release_item` fix (above), un-landed at
R2 close — R2 froze the contract; pre-R3 repairs its input.

**Two-ladder note (CAPTURE-CANDIDATE, 2026-07-20).**  The codebase already uses "rung" for an
orthogonal **archival-identity-confidence ladder** (rung 0 embedded tags → rung 1 ISRC → … → rung 5
keyed AcoustID, in `_pipeline_io.py`/`__main__.py`: *how confidently a file matches an MB recording*).
J1's ladder is a different axis — **annotation quality/completeness**: *how completely a release could
be annotated*.  The two are orthogonal; R2 keeps "rung" for identity-confidence and names J1's ladder
"annotation tier" to avoid the collision.  A dir can be high-tier (full MB annotation) yet low-rung
(identity only from source tags), or vice versa.

### R3 — Source adapters  (Category B; on R1+R2; J1-ordered; ~9-10 sessions total)

**J1 order (descending clean population → maximises R5 drain-unlock per session):**

1. **R3b** whipper / MakeMKV rips — **first** (52 clean dirs; embedded MBID + TOC disc-ID = highest
   identity confidence; unlocks the reserved AccurateRip 4th archival dimension).  ~3 → **5 sessions**
   (IN PROGRESS 2026-07-20; sharded as PLAN R3b).  Survey found the TOC→MB→tier machinery already
   exists; the genuinely-new work is C-AR (AccurateRip provenance, per-track→tags + per-release→sidecar),
   whipper dir recognition (C-WHIP), single-disc TOC→full-verified promotion, and the J1 spot-check gate.
   MakeMKV deferred (census population is all whipper; MakeMKV emits no AccurateRip).
2. **R3a** PrestoMusic downloads (ISRC-bearing; 36 dirs; simplest single-source).  ~2 sessions.
3. **R3e** other-download/amazon clean (19 dirs; may collapse into R3a as a source-variant).  ~1-2.
4. **R3d** Track-mismatch-tolerant ingest — **after** the strict clean adapters prove C-TIER.
   **Sub-classified (J1):** **R3d-edition** (genuine edition/pressing mismatch, single-medium) vs
   **R3d-structure** (flat-local dir vs multi-disc-MB layout — Grieg, Tchaikovsky, Karajan Sampler,
   Puccini; needs multi-disc reconciliation, consumes C-S0).  The R3d PLAN reads `census-r0.json`
   deltas to size the two sub-classes.  ~3 sessions.
- **R3c Discogs adapter — PRUNED to BACKLOG (J1).**  Census refuted its premise: all 6 not-in-mb dirs
  are personal recordings, none Discogs-suitable.  Returns to BACKLOG as trigger-based (fires if a
  future census surfaces Discogs-suitable commercial releases).  The `alternate-source` tier is
  reserved in C-TIER so the adapter drops in later without re-freezing.
- **R3e-policy Not-in-MB routing** (retained as policy): default the 6 not-in-mb dirs to
  `source-tags-only` (tier 4) provisional ingest; MB-upstream creation is a per-release operator
  election, never the automatic default (the census's close-to-MB-ready subset is empty).  LDS Youth
  Music is manual-move-out (user, census adjudication log).

**Spot-check gate (J1):** before the first direct-ingest adapter bulk-runs, spot-check a sample of the
search-only (`mb-search-resolved`) population; fold into the first R3 adapter's PLAN as a gating step;
`needs-spot-check` is the persisted, `audit`-discoverable mechanism.  A high false-match rate is a
step-3 watch item that could reshard R3 order.

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
| **J1** *(FIRED 2026-07-20)* | end of R0 | Census distribution → R3 order/pruning; rung-ladder shape for R2; not-in-MB default posture.  Verdict `still-on-intent` + `additive-reshard`; no destructive-HALT.  Outputs folded into R2/R3/pre-R3 nodes above and recorded in the appendix.  R2 shard proceeds against C-TIER. |
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

## Scope estimate (static frame; R3 tightened by J1 2026-07-20)

R0 2 ✓ · R1 3-5 ✓ · pre-R3 fix 1 ✓ · R2 3 ✓ · R3 11-12 · R4 3-6 · R6 5-8 → **~26-37 agent sessions**, plus
the operator-paced R5 drain.  J1 tightened the R3 range from the provisional 8-16 to 9-10 on the
census distribution; the R3b survey (2026-07-20) then re-sized R3b 3→5, giving **11-12**: pre-R3 fix 1 ✓ ·
R2 3 ✓ · R3b 5 · R3a 2 · R3e 1-2 · R3d 3 · R3c 0 (pruned).  The
clean-ingest adapters (R3a/R3b/R3e ≈ 107 dirs) dominate; R3d (18) is sub-classified; R3c (Discogs) is
pruned to BACKLOG.

## Out of scope (stays in BACKLOG)

Act III-b (perpetual by definition); the **playlist library** (graduates to its own ROADMAP when Act I
nears completion — decided 2026-07-18); MB-upstream data edits and the editorial/scholarly track
(operator/research-paced); musicbrainzngs2 contributions (external repo, maintainer-paced); AcoustID
seeded-candidate extension, AccurateRip backfill, and misc items (trigger- or dependency-based).

## Discoveries appendix

(Mid-session discoveries append here; evaluated at the next sub-track boundary.)

- **R1 boundary (2026-07-19) — search/disc-ID transport gap.**  R1's session list never enrolled the
  two `_discover.py` search/disc-ID call sites (`_search_mb_releases` → `mb.search_releases`;
  `_toc_lookup_mb_releases` → `mb.get_releases_by_discid`); they kept the legacy `_mb_retry`/`_mb_call`
  path and the last `"404" in str(exc)` scrape.  Static-frame consequence: the "every remote fetch
  routes through `_net`" property R3 adapters assume did **not** hold literally at R1 close.
  Resolution: sharded as **PLAN R1-F** (a BACKLOG-resident completion of R1, not a new DAG node) —
  sequence before any R3 adapter.  No design change to the arc; scope-completeness only.

- **R0 boundary (2026-07-20) — `_parse_release_item` search-result track-count bug.**  `_discover.py`
  `_parse_release_item` uses `len(track-list)`, which yields 0 for MB search results (the API returns
  `track-list: []` alongside `track-count: N`).  The census script worked around it with a custom
  `_extract_track_count`.  Static-frame consequence: a **latent bug in the ingest path** that will
  mis-count tracks for every search-driven ingest — it must be fixed before R3 adapters rely on
  search-result track counts.  Resolution: **additive-reshard signal — a pre-R3 fix session** (or fold
  into R2 substrate work).  For J1 to sequence.  (CAPTURE-CANDIDATE surfaced at R0 close.)

- **R0 boundary (2026-07-20) — `in-mb-mismatch` is doing double duty.**  The 18 `in-mb-mismatch` dirs
  split into two structurally different cases: (a) genuine **edition mismatches** (different pressing /
  track selection) and (b) **flat-local / multi-disc-MB structure mismatches** (local flat dir holds
  all tracks; MB spreads them across discs — e.g. Grieg Edition 337 vs 507, Tchaikovsky 30 vs 62).
  These are not data errors; they need different adapter handling.  Static-frame consequence: **R3d may
  need sub-classification** (edition-mismatch vs structure-mismatch) before adapter ordering, and R3d
  must handle multi-disc reconciliation.  For J1 to decide.

- **R0 boundary (2026-07-20) — search-only vs embedded-MBID confidence asymmetry.**  Of the 107
  `in-mb-clean` dirs, only 8 carry an embedded `MUSICBRAINZ_ALBUMID`; 99 were resolved by Pass 2 MB
  search (track-count reconciliation, not identity).  Static-frame consequence: **search-only
  `in-mb-clean` is lower-confidence than embedded-MBID `in-mb-clean`** and should be weighted below it
  when J1 orders R3a/R3b/R3e; a spot-check of the search-only population is warranted before finalising
  direct-ingest adapter order.  For J1 to weigh.

- **J1 adjudication (2026-07-20) — verdict `still-on-intent` + `additive-reshard`, no destructive-HALT.**
  Census reinforces coverage-before-quality (107/147 clean-ingestable).  Resolutions folded into the R2,
  R3, and pre-R3 nodes above: (1) R3 ordered R3b→R3a→R3e→R3d by descending clean population (drain-unlock);
  (2) **R3c Discogs pruned** to trigger-based BACKLOG (zero census population); (3) 5-tier annotation
  ladder with the **`mb-search-resolved` tier added** (99/107 search-resolved ≠ identity); (4) `_parse_release_item`
  fix sequenced as its **own pre-R3 session** (not folded into R2 — the `mb-search-resolved` tier keys on
  track-count); (5) **R3d sub-classified** edition vs structure; (6) not-in-MB defaults to `source-tags-only`;
  (7) spot-check gate on the search-only population folds into the first R3 adapter.  Tightened R2+R3 to
  ~12-14 sessions.  No frozen contract invalidated (the bug is a parse error, orthogonal to
  defensive-download and confirmation-provenance).

- **R2 substrate survey (2026-07-20) — two-ladder terminology collision (CAPTURE-CANDIDATE).**  The
  codebase already uses "rung" for an **archival-identity-confidence** ladder (rung 0–5 in
  `_pipeline_io.py`/`__main__.py`); J1's ladder is the orthogonal **annotation-quality** axis.  R2 keeps
  "rung" for identity-confidence and names J1's ladder **annotation tier** (`ANNOTATION_TIER` on
  `ProvenanceSidecar`; contract C-TIER).  A dir can be high-tier / low-rung or vice versa.  This framing
  is durable through R3 and Act III-b.  **Graduated at the R2 boundary (2026-07-20):** confirmed in code as
  `AnnotationTier` (annotation) vs `_IDENTITY_METHODS` rungs 0–5 (identity); a NOTES capture is warranted
  (see exit report).  R2 also established the **monotonic-upgrade carve-out** on `ProvenanceSidecar`
  idempotency (annotation_tier may rise, never silently lower) — a durable prose sub-contract of C-TIER for
  Act III-b.

- **pre-R3 boundary (2026-07-20) — R3b substrate mostly pre-exists; MakeMKV deferred.**  The R3b survey
  found `parse_disc_toc` / `_toc_lookup_mb_releases` / `_match_medium_by_toc` / `CensusSignal.EMBEDDED_MBID`
  already in place, so R3b's new work is narrower than the ~3-session estimate implied and sharpens to 5
  contract-bounded sessions (C-AR + C-WHIP frozen at R3b S1).  Static-frame consequences: (1) the reserved
  "4th archival dimension" (BACKLOG) is realised as **C-AR** — per-track AccurateRip in the `TrackTags`/
  `TransactionEntry` 4th-dim slot (tags), per-release summary in `ProvenanceSidecar`; (2) `run()` currently
  gates TOC→`full-mb-verified` to multi-disc only — R3b S3 lifts this to single-disc under whipper
  provenance; (3) MakeMKV is deferred (zero census population, no AccurateRip) — **C-WHIP names whipper
  only**.  C-AR mirrors whipper's `WhipperLogger` schema 1:1 (v1/v2 per-track Result+Confidence+CRC;
  release-level MB/CDDB disc-ID + self-attesting log SHA-256) — a faithful capture of the AccurateRip
  convention, verified against the whipper source, not a nonstandard invention.
