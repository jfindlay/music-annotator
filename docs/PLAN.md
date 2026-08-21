<!-- juncture-tier: opus -->
<!-- multi-sub-track plan on the library-completion arc (docs/ROADMAP.md).  Four corrective
     sub-tracks, each a confirmed defect in existing machinery, none freezing new policy:
       A. Over-long-name truncation strips the file extension (7 stranded FLACs found live).
       B. Maintenance-pass collision disambiguation emits an empty ` []` suffix (default-constructed
          MBRelease passed into the suffix builder; the real release_id was in hand and discarded).
       C. Top-dir derivation injects the album name and drops the performer for compilation /
          no-composer releases — a regression against C-UNIVERSAL's own documented intent
          (~2640+ live top-dir renames observed replacing "<composer> - <conductor; ensemble>" with
          "<composer> - <ALBUM>").  Corrective: restore C-UNIVERSAL; do NOT decide the editorial
          field set/order (soloist promotion etc.) — that is J2 policy (docs/ROADMAP.md).
       D. scripts/ drain: three scripts are superseded by shipped CLI actions and two are spent
          one-shot census tools; delete them.  (The two uncovered scan_* scripts migrate into `audit`
          under ROADMAP R4b — not this shard.)
     A/B/C are pre-R6d correctness blockers: R6d's destructive re-derivation would either strand the
     extension-less files (A), lay the library out with non-identifying ` []` directories (B), or bake
     the album-name-in-path regression across the whole library (C).  The sub-tracks touch different
     modules and have independent KATs; sequence A → B → C → D. -->

# PLAN — pre-R6d correctness fixes (four sub-tracks)

## Purpose (design intent)

*(Re-read at every ◆ boundary — anti-defocus anchor.)*

Four confirmed defects in the already-shipped machinery.  A/B/C would corrupt a live R6d destructive
re-derivation; D is housekeeping.  All are corrective (restore an existing invariant or documented
intent), none freezes new policy.  They are orthogonal — different modules, different failure modes,
different KATs — but share one arc goal: **every actioned file lands as a suffixed, in-bounds,
uniquely-disambiguated, performer-identifying, journalled track.**

- **Sub-track A — extension-loss on over-long names.**  Ingest truncation eats the `.flac`/`.mp3`
  suffix on leaves whose title fills the byte budget; 7 real files are stranded (invisible to every
  maintenance pass, which all gate on an audio suffix).
- **Sub-track B — empty collision suffix in maintenance passes.**  `repath`/`regroup`/`unify` pass a
  *default-constructed* `MBRelease()` into the shared collision-suffix builder, so the disambiguator
  emits ` []` (empty brackets) instead of a release-identifying token — silently defeating the whole
  point of collision disambiguation.
- **Sub-track C — top-dir injects album name, drops performer.**  `_top_dir_component`'s
  compilation branch (keyed on the MB free-classification parameter `releasetype_secondary`) and its
  no-composer branch both build the topmost path component as `<…> - <ALBUM>`, discarding the
  conductor/ensemble.  This *contradicts C-UNIVERSAL's own documented intent* (`docs/NOTES.md`): the
  topmost component must not derive from a free-classification parameter, and album identity migrates
  to the playlist lens, never a directory.  Confirmed ~2640+ live top-dir renames replacing a real
  performer blob with an album/collection title (undercount — ensemble-name cases missed by the
  heuristic).  **Operator ruling (2026-08-20, editorial authority): album name belongs to the
  playlist lens only; the general library taxonomy is uniform-strict on composer(s), soloist(s),
  conductor(s), ensemble(s), rec/rel year, work taxonomy.**  This *overturns REND-23/C-INIT* (which
  had frozen "compilation/recital → `<albumartist> - <album>`" as deliberate) in favour of
  C-UNIVERSAL's stated principle; the two frozen artifacts were in conflict and the operator resolved
  it C-UNIVERSAL's way.  Corrective scope for this shard: restore the `<composer> - <performers>`
  shape and the performer-led floor (drop album-name injection; stop keying on
  `releasetype_secondary`).  **Deferred within the operator ruling (return-and-revise-later,
  explicitly NOT in this shard):** the operator now wants the **soloist in the path** — reversing the
  earlier SEL-11 "universally added then universally removed" as likely overengineered.  That is a
  **C-NOSOLO reopen**; it is desired but sequenced after the unfinished corrective/drain tasks, routed
  to ROADMAP R6d planning.  S5/S6 therefore restore performer-in-path **without** adding the soloist
  yet — the soloist lands in a later, separately-owned revision.
- **Sub-track D — scripts/ drain.**  Delete the three scripts superseded by shipped CLI actions
  (`preflight`, `repatch-acoustid`, `repatch-catalogue-colon`) and the two spent one-shot census
  tools whose artifacts are already committed under `docs/`.

**Out of scope (named, not silently dropped).**

- **No R6d destructive run.**  A/B/C clear pre-R6d blockers; none runs the pass.
- **No soloist-in-path in this shard.**  Sub-track C restores C-UNIVERSAL (performer in path, album
  name out of path) and *overturns REND-23* per the operator ruling, but does NOT add the soloist —
  even though the operator now wants it (a C-NOSOLO reopen).  The soloist promotion is deferred to a
  later revision (operator: "focus on completing the unfinished tasks; return and revise later"),
  routed to ROADMAP R6d planning.  The depth clamp and leaf/work-dir rendering are untouched.
- **No `audit` extension in this shard.**  The two uncovered scan scripts
  (`scan_fragmentation.py`, `scan_nonuniform_depth.py`) hold detection logic not yet in the package;
  migrating them into `audit` is a design-bearing item routed to ROADMAP R4b, not deleted here.
- **No destructive live repair of the affected files in-shard.**  The fixes are built and proven on
  fixtures; the real files on hades are repaired by the operator's next `repath` under the existing
  move/verify/journal provenance.

## Verify gate

Discovered from `pyproject.toml` (tox envs; do not assume `make`).

- **VERIFY_TEST**: `~/.local/bin/tox -e test` (`pytest tests/`; **100% branch coverage**).  Every new
  branch needs an explicit test.
- **VERIFY_TYPES**: `~/.local/bin/tox -e check_type` (`mypy src/ tests/`, strict; no `Any`, no
  `cast()`).
- Full gate before every ledger-done: `~/.local/bin/tox -m analyze` (build + test + type + format +
  lint 10.00/10 + pyupgrade).  Import order via `~/.local/bin/tox -m edit`, never hand-edited.

## Session list

| # | Session | Cat | Tier | Sub-track | Consumes | Expected files |
|---|---------|-----|------|-----------|----------|----------------|
| 1 | Fix source truncation to reserve+preserve the suffix (stem+suffix ≤ NAME_MAX) | C | Sonnet | A | C-L0/C-L1 (leaf naming) | `src/music_annotator/_tags.py`, `src/music_annotator/_pipeline.py`, `tests/unit/test_annotator.py`, `tests/unit/test_pipeline.py` |
| 2 ◆ | Add repath repair case for extension-less track files | C | Sonnet | A | C-PROV/C-MOVE, S1 output | `src/music_annotator/_pipeline_maint.py`, `tests/unit/test_pipeline.py` |
| 3 | Thread the real release id into the maintenance-pass collision suffix | C | Sonnet | B | C-COLLISION (existing suffix machinery) | `src/music_annotator/_pipeline_maint.py`, `tests/unit/test_pipeline_maint.py` |
| 4 ◆ | Guard `_collision_suffix` against empty id + assert suffix content in tests | C | Sonnet | B | S3 output | `src/music_annotator/_pipeline.py`, `tests/unit/test_pipeline.py`, `tests/unit/test_pipeline_maint.py` |
| 5 | Stop keying the top dir on `releasetype_secondary`; drop album-name injection in the compilation branch | C | Sonnet | C | C-UNIVERSAL (restore documented intent) | `src/music_annotator/_tags.py`, `tests/unit/test_annotator.py` |
| 6 ◆ | Stop the box-set performers component falling back to the edition title on repath (+ drop ` - <ALBUM>` in the no-composer branch) | C | Sonnet | C | S5 output, C-UNIVERSAL | `src/music_annotator/_tags.py`, `src/music_annotator/_pipeline_maint.py`, `tests/unit/test_annotator.py`, `tests/unit/test_pipeline_maint.py` |
| 7 | Delete the three superseded scripts | C | Sonnet | D | (shipped CLI actions supersede) | `scripts/preflight_r6d.py`, `scripts/scan_acoustid_tags.py`, `scripts/scan_catalogue_colon.py` (deletions) |
| 8 ◆ | Delete the two spent census scripts after verifying artifacts are committed | C | Sonnet | D | S7 | `scripts/census_original.py`, `scripts/census_styleguide.py` (deletions) |

`Cat`: **C (corrective)** — all eight rows fix a confirmed defect or drain dead code; none freezes a
new policy.  ◆ boundaries: S2 (Sub-track A), S4 (Sub-track B), S6 (Sub-track C), S8 (Sub-track D).

`Tier`: **Sonnet** throughout.  Every defect's root cause is confirmed by code read, the fixes are
bounded, and KATs pin correctness.  Sub-track C's editorial residue (soloist promotion) is
deliberately deferred per the operator ruling (2026-08-20), not left open for the executor.  The one
in-shard fork is S6's D-12 diagnostic (hydration-loss vs data-absence) — a *diagnostic*, not a design
decision: both branches have a defined fix and KAT, and the executor halt-and-surfaces if a fixture
cannot disambiguate.  `juncture-tier: opus` kept (arc default); no juncture fires in an eight-row
corrective plan.

**Sizing (levers named).**  Default band ~150–400 LOC / 2–4 files.  All rows sit below band — each is
a focused correction or deletion, not a new subsystem.

- **S1 ≈ 40–90 LOC** — `_tags.py` (`_proposed_short` extension-awareness) + `_pipeline.py`
  (measurement site) + tests.
- **S2 ≈ 40–80 LOC** — `_pipeline_maint.py` (repath repair branch) + tests.
- **S3 ≈ 30–70 LOC** — thread `release_id` through the three passes' plan-building + suffix call;
  build the stub from the real id.  Below band — a threading correction, not new logic.
- **S4 ≈ 30–60 LOC** — one guard in `_collision_suffix` + suffix-asserting test additions.
- **S5 ≈ 30–70 LOC** — remove/rework the compilation branch in `_top_dir_component` + tests.
- **S6 ≈ 40–90 LOC** — the box-set performers fix (hydration repair *or* guarded fallback, per the
  D-12 diagnostic) in `_tags.py`/`_pipeline_maint.py` + drop the no-composer ` - <ALBUM>` suffix +
  tests.  If the D-12 diagnostic shows a deep hydration bug, this can exceed band — resize/split then.
- **S7 ≈ 0 LOC** — three file deletions; verify nothing imports them (they are `scripts/`-local).
- **S8 ≈ 0 LOC** — two file deletions after confirming `docs/census-r0.{json,md}` and
  `docs/census-library.{json,md}` are present and complete.

**Why B is two rows, not one.**  S3 is the functional fix (real id reaches the suffix builder); S4 is
the defence-in-depth guard *plus* the test-assertion correction that would have caught the bug.  They
have different KATs (S3: suffix is release-identifying; S4: empty id raises, and tests assert the
produced suffix value).  Keeping them separate keeps each commit single-focus.

**Why C is two rows, not one.**  S5 and S6 fix two *different* mechanisms that both put a
release/edition/album name where a performer belongs.  S5: the `releasetype_secondary`-keyed
compilation branch of `_top_dir_component` injects `<ALBUM>` (the Karajan "Ouvertüren" case, ~low
hundreds).  S6: the Case-3 performers component collapses to the edition title on repath (the
"Complete Mozart Edition" / "Bach Edition" / "The String Quartets" case, ~5000+ — the dominant bug),
a `build_dest_path`/`_hydrate_performer_lists` defect, plus the smaller no-composer ` - <ALBUM>` drop.
They have different root modules and different KATs; keeping them separate isolates the two distinct
C-UNIVERSAL violations and keeps each commit single-focus.

## Session detail

### S1 — Fix source truncation to reserve+preserve the suffix

**Deliverable.**  The over-long-name truncation path *always* produces a leaf whose stem **plus the
audio suffix** is ≤ `_NAME_MAX` bytes and which retains a correct `.flac`/`.mp3` extension.

**Root cause (confirmed by code read).**  At ingest (`_pipeline.py`):

1. `dest_file = dest_base.with_suffix(src_file.suffix.lower())` (`_pipeline.py:1798`) attaches the
   `.flac` suffix — the leaf is `01 - …op. 23.flac`.
2. `_resolve_long_names` (`_pipeline.py:796`) measures each **whole part including the `.flac` leaf**
   (`len(part.encode("utf-8")) > _NAME_MAX`, ~line 826) and calls `_proposed_short` (`_tags.py:125`).
3. `_proposed_short`'s ellipsis fallbacks (word-boundary `_tags.py:193`; hard byte-cut `_tags.py:205`)
   cut to `_NAME_MAX - 3` bytes and append `…`, **treating the trailing `.flac` as ordinary bytes
   with no awareness it must be preserved.**  For a leaf where the title fills the budget, `.flac` is
   inside the cut region and is eaten.

**The fix (executor picks the cleaner of two shapes; both must satisfy the KAT).**

- **Option A — suffix-aware `_proposed_short`.**  Pass the extension (or its byte length) into
  `_proposed_short` so every strategy budgets `_NAME_MAX - len(suffix.encode())` for the stem and the
  suffix is re-appended after truncation, never inside the cut.  Callers: `_resolve_long_names`
  (`_pipeline.py:827`) and the prompt sites (`_pipeline.py:174`, `_discover.py:140,302`) that display
  `_proposed_short` as the default.
- **Option B — measure/truncate the stem, not the whole leaf.**  In `_resolve_long_names`, split the
  leaf into stem + suffix, run `_proposed_short` on the stem against a suffix-reduced budget, then
  re-attach the suffix.  Leaves `_proposed_short` unchanged.

**Register subtlety — `safe_name` trailing dots.**  `safe_name` (`_tags.py:103`) *intentionally
preserves trailing dots* ("op." with no opus number).  The stem legitimately ends in `.` — the fix
must split stem/suffix on the **known audio extension** (`src_file.suffix`), NOT on
`Path.suffix`/`os.path.splitext` (which would treat `op. 23`'s `. 23` as the extension — the very
confusion that produced the bug).

**KAT (must be in the test files):**
- Title within `_NAME_MAX` but title+`.flac` over → shortened, ends `.flac`, ≤ `_NAME_MAX`.
- Title already over `_NAME_MAX` → shortened, ends `.flac`, ≤ `_NAME_MAX`.
- Regression guard: a normal short leaf is unchanged (no gratuitous ellipsis).
- Trailing-dot case (`…op. 23.flac`): the `.` in `op. 23` is NOT mistaken for the extension.

**Subtleties.**
- Replace the `_pipeline.py:823-825` comment ("the extension is short … measure the full part") — the
  hand-wave that caused this — with corrected reasoning (stem measured against a suffix-reduced
  budget; suffix always re-appended).  State the *property*, not the plan coordinate.
- 100% branch coverage: each new strategy branch and the stem/suffix split need explicit arms.

### S2 ◆ — Add repath repair case for extension-less track files

**Deliverable.**  `repath` detects an extension-less file that is a valid audio track, appends the
correct suffix, verifies the repaired leaf is within `_NAME_MAX`, and moves it through the normal
C-PROV chain — in place of the current `repath_unsupported_format` + silent skip
(`_pipeline_maint.py:503-510`).

**Downstream harm this repairs.**  Every maintenance pass filters on an audio suffix before acting
(`_pipeline_maint.py:1343, 1532, 1770` existence filters; `repath` matches `ext` at `503-510`).  The
7 extension-less files are invisible to repath/regroup/unify — they silently opt out.  R6d's
destructive re-derivation would leave them stranded at broken names.

**The repair logic.**
- When `current_path.suffix.lower()` is not in `{".flac", ".mp3"}`, **do not immediately skip.**
  Probe with mutagen (the same readers `_read_tags_flac`/`_read_tags_mp3` use): a FLAC opens as
  `mutagen.flac.FLAC`, an MP3 as `ID3`/`MP3`.  On success, determine the correct extension.
- **Compute the repaired leaf** = current stem + correct suffix.  If it exceeds `_NAME_MAX`, run it
  through the S1-fixed shortening (the 7 real files are *already at/over* the limit — the repair MUST
  re-shorten, not just append).  This is why S2 consumes S1.
- **Move via `_move_verify_journal`** (`_pipeline_maint.py:301`) — hash → move → verify → journal.
  Executor decides the action string (`"repathed"` is acceptable; a distinct action only if the audit
  vocabulary needs it — pick the lighter and state which).
- **If NOT identifiable as audio**, keep log-and-skip, but with a message distinguishing "not a track
  file" from the old misleading "unsupported format".

**KAT (must be in the test files):**
- repath over a library with an extension-less-but-valid FLAC: identified, renamed `.flac`,
  length-checked, journalled through C-PROV — no skip warning.
- repath over a genuinely non-audio extension-less file: skipped with the *new* "not a track"
  message, no move, no journal entry.
- Repaired-leaf-over-`_NAME_MAX`: the append triggers S1 shortening; final name in-bounds, ends
  `.flac`.

**Subtleties.**
- **Do not weaken C-PROV.**  No journal entry before `_verify_copy` passes (repo `AGENTS.md`
  confirmation-provenance invariant).
- **Idempotency.**  After repair the file has a suffix — no-op on the next repath.  The repair branch
  must not re-fire on an already-suffixed file.
- **Ordering.**  A `repath` repair pass should run **before** the destructive regroup/unify in R6d so
  those passes see the now-suffixed files (natural extension of the existing Phase-A-before-Phase-B
  rule — see the R6d node).

### S3 — Thread the real release id into the maintenance-pass collision suffix

**Deliverable.**  `repath`, `regroup`, and `unify` derive the collision-disambiguation suffix from
the file's **real release id**, so the disambiguated directory carries a release-identifying token
(the 8-char MBID prefix) instead of an empty ` []`.

**Root cause (confirmed by code read).**  All three passes call the shared
`_apply_collision_suffix` (`_pipeline.py:497`), which derives its suffix via `_collision_suffix`
(`_pipeline.py:474`).  That helper's guaranteed branch is `return release.id[:8]` (`_pipeline.py:494`)
— but each pass passes a **default-constructed `MBRelease()`** (`_pipeline_maint.py:639, 861, 1243`),
whose `id` defaults to `""` (per the every-field-defaults-empty model convention).  So
`_collision_suffix` returns `""`, and `_apply_collision_suffix` formats `f"{work_dir} [{suffix}]"`
(`_pipeline.py:539`) → `work_dir []`.  The `collision_nonmatch_suffix` warning logs `suffix=` empty,
exactly as observed.

**The real id is already in hand — the fix is threading, not a network fetch.**
- `_resolve_current_lib` (`_pipeline_maint.py:264`) returns a `{current_path: release_id}` map,
  seeded from `"tagged"` entries (real MBID) and preserved across moves via `pop(old_path, …)`
  (`:291`).
- **`regroup` and `unify` already carry the real `rid`** in each `plan_pairs` tuple (5th element,
  e.g. `:864-865`, `:1246-1247`) and *discard* it in favour of `MBRelease()`.  Build the stub from
  that `rid` instead.
- **`repath` drops the id** at `existing_files = [p for p in current_lib]` (`:488`) — it keeps only
  the path.  Thread the id from `current_lib` through `_repath_file_data` (`:499`, currently a
  4-tuple) into `plan_pairs` (`:549`, currently a 4-tuple) so it is available at the suffix call
  (`:638-640`).

**The stub construction.**  Replace `MBRelease()` at the three sites with `MBRelease(id=<the file's
release_id>)`.  The catalog-number branch of `_collision_suffix` (`_pipeline.py:490-493`) is
unreachable from embedded tags (no `label_info_list` available in maintenance) — the MBID-prefix
branch is the operative one and is what the real id feeds.

**Per-file id note.**  `regroup`/`unify` are already release-driven (one `_move_verify_journal` batch
per unique `rid`), so different files in one plan may carry different ids — the stub must be built
per-collision-entry, not once per pass.  `_apply_collision_suffix` currently takes one `release` for
the whole batch; the executor must either (a) group non-matches by `rid` and call once per group, or
(b) refactor the suffix application to look up the id per entry.  **Prefer (a)** — it reuses the
existing per-`rid` grouping the passes already do and keeps `_apply_collision_suffix`'s signature
stable for the ingest caller (`_pipeline.py:1831`, which legitimately has one real release).

**KAT (must be in the test files):**
- A `repath`/`regroup`/`unify` collision non-match produces a work_dir suffixed with the real
  8-char MBID prefix — **assert the resulting path contains `[<mbid8>]`, not `[]`.**
- Two different non-matching releases colliding in one pass get *distinct* suffixes (the failure the
  empty ` []` masked: two files would both get `[]` and re-collide).

**Subtleties.**
- Do not touch the ingest caller's behaviour (`_pipeline.py:1831`) — it already has a real
  fetched release.  The fix is confined to the three maintenance passes.
- The `release_id` may be `""` for a file whose only journal entry is a `repathed` row with empty id;
  `_resolve_current_lib`'s `pop` preserves the *seeded* id, so a tagged-then-repathed file resolves
  to its real id.  A file with genuinely no seeded id is the S4 guard's responsibility.

### S4 ◆ — Guard `_collision_suffix` against empty id + assert suffix content

**Deliverable.**  (1) `_collision_suffix` never silently returns `""`; an empty id is a loud failure,
not a degenerate ` []`.  (2) The collision tests assert the *value* of the produced suffix, closing
the coverage-blind gap that let the ` []` bug survive the full gate.

**Why the guard, given S3 fixes the source.**  Defence in depth.  S3 makes the real id reach the
builder in the normal path; the guard is a regression tripwire for any future caller that reconstructs
a release without an id.  The docstring already *promises* `release.id[:8]` is "guaranteed unique and
always present" (`_pipeline.py:485`) — the guard makes that promise enforced rather than assumed.

**The guard.**  In `_collision_suffix`, after both branches, if the result is empty raise a
`RuntimeError` (or `ValueError`) naming the invariant: a collision suffix cannot be derived without a
release id.  **Tradeoff named:** fail-loud turns a live pass into an abort instead of limping on.
That is *worse* at resilience but *better* at data-integrity — a degenerate ` []` silently corrupts
the library layout, which is exactly what this subsystem exists to prevent, and it matches the repo's
"do not silently degrade" posture (repo `AGENTS.md` defensive-download invariant).  After S3 the guard
should essentially never fire.

**The test-assertion correction (the reason the bug survived `tox -m analyze`).**  The existing
collision tests are **coverage-complete but assertion-blind on the suffix value**:
`test_unify_collision_suffix_applied` (`test_pipeline_maint.py:4441`) executes `_apply_collision_suffix`
(coverage satisfied) but only asserts a `"unified"` journal entry exists — it never asserts the
resulting *path*.  100% branch coverage is not value-correctness.  This row adds assertions on the
produced suffix to the maintenance-pass collision tests (`test_pipeline_maint.py:4441` and the
`repath`/`regroup` equivalents), and a direct `_collision_suffix` empty-id test in
`test_pipeline.py` (near the existing `_collision_suffix` tests at `test_pipeline.py:7960-8008`).

**KAT (must be in the test files):**
- `_collision_suffix(MBRelease())` (empty id, no label info) **raises** — with a message naming the
  missing-id invariant.
- `_collision_suffix(MBRelease(id="abcdef12-…"))` returns `"abcdef12"` (existing behaviour, keep).
- Each maintenance-pass collision test asserts the produced work_dir ends in `[<mbid8>]` (a real,
  non-empty, release-identifying token).

**Subtleties.**
- The empty-id raise adds a branch — it needs its own coverage arm.
- Do not let the guard fire in the ingest path: the ingest release always has an id (fetched);
  the guard's only realistic trigger is a mis-threaded maintenance stub, which S3 prevents.

### S5 — Stop keying the top dir on `releasetype_secondary`; drop album-name injection (compilation branch)

**Deliverable.**  `_top_dir_component`'s compilation branch no longer keys on the MB
free-classification parameter `releasetype_secondary`, and no longer builds the topmost path
component as `<…> - <ALBUM>`.  A release with a linked composer renders `<composer> - <performers>`
(the Case-3 shape) regardless of any "Compilation" secondary type — restoring C-UNIVERSAL's
documented intent that the topmost, most topology-defining component is derived only from
scholarship-stable data and never carries the album name.

**Root cause (confirmed by code read).**  `_top_dir_component` (`_tags.py:233–296`):

1. Case 1 (`_tags.py:273–280`) fires when `releasetype_secondary` contains `"Compilation"` and
   returns `safe_name(f"{artist_component} - {album}")` — the album name enters the topmost path
   component, and the trigger is a free-classification parameter.
2. This directly contradicts the C-UNIVERSAL rationale recorded in `docs/NOTES.md` (the epistemic
   criterion: free-classification parameters — `releasetype_secondary` types named explicitly — must
   never define library topology; album/sequence identity migrates to the playlist lens).
3. Observed live: the Karajan "Ouvertüren" discs (MB-flagged Compilation) render
   `Rossini - Ouvertüren` instead of `Rossini - Herbert von Karajan; Berliner Philharmoniker`,
   discarding a perfectly good conductor+ensemble credit.

**The fix.**  When a composer is present (`CWP_COMPOSER_LASTNAMES` or `CEA_COMPOSER_LASTNAMES`
non-empty), fall through to the `<composer> - <performers>` default (Case 3) — i.e. the compilation
branch stops short-circuiting on `releasetype_secondary`.  The performers component is the one already
assembled in `build_dest_path` (`_tags.py:1204–1216`, album-level conductors/ensembles with the
existing per-track and `ARTIST` fallbacks).  Do **not** add the soloist here — the operator wants the
soloist in the path eventually (a C-NOSOLO reopen) but has deferred it to a later revision; S5 stays
a bounded restore, not the soloist promotion (ROADMAP R6d planning owns that).

**KAT (must be in the test files):**
- A release with `releasetype_secondary` containing "Compilation" **and** a linked composer renders
  `<composer> - <performers>` — assert the produced top dir equals e.g.
  `Rossini - Herbert von Karajan; Berliner Philharmoniker`, and that it does **not** contain the
  `ALBUM` value ("Ouvertüren").
- Regression guard: a non-compilation single-composer release is unchanged.

**Subtleties.**
- **Do not touch the genuinely-composerless path** — that is S6.  S5 only removes the
  `releasetype_secondary` short-circuit for composer-bearing releases.
- **REGISTER rule.**  State the property in code/tests — "the topmost path component derives only from
  composer + performers; the album name never appears in the path; a free-classification parameter
  never gates it" — never a plan coordinate.
- Update the `_top_dir_component` docstring (`_tags.py:236–265`), which currently *documents* the
  album-name compilation shape, to state the restored invariant.

### S6 ◆ — Stop the box-set performers component from falling back to the edition title on repath

**This is the dominant bug (~5000+ renames), and it is a different mechanism from S5.**  It is *not*
Case 1 (Compilation) and *not* the no-composer branch — the composer is present, so
`_top_dir_component` returns `None` (Case 3) and `build_dest_path` renders `<composer> -
<performers>`.  The defect is that on **repath** the `<performers>` component collapses to the
release's *edition/collection title* (e.g. `Complete Mozart Edition`, `Bach Edition`,
`The String Quartets`, `The Piano Sonatas`) instead of the real conductor/ensemble.

**Deliverable.**  On `repath` (and `regroup`/`unify`), a composer-bearing box-set track renders
`<composer> - <real conductor; ensemble>` — matching what the *current on-disk path already shows*
(e.g. `Mozart - Sir Neville Marriner; Academy of St Martin in the Fields`) — not
`<composer> - <edition title>`.

**Root cause (confirmed by code read).**

1. The current on-disk paths carry the *correct* performer (`Sir Neville Marriner; Academy of St
   Martin in the Fields`), so the performer was renderable at ingest via the per-track
   `cea_conductors_list`/`cea_ensembles_list` fallback (`_tags.py:1213`).  Ingest writes the
   recording-relation conductors/ensembles to `CEA_CONDUCTORS`/`CEA_ENSEMBLES`.
2. `ARTIST` is set at ingest to `rec_artist_phrase` — the *recording-level* MB artist credit
   (`_tags.py:801`).  For box-set recordings that credit is the edition/collection title, so the
   embedded `ARTIST` tag literally contains `Complete Mozart Edition`.
3. On repath, `_hydrate_performer_lists` (`_pipeline_maint.py:517–522`) exists *specifically* to
   rebuild the performer `ArtistEntry` lists from the embedded `CEA_*` tags so `build_dest_path` does
   **not** "fall back to the raw CEA_ENSEMBLE_NAMES / ARTIST string" (its own comment).  When those
   lists come back empty, `build_dest_path`'s final fallback (`_tags.py:1216`,
   `CEA_ENSEMBLE_NAMES or ARTIST`) fires and returns the edition title from `ARTIST`.
4. `_is_album_artist` (`_tags.py:751`) filters the album-level lists against `release.artist_credit`,
   which for a box set is the composer/edition — so `CEA_ALBUM_CONDUCTORS`/`CEA_ALBUM_ENSEMBLES` are
   empty, and the per-track lists are the only carrier of the real performer.

**The load-bearing diagnostic the executor MUST run FIRST (D-12).**  The current path proves the
performer was renderable once, so exactly one of these is true and the fix differs per branch:
- **(a) Hydration-loss (corrective).**  The embedded files *do* carry `CEA_CONDUCTORS`/`CEA_ENSEMBLES`,
  but `_hydrate_performer_lists` fails to populate the per-track lists (a repath re-derivation
  regression).  Fix: repair hydration so line 1213 fires and the performer is preserved.  Pure
  corrective.
- **(b) Data-absence (needs a floor decision).**  The embedded files genuinely lack the `CEA_*`
  performer keys (ingest never wrote them for these box sets, or wrote them empty), so no
  tag-only re-derivation can recover the performer.  Then the final fallback at `_tags.py:1216` must
  stop using `ARTIST` when `ARTIST == ALBUM`/`== ALBUMARTIST` (the edition-title tell), and the
  correct render is `<composer>` alone (or a documented "Unknown Performers"), never the edition
  title.  This still restores C-UNIVERSAL (no album/edition name in the path) but is a fallback
  redesign, not a hydration fix.

The executor determines (a) vs (b) by inspecting a real embedded file's tags on hades (operator-run,
read-only) or by a fixture that reproduces each shape.  **If it cannot be determined without live
tags, halt-and-surface** — do not guess, because (a) and (b) have different fixes and different KATs.

**The fix (both branches share this floor).**  The `<performers>` component must **never** resolve to
the release/edition title.  At minimum, `_tags.py:1216`'s `ARTIST` fallback is guarded: if `ARTIST`
equals `ALBUM` or `ALBUMARTIST` (edition-title tell), do not use it — drop to the composer-only render
or an explicit "Unknown Performers", never the edition string.  Do **not** add the soloist (deferred
C-NOSOLO reopen).

**KAT (must be in the test files):**
- A composer-bearing box-set track whose embedded `CEA_CONDUCTORS`/`CEA_ENSEMBLES` carry the real
  performer renders `<composer> - <conductor; ensemble>` on repath — assert it equals
  `Mozart - Sir Neville Marriner; Academy of St Martin in the Fields` and does **not** contain the
  `ALBUM`/edition value (`Complete Mozart Edition`).
- A composer-bearing box-set track whose embedded tags carry `ARTIST == ALBUM` (edition title) and no
  `CEA_*` performer keys renders **without** the edition title (composer-only or "Unknown
  Performers") — assert the top dir does not contain the edition string.

**Subtleties.**
- **Do not regress the performer-led floor.**  An empty top dir corrupts downstream `work_top_dir`
  derivation (`_tags.py:1227–1231` comment); the branch must always yield a non-empty component.
- **The separate genuinely-no-composer, performer-led case** (Case 2, `_tags.py:288–293`) also
  appends ` - <ALBUM>`; drop that album suffix here too so a true performer-led release lands under
  its performer/`ALBUMARTIST`, not under the album title.  This is the smaller, cleanly-corrective
  half of S6.
- 100% branch coverage: the hydration path (or the guarded fallback), the edition-title guard, and
  the floor each need explicit arms.
- **REGISTER rule.**  Same as S5 — state the property ("the performers path component never resolves
  to the release/edition title") — never a plan coordinate.

## Cross-session contracts

### Produced (frozen this plan)

- **None.**  All four sub-tracks are corrective: they restore already-frozen invariants (C-L0/C-L1
  leaf naming; C-PROV/C-MOVE move provenance; the collision-suffix machinery's promise that every
  disambiguated path is release-identifying; C-UNIVERSAL's documented intent that the topmost path
  component is scholarship-stable and carries no album name), or drain dead code (Sub-track D).  No
  new contract.  A Cat-C shard that freezes no contract is legitimate when the fix restores an
  existing invariant.  Note: Sub-track C restores C-UNIVERSAL's *documented* intent but deliberately
  does **not** decide the editorial residue (soloist promotion, performer field set/order) — that is
  frozen later at J2 (`docs/ROADMAP.md`), not here.

### Consumed (frozen upstream — honour, do not re-open)

- **C-L0 / C-L1** — leaf/intermediate numbering.  Sub-track A operates on the leaf *after* numbering.
- **C-PROV / C-MOVE** — move/verify/journal provenance.  Both sub-tracks' moves go through
  `_move_verify_journal` unchanged.
- **C-COLLISION** (the shared `_assess_collisions` / `_apply_collision_suffix` / `_collision_suffix`
  machinery, single collision authority for both ingest and maintenance) — Sub-track B restores its
  release-identifying-suffix promise; it does not change the collision *decision* logic.
- **C-UNIVERSAL** (the universal, prefix-less top-dir over scholarship-stable components; the tag
  layer ≠ path layer boundary; album identity migrates to the playlist lens; free-classification
  parameters never define topology) — Sub-track C restores this contract's documented intent in
  `_top_dir_component`; it does not change the contract, the depth clamp, or the work/leaf rendering.
  The editorial field set/order the contract leaves open remains open here (J2 owns it).
- **The confirmation-provenance + defensive-download invariants** (repo `AGENTS.md`) — no journal
  entry before `_verify_copy` passes; fail-loud on integrity errors (Sub-track B's guard is an
  instance of this posture).
- **`_NAME_MAX = 255`** (`_tags.py:94`) — unchanged; Sub-track A makes truncation *respect* it
  inclusive of the suffix.
- **Model convention** — every `MBRelease` field defaults to `""`/`[]` (repo `AGENTS.md`).  This is
  *why* `MBRelease().id[:8] == ""`; Sub-track B does not change the convention, it stops relying on a
  default-constructed release for a value the default cannot supply.

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 1 | Fix source truncation to reserve+preserve the suffix | done | 0c894d1 | (none — corrective) |
| 2 ◆ | Add repath repair case for extension-less track files | done | bdf23a6 | (none — corrective) |
| 3 | Thread the real release id into the maintenance-pass collision suffix | done | 040a179 | (none — corrective) |
| 4 ◆ | Guard `_collision_suffix` against empty id + assert suffix content | done | 166e506 | (none — corrective) |
| 5 | Stop keying top dir on `releasetype_secondary`; drop album-name injection (compilation branch) | done | 9b19b91 | (none — corrective) |
| 6 ◆ | Drop album-name injection in the no-composer branch; keep performer-led floor | done | 0c5eb5a | (none — corrective) |
| 7 | Delete the three superseded scripts | done | a2f1459 | (none — drain) |
| 8 ◆ | Delete the two spent census scripts after verifying artifacts | pending | — | (none — drain) |

## Action-frame digest

### S6 — 2026-08-20
Discovery/flex: D-12 diagnostic resolved as branch (a) — hydration-loss: _hydrate_performer_lists was creating per-track ensemble ArtistEntry objects with MBIDs from MUSICBRAINZ_ALBUMARTISTID (the release's artist-credit MBID pool), which for box-sets is the edition entity's MBID, not the ensemble's; _canonical_name then returned the edition entity's name. Fix: per-track ensemble entries always created without MBIDs so _canonical_name falls back to entry.name (the as-credited CEA_ENSEMBLES value).
Affected: none (corrective — no contract changed)
Deferred: no
Texture: The D-12 diagnostic was resolvable from code analysis alone (no live tags needed); the hydration bug was in MBID sourcing, not in tag absence. The data-absence branch (b) was not needed.

## Discoveries & risks

- **D-1 (Sub-track A — why it exists).**  Live-library find: 7 extension-less FLACs, stranded from
  every maintenance pass because truncation ate the `.flac`.  Confirmed: `_proposed_short` ellipsis
  strategies cut `_NAME_MAX - 3` bytes with no suffix-awareness on a leaf already carrying `.flac`.
  *internal-continue.*
- **D-2 (Sub-track A risk — `Path.suffix` misuse in the fix itself).**  The stem/suffix split MUST
  key on the known source audio suffix, NEVER on `Path.suffix`/`splitext` — trailing-dot cases
  (`op. 23`) break a naive splitext.  Guard: the KAT trailing-dot case.  *internal-continue
  (halt-and-surface if no clean split without splitext exists).*
- **D-3 (Sub-track A — repair must re-shorten, not just append).**  The 7 real files are already
  at/over `_NAME_MAX`; a repair that only appends `.flac` recreates the over-length condition.  S2
  depends on S1.  *internal-continue.*
- **D-4 (Sub-track B — why it exists).**  `preflight` (dry-run `repath`/`regroup`/`unify`) proposed a
  `Pagliacci [rec 1965] []` rename with `suffix=` empty.  Confirmed: `MBRelease()` stub →
  `release.id == ""` → `_collision_suffix` returns `""` → ` []`.  All three passes affected;
  regroup/unify already carry the real `rid` and discard it.  *internal-continue.*
- **D-5 (Sub-track B — coverage was assertion-blind).**  The ` []` bug passed 100% branch coverage
  and the full `tox -m analyze` because the collision test *executed* the buggy branch but never
  *asserted the produced suffix value*.  S4's test-assertion additions close this class of gap, not
  just this instance.  **CAPTURE-CANDIDATE** (recorded to NOTES on approval).  *internal-continue.*
- **D-6 (Sub-track B — per-file id, not per-pass).**  regroup/unify plans mix release ids; the suffix
  stub must be built per-collision-group (by `rid`), not once per pass.  Prefer reusing the existing
  per-`rid` grouping over changing `_apply_collision_suffix`'s signature (which the ingest caller
  shares).  *internal-continue.*
- **D-7 (scope creep into a live destructive run).**  Tempting to "just fix the affected files on
  hades."  That is an operator destructive step, not an agent code session — build+prove on fixtures;
  the live repair rides the operator's next pass under C-PROV.  *destructive-HALT if a live-library
  mutation is proposed in-shard.*
- **D-8 (Sub-track C — why it exists; quantified; TWO mechanisms).**  `preflight` proposed ~8700
  `repath` top-dir changes; the dominant class (thousands — `Mozart - Complete Mozart Edition`,
  `Bach - Bach Edition`, `Schubert - The String Quartets`, `Handel - Orchestral Works`, …) replaces a
  real conductor/ensemble with the release's **edition/collection title**.  Two distinct root causes,
  confirmed by code read: **(S5) compilation branch** — `_top_dir_component` Case 1, keyed on
  `releasetype_secondary`, emits `<…> - <ALBUM>` (the Karajan "Ouvertüren" case, ~low hundreds); and
  **(S6) Case-3 performers fallback** — composer present, so the top dir is `<composer> - <performers>`,
  but on repath the `<performers>` component collapses through `build_dest_path`'s fallback chain to
  `ARTIST` (`_tags.py:1216`), which for box sets carries the edition title (the dominant class).  The
  current on-disk paths already show the *correct* performer, so it was renderable at ingest — the
  edition title is a repath re-derivation loss.  *internal-continue.*
- **D-9 (Sub-track C — the code contradicts its own contract).**  The album-name-in-top-dir behaviour
  is not merely undesirable; it violates C-UNIVERSAL's *documented* rationale in `docs/NOTES.md`: the
  topmost path component must derive only from scholarship-stable data (composer/performer/work/
  dates), never from a free-classification parameter (`releasetype_secondary` is named explicitly),
  and album identity migrates to the playlist lens.  The implementation drifted from the contract it
  claims to serve.  **CAPTURE-CANDIDATE** (recorded to NOTES on approval): *when a contract's
  documented intent and its implementation diverge, the divergence can pass every automated gate
  because the gates test the code's behaviour, not the contract's intent — the same class as D-5.*
  *internal-continue.*
- **D-10 (Sub-track C — corrective/editorial boundary + the REND-23↔C-UNIVERSAL conflict).**  Two
  frozen artifacts disagreed: REND-23/C-INIT froze "compilation/recital → `<albumartist> - <album>`"
  as *deliberate* (`census-impl.md`), while C-UNIVERSAL's rationale (`docs/NOTES.md`) forbids album
  name and free-classification parameters in the path.  **Operator resolved it 2026-08-20: C-UNIVERSAL
  governs; REND-23 overturned.**  The corrective line for this shard is "restore performer-in-path,
  album-name-out-of-path".  The **deferred** line — the operator now wants the soloist in the path
  (reversing the earlier SEL-11 "add-then-remove" as overengineered), a **C-NOSOLO reopen** — is
  desired but sequenced after the unfinished tasks (ROADMAP R6d planning).  S5/S6 must NOT bake in a
  field set/order beyond restoring `<composer> - <performers>`, and must NOT add the soloist yet.
  *halt-and-surface if a fix cannot restore performer-in-path without also touching the soloist or
  deciding the field order.*
- **D-12 (Sub-track C / S6 — hydration-loss vs data-absence, must be settled first).**  The S6
  edition-title bug has two possible causes with different fixes: (a) the embedded files carry
  `CEA_CONDUCTORS`/`CEA_ENSEMBLES` but `_hydrate_performer_lists` fails to populate the per-track
  lists on repath (a re-derivation regression — pure corrective, repair hydration); or (b) the files
  genuinely lack the `CEA_*` performer keys, so no tag-only re-derivation can recover the performer
  and the fallback must be redesigned to never emit the edition title (guard `ARTIST == ALBUM`).  The
  current on-disk path proving the performer was once renderable *suggests* (a), but this cannot be
  confirmed without inspecting a real embedded file's tags (not mounted in the dev env).  *S6 must run
  this diagnostic first; halt-and-surface if it cannot be determined from a fixture — (a) and (b) have
  different fixes and KATs, and guessing risks shipping the wrong one.*
- **D-11 (Sub-track D — scripts inventory, confirmed by code read).**  `preflight_r6d.py`,
  `scan_acoustid_tags.py`, `scan_catalogue_colon.py` are superseded by the shipped `preflight` /
  `repatch-acoustid` / `repatch-catalogue-colon` CLI actions (delete now).  `census_original.py` and
  `census_styleguide.py` are spent one-shot tools whose artifacts (`docs/census-r0.{json,md}`,
  `docs/census-library.{json,md}`) are already committed (delete after verifying).
  `scan_fragmentation.py` and `scan_nonuniform_depth.py` hold detection logic not in the package and
  not covered by `audit`; their migration into `audit` is routed to ROADMAP R4b, not this shard.
  *internal-continue.*

## Notes for executors

- **Tier routing.**  All eight rows **Sonnet** (bounded corrective fixes + deletions, KAT-pinned).
  `juncture-tier: opus` kept (arc default); no juncture fires.
- **Order.**  S1 → S2 (Sub-track A: S2 consumes S1's suffix-safe shortening), then S3 → S4
  (Sub-track B: S4's tests assert what S3 produces; S4's guard backstops S3), then S5 → S6
  (Sub-track C: S6 consumes S5's restored default shape), then S7 → S8 (Sub-track D: deletions).
  The sub-tracks are otherwise orthogonal (different modules); the dependency is *within* each
  sub-track, not across.
- **The KATs are mandatory and stand in for the live-fix proof.**  The affected files are not mounted
  in the dev env; fixtures reproduce their exact shapes (Sub-track A: title+`.flac` over `_NAME_MAX`,
  extension-less-but-valid FLAC on disk; Sub-track B: collision non-match resolving to a real MBID
  suffix; Sub-track C: a Compilation-flagged composer-bearing release, and a no-composer release whose
  `ALBUMARTIST` is a collection title).  If a fixture cannot reproduce the shape, the test is
  mis-constructed — surface it.
- **Sub-track C editorial guardrail.**  S5/S6 restore "performer in path, album name out of path"
  and nothing more.  Do **not** add the soloist to the path yet — the operator wants it eventually (a
  deferred C-NOSOLO reopen, ROADMAP R6d) but ruled it out of this shard — and do **not** freeze a
  performer field order beyond the existing `<composer> - <conductors; ensembles>` shape.  Also record
  the REND-23 overturn where the styleguide validate-record lives (see D-10), so the frozen case-ID is
  not left contradicting the code.  If a clean restore is impossible without deciding the field order
  or touching the soloist, halt-and-surface (D-10).
- **Sub-track D pre-delete check.**  Before S7/S8, confirm nothing in `src/` or `tests/` imports the
  deleted scripts (they are `scripts/`-local by design), and confirm the census artifacts
  (`docs/census-r0.{json,md}`, `docs/census-library.{json,md}`) exist and are complete.  Do **not**
  delete `scan_fragmentation.py` / `scan_nonuniform_depth.py` — they await ROADMAP R4b migration.
- **REGISTER rule (durable-file discipline).**  In source/tests, state the *property/invariant* —
  "truncation reserves the suffix's bytes so stem+suffix ≤ NAME_MAX"; "the collision suffix is
  derived from the file's release id so every disambiguated path is release-identifying"; "an empty
  release id cannot yield a collision suffix"; "the topmost path component derives from composer +
  performers and never carries the album name or a free-classification parameter" — never a plan
  coordinate.
- **Anneal denylist (◆ gate greps durable files for these).**
  - `\bS[1-9]\b` (plan session coordinates) — allow STYLEGUIDE rule-section forms (`\b[1-5]\.[0-9]\b`).
  - `\bR6[a-e]\b`, `\bR[0-9]\b`, `\bR4[a-c]\b`, `\bJ[1-3]\b` (roadmap node + juncture coordinates) —
    flag in durable source/docs; legitimate only in PLAN/ROADMAP/ledger/commit messages.
  - `sub-track`, `plan-run`, `plan-shard`, `juncture`, `inflection`, `action-frame`, `◆`,
    `pre-R6d` (as a coordinate), `C-COLLISION` (a plan-local contract name — do not leak into source).
  - Do **not** flag: `C-L0`, `C-L1`, `C-PROV`, `C-MOVE`, `C-UNIVERSAL`, `C-NOSOLO`, `SEL-11` (durable
    styleguide contract names), `_NAME_MAX`, `_proposed_short`, `_resolve_long_names`, `safe_name`,
    `build_dest_path`, `_top_dir_component`, `releasetype_secondary`, `repath`, `regroup`, `unify`,
    `with_suffix`, `repath_unsupported_format`, `_collision_suffix`, `_apply_collision_suffix`,
    `_assess_collisions`, `_resolve_current_lib`, `MBRelease` — legitimate domain/code vocabulary.
- **Invariants to preserve:** C-L0/C-L1 leaf naming; C-PROV/C-MOVE move provenance; C-UNIVERSAL's
  universal prefix-less top dir + tag-layer≠path-layer boundary; the confirmation-provenance
  invariant (no journal entry before `_verify_copy`); the fail-loud posture on integrity errors;
  `_NAME_MAX = 255` (respected inclusive of suffix); the every-field-defaults model convention
  (Sub-track B stops *relying* on it for a value it cannot supply).  All sub-tracks restore rather
  than change these.
- **Every code row runs `~/.local/bin/tox -m analyze` before ledger-done.**  Import order via
  `~/.local/bin/tox -m edit`, never hand-edited.  (S7/S8 are deletions; still run the gate to confirm
  no dangling import breaks build/lint.)
- **Suggested first `/plan-run` invocation:** `halt-at-boundaries` — eight rows with three real
  intra-sub-track dependencies (S2⊃S1, S4⊃S3, S6⊃S5).  Stop at the S2/S4/S6/S8 seams.  Watch items:
  (A) the KAT that title+`.flac`, not title alone, is what gets measured — the whole Sub-track-A bug
  in one assertion; (B) the KAT that asserts a *non-empty* `[<mbid8>]` suffix — the whole Sub-track-B
  bug in one assertion; (C) the KAT that asserts the top dir equals `<composer> - <performers>` and
  does **not** contain the `ALBUM` value — the whole Sub-track-C bug in one assertion.
