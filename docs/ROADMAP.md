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
                                        ▲
E styleguide arc [ROADMAP-styleguide.md] ┘   (peer arc; V1b/v1 gates J2; feeds R6e)
```

Critical path: **R0 → J1 → R2 → R3 (binding adapter) → R5 → J3 → R6**.  R1 is near-critical (gates
R3).  R4 is parallel but J2 gates R6.  **The editorial-styleguide arc (a peer roadmap,
`docs/ROADMAP-styleguide.md`) now gates J2** — its V1b/v1 completion is J2's editorial input; J2
cannot freeze the naming policy ahead of the styleguide v1.  R4b (the remaining in-arc R4 node) runs
parallel to that arc (inventory-first, remedy-routing — largely independent of attribution policy).
R5 is operator-paced, not agent-session-paced — the arc's schedule is dominated by the user's drain
rate, not AI throughput.

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
   identity confidence; unlocks the reserved AccurateRip 4th archival dimension).  ~3 → 5 sessions
   (**DONE 2026-07-21**, commits `92d9f7c`–`e326b5f`, PLAN R3b 5/5 rows).  Froze **C-AR** (AccurateRip
   provenance: per-track→tags flat-`str` fields + per-release→`ProvenanceSidecar` summary) and **C-WHIP**
   (whipper dir signature).  Delivered single-disc TOC→`full-mb-verified` promotion (whipper-anchored),
   whipper `.log`/`.cue`/`.toc` sidecar preservation, and the J1 spot-check gate (audit tier-pass extended
   with AR status).  MakeMKV deferred (census all whipper; MakeMKV emits no AccurateRip).  ◆ handed off to
   the R3a Presto adapter shard (this reconciliation).
2. **R3a** PrestoMusic downloads (ISRC-bearing; 36 dirs; simplest single-source).  ~2 → 3 sessions
   (**DONE 2026-07-21**, commits `80d0908`–`973577d`, PLAN R3a 3/3 rows).  Froze **C-ISRC** (an
   `ISRC_MATCH` `CensusSignal` promoting ISRC-match-against-the-selected-medium's-recordings to
   `full-mb-verified`; additive to C-TIER, not a re-freeze) and **C-PRESTO** (ISRC-presence download
   recognition → `origin_source`).  Delivered the ISRC-match tier rung (after embedded-MBID, before
   `SEARCH_HIT`; evidence rule: ≥1 confirmed match, no mismatch), the `is_presto_dir` recogniser,
   ISRC-verified audit surfacing, and a Presto integration test.  All three sessions were clean green
   runs (no discoveries, no contract flexes).  ◆ handed off to the R3e other-download adapter shard
   (this reconciliation).
3. **R3e** other-download clean (19 dirs; the source-variant collapse J1 anticipated — CONFIRMED).
   ~1-2 → **1 session** (**DONE 2026-07-21**, commit `b0acf73`, PLAN R3e 1/1 rows).  Renamed the
   ISRC-presence label `"presto"`→`"download"` (`is_presto_dir`→`is_download_dir`; recognition
   heuristic unchanged) and added the `test_other_download_full_pipeline` integration test.  Froze
   **C-DL** (generic download recognition + label), superseding C-PRESTO's `"presto"` literal.  The
   collapse was total (same recogniser, same ladder, honest label) — no new ingest path, no compiler
   contract touched (`origin_source` is free-form `str`).  ◆ handed off to the R3d shard (this
   reconciliation).
4. **R3d** Track-mismatch operator-override — **after** the strict clean adapters prove C-TIER.
   ~3 → **1 session** (**DONE 2026-07-21**, commit `55fa104`, PLAN R3d 1/1 rows).  **Collapsed at the shard
   boundary by an operator-policy decision (2026-07-21):** a track-count mismatch cannot be
   auto-reconciled — it needs physical-medium inspection or a re-rip, which is the operator's
   responsibility.  So R3d does **not** build multi-disc aggregation or an edition-vs-structure copy
   fork (both dropped).  Instead it gives the hard-fail track-count gate (`_pipeline.py:1554`
   `RuntimeError`, plus `_select_medium_with_reason`'s `ValueError`) an **operator override**,
   following the existing `_prompt_duration_warnings` / `confirm_disc` precedents: on mismatch,
   surface local-vs-MB counts (with an edition-vs-structure diagnostic derived from
   `shape.disc_subdirs` + count ratio, shown for context only) → operator **accepts** (ingest the
   selected/best medium at `mb-partial`, operator owns the discrepancy) or **declines** (skip; stays
   in `Original/` for physical-medium handling on the operator backlog).  Freezes **C-OVR** (the
   `confirm_count_mismatch` `DiscoverUI` Protocol method + the accept→`CensusSignal.MISMATCH` wiring).
   Consumes C-TIER's `mb-partial` tier and `CensusSignal.MISMATCH` **unchanged** (R2 over-specified
   them; no C-TIER re-freeze).  1 session: the `mb-partial`/`MISMATCH`/audit machinery already exists
   (R2), so the only new work is the Protocol method, the terminal prompt, the gate rewrite, and the
   integration test.  The 18 `in-mb-mismatch` dirs (9 presto, 5 whipper, 4 other-download) are worked
   by the operator via R5 drain, not auto-ingested.  **Froze C-OVR** — `confirm_count_mismatch` on
   **both** `DiscUI` (`_pipeline.py`, the callable seam) and `DiscoverUI` (`_discover.py`, the full
   surface) + accept→`MISMATCH` wiring + the positional-min-k truncation rule.  Freeze-time
   adjustments (within `@architect` latitude, no scope change): `selected_medium` widened to
   `MBMedium | None` for the multi-disc no-match path; the method lands on two protocols, not one.
   **The R3 code arc closes here** → handoff to R5 drain + the post-R3 structural-audit trigger (now
   eligible); the parallel R4/J2 track carries the remaining critical-path design work.
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
  the full corpus; within-classical component edge cases.  **DONE 2026-07-22** (commits `7666040`,
  `2cef03e`; PLAN R4a 2/2 rows).  Froze **C-CLASS** (top-level class scheme: `_top_level_class(tags)
  -> str`, 6-arm Picard-aligned routing table, tag-derivable signal, class-depth-aware `_work_top_dir`
  helper) and **C-INIT** (`_classical_top_dir(tags) -> str | None`: compilation → albumartist-last-name-first,
  recital → performer-first, single-composer → unchanged).  Sharded first among R4 after the R3 code
  arc closed.  ◆ closed; hands off to E + R4b.
- **R4b** Cross-medium fragmentation inventory (A-c): enumerate before designing; remedies may route
  to class B or III-b.  Runs parallel to E (inventory-first; largely independent of attribution policy).
  **DONE 2026-07-23 (`docs/PLAN-fragmentation.md` 2/2 rows; commits `fac01f3` scanner + C-FRAG-TAX / `b5f7d76`
  census + remedy-routing; ◆ still-on-intent).**  S1 froze **C-FRAG-TAX** (the fragmentation-shape vocabulary)
  in the read-only `scripts/scan_fragmentation.py`; S2 produced the documentary census
  (`docs/census-fragmentation.{md,json}`) on the D-A2 posture (hades not mounted) — **5 rg-multi-release
  pre-ingest candidates, 0 per-medium-credit-variance, 0 rg-vs-release-split; all 5 routed to III-b**.  The one
  arc-boundary finding (C-S0 aggregates within a release, not across a release-group, so box sets modelled as
  multiple releases fragment despite C-S0) is folded into the Discoveries appendix below as an R6d-planning
  input, not re-opened in-arc (D-4).
  **R4b integration follow-on (scanner → `audit` migration — surfaced 2026-08-20):** the R4b inventory
  froze C-FRAG-TAX in two read-only scripts (`scripts/scan_fragmentation.py` — the three residual
  fragmentation shapes not covered by `audit`'s `MUSICBRAINZ_ALBUMID`-keyed detection; and
  `scripts/scan_nonuniform_depth.py` — non-uniform `CWP_PART_LEVELS` + multi-recording-per-bottom-work
  detection).  Both hold detection logic that lives only in the scripts.  To drain `scripts/` fully
  and make these library-health checks re-runnable on the live library, **migrate both into the
  `audit` CLI action** as additional read-only detectors (a report-model design + a live scan — a
  design-bearing session, not a mechanical move), then delete the scripts.  Sequenced as a post-R4b
  integration item coordinated with any III-b regroup remedy; it freezes no new taxonomy (C-FRAG-TAX
  is the substrate).  The pre-R6d drain shard (`docs/PLAN.md`, Sub-track D) deletes only the three
  *superseded* scripts and the two spent census tools — it deliberately leaves these two scanners in
  place pending this migration.
- **R4c DISSOLVED into E (2026-07-22).**  R4c was scoped as a "small additive allowlist" widening the
  mechanical `top_work.type == "Concerto"` path-injection gate (`_tags.py:1189`) to a few more
  canonical-soloist dirs.  Operator refutation (2026-07-22): an allowlist is the tell of a *missing
  principle*, not a missing list entry — the real problem is a general performer-attribution editorial
  policy (soloists→conductors→ensembles; the Albinoni/concerto-grosso/choir+chorusmaster/modern-works
  hard cases prove it is editorial, not mechanical — the same work is attributed differently across
  releases).  R4c-as-written is therefore dissolved; its actual need (canonical-soloist promotion
  beyond mechanical Concerto) becomes one *application* of node **E** and lands wherever E directs
  (likely folded into R6d re-derivation or a thin post-E follow-on).  **E said otherwise (operator
  2026-07-23): SEL-11 overturned** — no canonical-soloist path promotion at all (the soloist is always
  in the tags); the `_tags.py:1189` gate is removed by a trivial post-v1 deletion shard, coordinated
  with R6d.  R4c's need is resolved by rejection, not generalisation.

### E — Editorial styleguide (the CE-replacement basis)  (gates J2; feeds R6e; **own ROADMAP**)

**Promoted above the R4 tail 2026-07-22; graduated to its own arc roadmap 2026-07-23 (operator
decision): `docs/ROADMAP-styleguide.md`.**  A generative editorial styleguide that is the
philosophical basis of the CE replacement — authored from principle; universal (realised by
music-annotator within and without Picard; CEv3 platforms its MB-derivable partition).  Charter and
session-1 adjudications in `docs/NOTES.md`; the document itself is `docs/STYLEGUIDE.md` (seeded
2026-07-22: three founding principles, five-layer × two-partition architecture, epistemic register
authored, 14-case register).  **Arc structure (see the styleguide ROADMAP): V1a source mining (3
Sonnet-autonomous sessions over CE docs / the implementation / the library data) → J-E1 → V1b
authoring (3 interactive sessions: ontology-via-sharp-cases freezing C-ONT, remaining adjudications +
layers 2–3, rendering + integration) → v1, which satisfies this arc's J2 gate.**  ~3 sessions
remaining to v1 (V1a done 2026-07-23; V1b sharded to `docs/PLAN.md`).  Post-v1 application shards
(sidecar case-IDs, removing the concerto gate (SEL-11 overturned, operator 2026-07-23),
composite-tag grammars) coordinate with R6d so the library re-derives once.  Register: generative
authoring on the Fable model (resolved 2026-07-22; no @dialectic handoff).  → `docs/ROADMAP-styleguide.md`;
`docs/NOTES.md` charter; BACKLOG A-c (superseded).

Ends at **J2 — the naming-policy freeze (FIRED 2026-07-30; verdict in the junctures table and
appendix)** (uniform-ceiling/ragged-floor already converged; A-b/A-c closed here).  → BACKLOG Act II
sections.

### R5 — Operational drain of `Original/`  (operator loop on hades; no agent sessions)

Bach Edition remainder is runnable today; each R3 adapter unlocks another census class.  Interleaves
with dev work throughout.  Exit condition: `Original/` empty — this is Act I's "done".

### R6 — Act III-a one-pass re-derivation  (gated on R5 exit + J2; ~5-8 sessions)

- **R6a** Hierarchy-depth normalisation (W3b/L2 uniform-ceiling clamp).  **DONE 2026-08-12**
  (`docs/PLAN.md` 3/3 rows; commits `4fe4025` clamp + `4de8c70` caller-threading + `73e11ba`
  census/anneal, ledger `fbb3c72`).  Froze **C-W3b-INT** (the `build_dest_path` depth-clamp
  interface: `work_group_modal_depth()` helper + a `group_modal_depth` parameter; corner pins
  tie→shallower / PL=0-orphans-excluded / all-orphan→0; the `min()` clamp is down-only, so no
  gap-vs-faithful discrimination and **no MB call** at render).  New ingests render clamped now;
  `run`/`repath`/`regroup` all compute and pass the modal depth (ingest/maintenance parity KAT).
  The destructive library-wide depth-repath rides R6d's one J3-gated pass (D-A5 precedent).  ◆
  `still-on-intent`.  **Fresh scan folded up:** 12,148 FLACs / 3,509 groups / **63 non-uniform
  (1.8%)** — supersedes the stale "36-group / 3.6%" figure; the six-shape taxonomy held (the reopen
  trigger did not fire); new Shape-D groups Wagner Ring Karajan {1,2,3,4} and Ring Solti {2,3,4}
  are both handled correctly by the min-clamp.
- **R6b** Catalogue-colon part-label retro-fix — **DONE 2026-08-13** (`docs/PLAN.md` 3/3 rows;
  commits `1f5d76a` detect+re-derive substrate / `317bf46` offline repatch pass / `16d31db`
  scan+census+anneal, ledger `bc497af`; ◆ `still-on-intent`).  **Not a paths-only repath:** the
  corruption is in embedded `CWP_PART_*` / `CWP_GROUPHEADING` **tag content**, which `build_dest_path`
  renders verbatim (`_tags.py:1414`) — so `repath` alone re-renders the corrupt `NN - 31` label.  The
  shard built + froze **new offline tag-content-repatch machinery** (detect a bare-catalogue
  `CWP_PART`; re-derive offline via `strip_common_prefix(CWP_WORK_i, CWP_WORK_{i+1})` — **no MB
  call**; rewrite the tags + rebuild `CWP_GROUPHEADING`, modelled on the `enrich`
  re-tag→verify→journal provenance chain), proven on fixtures via the src/tests gate.  **Froze
  C-CAT-INT** (`rederive_part_label` + `is_catalogue_colon_corrupt` + the `CANNOT_RECOMPUTE` sentinel
  in `_works.py`; the self-certifying catalogue-colon signature `_old_bare_colon_split`).  **Code-only:
  the destructive library-wide repatch rides R6d's one J3-gated pass (D-A5 precedent)** — and this
  machinery *closes the R6d tag-content gap* (below) for the catalogue-colon case: R6d gains a
  tag-content-repatch capability its paths-only engine currently lacks.
- **R6c** AcoustID tag naming + semantics — Picard alignment — **DONE 2026-08-13** (`docs/PLAN.md`
  4/4 rows; commits `a30ed93` policy freeze / `bd8a0b6` forward-write / `0004016` offline repatch /
  `4ec6907` scan+census+anneal, ledger `150da82`; ◆ `still-on-intent`).  **Froze C-ACID.**  Two
  persisted-tag migrations, both **code-only** (destructive library-wide repatch rides R6d, D-A5).
  **The two sub-questions were decided at PLAN derivation** against the live
  substrate (survey 2026-08-13): (1) **`ACOUSTID_ID` value source unified on the fingerprint
  `/v2/lookup` cluster UUID everywhere** (Picard-exact) — the survey corrected BACKLOG's premise:
  `fetch_acoustid_id`'s `list_by_mbid` value is *already* a cluster UUID (its own docstring,
  `_mb_api.py:1084`), and fpcalc + the `/v2/lookup` call already run at ingest (`_pipeline.py:1217,1226`,
  the cluster UUID currently *discarded*), so the switch adds **no new fpcalc dependency**; the S1
  inflection rules the no-api-key/no-fingerprint fallback (empty at ingest, not `list_by_mbid`
  retain).  (2) **`CHROMAPRINT_FP` → `ACOUSTID_FINGERPRINT` rename** with a dual-read transition (read
  both keys; R6d migrates existing files).  Freezes **C-ACID**.
- **R6d** Full-library repath under the frozen heuristics — the "more like itself" pass.  Gated by
  **J3**.  **J3-preflight DONE 2026-08-14** (`docs/PLAN.md` 4/4 rows; commits `ac9ddb7` DryRunPlan
  model + pass widenings / `d80cd38` composition harness / `9233438` CLI wiring / `05192f1` live run
  + J3 evidence artifact, ledger `df871f7`; ◆ `still-on-intent`).  **Froze C-PREFLIGHT.**  Live
  evidence (2026-08-14): 9,009 `unify` moves planned; `repath`/`regroup`/`enrich`/`repatch` all 0;
  no cross-pass overlaps; journal 47,559 → 56,568 entries projected; `Reference/` present at ~399 GiB.
  J3 is now decidable — the go/no-go input is produced; R5 exit (Original/ drained, operator-paced)
  is the remaining precondition.  A consolidated dry-run harness over the deferred passes
  (`repath`/`regroup`/`unify` + the R6b/R6c tag-content repatches) produces the three J3 evidence
  categories (dry-run change-set, journal capacity, `Reference/` retention support) without touching
  the live library.  Freezes **C-PREFLIGHT** (the typed dry-run-plan return shape).  J2 folded in (2026-07-30): the III-b rg-multi-release regroup (live hades scan
  prerequisite — R4b census) and the styleguide A-shards that reshape persisted tags/paths
  (concerto-gate deletion, REND-14 reorder + naming realignment, chorusmaster-into-`CONDUCTOR`,
  `IS_CLASSICAL` conditionalisation) land before or with this pass so the library re-derives once.
  **The four A-shards are DONE (2026-07-31, `docs/PLAN.md` 4/4 rows; commits `6eaedaa`
  concerto-gate deletion / `4d90566` REND-14 reorder + naming realignment / `b553f65`
  chorusmaster-into-`CONDUCTOR` / `e0b9f54` `IS_CLASSICAL` conditionalisation).**  They landed ahead
  of R6d as pure code+test grammar fixes — logically independent of the destructive repath; R6d now
  re-derives against already-corrected code.  Froze **C-NOSOLO** (no soloist enters the path
  component) and **C-RA-GRAMMAR** (recording-artist composite: billing order under
  `CEA_RECORDING_ARTIST`, no rename).  ◆ boundary `still-on-intent`.  **R6d planning caveat (stale docs):**
  `census-impl.md` / `NOTES.md` still describe the now-deleted `cea_album_soloists_unified`
  concerto-injection path rule — refresh before R6d consumes the census so R6d planning does not
  read a superseded rule.  **R6d unify-invocation caveat (2026-08-19; RESOLVED 2026-08-19, commit
  `2161dae`):** the destructive `unify` pass requires the `--user-agent-email` plumbing, now landed
  by the pre-R6d fix session; `unify`'s canonical name-form dereference is a permitted stable
  fixed-MBID lookup, not a `MB(*)` wildcard.  R6d's destructive run must supply the user-agent.
  **R6d BLOCKER caveat (extension-loss — surfaced 2026-08-20, `docs/PLAN.md`):** a live sweep found
  7 audio files written with no `.flac` suffix (Brahms variation sets, 1996 box) — single-track works
  whose over-long leaf name had its extension eaten by the truncation path (`_proposed_short`'s
  ellipsis strategies cut `_NAME_MAX - 3` bytes with no suffix-awareness, after `with_suffix` had
  already attached `.flac`).  Every maintenance pass gates on a `.flac`/`.mp3` suffix, so these files
  are stranded — silently skipped by `repath`/`regroup`/`unify` (`repath_unsupported_format`).  R6d's
  destructive re-derivation would leave them behind — a lossless-principle violation.  **Must clear
  before R6d:** fix the source truncation to reserve+preserve the suffix (stem+suffix ≤ `_NAME_MAX`),
  and add a `repath` repair case that identifies extension-less track files, re-suffixes them, and
  moves them through the C-PROV chain.  Sharded to `docs/PLAN.md` (pre-R6d fix, 2 rows).
  **R6d BLOCKER caveat (top-dir loses the performer — surfaced 2026-08-20, `docs/PLAN.md`):** the same
  live preflight sweep found thousands of `repath` top-dir renames replacing a real performer credit
  with a release/edition/album title.  **Two distinct mechanisms:** (1) `_top_dir_component`'s
  compilation branch (keyed on the free-classification parameter `releasetype_secondary`) emits
  `<…> - <ALBUM>` (the Karajan "Ouvertüren" case, ~low hundreds); and (2) — the **dominant class**
  (thousands: `Mozart - Complete Mozart Edition`, `Bach - Bach Edition`, `Schubert - The String
  Quartets`) — the composer is present (single-composer Case 3) so the top dir is
  `<composer> - <performers>`, but on repath the `<performers>` component collapses through
  `build_dest_path`'s fallback chain to `ARTIST` (`_tags.py:1216`), which for box-set recordings
  carries the edition title.  The current on-disk paths already show the correct performer, so this is
  a repath re-derivation loss (`_hydrate_performer_lists` / fallback), not an original-ingest error.
  The compilation-branch behaviour (mechanism 1) was frozen two ways at once — REND-23/C-INIT marked
  it *deliberate*
  (`census-impl.md`), while C-UNIVERSAL's rationale (`NOTES.md`) forbids album name and
  free-classification parameters in the path.  **Operator resolved the conflict 2026-08-20 (editorial
  authority): C-UNIVERSAL governs; REND-23 overturned — album name belongs to the playlist lens only,
  and the general library taxonomy is uniform-strict on composer(s), soloist(s), conductor(s),
  ensemble(s), rec/rel year, work taxonomy.**  R6d's destructive re-derivation would otherwise bake
  the album-name-in-path layout across the whole library.  **Must clear before R6d:** restore the
  `<composer> - <performers>` shape and the performer-led floor (drop the album-name injection; stop
  keying on `releasetype_secondary`).  Sharded to `docs/PLAN.md` (Sub-track C, 2 rows); the REND-23
  overturn is to be recorded in the styleguide validate-record.  **Soloist-in-path is a separate,
  operator-desired-but-deferred item (does NOT block R6d, NOT in the Sub-track-C shard):** see the
  C-NOSOLO reopen caveat below.
  **R6d planning
  caveat (paths-only vs tag-content — surfaced 2026-08-12,
  R6a shard):** the offline maintenance engine `repath`/`regroup`/`unify` re-derives **paths only**,
  from *embedded tags*, with **no MB network call** (`_pipeline_maint.py`).  So R6d's "one-pass
  re-derivation" as currently built re-paths the library under the latest `build_dest_path` policy
  (depth clamp, canonical name-forms, class/composer/work structure — all in one `repath`) but does
  **not** regenerate tag *content* (`CEA_*`, billing order, `IS_CLASSICAL`, …) from MB.  If R6d's
  intent includes tag-content re-derivation, that needs either a new offline tag-regeneration pass or
  a bulk re-`apply`/`search` — an explicit R6d PLAN-derivation scope decision, not covered by the
  existing repath engine.  (This is why the path-shaping code-only shards — canonical-name-forms,
  R6a depth — can land ahead of R6d: `repath` already re-derives their path output on demand.)
  **R6d tag-content gap — closing incrementally:** (a) **R6b (DONE 2026-08-13)** closed the
  catalogue-colon case — the first tag-content corruption with a *fully offline* re-derivation
  (`strip_common_prefix` over the embedded `CWP_WORK` pair, no MB call); the repatch pass
  (`repatch_catalogue_colon`, `CWP_PART_*` / `CWP_GROUPHEADING` rewrite on the `enrich` provenance
  chain) is built and R6d drives it destructively under J3.  (b) **R6c (DONE 2026-08-13)** added an
  AcoustID tag-content repatch (`ACOUSTID_ID` re-source + `ACOUSTID_FINGERPRINT` key migration via
  `repatch_acoustid_tags`) on the same chain — R6d drives it destructively under J3; the C-ACID
  dual-read retirement is deferred until the library is fully migrated (post-R6d).  Other
  tag-content re-derivation (`CEA_*`, billing order) still needs the explicit R6d scope decision above.
  **C-NOSOLO reopen — DECIDED (operator, 2026-08-20): reopen; soloist enters the path.**  The
  operator's path-ergonomics ruling lists the soloist among the elements the top dir should carry
  uniformly (composer(s), soloist(s), conductor(s), ensemble(s), rec/rel year, work taxonomy),
  explicitly reversing the earlier SEL-11 "universally added then universally removed" as *likely
  overengineered*.  This **overturns C-NOSOLO** ("no soloist enters the path component", frozen with
  the concerto-gate deletion, 2026-07-31).  **Sequencing (operator): deferred — "focus on completing
  the unfinished tasks; return and revise later if needed".**  So the soloist promotion is *not* in
  the pre-R6d Sub-track-C corrective shard (which restores "performer, not album" without touching the
  soloist), and is *not* a blocker.  It becomes a later A-node: define the soloist promotion rule +
  performer field set/order for the compact path (editorial — the Albinoni/concerto-grosso/
  choir+chorusmaster/modern-works hard cases the original R4c refutation named still apply and need an
  editorial rule, not an allowlist), then coordinate the code-only render change + the destructive
  library-wide repath with R6d's one pass under J3 (D-A5 precedent).  **When this lands, retire
  C-NOSOLO and update the styleguide SEL-11/REND records** so the frozen artifacts match the new
  ruling.
- **R6e** Conventions-spec finalisation (integrative writeup; consistently under-scheduled — allocate
  a full session minimum).

→ BACKLOG Act III-a sections and "Public conventions spec".

## Junctures

| Juncture | When | Adjudicates |
|----------|------|-------------|
| **J1** *(FIRED 2026-07-20)* | end of R0 | Census distribution → R3 order/pruning; rung-ladder shape for R2; not-in-MB default posture.  Verdict `still-on-intent` + `additive-reshard`; no destructive-HALT.  Outputs folded into R2/R3/pre-R3 nodes above and recorded in the appendix.  R2 shard proceeds against C-TIER. |
| **J2** *(FIRED 2026-07-30; REOPENED 2026-08-19; RE-FROZEN 2026-08-19)* | end of R4 + **styleguide v1** | **Taxonomy half re-frozen: C-CLASS refuted-and-deleted, superseded by C-UNIVERSAL (prefix-less universal top dir; the first path component is the scholarship-stable first-component shape, absorbing+generalising C-INIT); `IS_CLASSICAL` decoupled to the work-type predicate; the epistemic criterion pinned in NOTES.  Depth policy and the editorial half (styleguide v1) stand unchanged.**  Original freeze (2026-07-30): naming-policy `still-on-intent`, C-W3b graduated, editorial half = styleguide v1 by reference, C-S0 release-scoped.  A-b/A-c close; **Act II is done**.  Full adjudication + re-freeze in the Discoveries appendix. |
| **J3** *(UN-HALTED 2026-08-19; awaiting preflight re-run)* | before R6d | Go/no-go on the destructive-scale full-library repath: `Reference/` retention decision, journal capacity, dry-run evidence.  **The naming-policy re-freeze that halted J3 has landed (C-UNIVERSAL, commit `bec261d`).  The go/no-go input is the preflight re-run against the re-frozen policy — an operator step (`scripts/preflight_r6d.py` against live hades; the harness is built and green, C-PREFLIGHT).  The 2026-08-14 evidence is superseded; its change-set will be strictly larger (every former class-dir file now moves).** |

Post-R3, the **structural-audit trigger** fires (BACKLOG "Codebase maintenance cadence"): review the
coherence of the new module boundaries (adapters, rung substrate, `_net`) once settled.  Trigger-based;
stays in BACKLOG, referenced here for timing.  **Now fired-eligible as of the R3d ◆ (2026-07-21)** —
the R3 adapter arc has settled; the audit may be sharded whenever the operator elects it (it is off
the critical path, so R4/J2 was sharded first).

## Cross-session contracts

**Consumed (frozen — any invalidation is a destructive-HALT):** C-PROV / C-MOVE (move/verify/journal
provenance), C-L0 / C-L1 (leaf/intermediate numbering), C-S0 (aggregation spans media; mutation does
not), the defensive-download and confirmation-provenance invariants (repo `AGENTS.md`).

**Produced:** R1 freezes the `_net` core interface; R2 freezes the rung-marking contract; **the
naming policy froze at J2 (2026-07-30)** — C-CLASS/C-INIT ratified final, C-W3b graduated from
provisional (rule + sub-shape routing; interface mechanics resolve at R6a PLAN derivation), editorial
half = styleguide v1 by reference; R6 freezes the final path policy and externalises it as the
conventions spec.  Each sub-track PLAN names its own C-* contracts per existing convention.
**Amendment 2026-08-19: C-CLASS refuted-and-deleted; the taxonomy half re-froze as C-UNIVERSAL
(prefix-less universal top dir; the scholarship-stable first-component shape absorbs and generalises
C-INIT), commit `bec261d`.  `IS_CLASSICAL` derives from the work-type predicate (tag layer ≠ path
layer).  New prose contract: the epistemic criterion (defer to MB where variation is
scholarship-driven and converges; never let MB free-classification parameters define library
topology), pinned in NOTES.  Act II's taxonomy half is closed again.**

**Prose contracts:** the lossless principle (failure ≠ no-data), "path is a handle, not a manifest",
the layer-routing rule, "journal detects, tag adjudicates" — all in `docs/NOTES.md`; every PLAN
derivation re-reads them.

## Scope estimate (static frame; R3 tightened by J1 2026-07-20)

R0 2 ✓ · R1 3-5 ✓ · pre-R3 fix 1 ✓ · R2 3 ✓ · R3 9-10 · R4 (R4a 2 ✓ · R4b unsized; R4c dissolved) · R6 5-8
→ **~19-28 agent sessions** for this arc, plus the peer **styleguide arc ~3 remaining to v1**
(`docs/ROADMAP-styleguide.md`: V1a 3 · V1b 3; its post-v1 A-shards fold into R6d) and the operator-paced
R5 drain.  J1 tightened the R3 range from the provisional 8-16 to 9-10 on the
census distribution; the R3b survey (2026-07-20) then re-sized R3b 3→5, and R3d collapsed 3→1 at its
shard boundary (operator-override, no auto-reconciliation), giving **9-10**: pre-R3 fix 1 ✓ ·
R2 3 ✓ · R3b 5 ✓ · R3a 3 ✓ · R3e 1 ✓ · R3d 1 · R3c 0 (pruned).  The
clean-ingest adapters (R3a/R3b/R3e ≈ 107 dirs) dominate; R3d (18) is an operator-override gate (no
sub-class code fork); R3c (Discogs) is pruned to BACKLOG.

## Out of scope (stays in BACKLOG)

Act III-b (perpetual by definition); the **playlist library** (graduates to its own ROADMAP when Act I
nears completion — decided 2026-07-18); MB-upstream data edits and the editorial/scholarly track
(operator/research-paced); musicbrainzngs2 contributions (external repo, maintainer-paced); AcoustID
seeded-candidate extension, AccurateRip backfill, the local-accession ingest verb (C-LOCAL-ID;
design pinned in NOTES "Local accession identity"), and misc items (trigger- or dependency-based).

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

  **RESOLVED at R3d shard boundary (2026-07-21):** the sub-classification does **not** drive two
  adapter code paths after all.  Operator policy (a count mismatch needs physical-medium
  verification) makes both sub-classes share one outcome — surface the discrepancy for operator
  accept/override, never auto-reconcile.  R3d therefore needs **no** multi-disc reconciliation code;
  the edition-vs-structure distinction survives only as diagnostic display context in the override
  prompt.  The multi-disc-MB structure cases are handled by the operator against the physical media
  (R5), exactly as edition mismatches are.  Durable axiom (NOTES prose-contract): structural /
  physical-media disagreements are owned by the operator layer — the annotator surfaces-and-defers,
  never guesses.

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

- **R3b boundary (2026-07-21) — ISRC is an unwired tier signal (static-frame fact for R3a/R3e).**  The
  R3a survey found ISRC identity machinery already present (`_isrc_matches` = identity rung 1) but **not
  connected to the annotation-tier signal**: `run()`'s census-signal ladder (`_pipeline.py:1729–1735`)
  promotes on TOC-match or embedded-MBID else falls to `SEARCH_HIT`, so an ISRC-matching download lands at
  `mb-search-resolved` + `needs_spot_check`.  Static-frame consequence: the ISRC→tier promotion is R3a's
  core deliverable (C-ISRC), and the **recognition signature (per-track ISRC presence) is broader than
  Presto** — R3e (other-download/amazon, 19 dirs) will likely carry ISRCs too, so C-PRESTO's recogniser is
  reusable by R3e and R3e may collapse further into R3a's mechanism than J1 estimated.  Additive, no
  contract invalidated.

- **R3b boundary (2026-07-21) — the `CensusSignal` enum is the extensible seam, `AnnotationTier` is
  frozen.**  C-TIER froze the *tier vocabulary* (`AnnotationTier` + `ANNOTATION_TIER_ORDER` +
  `classify_annotation_tier`'s signature), but the `CensusSignal` enum and the classifier's match arms are
  the designed growth points (R2 left `alternate-source` signal-less by intent).  New identity evidence
  (ISRC now; Discogs later at R3c) enters by adding a `CensusSignal` + a mapping arm — **never** by editing
  `AnnotationTier`.  An adapter that appears to need a new *tier* (not a new signal) is a destructive-HALT
  signal that C-TIER was mis-frozen.  Durable through all remaining R3 adapters.

- **R3a boundary (2026-07-21) — the ISRC-presence recogniser never distinguished Presto from
  other-download (static-frame fact resolving R3e).**  C-PRESTO's runtime recogniser (`is_presto_dir`)
  keys on ISRC-presence alone; the census's presto-vs-other-download axis keys on booklet-PDF presence
  (offline only) — the two never aligned.  Since no Presto-specific runtime artifact was confirmed
  across the 36 R3a dirs, `origin_source="presto"` was in fact a *generic ISRC-bearing-download* label
  all along.  Static-frame consequence: R3e is not a new adapter but a **label-truthfulness rename**
  (`"presto"`→`"download"`), and J1's "R3e may collapse into R3a as a source-variant" is CONFIRMED — the
  collapse is total (same recogniser, same ladder, honest label).  Additive-reshard; no compiler contract
  touched (`origin_source` is free-form `str`, not an enum).  Resolved by PLAN R3e (freezes C-DL,
  supersedes C-PRESTO's label).

- **R3a boundary (2026-07-21) — wrong-pressing false-promotion watch item (forwarded to R5 drain).**
  ISRC-promoted dirs become `full-mb-verified` + `needs_spot_check=False`, exiting the spot-check
  population.  Because an ISRC identifies a *recording* (not a *release*), a dir on a different pressing
  that shares recordings with the reconciled release could in principle over-promote.  R3a gated this by
  matching against the *selected medium's* recordings (not bare presence), which is the correct guard,
  but the residual risk (same recording legitimately on multiple releases) is a **static-frame watch
  item for the R5 operator drain**: if the drain surfaces wrong-pressing full-verified entries, tighten
  the C-ISRC evidence rule (additive-reshard) or reconsider whether ISRC-match alone licenses
  `full-mb-verified` (destructive-HALT on C-ISRC).  Durable through R5 and Act III-b.

- **R3d boundary (2026-07-21) — C-OVR froze the positional-min-k ingest rule + a two-protocol
  surface.**  An accepted track-count mismatch copies exactly `k = min(n_src, n_medium)`
  positionally-aligned tracks at `mb-partial` (three positional loops in `run()` would `IndexError`
  otherwise); the dropped tail is neither copied nor journaled (operator owns the discrepancy).
  `confirm_count_mismatch` lands on **both** `DiscUI` (`_pipeline.py`, the callable seam) and
  `DiscoverUI` (`_discover.py`, the full surface) — the deliberate structural-subset split that avoids
  the `_discover → run` circular import; every test double for either protocol must implement it.
  Static-frame consequence: R5's operator drain surfaces accepted `mb-partial` entries via `audit`;
  any Act III-b re-derivation must preserve the min-k determinism.  No contract invalidated — wires
  C-TIER's existing `MISMATCH` signal unchanged; closes the R3 code arc.

- **R4a shard boundary (2026-07-21) — the non-classical population the taxonomy must admit is thin
  (static-frame fact tightening R4a).**  Of the 15 `non-classical-other` census dirs, the operator has
  already elected to manually move most out (`Audiobooks`, `GarageBand`, `Lydia *`, `nachtmusick`,
  `Playlists`, `Into The Woods`, `Caro mio ben`, `LDS Youth Music`).  The genuine "must be housed by
  the top-level class scheme" residue is small — audiobook/spoken-word (`Aesop_Fables`),
  children's-pop (`Kidz Bop` ×2, `Education`), new-age (`HypnoBirthing`), and the aggregate
  `Amazon Music` dir.  Consequence: R4a's top-level class scheme must exist and be principled (full
  inclusion is the north star), but it is **designed against a thin live non-classical population**,
  not a large one — the LoC-style "class for everything" is a durable design frame, not a large
  immediate migration.  For the R4a substrate session to weigh.

- **R4a shard boundary (2026-07-22) — R4c under-scoped; the editorial basis is a J2-gating node
  (promoted to E).**  Sharding R4c surfaced that "small additive concerto-soloist allowlist" is a
  symptom of a *missing editorial principle*, not a missing list entry.  Operator reframe: performer
  attribution is a general policy (three categories soloists→conductors→ensembles, the audible-credits
  analogy; hard cases — Albinoni Adagio organ-vs-violin, concerto grosso, orchestra+independent-choir
  chorusmaster, modern works attributed to the ensemble, ensemble+guest-soloists — that have no
  mechanical answer, proven editorial by cross-release attribution variance).  Two principles (coherence
  across surfaces; a generative well-designed styleguide) become the CE-replacement basis.  Static-frame
  consequence: **R4c dissolved, node E added and promoted to the R4-tail critical path (gates J2), R4b
  parallel.**  Register is @dialectic (generative authoring), not sharding — no PLAN.md.  Full charter
  captured to NOTES.  (CAPTURE-CANDIDATE surfaced and folded here at the boundary.)

- **J2 adjudication (2026-07-30) — verdict `still-on-intent`; naming-policy freeze granted.**  Both
  halves cross-validated with no conflict (R4b census: no C-CLASS/C-INIT conflicts; styleguide S6:
  layer 4 describes, never redefines).  (1) **Taxonomy**: C-CLASS + C-INIT ratified as final
  naming-policy components.  (2) **Depth policy**: C-W3b graduates from provisional — the
  uniform-ceiling/ragged-floor rule is doubly grounded (NOTES "two durable rules" + STYLEGUIDE 4.5,
  independently converged), so the rule, the two-sub-shape routing (data-gap stays shallow and
  visible; faithful over-resolution clamps), and the corner pins (modal ties → shallower; PL=0
  orphans excluded) freeze; the `build_dest_path` interface mechanics (`depth_clamp` posture) and
  tag-data-sufficiency resolve at R6a PLAN derivation, and R6a's fresh `scan_nonuniform_depth.py`
  run stays gating (the 36-group census is stale by construction; a new shape the rule mishandles is
  a reopen trigger).  (3) **Editorial signals**: defined by STYLEGUIDE v1 by reference — billing
  order (4.2), the SEL-11 overturn (soloists never in the path; the concerto injection is
  policy-dead as of this freeze, code-alive until its deletion shard), date-basis visibility
  (REND-24), path-is-a-handle, case-ID sidecar marking (5.5).  Consequence: the post-v1 A-shards
  that reshape persisted tags/paths (concerto-gate deletion, REND-14 reorder + naming realignment,
  chorusmaster-into-`CONDUCTOR`, `IS_CLASSICAL` conditionalisation) are R6-planning inputs,
  sequenced before or with R6d so the library re-derives once.  (4) **C-S0 ruling (operator, option
  A)**: C-S0 stays release-scoped; the rg-multi-release shape consolidates via the III-b regroup
  folded into R6d planning, with the live hades scan as the authoritative prerequisite — the
  RG-aggregation semantics are unspecifiable on documentary evidence (an RG also groups pressings:
  the Wagner Ring instance is a dedup question, not aggregation).  Extending C-S0 remains available
  as an R6-planning decision if the live scan shows the shape pervasive in `Done/`.  No frozen
  contract invalidated; no destructive-HALT.  A-b/A-c close; Act II is done; R6 unblocks for PLAN
  derivation (R6d gated on J3 + R5 exit, unchanged).

- **A-shards ◆ boundary (2026-07-31) — post-v1 styleguide application complete; grammar matches v1.**
  The four tag/path-grammar shards enacting STYLEGUIDE v1 landed ahead of R6d (`docs/PLAN.md` 4/4;
  see R6d node for commits).  Froze C-NOSOLO and C-RA-GRAMMAR.  Two static-frame facts for R6d
  planning: (1) the **standing-rule-2 naming-drift hazard is resolved** — the S2 inflection juncture
  found the queued premise imprecise (CE's `_cea_recording_artist` *is* the assembled composite; the
  verbatim credit is `CEA_MB_ARTISTS`), so the composite stays under `CEA_RECORDING_ARTIST` with **no
  library-wide tag rename** at R6d; (2) **stale census/NOTES docs** still reference the deleted
  `cea_album_soloists_unified` field — a doc-freshness item to clear before R6d consumes the census.
  No frozen contract invalidated; no destructive-HALT.  R6d re-derives against corrected code.

- **R4b ◆ boundary (2026-07-23; folded up 2026-08-12) — C-S0 is release-scoped, not release-group-scoped
  (R6d-planning input).**  The fragmentation census (`docs/census-fragmentation.{md,json}`) confirmed that
  C-S0 aggregates media *within* a release (album MBID), **not across releases in a release-group**, so box
  sets modelled by MB as multiple releases fragment across top_dirs despite C-S0 — 5 such rg-multi-release
  candidates found (documentary census; hades unmounted, D-A2 posture), all **routed to III-b regroup**.  This
  is the same shape J2 already ruled on (C-S0 stays release-scoped; the rg-multi-release regroup folds into
  R6d planning with the live hades scan as the authoritative prerequisite).  Static-frame consequence for R6d:
  the III-b regroup pass must handle rg-multi-release consolidation, and the **live hades scan is load-bearing**
  (the Furtwängler-style partial-ingest scenario — some discs already in `Done/` — means the documentary count
  is a floor, not the truth).  Not an in-arc contract change (D-4); an R6d-planning input.  No destructive-HALT.

- **C-UNIVERSAL re-freeze ◆ boundary (2026-08-19) — taxonomy half re-frozen; styleguide-sync debt
  queued (static-frame delta).**  The naming-policy re-freeze that the C-CLASS refutation demanded
  landed as a single-row code sub-track (`docs/PLAN.md`, ledger done; commit `bec261d`).  Deleted
  `_top_level_class`; generalised+renamed `_classical_top_dir` → `_top_dir_component` (logic
  unchanged — a pop album is the performer-led branch); collapsed the class-prefix assembly in
  `build_dest_path`; rewired `IS_CLASSICAL` to the CE-classical work-type predicate.  Froze
  **C-UNIVERSAL** (replaces C-CLASS, absorbs+generalises C-INIT) + the **epistemic-criterion prose
  contract** (NOTES).  Static-frame facts folded up: (1) **D-2 collapse-equivalence resolved as an
  intended behaviour change** — the old Soundtracks bare-`<album>` arm folds into the performer-led
  `<albumartist> - <album>` shape; editorial-class distinctions relocate to the playlist lens per the
  two-lens principle (no shape silently lost — the executor diffed the arms).  (2) **Styleguide-sync
  debt queued (not yet paid):** durable prose still describes the deleted C-CLASS as a live frozen
  contract — `models.py:1385` (misstates the `IS_CLASSICAL` basis), `_tags.py:1156` (renamed helper),
  STYLEGUIDE 4.5 / REND-21 / REND-22 / REND-23, and `census-impl.md` 5.1 / 5.2.  Deferred out of the
  code-freeze session by design (the code gate does not block on prose sync); sharded as a thin
  documentary-accuracy follow-on.  No frozen contract invalidated; no destructive-HALT.  **The
  styleguide-sync follow-on is DONE (2026-08-19, commit `02a9c83`; froze no new contract) — see the
  styleguide-sync ◆ entry below.**  The J3 preflight re-run is the next *operator* step (un-halted;
  live-library, not an agent session).

- **R6b ◆ boundary (2026-08-13) — catalogue-colon retro-fix machinery landed; live-population re-check
  still owed (R6d-planning input).**  R6b froze C-CAT-INT and built the offline `repatch_catalogue_colon`
  pass, code-only (destructive run rides R6d).  Static-frame fact for R6d planning: the **live-population
  re-check is still owned by the operator** — the "1 release fired / ~16 latent" figure is stale by
  construction (BACKLOG:255), and R6b's S3 scan runs against a mounted library the operator must supply
  before R6d's destructive pass.  The detect-by-disagreement predicate is self-certifying (fires exactly
  where the old bare-`":"` split fired), so a census signature where a *correct* label disagrees with its
  recomputation remains the forward-fix reopen trigger — carried to R6d, not re-opened in-arc.  No frozen
  contract invalidated; no destructive-HALT.

- **R6c ◆ boundary (2026-08-13) — AcoustID Picard-alignment machinery landed; two R6d-planning
  inputs.**  R6c froze C-ACID and built the offline `repatch_acoustid_tags` pass + the forward-write
  alignment, code-only (destructive run rides R6d).  Static-frame facts for R6d planning: (1) R6d's
  one-pass now drives a **third** tag-content repatch (AcoustID key migration + `ACOUSTID_ID`
  re-source) alongside R6b's catalogue-colon pass, on the same `enrich` re-tag→`_verify_copy`→journal
  chain; (2) the C-ACID **dual-read (read both `ACOUSTID_FINGERPRINT` and legacy `CHROMAPRINT_FP`) is
  retained until the library is fully migrated** — its removal is an explicit post-R6d decision, not
  an in-R6d step.  `fetch_acoustid_id` survives as an exported helper (no longer the `ACOUSTID_ID`
  source).  No frozen contract invalidated; no destructive-HALT.  **Next shardable slice:** with
  R6a/R6b/R6c all landed as code-only machinery, the remaining R6 code work is R6d's destructive
  one-pass (gated on J3 + R5 exit) — so the immediate agent-shardable target is the **R6d J3-preflight
  sub-track** (a consolidated dry-run harness producing J3 go/no-go evidence; sharded 2026-08-13,
  `docs/PLAN.md`), which advances J3 without touching the live library.

- **Styleguide-arc node-A tail (2026-08-12) — no shardable styleguide sub-track remains.**  With
  `path-canonical-name-forms` done (C-CANON), the styleguide arc's node A has exhausted its three enumerated
  application shards (editorial-notes field / composite-tag grammar / normalisation — done or discharged); its
  other post-v1 nodes are operator/trigger-paced (P = R6e here; C = CEv3, own future roadmap; L = perpetual
  loop).  Consequence: the sole live agent-shardable frontier is this arc's **R6** (Act III-a), and R6b/R6c
  are runnable now on `Done/` (operator-confirmed 2026-08-12) — only R6d is J3-gated.  R6a (depth
  normalisation) is **done** (2026-08-12); R6b (catalogue-colon retro-fix) is **done** (2026-08-13);
  **R6c (AcoustID Picard alignment) is sharded next** (`docs/PLAN.md`, 2026-08-13).

- **R6d-preflight ◆ boundary (2026-08-19) — `unify` makes a narrow, stable MB name-form lookup;
  the "offline passes make no MB call" property is refined, not restored (pre-R6d fix input).**
  Running the `unify` dry-run surfaced that the standalone `unify` subcommand cannot be invoked:
  it lacks the `--user-agent-app`/`--user-agent-email` flags and the `init_mb()` call that
  `apply`/`search`/`preflight` carry, and dies with a `musicbrainzngs.UsageError` on the first
  artist.  Root cause: C-CANON (canonical name-forms) added a `fetch_artist_aliases(mbid)` call
  inside `build_dest_path`, so `unify`/`repath`/`regroup` — which the R6a node recorded as making
  "no MB network call" — now dereference each embedded artist MBID for its primary-flagged canonical
  alias.  **Operator ruling (2026-08-19):** this is acceptable and does *not* violate the
  determinate-transition principle.  `unify` must effect a determinate A → A′ specified over the
  current library state; what is forbidden is folding in `MB(*)` — a wildcard of new/volatile MB
  data whose answer drifts with MB's catalog (search, re-identification, relationship refetch).  A
  fixed-MBID dereference of an entity's own stable primary name-form is a narrow, well-defined,
  cached lookup of data that simply isn't local yet — not a wildcard.  Static-frame consequence:
  the R6a "offline maintenance passes make no MB call" property is **refined** to "make no
  *wildcard* MB call (no search / no re-identification); a stable fixed-MBID name-form dereference
  is permitted."  Resolution: a **pre-R6d fix session** (`docs/PLAN.md`) wires `unify`'s `init_mb`
  plumbing and corrects the subcommand's false "No MusicBrainz network calls are made" epilog to
  state the true A → A′ posture.  No frozen contract invalidated (reaffirms C-CANON's network
  posture; does not re-freeze it).  No destructive-HALT.  **Residual reopen trigger:** if strict
  determinism is later required (an MB primary alias drifting between ingest and `unify` re-paths a
  dir), persist the canonical form as an embedded tag at ingest and read it offline — deferred, not
  this shard's scope.

- **Operator refutation (2026-08-19) — C-CLASS refuted; destructive-HALT; J3/R6d halted pending a
  naming-policy re-freeze.**  The top-level class dirs (`Classical/`, `Popular/`, `Compilations/`,
  `Soundtracks/`, `Spoken Word/`, `Unsorted/`) rendered by `_top_level_class` (C-CLASS, frozen at
  R4a, ratified final at J2) are refuted by operator judgment: they are derived from MB's
  **entropic free classification parameters** (release-group primary/secondary types, the
  is-classical predicate), which are crowd-set, inconsistently applied, and unanchored — so the
  topmost, most topology-defining path component was built on MB's *noisiest* data, scrambling the
  library topology (the same programme lands under different classes by MB editor whim).  **The
  epistemic criterion (operator, to be pinned as a NOTES prose contract at the re-freeze):** defer
  to MB where variation is *scholarship-driven* and converges (composer identity, recording dates,
  catalogue facts); never let MB's free classification parameters define library topology.  The
  class partition also violated the arc's own two-lens principle (filesystem = catalog, playlists =
  reading room) by baking editorial categorization into the catalog lens — all editorial views
  belong in the playlist lens (charter: `docs/ROADMAP-playlists.md`, 2026-08-19).  **Replacement
  direction (to be adjudicated, not yet frozen):** a universal top dir; scholarship-stable path
  components (composer, work, dates, performers); C-INIT's initial-component rules may survive
  generalised — its fate, the `IS_CLASSICAL` tag's fate (tag layer ≠ path layer), and the migration
  of already-enacted class dirs in the live library are the re-freeze session's scope.
  **Consequences:** (1) **J2 is partially reopened** — C-CLASS un-ratified; C-INIT open; the
  editorial half (styleguide v1) is untouched.  (2) **J3 and R6d's destructive pass are HALTED** —
  the 2026-08-14 preflight evidence is stale by construction (the 9,009 `unify` destinations embed
  class dirs; the re-frozen policy's change-set will be strictly larger since every class-dir file
  moves), and running now would enact dead policy and force a second library-wide re-derivation,
  violating the re-derive-once intent.  Preflight re-runs after the re-freeze.  (3) The pre-R6d
  unify-plumbing fix is DONE (2026-08-19, commit `2161dae`) — policy-neutral invocability restored.
  (4) The **playlist library graduates** to its own roadmap now (its trigger effectively fired; the
  operator delivered its charter).  Next agent-shardable step: the **naming-policy re-freeze
  adjudication** (a dedicated interactive session; J2-reopening freeze — fresh deliberate act, not
  a session-tail decision).

- **styleguide-sync ◆ boundary (2026-08-19) — durable-prose debt paid; the agent-shardable arc is
  now operator-blocked (static-frame delta).**  The styleguide-sync follow-on (`docs/PLAN.md`, 1/1
  row done, commit `02a9c83`) re-aligned durable prose to the C-UNIVERSAL freeze: `models.py` /
  `_tags.py` comments, STYLEGUIDE 4.5 / REND-21/22/23, and `census-impl.md` no longer describe the
  deleted `_top_level_class` / C-CLASS routing as live.  Froze no new contract (documentary
  re-alignment to already-frozen C-UNIVERSAL + the epistemic criterion).  With this and the pre-R6d
  unify-plumbing fix (`2161dae`) landed, **every agent-shardable node in the R6 code arc is done**
  (R6a–c machinery, the J3-preflight harness, the pre-R6d fix, the C-UNIVERSAL re-freeze, this
  sync).  Static-frame consequence: **the arc's remaining critical path is entirely operator-paced**
  — J3's go/no-go preflight re-run against live hades under the re-frozen C-UNIVERSAL policy (not an
  agent session), gated behind the R5 drain of `Original/` (operator loop).  R6d's destructive
  one-pass re-derivation follows both.  **R6e (conventions spec) is deliberately held** until after
  R6d so it describes final on-disk conventions rather than churning ones (BACKLOG timing note).
  **Off-critical-path agent work remains available but unshelved by operator election:** the
  structural-audit sub-track (fired-eligible since R3d) and an R6e *draft*.  No frozen contract
  invalidated; no destructive-HALT.  **Next step is operator, not agent:** drain `Original/`, then
  re-run the J3 preflight.

- **Operator design session (2026-08-19) — local accession identity for never-external releases
  (design pinned to NOTES; trigger-based shard).**  Full inclusion's floor is the custom album
  that will never be in MB or any datastore.  Design decided and captured as a NOTES prose
  contract ("Local accession identity"): a minimal required tag set at a catalog-gate ingest verb
  (reachability is derivational, so the integrity guarantee is an input gate on the path grammar's
  inputs), plus a locally-minted UUIDv4 in `MUSICANNOTATOR_RELEASEID` as the permanent accession
  join key — complementary to the MBID, never minted into `MUSICBRAINZ_ALBUMID`, namespace
  determined by provenance not inspection, C-TIER monotonic upgrade if MB identity ever arrives.
  Closes the design half of the gap that `apply`/`search` require an MB release and `run()` cannot
  produce `source-tags-only`; the ingest verb (working name C-LOCAL-ID) is a trigger-based shard,
  elected when the operator chooses to catalog never-external material.  No frozen contract
  invalidated; no destructive-HALT.
