# PLAN — leaf-`nn` renumbering: cross-session consolidation collision (source fix + retroactive repair)

**Status:** handoff draft for `@build`. Produced by an architect-mode investigation 2026-08-30 against the live library.
**Not the active rolling PLAN** (that is C-LOCAL-ID). This is a parked, ready-to-execute plan for a distinct defect.

## Purpose (design intent)

The leaf `nn - ` prefix on a catalog file is `CWP_MOVT_NUM`: a gap-free, 1-based, playback-ordered index assigned over
one **top-work group** by `_apply_workgroup_unification` (`_pipeline.py:914`). Its ordering authority is the
enumeration order of `all_media_pairs`, i.e. `(medium.position, track.position)` — equivalently, embedded
`(DISCNUMBER, TRACKNUMBER)`. The L0/L1 leaf-numbering fix (contract C-L0) made this correct **for a single `run()`
invocation**.

The residual defect: `CWP_MOVT_NUM` is only gap-free *within one ingest session*. When the **same MB work is ingested
across separate sessions** (separate discs of a box set ripped/ingested at different times), each session numbers
`CWP_MOVT_NUM` from 1 independently. A later directory-consolidation (`regroup`/`unify`, or a prior manual collapse)
merges those fragments into one work directory **without renumbering**, so the leaf prefixes restart and collide:

```
Bach - Helmut Walcha/Die Kunst der Fuge, BWV 1080 [rel 2000]/
  01 - ... XVIII.flac    01 - ... I.flac    01 - ... VI.flac    ← three "01"s
  02 - ... II.flac       02 - ... VII.flac  ...
```

Live scan of `~/Remote/hades/Music/Done/` (12k+ FLACs): **40 collision directories**.

## The live source bug (must be fixed, not only the library)

`regroup()` (`_pipeline_maint.py:2348`) and `unify()` (`:2650`) both call
`build_dest_path(..., global_track_idx=0)` using the **embedded** `CWP_MOVT_NUM`, and never re-run the movement-number
pass over the newly-merged group. So consolidating cross-session fragments **reproduces the collision on every run**.
Repairing the library without fixing this means the next `maintain`/`regroup`/`unify` re-damages it.

**Shared authority (both paths must use it identically → idempotent convergence):**
group by `CWP_WORKID_TOP`; order by embedded `(DISCNUMBER, TRACKNUMBER)`; assign gap-free 1-based `CWP_MOVT_NUM`
(and mirror to `CWP_MOVT_TOT`, `MOVEMENTNUMBER`, `MOVEMENTTOTAL`). This is exactly what `run()`'s movement-number
pass computes; the maintenance path must reconstruct the same ordering from tags.

The movement-number pass (`_pipeline.py:911-920`) reads **only `group_idxs` ordering + `len()`** — no `MBRelease`
dependency. It is cleanly extractable into a tag-only helper shared by `run()` and the maintenance path.

## Issue inventory (40 dirs, three sub-populations)

Authoritative snapshot: `docs/census-leaf-renumber.json` (produced from the 2026-08-30 live scan;
re-run `analyze_collisions.py` from `/tmp/opencode/` against a mounted library to refresh).

- **Population A — balanced splits (33 dirs).** Comparably-sized disc fragments of one work (`[12,15]`, `[15,15]`,
  `[14,18]`). `(DISCNUMBER, TRACKNUMBER)` renumbering is **provably faithful**: it reproduces what a corrected
  `run()` computes. **Auto-fixable.**
- **Population B — stray-minority (7 dirs).** A 1–2-file fragment merged with a large one (Walcha `[1,5,15]`;
  Bruckner 5/8, Mahler 6, Idomeneo, Così Davis, Verdi Requiem). `(disc,track)` is ties-free but can be
  **musically wrong** (Walcha's stray disc-6 `XVIII` sorts to position 1). **Hold for per-dir operator review** —
  these may signal a tag mis-grouping (wrong `DISCNUMBER`/`MUSICBRAINZ_ALBUMID`) that should be fixed at the tag layer,
  not renumbered.
- **Out of scope (subset overlapping the 40).** Dirs with 2 distinct `CWP_WORKID_TOP` (two works sharing one
  directory) and dirs with truncated titles + DE/EN duplicates (Brahms *Hungarian Dances*) are a **different shape**
  (mis-grouping / over-truncation / dedup). Route to the existing over-truncation and dedup backlog tracks; do NOT
  renumber them here.

**Discarded false-positive heuristic:** a naive "non-contiguous prefix" scan flags 896 dirs — all normal nested layout
(leaf subdirs carry the work-global `CWP_MOVT_NUM`, so a single subdir shows a contiguous *sub-range* like `[10,11,12]`,
not `[1,2,3]`). The **duplicate-prefix within one directory** signal is the true anomaly. Do not ship the gap heuristic.

## Verify gate

- **VERIFY_TEST**: `~/.local/bin/tox -e test` (pytest, `fail_under = 100` branch coverage).
- **VERIFY (full)**: `~/.local/bin/tox -m analyze` (build + test + mypy strict + ruff + pylint 10.00/10 + pyupgrade).

Every session declares done only on a green `~/.local/bin/tox -m analyze`.

## Session list

| #  | Session (commit-title shaped)                                                     | Cat | Expected files |
|----|-----------------------------------------------------------------------------------|-----|----------------|
| S1 | Extract tag-only movement-renumber helper shared by run() and maintenance         | B   | `src/music_annotator/_pipeline.py`, new shared helper (e.g. `_works.py` or `_tags.py`), `tests/unit/test_pipeline.py`, `docs/census-leaf-renumber.md` (new), `docs/census-leaf-renumber.json` (new) |
| S2 | Fix source bug: regroup()/unify() re-derive CWP_MOVT_NUM over merged group        | B   | `src/music_annotator/_pipeline_maint.py`, `tests/unit/test_pipeline.py` |
| S3 ◆ | Add `renumber-leaves` subcommand: tag-rewrite + rename on the provenance chain   | I   | `src/music_annotator/_pipeline_maint.py`, `src/music_annotator/__main__.py`, `src/music_annotator/__init__.py`, `tests/unit/test_main.py`, `tests/integration/test_integration.py` |

## Session detail

### S1 — extract the shared renumber authority + census

Produce `docs/census-leaf-renumber.md` + `docs/census-leaf-renumber.json` from the validated investigation output
(at `/tmp/opencode/collisions.json` as of 2026-08-30) — directly authored, no permanent script. The investigation
script (`/tmp/opencode/analyze_collisions.py`) served its diagnostic purpose and is not ported into `scripts/`.

Extract the movement-number pass (`_pipeline.py:911-920`) into a tag-only helper:

```
def assign_group_movement_numbers(tracks_in_group_ordered) -> None
```

that takes tracks pre-sorted by `(DISCNUMBER, TRACKNUMBER)` within one `CWP_WORKID_TOP` group and writes gap-free
1-based `cwp_movt_num` / `cwp_movt_tot` / `movementnumber` / `movementtotal`. `run()` calls it with `group_idxs`
already in `(medium.position, track.position)` order (unchanged behaviour — regression-guard this). The maintenance
path will call it with tracks sorted by embedded `(DISCNUMBER, TRACKNUMBER)`. Keep the `MBRelease`-dependent passes
(composer / recording-date / first-release-date unification) in `_apply_workgroup_unification`; only the
movement-number sub-pass moves.

**KAT:** `run()` output for a single-session multi-disc work is byte-identical before/after the extraction.

### S2 — source-bug fix in the consolidation passes

In `regroup()` and `unify()`, after reading tags for a merged group and before `build_dest_path`: group the merged
files by `CWP_WORKID_TOP`, sort each group by embedded `(DISCNUMBER, TRACKNUMBER)`, and call
`assign_group_movement_numbers` to overwrite the (stale, per-session) `cwp_movt_num` in the in-memory `TrackTags`
**and** schedule the corresponding tag rewrite on disk (the passes already ride the tag-rewrite → `_verify_copy` →
journal chain). This makes consolidation renumber correctly and prevents recurrence.

**Idempotency KAT:** running the fixed `regroup`/`unify` on an already-correct (single-session) work is a no-op
(no tag change, no move).

**Convergence KAT:** after the S3 repair runs, a subsequent fixed-`regroup` over the same releases is a no-op —
repair and source-fix compute the identical `CWP_MOVT_NUM`.

### S3 ◆ — `renumber-leaves` subcommand (retroactive repair)

New top-level subcommand (sibling of `repath`/`regroup`/`unify`), following their exact shape:

- `music-annotator renumber-leaves <dest_dir> [--dry-run] [-y/--yes]`; **dry-run is the safe preview**, `--yes` skips
  the confirmation prompt.
- Scan for collision dirs (duplicate `nn` prefix within one directory). For **Population A** dirs: re-derive `CWP_MOVT_NUM` via
  `assign_group_movement_numbers`, rewrite the affected tags, recompute the destination via `build_dest_path`, and
  move — each file on the full provenance chain (SHA source → rewrite tags → move → SHA verify → `_verify_copy` tag
  round-trip → **only then** append `action="renumbered"` journal entry). Reuse the collision-suffix machinery for any
  genuine byte-identical destinations.
- For **Population B** (stray-minority) and out-of-scope dirs: **do not move**. Emit them to a report requiring
  explicit per-dir operator confirmation. `--yes` must NOT auto-consent these (integrity prompts are not bulk
  consent, per the `reconstruct-xrefs` precedent).

**Provenance-chain invariant (AGENTS.md):** no `"renumbered"` journal entry before `_verify_copy` returns
successfully; the user-facing confirmation derives only from journalled `action="renumbered"` entries gated on
verification. Preserve this exactly.

## Cross-session contracts

### C-L5 — consolidation re-derives the per-group leaf index from embedded tags  *(Defined-in S1; Consumed-by S2, S3)*

**Frozen at S1.**  The leaf `nn` prefix (`CWP_MOVT_NUM`) is the per-top-work-group gap-free playback index.  It is
session-local and must never be trusted across a merge.  The shared authority is
`assign_group_movement_numbers(tracks_in_group_ordered)` in `_tags.py`.  Ordering authority: `(DISCNUMBER,
TRACKNUMBER)` within one `CWP_WORKID_TOP` group.  Both the ingest path (`_apply_workgroup_unification`) and the
consolidation path (`regroup`/`unify`) must call this function with the same ordering rule so that the leaf `nn` is
idempotent across sessions.

## Progress ledger

| #   | Session                                                                                    | Status  | Commit  | Froze  |
|-----|--------------------------------------------------------------------------------------------|---------|---------|--------|
| S1  | Extract tag-only movement-renumber helper shared by run() and maintenance                  | done    | e2a6e27 | C-L5   |
| S2  | Fix source bug: regroup()/unify() re-derive CWP_MOVT_NUM over merged group                | pending | —       | —      |
| S3 ◆ | Add `renumber-leaves` subcommand: tag-rewrite + rename on the provenance chain           | pending | —       | —      |

## Action-frame digest

*(none yet)*

## Notes for executors

**Register / anneal denylist.** Durable files state the property/invariant, never the plan coordinate. Do not write
`S1`/`S2`/`S3`, `Population A/B`, or `renumber-leaves`-as-session-vocabulary into source, tests, or docstrings.
Legitimate durable vocabulary: the tag names (`CWP_MOVT_NUM`), the ordering authority
(`(DISCNUMBER, TRACKNUMBER)` within `CWP_WORKID_TOP`), the `action="renumbered"` journal verb, and the invariant
"leaf `nn` is the per-top-work-group gap-free playback index, re-derived from tags on every consolidation."

**Contract naming.** If a durable contract name is minted for the shared authority, name it in the C-L* family
(e.g. C-L5: "consolidation re-derives the per-group leaf index from embedded `(DISCNUMBER, TRACKNUMBER)`; embedded
`CWP_MOVT_NUM` is session-local and never trusted across a merge"). Record it in `docs/NOTES.md` at the ◆ boundary.

**Cross-reference.** This closes the residual half of the L0/L1 leaf-numbering work for the cross-session-then-
consolidated class, and complements the R4b fragmentation census (which addresses top-dir fragmentation; this
addresses the leaf-collision that consolidation leaves behind). The `scan_nonuniform_depth.py` header already names
"the leaf-numbering bug" — this plan is its retroactive closure.
