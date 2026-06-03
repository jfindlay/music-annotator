# music-annotator — Plan: Library-wide Maintenance (naming + provenance + cache)

**Status: sharded — executable by `/run-plan`.**

Carved from a 2026-06 empirical audit of the already-annotated library (`/home/findlay/Music/Done`
on hades; 343 top dirs / 1,384 work_dirs / 16,573 journal entries).  The audit replaced the
original top-down N1–N4 sketch with a measured phenomenology; the original design prose is preserved
at the bottom of this file under "Original sub-track design (superseded as the primary frame)" so no
context is lost.

This is one of several independent plans — see `docs/PLAN.md` (the index) for the full set.
Invariants referenced below are defined in `docs/NOTES.md`.

---

## Purpose (design intent)

A full maintenance pass over the **already-annotated** library, driven by an empirical audit rather
than a presumed failure list.  Three things, in dependency order:

1. **Establish the regenerable cache** (database-as-infrastructure — `docs/NOTES.md`).  The journal
   is a derived index over authority in the tracks+sidecars; prove and ship its regeneration from a
   library scan, after first migrating the one non-reconstructable datum (rip origin-time) into the
   provenance sidecar.
2. **Unify naming fragmentation** — works/releases split across directories by a per-track path
   dimension (performer or composer) that should be unified at the work/release level.
3. **Repath stale path-fossils** — directories whose *paths* encode pre-L0/L1 numbering bugs even
   though their *tags* already carry the corrected values; plus the deferred L2 depth normalisation,
   confirmed *prevalent* (35 work_dirs) by the audit.

---

## What the 2026-06 audit established

CONFIRMED findings that sharpen session scope:

- **No authority leak; journal is regenerable.**  `destination` reconstructs from embedded tags via
  `build_dest_path` at 100% sample match; `MUSICBRAINZ_ALBUMID` + identity triple are tag-held;
  `freedb_disc_N.yaml` provenance sidecars exist in 100% of 1,384 work_dirs.  The only
  non-reconstructable field is `source` (rip-origin path) — provenance, not authority.
- **Most naming bugs are stale path-fossils, not logic gaps.**  Leaf collisions (21 dirs), leaf gaps,
  `dd.dd` over-application (4 work_dirs / 79 files), missing `CWP_INTER_INDEX_{i}` — tags already
  carry the corrected values; only the on-disk paths are wrong.  These fix by `repath`, not new
  logic.
- **The genuinely new logic is narrow:** performer/composer-split unification (W2) and L2 depth
  normalisation (W3b).
- **N1's real shape is per-track path-dimension split.**  29 releases split by per-track
  `CEA_SOLOISTS`; 1 release (~20 dirs) split by per-track `CEA_COMPOSER_LASTNAMES` (Benny Goodman).
- **L2 is the prevalent shape** (35 work_dirs), splitting into ragged-floor-faithful (~28) and
  over-resolution-clamp (~7).
- **Rip origin-time is journal-only** today — the one datum a regenerate would lose; must migrate
  into `freedb_disc_N.yaml` first.

---

## Dependencies (substrates — both landed)

- **`PLAN-multimedium.md` C-S0** — cross-medium work-group aggregation.  W2 unification must operate
  over cross-medium groups.  **Complete; frozen.**
- **`PLAN-fingerprint.md` F0–F8** — archival identity triple + `audit` machinery.  W1's
  regenerate-diff and W2's detect→adjudicate step consume the same infrastructure.  **Complete.**

---

## Cross-session contracts

### C-W1 — Regenerable-cache interface

Defined by W1b (the `rebuild` subcommand).  Frozen when W1b lands.

- **`rebuild` subcommand**: scans a library root (`dest_root`), reads embedded tags + sidecars per
  file, and produces the cache.  Output schema is identical to the existing `TransactionLog`
  (`TransactionEntry` list) so existing `audit`/`regroup`/`repath` consumers require no change.
- **Origin-time field**: after W1a lands, each `TransactionEntry` produced by `rebuild` carries an
  `origin_time` string (ISO-8601, sourced from `freedb_disc_N.yaml`) in addition to the existing
  `timestamp` (annotation time from file mtime).
- **Storage format** (decided at W1b): initial implementation uses the existing flat JSON
  (`music_annotator_journal.json`) appended/replaced by `rebuild` — this avoids any consumer
  breakage.  SQLite migration is a separate future decision gate on proven queryability need; W1b
  must not architect for SQLite as a prerequisite.
- **`audit --diff` mode** (C-W1c): the diff compares a freshly-rebuilt in-memory cache against the
  on-disk journal file, field by field per destination path, and reports three buckets: `matches`,
  `stale` (journal has path, rebuild has different value — expected after a `repath`/`regroup`),
  `leaked` (journal has field value not reproducible by rebuild — *not expected*; surfaces a real
  authority leak).

**Downstream consumers:** W2 (detect step uses the rebuilt cache), W3 (repath reads it), W1c
(`audit --diff`).

### C-W2 — Unified-path policy for fragmented releases

Defined by W2a (performer-split).  Frozen when W2a lands.

- **Detection**: a release is fragmented when ≥2 distinct top_dirs share the same
  `MUSICBRAINZ_ALBUMID`.  Join key is the tag, not the journal.
- **Canonical top_dir**: `build_dest_path`'s existing work-level unification logic (cross-medium
  composer pass, `recording_date_work` pass) already computes the correct answer when given all
  tracks of the release as a group.  The W2 fix is to *run that pass over the full release* — not
  per-top_dir fragment — and recompute the unified path.  Concretely: collect all files for a
  `MUSICBRAINZ_ALBUMID`, pass them as a synthetic single-medium group to the existing work-level
  passes, derive the unified top_dir, then `regroup`-style move the fragments to it.
- **Multi-composer compilation exception (W2b — resolved)**: when `CEA_COMPOSER_LASTNAMES` varies
  across tracks of a single non-classical release (the Benny Goodman shape), the canonical top_dir
  uses the **album-artist sort-name** as the composer component, not the per-track composer field.
  Rationale: the path is a handle, not a manifest (CE anchor).  For a non-classical compilation the
  release's canonical identity is its curator/performer (`ALBUMARTIST`), not the per-track
  songwriter.  `ALBUMARTIST` is already the release-level primary attribution — the curator's
  resolution of "whose release is this" — and using it as the path prefix produces a stable,
  user-meaningful handle (`Goodman, Benny - The Benny Goodman Story/…`) that does not fork per
  track.

  **Concrete rule (for W2b implementor):**

  1. *Detection trigger*: a release group is a multi-composer compilation when `CEA_COMPOSER_LASTNAMES`
     is non-empty and takes ≥2 distinct values across the tracks of one `MUSICBRAINZ_ALBUMID`.
  2. *Canonical composer component*: read `ALBUMARTISTSORT` from any track in the group (it is
     uniform across a release; the annotation pipeline embeds it from `release.artist_credit`
     sort-names).  Apply `last_name()` to produce the sort-name last-name form (e.g.
     `"Goodman, Benny"` → `"Goodman, Benny"` — already in sort-name form; `last_name()` strips
     only the given-name suffix when the sort-name is already inverted).  Use this value as the
     uniform `CEA_COMPOSER_LASTNAMES` substitute when calling `build_dest_path` for every track in
     the group.
  3. *Fallback*: if `ALBUMARTISTSORT` is empty or `"Various Artists"`, use `"Various"` as the
     composer component — the CE convention for multi-artist compilations with no single canonical
     identity.  Do **not** fall back to plurality-composer counting: it is fragile, non-stable
     across MB data updates, and misleading for compilations where the dominant songwriter is not
     the release's identity.
  4. *Implementation site*: this normalisation is a **pre-processing step in `unify()`**, not a
     change to `build_dest_path`.  `unify()` constructs a synthetic `TrackTags` (or patches the
     `file_dict`) with the uniform composer value before calling `build_dest_path` for each track
     in the group.  `build_dest_path` itself is unchanged.
  5. *Scope gate*: apply this rule only when the release is confirmed non-classical.  The signal is
     the absence of `CWP_WORK_TOP` (no MB work link → non-classical) or `CWP_WORKTYPE_GENRES_TOP`
     not containing `"Classical"`.  A classical release with a varying `CEA_COMPOSER_LASTNAMES`
     (e.g. a multi-composer anthology) routes through the existing cross-medium composer-pass
     unification (W2c), not this rule.
- **Arranger/finisher credit** (W2c): the group-wide aggregated composer value (already computed by
  the cross-medium composer pass) is the canonical value; per-track variations are not visible in
  the path.

**Downstream consumers:** W2b, W2c, W2d (all consume the detection mechanism and canonical-path
rule).

### C-W3b — Depth-normalisation rule in `build_dest_path`

**Deferred to `docs/BACKLOG.md`** (W3b session moved out of this plan; see BACKLOG "L2 depth
normalisation" entry for the full design and reopen criteria).  C-W3b is not frozen by this plan.
W3a targets only stale path-fossils whose fix does not depend on the new depth logic.

---

## Session list

Sessions are in dependency order.  W2 and W3a are independent of each other (both depend on W1).
W3b is deferred to `docs/BACKLOG.md` (dedicated multisession).

### W1a — Origin-time rescue  `[substrate]`

**Goal**: migrate rip/download origin-time out of the journal and into the authoritative
`freedb_disc_N.yaml` sidecar, so that the one provenance datum a regenerate would lose is safe in
the self-contained track+sidecars unit before the journal is ever replaced.

**Deliverables**:
- New `enrich --origin-time` mode (idempotent, re-runnable per P-FP3): reads the on-disk journal,
  groups entries by destination file, takes the earliest `timestamp` per work_dir as the
  annotation time, takes the `source` rip-path's parent as the origin provenance label, and writes
  an `origin_time` and `origin_source` field into the matching `freedb_disc_N.yaml` sidecar YAML.
- Sidecars without a `freedb_disc_N.yaml` (legitimately absent — PrestoMusic downloads, etc.) get a
  sibling `music_annotator_provenance.yaml` sidecar instead; same fields; same format.
- The new sidecar fields are added to `CoverImage`/`TrackTags`/relevant models if read-back is
  needed for W1b.
- Tests: 100% branch coverage.  New sidecar write path; idempotency (run twice, same result);
  legitimately-absent-sidecar path.

**Contracts produced**: none frozen yet (W1b contracts the `rebuild` interface; W1a only establishes
the sidecar field convention).

**Files expected**: `src/music_annotator/_pipeline_io.py` (new `enrich --origin-time` mode or helper),
`src/music_annotator/models.py` (sidecar provenance fields), `src/music_annotator/__main__.py` (CLI
wire-up), `tests/unit/test_pipeline.py`, possibly `tests/integration/test_integration.py`.

---

### W1b — Regenerate-from-scan (`rebuild` subcommand)  `[substrate — freezes C-W1]`

**Goal**: ship the `rebuild` subcommand that proves the database-as-infrastructure claim: the journal
is regenerable from the tracks+sidecars alone.

**Deliverables**:
- `rebuild` subcommand: walks `dest_root`, reads tags + sidecars per FLAC/MP3 file, emits a new
  `TransactionLog` in the existing JSON format.  Each reconstructed `TransactionEntry` carries:
  `destination` (the file's current path), `release_id` (from `MUSICBRAINZ_ALBUMID`), the identity
  triple (`audio_hash` recomputed from audio, `chromaprint_fp` and `acoustid_id` from tags),
  `timestamp` (annotation time from file mtime, ISO-8601), `origin_time` (from
  `freedb_disc_N.yaml`/`music_annotator_provenance.yaml` — populated by W1a), `action="tagged"` for
  audio files, `action="sidecar"` for sidecar files.
- Output replaces `music_annotator_journal.json` only when `--write` is passed; default is dry-run
  (`--dry-run`).
- `rebuild --dry-run` is the self-sufficiency proof: run it, diff against the existing journal.  Any
  unexplained non-match is a candidate authority leak or expected staleness (repathed/regrouped
  entries not yet re-scanned).
- Freezes **C-W1** (the `origin_time` field, the `rebuild` output schema, the dry-run default).
- Tests: 100% branch coverage.  Dry-run vs write mode; origin-time present/absent; mixed FLAC+MP3.

**Contracts frozen**: C-W1.

**Files expected**: `src/music_annotator/_pipeline_io.py` (new `rebuild` walk + reconstruction logic),
`src/music_annotator/_pipeline.py` or `__main__.py` (subcommand wire-up),
`src/music_annotator/models.py` (any new fields), `tests/unit/test_pipeline.py`,
`tests/integration/test_integration.py`.

---

### W1c — `audit --diff` mode  `[algorithm]`

**Goal**: extend the existing `audit` subcommand with a `--diff` flag that compares a freshly-rebuilt
in-memory cache against the on-disk journal, making the regenerate-diff a permanent maintenance
health-check.

**Deliverables**:
- `audit --diff`: calls `rebuild` (in-memory, no write), then diffs against `read_journal()` field
  by field per destination path.  Emits three buckets per the C-W1c spec: `matches`, `stale`,
  `leaked`.  `leaked` entries are printed as warnings; any `leaked` entry is a test failure in CI if
  one is ever introduced.
- Summary line: `N matches, N stale (expected after repath/regroup), N leaked`.
- The stale bucket is expected to be non-empty until W3a/W3b `repath` runs; `leaked` should always
  be zero.
- Tests: 100% branch coverage.  Matches-only case; stale case (journal has old path, rebuild has new
  one); leaked case (journal has unreconstructable field — test must demonstrate what that looks like
  so the bucket logic is exercised).

**Contracts consumed**: C-W1.

**Files expected**: `src/music_annotator/_pipeline_io.py` (diff logic),
`src/music_annotator/_pipeline.py` or `__main__.py` (flag wire-up),
`tests/unit/test_pipeline.py`.

---

### W2a — Performer-split unification  `[algorithm — freezes C-W2 (performer part)]`

**Goal**: detect and consolidate releases fragmented across multiple top_dirs by per-track
`CEA_SOLOISTS` variation (29 releases; dominant N1 shape).

**Deliverables**:
- New `unify` subcommand (or extend `regroup`): groups files by `MUSICBRAINZ_ALBUMID` across the
  library; for each release with ≥2 distinct top_dirs, uses `build_dest_path` over the full
  release group (all tracks, cross-medium) to compute the canonical top_dir; moves fragments to the
  canonical path; appends `action="unified"` journal entries per the re-journal obligation (C-L4
  posture: SHA before move, move, SHA after, `_verify_copy`, then journal).
- Canonical top_dir algorithm: run the existing work-level unification passes (`top_work_groups`
  composer pass, `recording_date_work` pass) over the full release's tracks as a single group.  The
  unified performer credit comes from the cross-medium union of `CEA_SOLOISTS` where applicable
  (C-S4 concerto-soloist rule), not per-track.
- `--dry-run` / `--yes` flags; confirmation prompt by default (aligns with `prune`/`regroup`
  posture).
- Freezes the performer-split part of **C-W2**.
- Tests: 100% branch coverage.  Fragmented release detected; canonical path computed; move +
  re-journal; dry-run; idempotency (second run finds nothing to do).

**Contracts frozen**: C-W2 (performer-split part; composer-split part awaits W2b juncture).

**Files expected**: `src/music_annotator/_pipeline.py` (new `unify` logic),
`src/music_annotator/_pipeline_io.py` (journal extension for `"unified"` action),
`src/music_annotator/models.py` (new action string in docstring/annotation),
`src/music_annotator/__main__.py`, `tests/unit/test_pipeline.py`,
`tests/integration/test_integration.py`.

---

### W2b — Composer-split unification  `[algorithm — @plan juncture before sharding]`

**`@plan` juncture required before this session is sharded.**  The editorial question — canonical top_dir
for a multi-composer compilation (the Benny Goodman shape) — must be resolved by a `@plan`
inflection review before W2b is dispatched.  The question is:

> When `CEA_COMPOSER_LASTNAMES` varies per track across a single release that has a well-defined
> `ALBUMARTIST` (a non-classical compilation), should the canonical top_dir use `ALBUMARTIST` as the
> path prefix (e.g. `Goodman, Benny - The Benny Goodman Story/…`) or should it use the release's
> single dominant/plurality composer (or no composer prefix)?  Refract through the CE anchor.

**Provisional scope (subject to juncture)**: same mechanism as W2a (detect by `MUSICBRAINZ_ALBUMID`,
derive canonical path, move, re-journal), but with the composer-split path rule substituting the
per-track `CEA_COMPOSER_LASTNAMES` with the agreed canonical value (likely `ALBUMARTIST`-derived).
W2a's `unify` subcommand is extended to handle this shape.

**Contracts consumed**: C-W2 (performer part); **C-W2 composer extension frozen here** after juncture.

---

### W2c — Arranger/finisher work-level path credit  `[algorithm]`

**Goal**: ensure that works where an arranger/finisher is credited as `"composer"` with the
`"additional"` attribute on *only some movements* produce a consistent top_dir across all movements
(the Mozart K.626 Süßmayr shape).  This is the library-wide retroactive counterpart to the
per-release fix already shipped in `PLAN-multimedium.md` S1.

**Deliverables**:
- Audit pass: for each `CWP_WORKID_TOP` group, compare the `CEA_COMPOSER_LASTNAMES` values across
  all tracks.  Report groups where the value varies (the symptom).
- Fix: the `unify` subcommand (from W2a) is extended to include the composer-pass unification over
  the full work group — already done by the cross-medium composer pass, so this is primarily
  confirming the `unify` command's call site reaches the right pass.
- If any groups are not already fixed by the W2a canonical-path algorithm, add the specific
  handling.
- Tests: 100% branch coverage.  Arranger-only movement (empty `role_buckets.composers`) produces
  same top_dir as composer-credited movement in the same group.

**Contracts consumed**: C-W2.

**Files expected**: minor extension to `src/music_annotator/_pipeline.py`; tests.

---

### W2d — Empty work_dir names  `[editorial routing — may be zero-code]`

**Goal**: resolve work_dirs whose `work_dir` component is `" [rel YYYY]"` (blank `CWP_WORK_TOP` =
no MB work link on the tracks).

**Decision to make in this session**:
- If `CWP_WORK_TOP` is empty because the track has no MB work relation (a data-quality gap), the
  blank path is *correct and visible* — it exposes the gap upstream so it can be fixed in MB.  The
  right fix is to submit the work link to MB, not to invent a renderer fallback.  Per NOTES Rule 1
  (ragged-depth source routing): **data-quality gaps route upstream, not to the renderer**.
- If the blank top_dir is causing downstream breakage (path collisions, player confusion), a
  renderer fallback (use `ALBUM` or `TITLE` as the work stand-in) is warranted.  Confirm empirically
  against the ~5 affected work_dirs.
- Likely outcome: zero code; a note recording the routing decision and the specific MB work-link
  submissions needed for the ~5 affected releases.

**Contracts consumed**: C-W2.

**Files expected**: possibly none (editorial resolution only).  If a renderer fallback is warranted,
extends `src/music_annotator/_tags.py` (`build_dest_path`).

---

### W3a — Mechanical repath  `[algorithm]`

**Goal**: repath the stale path-fossils whose tags already carry the corrected values: leaf
collisions (21 dirs), leaf gaps, `dd.dd` over-application (4 work_dirs / 79 files), missing
`CWP_INTER_INDEX_{i}`.  Uses the existing `repath` subcommand; this session is primarily tests +
validation that `repath` handles each fossil shape correctly.

**Deliverables**:
- Run `repath --dry-run` against the W1-rebuilt cache; confirm all expected moves are detected; run
  the full `repath` (with confirmation/`--yes`).
- If any fossil shape is *not* handled correctly by the existing `repath` logic, fix it here.
  Known gap: `repath` must not regress the legitimate partial-performance-collision case (files that
  legitimately share a collision-suffix should not lose it).
- After `repath` completes, run `audit --diff` (W1c) to confirm the stale bucket shrinks as
  expected.
- Tests: extend `test_pipeline.py` with the three specific fossil shapes (leaf-collision,
  `dd.dd`-fossil, missing-inter-index) to confirm `repath` handles them; confirm the collision case
  is not regressed.

**Contracts consumed**: C-W1 (rebuilt cache), C-W3b is **not** required (W3a targets fossils whose
fix does not depend on the new depth logic).

**Files expected**: primarily tests.  If `repath` gaps are found: `src/music_annotator/_pipeline.py`,
`src/music_annotator/_pipeline_io.py`.

---

### Codebase-audit sessions  `[cross-cutting; schedule after W1b]`

The multimedium plan surfaced four codebase-audit handoff items (NOTES "Codebase audit — handoff
brief").  These are independent of the naming/repath work and can be scheduled in parallel with W2
and W3 after W1b lands.  They are listed here to prevent them drifting to BACKLOG without an
execution decision:

1. **`WorkGroup`/`ReleaseContext` aggregation object** — five passes over the same `group_idxs` in
   `run()`.  Decide whether to lift into a first-class object.  Likely one session; may be zero-code
   if the decision is "not yet."
2. **`__init__.py` API-surface coherence** — the private-helper re-export pattern for test patching.
   One session; likely small refactor.
3. **`repath` confirmation-prompt gap** — `repath` mass-relocates with no prompt; all other
   destructive commands confirm.  One session; small.
4. **Module-boundary review** — `_pipeline.py` hosts three entry points sharing a near-verbatim
   move/verify/journal loop.  Likely one session to factor the shared primitive; may be a
   `_pipeline_maint.py` split.

These four are not in the progress ledger (they have no naming-specific contracts).  Track them
separately or absorb into this plan's ledger when scheduled.

---

## Dependency graph

```
W1a (origin-time rescue)
  │
W1b (rebuild subcommand)  ──── freezes C-W1
  │
W1c (audit --diff)        ──── consumes C-W1

W1b ──► W2a (performer-split unify)  ── freezes C-W2 (performer)
         │
         ├──► [@plan juncture] ──► W2b (composer-split unify)  ── freezes C-W2 (composer)
         ├──► W2c (arranger/finisher credit)
         └──► W2d (empty work_dir — editorial routing)

W1b ──► W3a (mechanical repath)      ── consumes C-W1 only
         │
         └──► [W3b deferred to BACKLOG — depth normalisation; separate multisession]
```

W2 and W3a are independent of each other and can be scheduled in parallel after W1b.

---

## Progress ledger

**Preflight bindings** (resolved 2026-06-02):
- PLAN: `docs/PLAN-naming.md`
- VERIFY: `~/.local/bin/tox -m analyze` (combined gate — satisfies tests + types + lint + format + coverage in one run)
- Juncture tier: T1 (recalibrated; `@general` subagent for adjudication, not `@plan-deep`)
- W3b: deferred to `docs/BACKLOG.md`; dedicated multisession planned

| Session | Status  | Commit | Notes |
|---------|---------|--------|-------|
| W1a     | done    | 05fc10d | Already implemented before chain start; `enrich --origin-time` + `ProvenanceSidecar` model |
| W1b     | done    | 9e70188 | `rebuild_journal()` + `rebuild` subcommand; `origin_time` on `TransactionEntry`; C-W1 frozen |
| W1c     | done    | 2411e5f | `audit --diff` + `diff_journal()` + `JournalDiffResult`; matches/stale/leaked buckets |
| W2a     | done    | fe8e65b | `unify` subcommand + `detect_fragmented_releases()`; C-W2 (performer-split) frozen |
| W2b     | done    | 678acbf | Composer-split pre-processing in `unify()`; ALBUMARTISTSORT canonical; C-W2 fully frozen |
| W2c     | pending | —      |       |
| W2d     | pending | —      |       |
| W3a     | pending | —      |       |
| W3b     | deferred | — | Moved to `docs/BACKLOG.md`; dedicated multisession planned |

---

## Action-frame digest

*Append-only.  Updated at non-trivial iterations (discoveries, contract flexes, notable texture).*

**2026-06 pre-shard audit** — Library scan confirmed no authority leak; journal regenerable; dominant
naming anomaly is per-track performer/composer-split (29 releases), not spelling variation as the
original sketch assumed.  L2 depth is prevalent (35 work_dirs), not a footnote.  Rip origin-time is
the sole journal-only datum and must migrate to sidecar before journal replacement.  Two `@plan` junctures
identified: W2b editorial (multi-composer top_dir) and W3b architectural (depth-clamp in
`build_dest_path`).

---

## Original sub-track design (superseded as the primary frame; preserved for context)

The pre-audit sketch.  Its N1/N2 hypotheses were directionally right but incomplete (the audit found
performer/composer-split is the real N1 shape, N2 `dd.dd` is small, and the deferred L2 depth case
is prevalent); N3/N4 are deferred out of carve.

### N1 — Unify works split by directory-name variation

Works split across directories by different language, misspelling, or punctuation variation are
reconciled to one canonical handle.  The join key is the embedded MB work/release tag, not the
directory name (a *handle, not a manifest* — a length-shortened or manually-renamed dir loses any
embedded key; see NOTES.md).

- **Arranger/finisher in directory path — work-level only.**  When a finisher/completion/arranger is
  credited as `"composer"` with the `"additional"` attribute on only some movements, the per-track
  `role_buckets.composers` is empty for those movements and `effective_composers` falls back to
  `additional_composers`, producing a different `CWP_COMPOSER_LASTNAMES` — and a different `top_dir`
  — than movements with a plain primary-composer relation (e.g. Mozart K. 626 Süßmayr movements land
  in `Mozart; Süßmayr - …`).  `PLAN-multimedium.md` S1 fixes this *within and across media* via the
  cross-medium composer pass for *new* annotations.  This plan's job is the **library-wide
  retroactive pass** over already-annotated dirs.  Any arranger-style path credit must be applied at
  the work level (group-wide aggregated value), never per-track.  (Now W2c.)
- **Multi-medium limitation (inherited).**  `PLAN-multimedium.md` S0 removes the per-medium
  limitation for the `recording_date_work` union, `recording_first_release_date` normalisation, and
  composer unification.  Any work-spanning normalisation in this plan must consume C-S0.

### N2 — Retroactive `dd.dd` leaf-prefix cleanup

`PLAN-multimedium.md` S3 mechanically fixes the `dd.dd` over-application (prefix added to some
multitrack works that are not partial-performance collisions) for *new* annotations.  This sub-track
is the **library-wide retroactive pass** over already-annotated dirs that carry the stale prefix.
Must not regress the legitimate partial-performance-collision case the prefix exists for.  (Audit:
small — 4 work_dirs / 79 files; folds into W3a mechanical repath.)

### N3 — Re-annotation / update-diff mode  *(DEFERRED out of carve)*

An update-diff function to capture tag improvements or additive cover art: diff the library against
updated MusicBrainz / Cover Art Archive / Discogs / Wikipedia data and apply the additive
improvements.  Depends on those external integrations existing (several are `docs/BACKLOG.md` items).

### N4 — User-improvement mode  *(DEFERRED out of carve)*

- The user adds cover art for a release.
- music-annotator extracts metadata (dates, producers, performers, etc.) from the cover art and
  updates the appropriate tags.

(Also touches "additional ensembles attributed to a single track in a multitrack work could fork the
written paths for those tracks" — a whole-library-context observation; resolve its path-vs-tag
treatment through the Classical Extras anchor when sharded.)
