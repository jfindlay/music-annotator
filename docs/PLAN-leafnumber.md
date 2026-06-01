# music-annotator — Plan: Leaf Numbering & Hierarchy-Depth Correctness

This plan is **session-sharded** for autonomous execution by `/run-plan`.  One row = one
`@build`/`@general` dispatch = one commit, ending green under `~/.local/bin/tox -m analyze`.
`@plan-deep` orchestrates, verifies each session contract, and dispatches `@committer`.  State lives
in the Progress ledger below, not in context.

It is **deliberately self-contained**: a `@build` session executing one row needs only this file plus
the code anchors it names — it should **not** need to load `docs/NOTES.md` or the sibling plans.  The
single cross-plan fact a session must respect is stated once in *Substrate already in place* below
(the post-S0 `tags_map`/`top_work_groups` shape); everything else is inline.

---

## Purpose (design intent)

Make the **leaf track number** and the **directory depth** correct for works whose MusicBrainz
representation puts more than one recording under a single bottom work, or varies hierarchy depth
across sibling movements of one work.  Two editorial invariants govern every decision:

- **Path is a handle, not a manifest.**  The leaf number and directory names are short, stable
  locators — they must sort in **playback order**, be **gap-free**, and be **uniform** across the
  siblings of one work.  Full per-movement detail lives in tags, not in the path.
- **A path-policy change is retroactive.**  Every already-annotated work on disk was built under the
  old policy.  Changing path construction is inseparable from a maintenance-mode re-path pass that
  re-locates and re-journals those works.  The fix is not ingest-only.

**Re-read this section at every ◆ sub-track boundary** (anti-defocus check).

---

## The bug, in one paragraph (so a session needs no other reading)

The leaf `nn` in `build_dest_path` is the bottom-work's `CWP_ORDERING_KEY_0` (`_tags.py:1064`,
`:1078`).  That is correct **iff each MB bottom work maps to exactly one recording**.  It breaks when
one MB bottom work contains several recordings — every sub-section of a movement (Mahler 9: 8
recordings of movement I, all `CWP_ORDERING_KEY_0 = 1`) or every recording of an opera scene (Wagner
Meistersinger: all of Akt I Scene I share `= 1`) gets the **same** leaf number.  Because titles no
longer collapse (the old truncating `safe_name` was removed in `9db47ab`), the destinations are no
longer byte-identical, so `_dedup_plan_entries` (`_pipeline.py:655`, fires only on identical paths)
**no longer triggers** — the `.dd` machinery is dead.  Net current output: many files all numbered
`01`, sorted alphabetically by subtitle instead of by performance order.  Separately, MB+CE assign
**non-uniform hierarchy depth** within one work (Handel Water Music Suite 1: most movements are
`CWP_PART_LEVELS = 2` flat files, but movement III has MB sub-parts so its recordings are
`CWP_PART_LEVELS = 3` and spawn an extra intermediate directory among the flat siblings).  The full
phenomenology and the clean Bach-Mass counter-example are in `docs/NOTES.md` "Leaf-numbering &
non-uniform-depth bugs" — load it only if a design question needs the raw evidence.

## Substrate already in place (the key enabling fact)

`run()`'s `top_work_groups` pass (`_pipeline.py:988-1003`) already enumerates every track of a
top-work group **in track order, across all media** (post-S0), assigning `cwp_movt_num` =
`movementnumber` = the 1-based position within the group (`:999`, `:1001`).  Verified against real
data: this enumeration is gap-free, playback-ordered, and disc-spanning — i.e. it is *exactly the
leaf index the fix needs*.  The grouping key is `cwp_workid_top or musicbrainz_workid`, so a
multi-work album (e.g. Handel's three suites share one top work → one group of 20) and a single
symphony (one group of N) both behave correctly.  **The substrate exists; the bug is that
`build_dest_path` consumes `CWP_ORDERING_KEY_0` for the leaf instead of `cwp_movt_num`.**

---

## Session list

`Cat` = A substrate / B algorithm / C maintenance / X writeup.  `T` = tier: O = Opus inflection
(orchestrator designs inline, HALT for sign-off); S = Sonnet `@build`.  ◆ = last session of a
sub-track.  `Dep` lists prerequisite sessions.  Table intentionally wider than 128 chars.

| #  | Title (commit-shaped)                                          | Cat | T | Dep      | Expected files                                                       | KAT |
|----|----------------------------------------------------------------|-----|---|----------|----------------------------------------------------------------------|-----|
| L0 | Leaf `nn` from per-group track index, not ordering-key         | A   | O | —        | `_tags.py`, `tests/unit/test_annotator.py`                          | `test_split_movement_leaf_sequential` |
| L1 | Uniform intermediate-dir numbering from per-group sub-index ◆  | B   | S | L0       | `_pipeline.py`, `_tags.py`, `tests/unit/test_annotator.py`, `tests/unit/test_pipeline.py` | `test_opera_scene_intermediate_dir_numbered` |
| L2 | Normalise hierarchy depth within a work-group **(DEFERRED)**   | B   | O | L0       | `_tags.py`, `tests/unit/test_annotator.py`                          | `test_mixed_depth_suite_renders_uniform` |
| L3 | Retire dead `_dedup_plan_entries` + `.dd` machinery ◆         | B   | S | L0,L1    | `_pipeline.py`, `tests/unit/test_pipeline.py`                       | `test_no_dd_suffix_on_distinct_titles` |
| L4 | `repath` maintenance mode: re-path annotated works             | C   | O | L0,L1    | `__main__.py`, `_pipeline_io.py`, `models.py`, `tests/unit/test_main.py` | `test_repath_moves_and_journals_legacy_layout` |
| L5 | Integrative writeup + invariants ◆                             | X   | S | L1,L3,L4 | `docs/NOTES.md`, `README.md`                                         | — (prose) |

### Sub-track boundaries

- **◆ Sub-track A — leaf & intermediate numbering** ends at L1.  Ships: every leaf and intermediate
  directory numbered by its position within the unified work-group, gap-free and playback-ordered.
- **◆ Sub-track B — dead-code removal** ends at L3.  Ships: the now-unreachable dedup pass is removed
  (or repurposed) so the path code has a single numbering authority.  **L2 (depth uniformity) is
  DEFERRED** (see the L2 note and the "L2 deferred" Discovery) — sub-track B no longer carries it.
  L3 depends only on L0/L1 (leaf numbering no longer routes through dedup once L0/L1 land; depth
  normalisation is not a precondition for retiring dedup).
- **◆ Sub-track C — retroactive maintenance** is L4 (single session, but Opus-designed because the
  move/journal/verify provenance is delicate).  Ships: a mode that re-paths already-annotated works
  to the new policy.  **L4 is the user-flagged hard requirement** — the fix is incomplete until the
  existing library can be brought forward.
- **L5** is the capstone: name the new invariants and document `repath`.

### Notes per session

- **L0 (Opus inflection — design inline, then HALT).**  Replace the leaf-`nn` source in both
  `build_dest_path` branches (`_tags.py:1064-1067` and `:1078-1081`).  Today the priority is
  `CWP_ORDERING_KEY_0 → MOVEMENTNUMBER → global_track_idx/track.position`.  The fix inverts the top
  two: the leaf must be the **per-group track index** (`cwp_movt_num`, exposed as `MOVEMENTNUMBER` in
  `file_dict`), with `CWP_ORDERING_KEY_0` demoted or dropped from the leaf entirely.  **Open design
  questions to resolve at sign-off:**
  1. Is `MOVEMENTNUMBER` (set from `cwp_movt_num`) sufficient as the sole leaf source, or is a new
     dedicated `TrackTags` field warranted (so the leaf is not coupled to the user-facing
     `MOVEMENTNUMBER` tag, which CE conventions may want to mean something else)?  Decide whether to
     widen the model (compiler-enforced contract C-L0) or reuse the existing field.
  2. Width: `MOVEMENTTOTAL`-driven width already exists (`_tags.py:1031-1032`).  Confirm it keys on
     the group total, not the medium total, post-S0.
  3. Does dropping `CWP_ORDERING_KEY_0` from the leaf regress the clean Bach-Mass case (one recording
     per bottom work, `ORDERING_KEY_0` happens to equal the group index)?  Verify the per-group index
     reproduces 1..27 there — it should, by construction, but assert it in a KAT.
  4. The `global_track_idx` ultimate fallback (`:1066`/`:1080`) stays as the no-work-hierarchy
     escape hatch.
  KAT `test_split_movement_leaf_sequential`: a single-work album where movement I has ≥3 sub-section
  recordings sharing one bottom work; assert leaves are `01, 02, 03, …` in `movementnumber` order
  with no repeats and no `.dd`.  Plus a Bach-Mass-shaped regression: distinct bottom works → leaves
  still `01..N` gap-free.
- **L1.**  The intermediate directories (opera acts, suite groupings — built at `_tags.py:1052-1057`)
  take their `nn` from `CWP_ORDERING_KEY_{i}`.  This has the same defect one level up when an
  intermediate node groups several children.  Apply the same per-group-index principle to each
  intermediate level: the `nn` for an intermediate dir must be that node's position among its
  siblings within the parent, gap-free.  KAT: Wagner-shaped 3-level opera — assert `02 - Akt I`,
  `03 - Akt II`, … and within an act the scene dirs number `01, 02, …`.
- **L2 — DEFERRED (decision at the 2026-06-01 HALT; do not implement yet).**  At the L2 Opus-inflection
  HALT the policy was *designed* (see the "L2 deferred — converged design" Discovery for the resolved
  rule) but the user elected **not to ship any depth-normalisation now**.  Rationale: the design choice
  between uniform-ceiling and any alternative is better made from a *maintenance position* once the
  library is complete and the full distribution of depth shapes is known, rather than from the current
  36-group census.  L2 is therefore parked; L3 and L4 proceed on `L0,L1` alone (depth normalisation is
  not their precondition).  The analysis below is retained verbatim as the inheritance for whoever
  resumes L2 — **read it together with the converged design in the Discovery before reopening.**
- **L2 (original brief — Opus inflection — design inline, then HALT).**  Decide and implement the
  depth-rendering policy for work-groups whose tracks carry non-uniform `CWP_PART_LEVELS`.  **A library census now
  exists (see "Non-uniform-depth census" appendix) — it changes the framing materially: "non-uniform
  depth" is SIX distinct shapes, and the dominant one is NOT a bug.**  The policy must discriminate by
  shape, not blanket-normalise:
  - **Shape A (20/36 groups) — overture/sinfonia/epilogue at PL=1 among PL=2 numbers — is CORRECT
    as-is and must be preserved.**  The Wagner *Vorspiel* and Mozart *Ouverture* legitimately sit at
    the top of the opera while arias nest under acts.  A blanket "flatten to shallowest" would wrongly
    pull every aria up to the overture's level; "promote to deepest" would bury the overture under a
    fake act.  **L2 must not touch Shape A.**  This is the key finding the census bought.
  - **Shapes C/D (3 groups: Handel Water Music, Bach Matthäus-Passion, Haydn Schöpfung) — the genuine
    target.**  A movement/number has MB sub-parts (IIIa/IIIb; lettered a/b/c recits) so its recordings
    nest one level *deeper than their flat siblings within the same section*.  This is the ragged-depth
    bug.  Candidate policies for this shape only: (a) **flatten** the extra sub-part level into the leaf
    so the suite/oratorio is a flat, scannable list (likely preferred — refract through "path is a
    handle"); (b) **threshold-promote** only movements with ≥2 sub-recordings.
  - **Shape B (9 groups) — mixed flat/split movements (some movements single-track PL=1, others split
    PL=2).**  Decide whether this is even in scope: it may be acceptable as-is (a split movement
    legitimately nests), or it may want the same flatten treatment as C/D.  Resolve at HALT.
  - **Shape E (2 groups: Mozart K.136, Litaniae K.243) — PL=0 ORPHANS are a different bug** (the
    movement's MB work has no `part of` link, so CWP resolves it as a standalone top work).  This is an
    MB-data-gap / hierarchy-resolution defect, NOT a rendering-policy question — **scope it out of L2**
    and log it as a separate Discovery (candidate: a follow-on `_works.py` fix, or an editorial
    allowlist).  Do not let it contaminate the L2 policy.
  - **Shape F (2 groups: Tannhäuser, Tristan highlights) — excerpt discs** where two tracks come from
    different hierarchy depths of the same opera.  Edge case; decide whether L2 handles it or defers.

  KAT: Handel-shaped suite (Shape C) renders uniform-depth with contiguous numbering and no gap where
  the nested movement was; **plus a Shape-A regression KAT** asserting the Wagner/Mozart overture
  stays at the top level and arias stay nested (the policy did not over-normalise).
- **L3.**  With L0-L1 in place (L2 deferred), `_dedup_plan_entries` (`_pipeline.py:655-697`, called at `:1147`) can
  no longer fire on legitimate split-works (distinct titles → distinct paths) and the `.dd` machinery
  is dead.  Remove it, or repurpose it strictly as a last-resort guard for *genuinely* byte-identical
  destinations (true duplicate recordings).  Keep the numbering authority single — do not leave two
  systems that can both renumber.  KAT: the Mahler/Wagner inputs produce no `.dd` filenames.
- **L4 (Opus inflection — design inline, then HALT).**  New `repath <dest_dir>` mode that walks the
  existing library, recomputes each work's destination under the new policy **from the embedded tags
  alone** (no MB fetch — the tags already carry `CWP_*`, `MOVEMENTNUMBER`, `CWP_WORKID_TOP`), and
  moves files to their corrected paths.  Critical: every move MUST append its own journal entry
  (new `action="repathed"`, recording old→new `destination`) or the library's provenance decays.
  Preserve the copy/verify discipline: re-path = move + re-verify tag round-trip, never a blind
  rename.  This is the join with the existing `audit`/regroup machinery in the multimedium plan —
  reuse `read_journal`/`_read_tags_*` patterns; do not re-derive them.  **Design at HALT:** dry-run
  vs apply, collision handling when two legacy layouts map to one new path, and whether `repath`
  reuses the `run()` collision policy.  KAT: a fabricated legacy-layout dir (old per-work-key
  numbering) → assert it is moved to new-policy paths and a `repathed` journal entry is written.
- **L5.**  Name the invariants in `docs/NOTES.md`: leaf = per-group track index; intermediate `nn` =
  per-group sibling index; one rendering depth per work-group; `repathed` journal obligation.  Update
  the NOTES "Leaf-numbering & non-uniform-depth bugs" entry to mark the bug fixed and point at this
  plan's resolved decisions.  Document `repath` in `README.md` if user-facing.

---

## Cross-session contracts

### Compiler-enforced

- **C-L0 — leaf-index source (FROZEN BY L0 — resolved at sign-off).**  `build_dest_path` derives the
  leaf `nn` from **`CWP_MOVT_NUM`** — the per-group, gap-free, playback-ordered, disc-spanning track
  index set in `run()`'s top-work-group pass (`_pipeline.py:1001`) — falling back to `global_track_idx`
  then `track.position`.  `CWP_ORDERING_KEY_0` is **not** used for the leaf.  **No new `TrackTags`
  field is added**: `cwp_movt_num` (`models.py:1196`, surfaced as `CWP_MOVT_NUM` via
  `to_file_dict()`) already exists and is reused.  The leaf reads the **CWP path-construction**
  vocabulary (`CWP_MOVT_NUM`), deliberately NOT the user-facing standard movement tag
  (`MOVEMENTNUMBER`), so the path stays decoupled from a tag CE conventions may later repurpose.
  Consumed by L1, L2, L3.
  - **Permanence (decided at sign-off).**  `CWP_MOVT_NUM` is the leaf authority **permanently**.  It
    is correct under *both* MB data shapes — before and after any upstream MusicBrainz submit-mode
    clean-up — because it is computed from the unified work-group enumeration, not from MB
    `ordering-key`.  A future MB-data correction that makes `CWP_ORDERING_KEY_0` per-recording-distinct
    does **NOT** license reverting the leaf to ordering-key: doing so would recouple path stability to
    remote-data quality and force a library-wide re-path on every upstream correction, violating
    *path is a handle, not a manifest*.  L3 and any later maintainer treat the per-group index as the
    single, permanent numbering authority.
  - **Site comment obligation.**  Both leaf sites must carry a one-line comment stating *why* the leaf
    reads `CWP_MOVT_NUM` and not `MOVEMENTNUMBER` (they hold identical values today but are distinct
    vocabularies — standard tag vs CWP path index — and may diverge).
- **C-L1 — per-group intermediate sibling-index (FROZEN BY L1 — added via re-shard).**  The
  substrate that made C-L0 a clean `_tags.py`-only change does **not** exist for intermediate levels:
  they carry only the raw MB `cwp_ordering_key_{i}` (`_tags.py:848`), which is non-gap-free and can
  collapse when one intermediate node groups several children.  `build_dest_path` is per-track and
  cannot see the sibling set.  L1 therefore adds a **pipeline-side enumeration** mirroring the leaf
  pass: within each top-work group (the existing `top_work_groups` loop, `_pipeline.py:994`), for
  each intermediate level `i >= 1`, rank the **distinct `cwp_workid_{i}` values that share a parent
  `cwp_workid_{i+1}`** by ascending `cwp_ordering_key_{i}`, assigning a **gap-free, 1-based**
  sibling index.  Store it as model_extra `cwp_inter_index_{i}` on every track of that node.
  `build_dest_path`'s intermediate-dir loop (`_tags.py:1059-1064`) consumes `CWP_INTER_INDEX_{i}`
  for the `nn`, falling back to the old `_nn(cwp_ordering_key_{i}, i)` only when the index is absent
  (no-group / no-hierarchy escape hatch).  Additive: no field is removed; `cwp_ordering_key_{i}`
  stays (still used for ranking and as fallback).  Consumed by L2 (depth normalisation reads the
  same per-group structure) and L3.
- **C-L4 — `TransactionEntry.action` gains `"repathed"` (WIDENED BY L4).**  Additive `str` value
  alongside the existing set; existing values and `str` typing unchanged.

### Test-enforced (KATs grow monotonically)

Each row's KAT must stay green at every later session.  The split-work KATs are the regression guard:
any change that reintroduces ordering-key-as-leaf breaks `test_split_movement_leaf_sequential`.  The
existing `build_dest_path` and dedup tests in `test_annotator.py`/`test_pipeline.py` must be updated,
not deleted, when L3 removes the dedup pass — convert them to assert the new single-authority
behaviour.

### Prose-enforced (invariants)

- **P-L1 — Leaf and intermediate numbers sort in playback order, gap-free, per work-group.**
- **P-L2 — One rendering depth per work-group** (no sibling movement nests while another stays flat).
- **P-L3 — Path-policy changes are retroactive.**  A leaf/depth change is incomplete without a
  `repath` pass that brings the existing library forward and re-journals every move.

---

## Progress ledger

Source of truth for resuming cold.  `/run-plan` updates this on each successful commit.

| #  | Status  | Commit | Froze / widened     | Notes |
|----|---------|--------|---------------------|-------|
| L0 | done    | 011490a | C-L0 **FROZEN** | Leaf = `CWP_MOVT_NUM` (existing field reused, no model change); `CWP_ORDERING_KEY_0` dropped from leaf; permanent authority. KAT `test_split_movement_leaf_sequential` (collision + Bach-Mass regression) asserts it. Site-comment obligation met at both leaf sites. Files: `_tags.py`, `test_annotator.py`. tox -m analyze green. |
| L1 | done    | c8ee525 | C-L1 **FROZEN** (◆ sub-track A) | RE-SHARDED (additive): widened to touch `_pipeline.py`; added `cwp_inter_index_{i}` substrate (mirror of `cwp_movt_num`), gap-free per-group sibling index; `build_dest_path` consumes it, raw ordering-key = fallback. KAT `test_opera_scene_intermediate_dir_numbered` + pipeline substrate test assert it. One legitimate `# pragma: no cover` arm (unreachable empty-node-order guard) verified by orchestrator. consumes C-L0. tox -m analyze green. |
| L2 | DEFERRED | —     | — (Opus inflection) | Design *converged* at HALT (uniform-ceiling rule — see Discovery), but user deferred shipping until the library is complete and the full depth-shape distribution is known (maintenance position). Deps for L3/L4 softened to L0,L1. No code. |
| L3 | pending | —      | — (◆ sub-track B; dep L0,L1) | remove/repurpose dead dedup; single numbering authority. Depth normalisation (L2) is NOT a precondition. |
| L4 | pending | —      | C-L4 (◆ sub-track C; dep L0,L1) | Opus inflection; `repath` mode; user-flagged hard requirement. Re-paths for the L0/L1 leaf+intermediate numbering change (L2 depth change deferred). |
| L5 | pending | —      | — (◆ capstone)      | writeup + invariants |

---

## Discoveries & risks

Append during execution; evaluate at sub-track boundaries.

- **PRECONDITION — no Makefile.**  Drive everything through `~/.local/bin/tox -m analyze`.  Hard bar:
  **100% branch coverage** and **pylint 10.00/10** — every new branch (including `case _: # pragma:
  no cover` arms) needs a test.
- **CONFIRMED phenomenology (four shapes + counter-example), so L0/L1 are well-bounded.**  Symphony
  (Mahler 9), opera (Wagner Meistersinger, Mozart Così), suite-with-nested-movement (Handel Water
  Music), and the clean multi-disc Bach Mass.  Tags read directly from disk; see NOTES.  No further
  ingest phenomenology is needed to design L0/L1/L3.
- **L2 DEFERRED — converged design (record for the future maintenance-position session).**  At the L2
  Opus-inflection HALT the depth-normalisation policy was designed to completion, then the user elected
  to **defer shipping** until the library is complete and the full depth-shape distribution is known
  (designing from a maintenance position, not from the 36-group census).  The converged design, kept
  here so a future session inherits it rather than re-deriving:
  - **Data-quality vs work-structure (the framing question).**  Ragged depth has two distinct sources
    demanding *opposite* treatments.  (i) A *data-quality gap* — Shape E's PL=0 orphan, where an MB
    work is *missing* a `part of` link — should be fixed *upstream* (`_works.py` / submit-mode) and
    kept *visible* in the path until then.  (ii) *Faithful non-uniform granularity* — Shapes C/D, where
    MB correctly models some movements more finely (IIIa/IIIb; lettered recits) — is NOT a defect; the
    *renderer* is the right layer to *down-project* it.  Conflating the two sends the fix to the wrong
    layer.  (CAPTURE-CANDIDATE.)
  - **The universal rule (uniform-ceiling, ragged-floor).**  Strict uniform depth is unachievable
    without inventing phantom directories or destroying real act structure.  The achievable *universal,
    simple* rule — covering 1-level lieder through the Ring Cycle and arbitrarily-deep contemporary
    works — is: **a track renders at `min(its own MB depth, the work-group's modal MB depth)`.**  Every
    track is a leaf at its render depth; structure *below* the render depth collapses into the leaf
    (disambiguated by TITLE + the gap-free per-group `cwp_movt_num` from C-L0); structure *at or above*
    renders as nested dirs numbered so depth-first traversal sorts to play order.  This clamps
    over-resolved tracks *down* (removing structure = faithful) but never pads shallow tracks *up*
    (inventing structure = unfaithful) — hence it preserves Shape A's genuinely-top-level overture
    (ragged *floor*, accepted) while removing Shapes C/D's over-resolution (ragged *ceiling*, fixed).
    (CAPTURE-CANDIDATE: *clamp-down and pad-up are not symmetric; removing over-resolution is faithful,
    padding under-resolution invents structure.*)
  - **Pinned corner cases (no real-data precedent — by fiat):** modal ties → shallower depth (favours
    handle-simplicity); PL=0 orphans (Shape E) are EXCLUDED from the modal computation and never
    clamped, so a real `_works.py` orphan bug stays visible and does not drag the modal to 0.
  - **Materialization:** an additive pipeline pass (mirroring C-L0/C-L1) writes `cwp_render_levels =
    min(own_part_levels, group_modal)` as model_extra; `build_dest_path` reads it for the depth branch
    (`_tags.py:1032`), falling back to raw `cwp_part_levels` when absent (no-group escape hatch).  No
    frozen contract altered; the L1 `cwp_inter_index_{i}` numbering is UNAFFECTED — the depth-first
    lexical-sort invariant holds under sibling-index numbering, so C-L1 stays frozen.  Leaf labels are
    NOT folded (the IIIa/IIIb MB sub-part label stays in `CWP_PART_0`, not the path).
  - **Reopen criteria:** revisit when the library is complete (more depth shapes likely), or sooner if
    a new shape appears that the uniform-ceiling rule mishandles.  Shape F (excerpt discs, 2 groups,
    depth spread {1,3}) was deferred at the same HALT — modal is near-arbitrary on a 2-track group;
    decide its handling when L2 reopens.
- **RESOLVED scope, OPEN policy — L2 depth (census complete; see appendix).**  A full library scan
  (3663 files, 1006 work-groups) found **36 non-uniform-depth groups in 6 shapes**.  The census
  reframed L2: the dominant Shape A (20 groups, overture-at-PL=1) is **correct and out of scope**;
  the genuine target is Shapes C/D (3 groups); Shape E (2 groups, PL=0 orphans) is a **separate MB-
  data bug, scoped OUT of this plan** (see next item); Shape B (9) and F (2) need a per-shape call at
  the HALT.  The remaining open question is the editorial policy for C/D (flatten vs threshold-promote),
  not whether the phenomenon is in scope.
- **DISCOVERY (out of scope) — PL=0 orphan tracks are a hierarchy-resolution bug, not a numbering one.**
  Two groups (Mozart Divertimento K.136 "II. Andante"; Litaniae K.243 "X. Miserere") have a single
  movement whose MB work record carries no `part of` relation, so `build_work_hierarchy` resolves it
  as a standalone top work (`CWP_PART_LEVELS=0`, `workid_0 == workid_top`).  This is a `_works.py`
  hierarchy-resolution / MB-data-gap defect.  It does not belong to leaf-numbering; spin a separate
  backlog item rather than letting it widen L2.
- **OPEN — L4 collision semantics.**  When two legacy layouts collapse to one new-policy path (e.g.
  the old nested movement-III dir and the flat siblings now share a parent), `repath` must resolve
  the merge deterministically.  Design at the L4 HALT.
- **DISCOVERY (forward hook for submit mode) — the `CWP_ORDERING_KEY_0` vs `CWP_MOVT_NUM` divergence
  is itself the submit-mode worklist.**  Where a bottom work holds >1 recording, `CWP_ORDERING_KEY_0`
  is constant across those recordings while `CWP_MOVT_NUM` increments — so the per-bottom-work spread
  between the two flags exactly the 16 multi-rec groups from the census whose MB data could be cleaned
  upstream (split bottom works into per-section sub-works).  C-L0 makes the *path* invariant to whether
  that clean-up ever happens; this divergence is the signal for a future MB-submit-mode plan, not a
  reason to weaken C-L0.  (Capture candidate: "where a derived index and a source key that *should*
  agree diverge, the divergence set is the upstream-data-repair worklist.")
- **RE-SHARD (additive, approved at L1 pre-dispatch) — L1 widened to touch `_pipeline.py`.**  L0's
  fix was a clean two-file change only because the leaf's per-group index (`cwp_movt_num`) already
  existed in the substrate.  The orchestrator caught, before dispatching L1, that **no analogous
  per-group sibling-index exists for intermediate levels** — they carry only the raw MB ordering-key,
  and `build_dest_path` (per-track) cannot derive a sibling rank locally.  L1's expected files were
  widened from {`_tags.py`, `test_annotator.py`} to {`_pipeline.py`, `_tags.py`, `test_annotator.py`,
  `test_pipeline.py`} and contract **C-L1** added (new `cwp_inter_index_{i}` substrate field).  This
  is additive — no frozen contract altered, no committed session reordered — so it was permitted with
  user approval despite the run lacking `may-reshard`.  Lesson: *a fix that is clean at the leaf
  because a substrate index already exists is not automatically clean one level up — verify the
  analogous substrate before scoping the sibling session to the same file set.*
- **RISK — double numbering authority.**  Until L3 removes the dedup pass, both `cwp_movt_num` and
  `_dedup_plan_entries` can assign leaf numbers.  L0-L2 must not rely on dedup; L3 closes the risk by
  making the per-group index the sole authority.

---

## Non-uniform-depth census (library scan, for L2 design)

Full scan of `~/Remote/hades/Music/Done/` — **3663 FLACs, 0 MP3, 1006 work-groups** (a work-group =
all tracks of one release sharing a `CWP_WORKID_TOP`).  A group is *non-uniform* when its tracks carry
differing `CWP_PART_LEVELS`.  **36 groups (3.6%)** are non-uniform, in six shapes.  Scan script (kept
for reproducibility / the L4 `repath` detector): `/tmp/opencode/scan_nonuniform_depth.py`.

| Shape | n | What it is | Correct? | L2 treatment |
|-------|---|------------|----------|--------------|
| **A** | 20 | Overture/sinfonia/epilogue at PL=1 among PL=2 acts/numbers (Die Meistersinger Vorspiel, Così Ouverture, Nutcracker Ouverture ×3, Verdi Requiem Offertory, Missa solemnis Agnus Dei) | **YES — overture genuinely sits at top of the opera** | **Out of scope — preserve. Must not over-normalise.** |
| **B** | 9 | Mixed flat/split movements: some movements single-track (PL=1), others split into sub-movements (PL=2) (Mozart Missa c-Moll, Requiem K.626, Verdi Requiem, Mendelssohn *Lobgesang*, four Grumiaux violin sonatas, Divertimento K.287) | Arguably correct | Decide at HALT (likely acceptable as-is) |
| **C** | 1 | Suite with one multi-part movement (Handel Water Music — Suite 1 movt III has sub-parts IIIa/IIIb → PL=3 among PL=2) | **NO — ragged depth** | **Primary target** |
| **D** | 2 | Oratorio with multi-part numbers (Bach Matthäus-Passion: 14 PL=3 tracks from lettered recits; Haydn *Schöpfung*: Nr.18/19 → XIXa/b) | **NO — ragged depth** | **Primary target** |
| **E** | 2 | PL=0 orphan: a movement's MB work has no `part of` link → resolved as standalone top work (Mozart Divertimento K.136 "II. Andante"; Litaniae K.243 "X. Miserere") | **NO — different bug** | **Out of scope → separate backlog item** |
| **F** | 2 | Highlights disc with depth-mismatched excerpts (Tannhäuser: Overtüre PL=1 vs Bacchanale PL=3; Tristan: Vorspiel PL=2 vs Liebestod PL=3) | Edge case | Defer / decide at HALT |

**Extreme case:** Tannhäuser highlights — depth spread of 2 (PL={1,3}) in a 2-track group; the only
true spread-≥2 case among non-zero depths.

**The bigger, orthogonal signal — multi-recording-per-bottom-work (16 groups).**  Independently of
depth, 16 groups have at least one bottom work (`CWP_WORKID_0`) holding >1 recording — this is the
*direct* driver of the leaf-collision bug that L0 fixes.  Only **3** of these 16 overlap the
non-uniform-depth set (Handel, Così, Die Meistersinger — the last has 12 bottom-works holding >1 rec,
max 10, the worst leaf-collision in the library).  The other **13 are uniform-depth** (Mahler 9 — 4
bottom-works ×up to 8 recs; Boccherini *Musica notturna* ×5; Sibelius Symphony 7 ×4; …).  **Design
consequence: L0/L1 (per-group leaf index) is the load-bearing fix — it covers all 16 multi-rec groups
regardless of depth.  L2 (depth) is the smaller, secondary concern touching only Shapes C/D (3
groups).  Do not let L2's intricacy inflate its priority relative to L0.**
