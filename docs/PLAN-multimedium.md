# music-annotator — Plan: Multi-medium Paths & Library Maintenance

This plan is **session-sharded** for autonomous execution by `/run-plan` (see
`~/.config/opencode/multi-session-planning.md` and `~/.config/opencode/command/run-plan.md`).
The currency is the *commit-shaped session*: one `@build`/`@general`/`@explore` dispatch producing
one commit, ending with green checks.  `@plan-deep` orchestrates; it verifies each session contract
and dispatches `@committer`.  State lives in the Progress ledger, not in context.

This is one of several independent sharded plans, each with its own ledger — see `docs/PLAN.md`
(the index) for the full set.  Siblings: `docs/PLAN-fingerprint.md` (acoustic fingerprinting &
archival identity) and `docs/PLAN-naming.md` (library-wide dir/file-naming unification, which
depends on this plan's S0 substrate).  Cross-cutting backlog and external-dependency tracks live in
`docs/BACKLOG.md`.

The active scope is a single coherent featureset — **multi-medium-correct path construction and
library maintenance** — decomposed into one substrate session plus three sub-tracks.  Items deferred
until their substrate lands stay in the Roadmap appendix below (sub-track granularity), to be
re-sharded only when their substrate lands; everything outside this featureset has moved to its
sibling plan or to `docs/BACKLOG.md`.

---

## Purpose (design intent)

Make path construction and library maintenance **correct for works that span multiple media**, and
add the **soloist** as a path dimension when the work's canonical identity demands it — all refracted
through the two editorial invariants already recorded in `docs/NOTES.md`:

- **Path is a handle, not a manifest.**  Primary attribution goes in the path; full credits go in
  tags.  Every dimension added to path construction needs a *work-level unification story* so that
  movements of one work never disagree on the path.
- **Journal detects, tag adjudicates.**  For library-grouping/regrouping work the journal is the
  cheap detector and the embedded `MUSICBRAINZ_ALBUMID` tag is the present-state authority.

The structural fact underneath the whole featureset: `run()` processes **one medium at a time**
(`_pipeline.py:905`, `all_track_pairs` is built from `selected_medium` only), so the three existing
work-level unification passes (`_pipeline.py:984–1082`) each carry the caveat *"spans movements on
the same medium only."*  A concerto split across two discs, a symphony whose movements straddle a
disc boundary, and a finisher credited on only the last disc are all silently mis-pathed today.  S0
removes that constraint; the sub-tracks build on it.

**Re-read this section at every ◆ sub-track boundary** to verify the work still tracks the intent
(anti-defocus check).

---

## Session list

One row = one dispatch = one commit.  `Cat` = category (A substrate / B algorithm / C optimization /
X context-substance).  `T` = tier: O = Opus inflection (orchestrator designs inline, then HALT for
sign-off); S = Sonnet `@build`.  ◆ marks the last session of a sub-track.  `Dep` lists the
session-numbers / frozen contracts a row depends on.  `Expected files` and `KAT` are the
`/run-plan` scope-drift and KAT-present gates; the per-session notes carry the exact line anchors.
This table is intentionally wider than the 128-char rule — tables don't wrap, and the data is the
point.

| #  | Title (commit-shaped)                                  | Cat | T | Dep        | Expected files                                                       | KAT |
|----|--------------------------------------------------------|-----|---|------------|----------------------------------------------------------------------|-----|
| S0 | Aggregate work-groups across all media in `run()`      | A   | O | —          | `_pipeline.py`, `tests/unit/test_pipeline.py`                        | `test_top_work_groups_span_all_media` ✅ done |
| S1 | Lock cross-medium unification with regression KATs ◆  | B   | S | S0         | `tests/unit/test_pipeline.py`                                        | `test_composer_unified_across_media`, `test_recording_first_release_date_unified_across_media` |
| S4 | Carry top-work type into `TrackTags`                   | A   | S | —          | `models.py`, `_tags.py`, `tests/unit/test_pipeline.py`              | `test_cwp_worktype_genres_top_populated` |
| S5 | Promote soloist into path for concerto works ◆         | B   | S | S0,S4      | `models.py`, `_pipeline.py`, `_tags.py`, `tests/unit/test_pipeline.py`, `tests/unit/test_annotator.py` | `test_concerto_soloist_in_top_dir`, `test_album_soloists_unioned_across_media` |
| S6 | Read-only `audit`: group journal by `release_id`       | B   | S | —          | `__main__.py`, `_pipeline_io.py`, `tests/unit/test_main.py`         | `test_audit_reports_mixed_mbid_and_split_release` |
| S7 | Confirm candidates via `MUSICBRAINZ_ALBUMID` tag       | B   | S | S6         | `_pipeline_io.py`, `tests/unit/test_main.py`                        | `test_audit_confirms_candidate_via_tag` |
| S8 | Add `regrouped` journal action and regroup move ◆      | B   | S | S6,S7      | `models.py`, `_pipeline_io.py`, `__main__.py`, `tests/unit/test_main.py` | `test_regroup_appends_journal_entry` |
| S9 | Integrative writeup + codebase-audit handoff ◆         | X   | O | S1,S5,S8   | `docs/NOTES.md`, `README.md`                                         | — (prose) |

### Sub-track boundaries

- **◆ Sub-track A — cross-medium unification** ends at S1.  **Re-shard (action-frame discovery,
  post-S0):** S0's clean implementation iterated the full global `tags_map`, so *all three*
  unification passes (composer, recording-date, first-release-date) became cross-medium as a side
  effect — the per-pass "lift the logic" work the original S1/S2 described no longer exists.  What
  remains is the named `*_across_media` **regression KATs** (the test-enforced substrate guard).
  S0's KAT already covers the cross-medium `recording_date_work` path; S1 adds the two it does *not*
  cover: composer cross-disc fallback propagation, and the first-release-date `[rel YYYY]`
  cross-disc fallback branch.  Original S2/S3 are retired from this plan: S2's logic is in S0 and its
  date KAT is folded into S1; **S3 is lifted out entirely** — see the new deferred sub-track below.
- **◆ Sub-track B — concerto-soloist override** ends at S5.  Ships: soloist in the directory path
  for canonical-identity works, with the soloist set unified across media (consumes S0).
- **◆ Sub-track C — release-fragmentation detector** ends at S8.  Ships: read-only `audit`, then the
  regrouping move.  This sub-track is **journal-side and orthogonal** — it consumes nothing from S0
  and may be executed in any order relative to A/B.
- **S9** is the integrative capstone for the whole featureset and the explicit handoff into the
  long-deferred **Codebase audit** (see Roadmap appendix), which the user has flagged as imperative
  *after* this featureset lands.

### Notes per session

- **S0 (Opus inflection point — SIGNED OFF; design frozen below).**  The substrate.  Smallest
  correct change: have `run()` build `all_track_pairs` from *all* media of the release (not just
  `selected_medium`) for the purpose of the post-processing passes, and merge `top_work_groups`
  (`_pipeline.py:966`) so a top-work MBID groups movements across disc boundaries.  Do **not**
  introduce a `ReleaseContext`/`WorkGroup` object yet — defer that to the codebase audit (S9
  handoff) unless a later session proves it necessary (log as a Discovery).  Preserve the
  single-medium *copy* semantics: only one medium's files are actioned per `run()`; the
  cross-medium aggregation feeds the *path/tag* passes, not the copy loop.  **This is the contract
  every sub-track-A/B session consumes — over-specify the grouping surface here.**

  **FROZEN DESIGN (Opus sign-off, Shape A + eager fetch + global-index filter):**
  - **Scope: ingest only.**  `run()` is the first-time-annotation path (reached from `apply` and
    `search`→`discover()`); it fetches MB because the source files have no tags yet.  The eager
    all-media fetch cost lives *entirely* here.  The maintenance/regroup path (S6–S8) is journal-
    + tag-driven, never fetches, and consumes nothing from S0.  Do not let the maintenance framing
    leak into S0.
  - **`tags_map` spans all media (Shape A).**  Build `tags_map` for *every* track on *every* medium,
    keyed by a single global index `0..N_total-1` over a new `all_media_pairs: list[tuple[MBTrack,
    int]]` (track, medium_pos) flattened across `release.medium_list` in medium-then-track order.
    `build_track_tags` + `fetch_recording_detail` + `fetch_acoustid_id` run for every such index.
    The work-grouping (`top_work_groups`) and all three unification passes (`:967–1082`) iterate
    this full map → they now span media for free.
  - **Copy plan filters to the selected medium (global-index filter).**  Replace the old
    `file_track_pairs = zip(src_files, all_track_pairs)` with a *copy subset*: the list of global
    indices `idx` whose `medium_pos == selected_medium.position`, zipped with `src_files` in
    order.  The track-count-mismatch check (`:908`), duration pre-flight (`:919`), copy-plan build
    (`:1104`), dedup/collision (`:1119–1170`), and the copy/tag/verify/journal loop (`:1178`) all
    operate on this copy subset **only** — preserving P3 and the journal-provenance chain verbatim.
  - **`global_track_idx` for filenames stays copy-subset-local.**  `build_dest_path`'s
    `global_track_idx` (`:1110`) is the 1-based enumeration over the *copy subset*, not the
    all-media index — preserving today's per-run unique-filename behaviour for the actioned medium.
  - **Single-medium releases: behaviourally identical to today.**  When `len(release.medium_list)
    == 1`, the all-media set equals the selected-medium set, so no extra fetches and no behaviour
    change.  (Falls out of Shape A naturally; no special-casing needed, but assert it in a test.)
  - **RISK focus (per Discoveries "substrate copy-semantics regression"):** the single most likely
    failure is leaking non-selected-media indices into the copy loop.  The `@build` session must
    keep `tags_map` global-indexed while every copy-side structure (`plan`, `plan_pairs`,
    `skip_dest`, `journal_entries`) is built from the copy subset.  The existing
    `len(src_files) != <copy-subset count>` check is the guard.
  - **KAT `test_top_work_groups_span_all_media`:** a 2-medium release whose movements of one top
    work straddle the disc boundary; assert (a) the unification passes treat them as one group
    (e.g. unified `recording_date_work` across both discs) and (b) only the selected medium's files
    are journalled `tagged`.  Plus a single-medium regression assert that fetch counts are
    unchanged.  The existing same-medium tests (`test_recording_date_work_unified_across_movements`,
    `test_composer_unified_*`) must stay green unmodified.
- **S1.**  Lift the composer unification pass (`_pipeline.py:984–1019`) to operate over the
  cross-medium `top_work_groups` from S0.  No new fields; the `cwp_composers_is_fallback` flag
  (`_tags.py:839`) and `effective_composers` fallback (`_tags.py:423`) are unchanged.
- **S2.**  Lift the `recording_date_work` pass (`_pipeline.py:1021–1054`) and the
  `recording_first_release_date` normalisation (`_pipeline.py:1056–1082`) to the cross-medium
  groups.  These two share the same `_begins`-empty condition and ship together (one conceptual
  unit: "session-date label correct across media").
- **S3.**  `_dedup_plan_entries` (`_pipeline.py:655–697`) already uses the 1-based global index so
  `dd.dd` leaf prefixes are unique across discs; verify and lock that behaviour with a multi-medium
  KAT, and fix the *prefixing-where-it-should-not-appear* case noted in the backlog (`dd.dd` added
  to multitrack works that are not partial-performance collisions).
- **S4 (small substrate).**  Add `cwp_worktype_genres_top: str = ""` to `TrackTags` (`models.py`,
  near line 1223) and populate it in `build_cwp_tags` from `top_work = work_hierarchy[-1]`
  (`_tags.py:348`), alongside the existing bottom-work `cwp.worktype_genres = work_hierarchy[0].type`
  (`_tags.py:359`).  Written to the file as a tag (do **not** add to the `to_file_dict` exclusion
  set).  This is the field `build_dest_path` needs because the *bottom* work's type is empty for a
  concerto movement — only the *root* work carries `"Concerto"`.
- **S5 (scope WIDENED — user sign-off, see Discoveries "S5 path-accumulation").**  Two parts:
  - **(a) Cross-medium soloist-union pass (`_pipeline.py`, `models.py`).**  Add a *path-only helper*
    field `cea_album_soloists_unified: str = ""` to `TrackTags` (`models.py`), added to the
    `to_file_dict` `excluded` set (path-only, NOT a written tag — mirrors `recording_date_work`).
    In `run()`'s top-work-group loop (`_pipeline.py:~1029`, alongside the composer / recording-date
    passes that already iterate `group_idxs` over `all_media_pairs` per C-S0), compute the **union**
    (dedup, order-preserving) of each group track's `cea_album_soloists` (with `cea_soloists` as the
    per-track fallback when album-level is empty) across the whole group, and write that unioned
    string to every group track's `cea_album_soloists_unified`.  This realises the editorial rule:
    **unified path components accumulate per work across media** — if a concerto's movements feature
    different soloists on different discs, all of them accumulate into the path.  (The per-track *tag*
    worldview is NOT changed to carry the union — that is a later initiative; only the path-helper
    accumulates.)
  - **(b) Path injection (`_tags.py`).**  In `build_dest_path`, after the album-conductor/ensemble
    block (`_tags.py:958–970`), when `top_work.type == "Concerto"` — read via
    `file_dict.get("CWP_WORKTYPE_GENRES_TOP") == "Concerto"` (C-S4) — inject `tags.cea_album_soloists_unified`
    (read directly off the `tags` object, as `tags.cea_album_conductors_list` is, since the field is
    excluded from `file_dict`), into the `performers` string.  Concerto-type detection via
    `top_work.type == "Concerto"` is the only mechanical case in scope; symphony-with-soloist and other
    canonical-feature works are an editorial allowlist deferred to the appendix (S5-open).
  - **P1 note:** soloist promotion is the CE-sanctioned *exception* to "path is a handle, not a
    manifest", not a licence to widen the path generally — gate it strictly on the Concerto case.
- **S6.**  New `audit <dest_dir>` argparse subparser after the `prune` block (`__main__.py:~334`)
  and a `case "audit":` arm before `case _:` (`__main__.py:417`); read-only, no
  `--user-agent-email` required.  Group `read_journal` (`_pipeline_io.py:526`) entries with
  `action == "tagged"` by `release_id`, derive the `work_dir` component as
  `Path(e.destination).relative_to(dest_root).parts[1]`, and report **case (a)** (one `work_dir`,
  multiple `release_id`) and **case (b)** (one `release_id`, multiple `work_dir`).  No tag read,
  no moves.
- **S7.**  For each S6 candidate dir only, read `MUSICBRAINZ_ALBUMID` back via the existing
  `_read_tags_flac`/`_read_tags_mp3` (`_pipeline_io.py:581/595`; key uppercases to
  `MUSICBRAINZ_ALBUMID` for both formats) to confirm the journal's `release_id` matches present
  state, distinguishing real fragmentation from journal staleness.
- **S8.**  Add `"regrouped"` to the documented `action` values on `TransactionEntry`
  (`models.py:1487`, inline comment lists the valid strings — keep it `str`, not an enum, per the
  existing model style) and implement a move that records old→new `destination`.  Any move MUST
  append its own journal entry or the detector decays with use (NOTES "journal detects, tag
  adjudicates", closing corollary).
- **S9.**  Name the new prose invariants in `docs/NOTES.md` (the cross-medium aggregation contract;
  the concerto-soloist path rule; the `regrouped` journal-action obligation), update `README.md` if
  the `audit` subcommand is user-facing, and write the handoff brief for the **Codebase audit**
  (module-boundary review, `__init__.py` API coherence, whether the deferred `ReleaseContext` object
  is now warranted).

---

## Cross-session contracts

The scaffolding that makes the sessions compose.  Three flavours, per the manual.  A contract is
**frozen** once the session that establishes it is `done` (see the ledger) — later sessions consume
it and must not break it.

### Compiler-enforced (interfaces / signatures / model fields)

- **C-S0 — cross-medium work-groups (FROZEN BY S0).**  `run()` exposes, to the post-processing
  passes, a `top_work_groups: dict[str, list[int]]` whose values index into a `tags_map` covering
  **all media** of the release, not just the selected medium.  The grouping key remains
  `cwp_workid_top or musicbrainz_workid`.  Over-specified by design: S1 (regression KATs), S5
  (soloist), and the lifted leaf-numbering bug-fix sub-track all consume this shape.  Widening it
  (extra grouping metadata) before it is frozen is allowed; altering it after freeze is a destructive
  re-shard → HALT.
- **C-S4 — `TrackTags.cwp_worktype_genres_top` (FROZEN BY S4).**  New `str` field, written to the
  output file as tag `CWP_WORKTYPE_GENRES_TOP`, carrying `work_hierarchy[-1].type`.  Consumed by S5.
- **C-S8 — `TransactionEntry.action` value set (WIDENED BY S8).**  `action` gains `"regrouped"`
  alongside `"tagged" | "skipped" | "dry_run" | "downloaded" | "sidecar"`.  Additive only; existing
  values and the `str` typing are unchanged.

### Test-enforced (KATs — grow monotonically)

Each row's KAT (session-list table) must be present and green at every subsequent session.  The
multi-medium KATs (`*_across_media`) are the regression guard for the substrate: any later session
that reintroduces single-medium-only aggregation breaks them.

- The existing same-medium tests
  (`test_recording_date_work_unified_across_movements`,
  `test_composer_unified_across_movements_when_additional_only_on_some`,
  `test_composer_unified_produces_same_top_dir`, and the `TestBuildDestPathEdgeCases` album-filter
  tests) **must continue to pass** — S0–S3 generalise these passes, they do not replace them.

### Prose-enforced (invariants — named, nothing auto-enforces)

- **P1 — Path is a handle, not a manifest** (`NOTES.md`).  Consumed by S5 (soloist promotion is the
  *exception* CE sanctions, not a licence to widen the path) and by S9.
- **P2 — Journal detects, tag adjudicates** (`NOTES.md`).  Consumed by S6 (journal = detector), S7
  (tag = authority), S8 (moves must re-journal or the detector decays).
- **P3 — Single-medium copy semantics preserved.**  S0 widens *aggregation* scope only; the copy/
  tag/verify loop and its journal-provenance chain (AGENTS.md "Transaction journal and user
  confirmation provenance") still action exactly one medium per `run()`.  Consumed by every
  sub-track-A/B session.
- **P4 — Defensive download posture** (AGENTS.md).  No session in this featureset adds a network
  call; if S7/S8 ever fetch, the two-layer retry pattern applies.

---

## Progress ledger

Source of truth for resuming the chain cold.  `/run-plan` updates this on each successful commit.

**Run bindings (resolved at loop start; reuse on cold resume):** `PLAN = docs/PLAN-multimedium.md`.
`VERIFY = ~/.local/bin/tox -m analyze` — a single combined gate (build + test + check_type +
check_format + check_lint + check_upgrade) that satisfies the test-green AND types-clean gates in one
green run, and additionally enforces 100% branch coverage, pylint 10.00/10, ruff, and pyupgrade.
Run config: default (self-review-and-continue at ◆; halt only at the four halt classes).

| #  | Status   | Commit | Froze / widened        | Notes |
|----|----------|--------|------------------------|-------|
| S0 | done     | 5b41781 | C-S0 (FROZEN)         | Opus sign-off done; Shape A + eager fetch + global-index filter; scoped to ingest |
| S1 | done     | efa1128 | KATs (test-enforced)  | ◆ sub-track A closed; KAT-only (logic landed in S0); test_composer_unified_across_media + test_recording_first_release_date_unified_across_media lock the cross-disc composer + [rel YYYY] passes |
| S4 | done     | 85ccb36 | C-S4 (FROZEN)         | small substrate; threaded via intermediate `CwpTags.worktype_genres_top` (standard CwpTags→TrackTags field pattern; `top_work` not in scope in `build_track_tags`) — external surface unchanged |
| S5 | done     | 11473c5 | cea_album_soloists_unified (path-only helper) | ◆ sub-track B closed; scope widened (re-shard `2252222`); cross-medium soloist-UNION pass in `_pipeline.py` (consumes C-S0) + concerto-gated path injection in `build_dest_path` (consumes C-S4, P1); soloist-first join; helper excluded from `to_file_dict` |
| S6 | done     | 9750449 | audit() + _journal_fragmentation_groups (S7 reuse) | journal-side detector only (P2); read-only `audit <dest_dir>` subcommand; allowed extra: `__init__.py` API re-export (public `audit` in `__all__`, helper in `_reexports` per existing patch-binding convention); `work_dir = destination.relative_to(dest_root).parts[1]` confirmed |
| S7 | done     | c74ab23 | _confirm_fragmentation, _read_albumid_tag (S8 reuse) | consumes S6 (P2 adjudication via tag read; read-only); allowed extra `__init__.py` re-export (S6 precedent); **S8 contract:** a candidate is `confirmed=True` if ANY backing entry's file tag matches its journal release_id — S8 acts only on confirmed candidates |
| S8 | pending  | —      | C-S8 (◆ sub-track C)   | consumes S6, S7 |
| S9 | pending  | —      | — (◆ capstone)         | consumes S1,S5,S8; Opus writeup + audit handoff |

_Retired from active scope:_ original **S2** (recording/first-release-date lift) — its logic landed
in S0; its date KAT is folded into S1.  Original **S3** (`dd.dd` fix) — re-scoped from a one-session
KAT into a separate **leaf-numbering bug-fix sub-track** (see Roadmap appendix; NOTES.md "Leaf-
numbering bug").

**Sub-track A (cross-medium unification) — CLOSED at S1 (commit `efa1128`).**  Anti-defocus check
passed: S0 delivered the all-media aggregation substrate; S1 locked the two regression KATs S0's own
KAT did not cover (composer cross-disc fallback, first-release-date `[rel YYYY]` cross-disc).  C-S0
consumed without widening or breaking; no contract drift.

**Sub-track B (concerto-soloist override) — CLOSED at S5 (commit `11473c5`).**  Anti-defocus check
passed: ships soloist-in-path for the canonical-identity concerto case, with the soloist set
cross-medium-unified (consumes C-S0).  The pre-dispatch re-shard (`2252222`) strengthened alignment
with the Purpose's "every path dimension needs a work-level unification story" — the union pass is
that story.  C-S0 and C-S4 consumed without alteration; new consumable surface
`cea_album_soloists_unified` (path-only helper) introduced; no contract drift.

**Frozen contracts:**
- **C-S0 (frozen by S0, commit `5b41781`).**  `run()` keys `tags_map` by a global index over
  `all_media_pairs` (every track on every medium, medium-then-track order); `top_work_groups` and
  the three unification passes iterate the full map → they span disc boundaries.  The copy plan and
  the copy/tag/verify/journal loop iterate a `copy_subset` (selected medium only); `CopyPlanEntry.idx`
  carries the global index so `tags_map[idx]` resolves, while `build_dest_path`'s `global_track_idx`
  is copy-subset-local.  Single-medium releases are behaviourally identical to pre-S0.  S1, S5, and
  the leaf-numbering bug-fix sub-track consume this; altering the index-keying or the copy-subset
  boundary after freeze is a destructive re-shard → HALT.
- **C-S4 (frozen by S4, commit `85ccb36`).**  `TrackTags.cwp_worktype_genres_top` is a `str` field
  (default `""`), written to the output audio file as tag `CWP_WORKTYPE_GENRES_TOP`, carrying
  `work_hierarchy[-1].type` (the top work's type).  Threaded internally through an intermediate
  `CwpTags.worktype_genres_top` field (the codebase's standard CwpTags→TrackTags pattern; the
  `top_work` local is not in scope in `build_track_tags`) — this is an implementation detail, not part
  of the consumed surface.  S5 consumes it via `file_dict.get("CWP_WORKTYPE_GENRES_TOP")`.  Additive;
  altering the field name or its source after freeze is a destructive re-shard → HALT.

---

## Discoveries & risks

Action-frame discoveries that update the static-frame roadmap.  Append during execution; evaluate at
sub-track boundaries.

- **DISCOVERY (additive re-shard, user sign-off) — S5 path-accumulation across media.**  Pre-dispatch
  analysis of S5 found a scope tension: the plan required the injected soloist set to be the
  "cross-medium-unified set from S0", but `cea_album_soloists` is built per-track (candidate pool =
  that track's recording credits, filtered to release-level artists), so a release-level soloist
  credited on only one disc makes the discs disagree on the path — and S5's stated expected files
  (`_tags.py`, `tests/unit/test_annotator.py`) excluded `_pipeline.py`, where the unification passes
  live.  User clarified the governing editorial rule: **when unioning tracks/work-hierarchies from
  multiple media into one unified library path, path components accumulate per work** — both a primary
  composer and a finisher credited on only one disc, and different soloists in different movements
  across discs, all accumulate into the final unified directory/file path.  (The per-track *tag*
  worldview need not carry the union yet — that is a separate later initiative.)  Resolution: S5
  widened additively (C-S0 untouched, still frozen) to add a path-only `cea_album_soloists_unified`
  helper computed by a cross-medium *union* pass in `_pipeline.py`; expected files grow to include
  `models.py`, `_pipeline.py`, `tests/unit/test_pipeline.py`.  This is the manual's "the plan was
  wrong about scope, additively corrected" case, not a destructive re-shard.
- **PRECONDITION — no Makefile (blocks an unmodified `/run-plan` run).**  This project drives
  everything through `tox` (`~/.local/bin/tox -m analyze`), which should be used instead..  Harder
  bar applies: **100% branch coverage** and **pylint 10.00/10** — every new branch (including `case
  _: # pragma: no cover` arms) needs a test.
- **S5-open — editorial scope of "concerto-identity".**  `top_work.type == "Concerto"` is the only
  mechanical signal in scope.  Organ symphonies (Saint-Saëns 3), violin-feature works ("Cinema
  Serenade"), and symphony-with-soloist are canonical-identity but not type-`Concerto`; they need an
  editorial allowlist or a "solo X" instrument-relation signal.  Deferred to the appendix item
  "Directory path — concerto-like soloist override".  S5 ships the `Concerto`-type case only; the
  allowlist is a follow-on session, not part of this featureset.
- **DISCOVERY — sub-track A re-shard (post-S0).**  S0's clean implementation (iterate the full
  global `tags_map`) lifted *all three* unification passes to span media as a side effect, so the
  original per-pass S1/S2 logic sessions are vacuous.  Sub-track A collapsed to a single KAT session
  (S1) that adds the `*_across_media` regression guards S0's KAT doesn't already cover.  This is the
  "the plan was wrong about X is a successful outcome" case from the multi-session manual.
- **DISCOVERY (resolves the old `dd.dd` RISK) — the leaf-numbering bug is upstream, not in
  `_dedup_plan_entries`.**  Diagnosed against a real Mahler-9 output dir (see NOTES.md "Leaf-numbering
  bug").  `_dedup_plan_entries` is mechanically correct — it fires only on byte-identical
  destination paths.  The real root cause: the leaf `nn` is the bottom-work's ordering-key (= the
  *movement* number), so every sub-section recording of one movement wants the same leaf number;
  combined with title collapse, this manufactures the collisions that make `dd.dd` over-fire, and the
  post-dot index is a non-restarting global running index so playback order breaks.  This is bigger
  and more uncertain than a one-session fix and **further phenomenology is expected** (other
  split-work shapes).  Lifted out of this plan into its own deferred sub-track (appendix); do not
  freeze a fix design until more real examples are collected.
- **RISK (RESOLVED by S0) — substrate copy-semantics regression.**  S0 kept the copy path scoped to
  `copy_subset` (selected medium) while `tags_map`/`all_media_pairs` span media; the `len(src_files)
  != len(copy_subset)` check guards it.  KAT `test_top_work_groups_span_all_media` asserts only the
  selected medium is journalled.  No regression.

---

## Roadmap appendix (sub-track granularity — not yet sharded)

Items deferred until their substrate lands or an external dependency clears.  Per the manual, these
are described at sub-track granularity; their sessions are crisply known only after the relevant
substrate session completes.  Full original prose is preserved so no design context is lost.

### Codebase audit (next after S9 — user-flagged imperative)

As the project grows, do a thorough review of principles, structure, and goals.  Evaluate whether
the module boundaries remain natural, whether the public API surface in `__init__.py` is still
coherent, and whether any accumulated conventions need revisiting.  **The S9 capstone hands off to
this**; specifically, decide whether the deferred `ReleaseContext`/`WorkGroup` aggregation object
(considered and deliberately not built in S0) is now warranted.

### Leaf-numbering / split-work path bug — MOVED to `docs/PLAN-leafnumber.md`

Lifted out, re-diagnosed against four real work-shapes, and **sharded into its own self-contained
plan** (`docs/PLAN-leafnumber.md`, L0–L5).  The re-diagnosis corrected the original framing: the
title-collapse symptom was a stale artifact of superseded code, `_dedup_plan_entries` is now dead
(distinct titles never collide), and a fourth bug (non-uniform hierarchy depth) surfaced.  The
enabling substrate (`top_work_groups`' per-group `cwp_movt_num`) was delivered by S0, so the fix is
smaller than feared.  Nothing further is owed from this plan; the leaf-number plan stands alone.

### Concerto-like soloist override — editorial allowlist (follow-on to S5)

The mechanical `top_work.type == "Concerto"` case ships in S5.  The remaining open item is the
non-mechanical canonical-soloist works: Saint-Saëns Symphony no. 3 (organ), "Cinema Serenade"
(violin), and symphony-with-soloist generally.  Candidate signals: "solo X" instrument relation
types, dedicated work-title patterns, or an editorial allowlist.  The rule answers "is the soloist
part of the work's canonical identity?" not "is the soloist on the release?".  All decisions refract
through the Classical Extras path-vs-tag distinction (primary attribution in path, full credits in
tags).  See NOTES.md "Path is a handle, not a manifest."

### Moved out of this plan

The following formerly lived in this appendix and have moved to their own homes during the
plan-split (see `docs/PLAN.md` index):

- **Library-wide dir/file-naming unification** (the "broader passes" — language/misspelling
  variations, work-level arranger/finisher path credit, library-wide `dd.dd` retroactive pass,
  re-annotation/update-diff mode, user-cover-art metadata extraction) → **`docs/PLAN-naming.md`**.
  It depends on this plan's S0 multi-medium substrate and on `PLAN-fingerprint.md`'s identity layer.
- **Source verification — Chromaprint `--verify-fingerprints`** → subsumed by
  **`docs/PLAN-fingerprint.md`** (rung 5 / F6, `fetch_acoustid_lookup`); retired there at F8.
- **Submit disc IDs to MusicBrainz**, **PrestoMusic / whipper / MakeMKV source support**, **playlist
  generation**, **CE-tag audit**, **cover-art sleeve type**, the **Deferred** editorial items
  (`[rec YYYY]` label, work-title authenticity, native-script names), and the **musicbrainzngs2
  contributions** track → **`docs/BACKLOG.md`**.
