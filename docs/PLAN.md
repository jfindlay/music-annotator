<!-- juncture-tier: opus -->
<!-- sub-track: R4a (library-wide taxonomy + initial directory component) — Act II naming-policy; first R4 shard after the R3 code arc closed; introduces the top-level class scheme (Picard release-type-aligned) then refines the within-classical initial component -->

# PLAN — R4a: library-wide taxonomy + initial directory component

## Purpose (design intent)

*(Re-read at every ◆ boundary — anti-defocus anchor.)*

Introduce the **top-level library class scheme** so classical art-music is one class among several and
the library admits everything LoC-style (the *full-inclusion* north star: the filesystem is a catalog,
nothing stays outside).  Today `build_dest_path` (`_tags.py:857`) has **no** top-level class routing —
every release lands at `dest_root / "<composer> - <performers>"`, a single classical-shaped scheme.  An
audiobook, a children's-pop compilation, or a soundtrack has no honest home.  R4a gives the path a
principled first component and then refines the within-classical initial component (recitals,
compilations, performer-led releases) that nests under it.

**The routing signal defers to Picard (operator decision, 2026-07-21).**  Picard classifies audio
artifacts on the MusicBrainz **release-group type** vocabulary — `%_primaryreleasetype%` (Album /
Single / EP / Broadcast / Other) and `%_secondaryreleasetype%` (Audiobook / Spokenword / Soundtrack /
Compilation / Live / …) — and the community "Classical music" naming scripts (and Classical Extras)
branch their top-level directories on exactly these.  R4a follows that precedent: the top-level class is
derived from the release-group primary/secondary type, refracted through the Classical-Extras
"is-this-classical-art-music?" stance for the main split.

**Why this sits on ready substrate (why S1 is tighter than it looks).**  The routing signal is *already
in the models and already tag-persisted*: `MBReleaseGroup.primary_type` + `secondary_type_list`
(`models.py:802,804`) are mapped into the `releasetype` / `releasetype_secondary` tags at
`_tags.py:698,702` — the Picard `%_primaryreleasetype%` / `%_secondaryreleasetype%` equivalents.  S1
does **not** add the signal; it **routes the top-level path component on the signal that already
exists** and is embedded-tag-derivable (so `repath`/`regroup`/`unify`, which call `build_dest_path` with
empty stubs and no group context, keep producing correct classes).  Freezes **C-CLASS** (the top-level
class-routing function + class vocabulary + the tag-derivable signal); S2 then freezes **C-INIT** (the
within-classical initial-component rule) nesting under it.

**The design frame is durable; the immediate non-classical migration is thin.**  Of the 15
`non-classical-other` census dirs the operator has already elected to manually move most out; the
genuine residue the class scheme must house is small (audiobook/spoken-word `Aesop_Fables`;
children's-pop `Kidz Bop` ×2, `Education`; new-age `HypnoBirthing`; the aggregate `Amazon Music`).  So
C-CLASS is a durable Act-III-a / III-b design frame — **not** a large immediate migration.  Build the
scheme correctly; do not over-fit it to five directories.

Every editorial decision here refracts through **Classical Extras** (NOTES "editorial anchor"), **"path
is a handle, not a manifest"** (NOTES), and the **layer-routing rule** (this is renderer/policy = class
A — fix top-level classification in the renderer, keep MB-data defects visible until fixed upstream).

## Verify gate

Touches `src/` and `tests/`; fully gated (100% branch coverage, strict mypy).  `/plan-run` re-discovers
these; stated here to document the gate:

- **VERIFY_TEST**: `~/.local/bin/tox -e test` — pytest, **100% branch coverage enforced**
  (`fail_under = 100`).  Every new class-routing arm (each top-level class, the classical default, the
  non-classical fallback) and each within-classical branch needs an explicit test; a `match/case` on the
  class needs a `case _: # pragma: no cover` arm per house convention.
- **VERIFY_TYPES**: `~/.local/bin/tox -e check_type` — mypy strict on `src/ tests/`, **zero errors**.  No
  `Any`, no `cast()` (the release-type signal is already typed on `MBReleaseGroup`; do not widen).
- Full gate before ◆ close: `~/.local/bin/tox -m analyze` (build + test + check_type + check_format +
  check_lint 10.00/10 + check_upgrade) green.

## Session list

| # | Session | Cat | Tier | Consumes | Expected files |
|---|---------|-----|------|----------|----------------|
| 1 @architect | Introduce the top-level library class scheme in `build_dest_path`; route on the MB release-group type (Picard-aligned); nest the classical composer-first scheme under it (freeze **C-CLASS**) | A | Opus | C-PROV/C-MOVE (repath/regroup/unify call `build_dest_path` unchanged), "path is a handle" + CE editorial anchor (NOTES) | `src/music_annotator/_tags.py`, `tests/unit/test_annotator.py`, `tests/unit/test_pipeline_maint.py`, `tests/integration/test_integration.py` |
| 2 ◆ | Refine the within-classical initial directory component: recitals, compilations, performer-led releases (freeze **C-INIT**) | I | Opus | **C-CLASS** (the classical subtree boundary; the class-routing seam) | `src/music_annotator/_tags.py`, `tests/unit/test_annotator.py`, `tests/integration/test_integration.py` |

`Cat`: **A** = substrate (S1 defines the top-level class interface every downstream path derivation —
R6b/R6d repath, Act III-b — sits on); **I** = integrative (S2 refines one component riding on frozen
C-CLASS + end-to-end proof).
`Tier`: **Opus / `@architect`** on both.  S1: the class vocabulary and the routing predicate are a
non-obvious substrate design whose cost-of-wrong propagates to the whole-library R6d repath (lever 3+4).
S2: the within-classical component (composer-first vs performer-first for recitals/compilations) is a
genuine Classical-Extras editorial judgment against live code, and it freezes a contract R6b/R6d
consume.  Both are the highest-cost-of-wrong path decisions in the Act II arc; neither is mechanical.
No `◆` on S1 (mid-sub-track; C-CLASS is still being consumed by S2).  `◆` on S2 — sub-track-final; its
boundary closes R4a and hands off to R4b/R4c (the remaining Act II design nodes) en route to J2.

**Split/merge rationale (levers named).**  BACKLOG/roadmap estimated R4 at ~3-6 sessions across
R4a+R4b+R4c; R4a itself is **2**.  The split S1|S2 is the **one-line-commit-title corollary**:
"Introduce the top-level class scheme" and "Refine the within-classical initial component" are two
commit-shaped titles — joining them with "and" is the tell that it is two conceptual units.  The split is
**contract-sharp** (the legitimate split condition, not fracturing a floor): S1 freezes the class
boundary; S2's within-classical refinement *nests inside* that boundary and consumes it.  Levers 1
(ambient complexity — `build_dest_path` ~270 lines, deeply branched), 3 (cost of a wrong top-level
scheme propagates through R6d's full-library repath), and 4 (path policy, high correctness-criticality)
all push toward the smaller-and-more-reviewable 2-session shape rather than one large substrate diff.
Lever 2 (the floor) is respected: the `repath`-class-routing correctness folds *into* S1 (the substrate
is not done if `repath` produces wrong classes), so S1 is not fractured below its irreducible unit.
Lever 5 (strong inner loop) makes the small commits safe but does **not** license opting the juncture
tier down here — lever 4 holds it (see header).

## Session detail

### S1 @architect — Introduce the top-level library class scheme (freeze C-CLASS)

**Deliverable.**
- Add a **top-level class-routing function** (proposed `_top_level_class(release, tags) -> str`, the
  C-CLASS freeze — `@architect` confirms/adjusts name and signature against live code) that derives the
  first path component from the **release-group type signal that already exists**:
  `release.release_group.primary_type` + `release.release_group.secondary_type_list`
  (`models.py:802,804`; already tag-persisted as `releasetype` / `releasetype_secondary` at
  `_tags.py:698,702`).  The mapping is Picard-aligned (see the routing table below).
- Insert the class component at the **top** of the path in `build_dest_path` (`_tags.py:1083`, currently
  `top_dir = safe_name(f"{composer} - {performers}")`): the path becomes
  `dest_root / <class> / <composer-performers-or-class-shaped-top_dir> / <work_dir> / …`.  The classical
  class **nests the existing composer-first `top_dir` unchanged** underneath it; non-classical classes
  get a class-appropriate top_dir shape (below), which S2 does *not* touch (S2 refines only the
  *within-classical* component).
- **Guarantee the class is embedded-tag-derivable** so `repath` / `regroup` / `unify` (which call
  `build_dest_path` with `MBRelease()` / `MBTrack()` empty stubs and no group context —
  `_pipeline_maint.py:378-381,591,962`) reconstruct the correct class from tags alone.  Since the class
  keys on `releasetype` / `releasetype_secondary`, which are written to file (verify they survive
  `to_file_dict`), the empty-stub path must read them from `tags`, **not** from `release.release_group`.
  **This is the substrate correctness core** — if the class can only be computed from the live
  `MBRelease`, `repath` silently mis-classifies the whole library on the next maintenance pass.  Resolve
  the tag-vs-model source deterministically and pin it with a `repath` KAT.
- Preserve backward-compat posture per NOTES: the top-level class is a *new* component, so existing
  annotated releases will re-path under R6b/R6d (the one-pass re-derivation) — R4a does **not**
  retro-migrate on disk; it fixes the forward path.  State this in the class-routing docstring.
- Add the KATs (below) and an **end-to-end class-routing integration test**.

**≥1 KAT.**  The class-routing integration test is the primary KAT (end-to-end, no internal-helper
patching per the integration convention): a classical release → path under the classical class with the
composer-first top_dir intact; an audiobook release (secondary-type `Audiobook`/`Spokenword`) → path
under the spoken-word class.  Plus unit KATs: `test_top_level_class_classical` (work-type/CE-classical →
classical class), `test_top_level_class_audiobook`, `test_top_level_class_soundtrack`,
`test_top_level_class_compilation_nonclassical`, `test_top_level_class_default_fallback` (no
release-type signal → the honest fallback class), `test_build_dest_path_nests_composer_under_class`
(classical top_dir unchanged beneath the class), and a **`repath` KAT**
`test_repath_reconstructs_class_from_tags` (empty-stub `build_dest_path` derives the class from
`releasetype`/`releasetype_secondary` tags, not the live release).

**Subtleties.**
- **The tag-vs-model class source is the one genuine substrate design point (why `@architect`).**  See
  the deliverable — the class must be derivable from embedded tags for `repath` correctness.  Confirm
  `releasetype` / `releasetype_secondary` are in `to_file_dict` (the explorer noted `recording_date_work`
  and `cea_album_soloists_unified` are *excluded* — do not assume every tag survives).  If the chosen
  signal is not embedded, either persist it or choose an embedded signal; do not compute the class from a
  source `repath` cannot see.
- **The classical-vs-non-classical split refracts through Classical Extras, not a genre string.**  "Is
  this classical art-music?" is an editorial predicate (CE stance) — the existing `CWP_WORKTYPE_GENRES_TOP`
  (defaults `"Classical"`) and the presence of MB work structure are the classical signal; a release with
  a non-classical secondary-type (`Audiobook`, `Spokenword`, `Soundtrack`) or no classical work structure
  routes to a non-classical class.  Do **not** collapse the top-level class into the free-text `GENRE`
  tag (uncontrolled, currently hardcoded `"Classical"`); use the controlled release-type vocabulary as
  Picard does.
- **Over-specify the class vocabulary (Category-A).**  Carry classes the census does not yet populate if
  confidence is reasonable (the LoC "class for everything" frame) — adding a class later is cheaper than
  a mid-library re-route.  But keep the *routing* deterministic and testable; an unused class arm still
  needs a KAT or a `# pragma: no cover`.
- **Non-classical top_dir shape is S1's, not S2's.**  For non-classical classes the `<composer> -
  <performers>` shape is often wrong (an audiobook has an author/narrator, a pop compilation has no
  composer).  S1 defines a class-appropriate top_dir for the non-classical classes (e.g. artist/album or
  author-shaped); S2 refines only the *within-classical* component.  Keep the non-classical shapes simple
  and honest — the population is thin (Discoveries R-3).
- **Copy-provenance / maintenance loops untouched (C-PROV/C-MOVE).**  S1 changes only the computed
  destination path; the copy/tag/verify/journal ordering and the `_move_verify_journal` single-site
  invariant are not touched.  `repath` moving files to the new class-prefixed paths still routes through
  the frozen primitive.

**Deferrals.**
- **On-disk retro-migration of already-annotated releases.**  Not built — the whole-library re-derivation
  under the frozen class scheme is R6b/R6d (the "more like itself" pass).  R4a fixes the forward path
  only; do not disrupt the in-progress library with piecemeal class renames (NOTES / BACKLOG A-a rule).
- **The within-classical initial component.**  Explicitly S2 (C-INIT) — S1 leaves the classical top_dir
  exactly as it is today (composer-first) beneath the new class; only the class prefix is added.
- **Non-music non-audio dirs the operator will hand-move** (`Playlists`, `GarageBand`, `Audiobooks`
  aggregate, `nachtmusick`, `Lydia *`, …).  The class scheme need not have a bespoke class for each; the
  operator removes them from the library.  Do not gold-plate a class per personal-collection folder.

### S2 ◆ — Refine the within-classical initial directory component (freeze C-INIT)

*(Lower-fidelity by design — crisply specified only after C-CLASS freezes at S1, per the substrate-first
rule.  S1's action-frame digest sharpens this.)*

**Deliverable (sketch).**
- Inside the frozen **classical class** (the C-CLASS subtree), refine the initial component beyond
  today's unconditional `"<composer> - <performers>"`: handle **recitals** (performer-led, no single
  composer — a performer-first component), **compilations** (multi-composer — the `_is_composer_split_release`
  signal at `_pipeline_maint.py:680` already detects this; align the path with it), and **performer-led
  releases** generally.  The rule answers "what is the primary attribution for a classical release that is
  not single-composer?" and refracts through CE (primary attribution in path, full credits in tags).
- Freeze **C-INIT** — the within-classical initial-component rule (composer-first default; performer-first
  for recitals; compilation handling) — consumed by R6b/R6d's re-derivation.
- KATs per branch (recital → performer-first; multi-composer compilation → compilation shape;
  single-composer → composer-first unchanged) + a within-classical integration test.

**Subtleties (anticipated — resolve at execution against live code).**
- `_is_composer_split_release` (`_pipeline_maint.py:680`, keys on `"Classical" not in cwp_worktype_genres_top`)
  is an existing compilation signal in the maintenance path; C-INIT should reuse/align with it, not invent
  a parallel discriminator (avoid two sources of truth).
- Refracts through "path is a handle" (primary attribution only in path) and the CE editorial anchor
  (validate the recital/compilation framing against CE; document any divergence).
- Must not regress the single-composer classical path (the dominant population); the composer-first
  default stays the fallback.

**Deferrals.**
- **R4c concerto-like soloist editorial allowlist** — a separate R4 node (BACKLOG A-c follow-on); not
  folded here.
- **R4b cross-medium fragmentation inventory** — separate R4 node; inventory-first, sharded later.

## Cross-session contracts

### C-CLASS — top-level library class scheme *(FROZEN at S1)*

The first path component of every **newly-computed** destination and the function that derives it.  **Flavour:
compiler-enforced** (the `_top_level_class` return contract is reached through `build_dest_path`, compiler-visible) +
**test-enforced** (a KAT per class arm + the `repath`-reconstructs-from-tags KAT + a work-top-dir-depth KAT).

**Frozen signature.**

```python
def _top_level_class(tags: TrackTags) -> str: ...
```

Signature resolved to `(tags: TrackTags) -> str` — **the `release: MBRelease` parameter from the sketch is dropped**.
The class MUST be derivable from embedded tags alone (the substrate correctness core), so passing `release` would invite
the exact `repath` mis-classification trap the freeze exists to prevent: `repath`/`regroup`/`unify` call
`build_dest_path` with an empty `MBRelease()` stub (`_pipeline_maint.py:378-379,588-589,957-958`), so a class computed
from `release.release_group` would be blank on every maintenance pass.  `build_dest_path` computes `file_dict =
tags.to_file_dict()` at its head (`_tags.py:934`) and passes `tags` (or `file_dict`) to `_top_level_class`; the class
reads only tag-derived values, never `release.release_group`.  Returns a `safe_name`-clean single path component (the
function calls `safe_name` on its result, or the caller wraps once — pin whichever in the KAT).

**Tag-derivable signal — verified against live code (confirmed).**
- `releasetype` (from `release.release_group.primary_type`, `_tags.py:702`) and `releasetype_secondary` (semicolon-joined
  `secondary_type_list`, `_tags.py:698`) are named `TrackTags` fields (`models.py:1254,1263`), are **NOT** in the
  `to_file_dict()` exclusion set (`models.py:1505-1513`), and are non-empty-guarded strings → they survive to file as
  `RELEASETYPE` / `RELEASETYPE_SECONDARY`.
- On the maintenance read-back path, `_tags_from_file_dict` (`_pipeline_maint.py:71-119`) repopulates any uppercase key
  matching a known field name; `releasetype`/`releasetype_secondary` are known fields and NOT in its `_excluded` set
  (`{recording_date_work, cwp_composers_is_fallback}`), so they round-trip.  **The full ingest→persist→read-back→build
  chain is sound; no new model field, no new tag, no `cast()` is required (R-1/R-2 satisfied).**

**Classical predicate — reuse the existing tested predicate (do not invent one).**  The CE-classical split reuses the
predicate already in `_is_composer_split_release` (`_pipeline_maint.py:709-716`): a release is **classical** when
`CWP_WORK_TOP` is non-empty (MB work structure present) AND `CWP_WORKTYPE_GENRES_TOP` contains `"Classical"`; otherwise
non-classical.  Both fields survive `to_file_dict` (not excluded) and round-trip via `_tags_from_file_dict` (both are
known fields), so the predicate is fully tag-derivable — consistent with the substrate core.  A non-classical
secondary-type (`Audiobook`/`Spokenword`/`Soundtrack`) short-circuits to the matching non-classical class even when work
structure is coincidentally present.

**Routing table (Picard-aligned; MB release-group Type vocabulary verified against musicbrainz.org/doc/Release_Group/Type).**
Evaluate in this order (first match wins); the `secondary_type_list` (semicolon-joined in `releasetype_secondary`) is
checked before `primary_type` because Picard's community classical scripts branch on `%_secondaryreleasetype%` first:
1. secondary contains `Audiobook` OR `Spokenword` OR `Audio drama` OR `Interview` → **`Spoken Word`** class.
2. secondary contains `Soundtrack` → **`Soundtracks`** class.
3. CE-classical predicate true (see above) → **`Classical`** class.
4. secondary contains `Compilation` (and not classical) → **`Compilations`** class.
5. `primary_type` in {`Album`, `Single`, `EP`, `Broadcast`, `Other`} (non-classical, no other signal) → **`Popular`** class.
6. no usable signal (empty `releasetype` and `releasetype_secondary`, predicate false) → **`Unsorted`** class (the honest
   fallback — do NOT silently force `Classical`).

Over-specify per Category-A: arms 1, 2, 4 may be thinly populated by the current census but are frozen now (adding a
class later is cheaper than a mid-library re-route).  Each arm — including unpopulated ones — carries a KAT; the
`match`/`case _` needs `# pragma: no cover` per house style.

**Path shape (class prefix insertion).**  `build_dest_path` prepends the class as the new first component.  The classical
class nests **today's `<composer> - <performers>` top_dir unchanged** beneath it (S1 does not touch it; S2/C-INIT refines
only the within-classical component).  Non-classical top_dir shapes are S1's and are kept **simple and honest** (R-3 —
the population is thin):
- `Spoken Word` / `Popular` / `Compilations`: `<ALBUMARTIST or ARTIST> - <ALBUM>` (author/narrator or artist, then title;
  a spoken-word/pop release has no composer, so the classical composer-first shape is wrong here).
- `Soundtracks`: `<ALBUM>` (the soundtrack title; composer/artist is unreliable across a VA score).
- `Unsorted`: `<ALBUM or "Unknown Album">` (honest minimal shape).

Resulting layouts: `dest_root/Classical/<composer> - <performers>/<work [YYYY]>/…` (classical, unchanged nesting) and
`dest_root/<NonClassicalClass>/<class-appropriate top_dir>/<work_dir>/…`.

**LOAD-BEARING RESOLUTION — the work-top-dir depth invariant (C-PROV boundary; part of the freeze).**  Adding a first
component makes new-ingest paths **three-level** (`<class>/<top_dir>/<work_dir>/`), but the copy/sidecar/audit machinery
derives the *work top directory* positionally from the **root down** as `parts[0]/parts[1]`, which would then resolve to
`<class>/<top_dir>` — one level too shallow — silently mis-placing cover-art/freedb/whipper/provenance sidecars and
mis-scoping the collision-suffix and fragmentation logic.  Affected sites (all assume a fixed two-level
`<top_dir>/<work_dir>/`): `_pipeline.py:1341-1342` (new-ingest sidecar `work_top_dir` — **NOT deferred; in R4a's own
scope**), `_pipeline.py:389,526-530` (collision grouping + `parts[1]` work_dir suffix rewrite), `_pipeline.py:1883`,
`_audit.py:341,448-474,528,689,675-678` (fragmentation + sidecar resolution + `detect_fragmented_releases`),
`_pipeline_io.py:1469,1836-1839` (journal rebuild + provenance sidecar).  The C-PROV *frozen primitive*
(`_move_verify_journal`) and the copy/verify/journal **ordering** are genuinely untouched — this is the adjacent
positional-depth assumption, not the primitive.

**Freeze decision:** the work-top-dir and work_dir-scope derivations must become **class-depth-aware**, not
`parts[0]/parts[1]`.  The frozen rule: **the work top directory is the two components immediately beneath the class**,
i.e. `parts[1]/parts[2]` for a class-prefixed path.  Implement via a single shared helper (proposed
`_work_top_dir(dest_file, dest_root) -> Path` and `_work_dir_component(rel_parts) -> str`) that skips the leading class
component, and route ALL the sites above through it.  `build_dest_path` remains the sole authority on structure; the
helper reads the same class-prefixed shape.  This keeps C-PROV's provenance chain intact (entries still written only
after verify) while correcting the directory the sidecars land in.

**⚠ DISCOVERY / RESHARD FLAG (surface at step-3 review and the S2 ◆).**  This depth reconciliation **expands S1's file
list beyond the PLAN-stated `_tags.py` + tests**: `_pipeline.py`, `_audit.py`, and `_pipeline_io.py` must also change
(the shared `_work_top_dir` helper + routing the ~14 positional sites through it, plus their tests
`tests/unit/test_pipeline.py`, `tests/unit/test_audit.py`, `tests/integration/test_integration.py`).  This is a
foreseen-class discovery (R-1/R-2: substrate mis-read surfaced, not silently widened) — it is **additive** (a
depth-invariant helper, no contract broken) and rides through as `internal-continue` for the executor, but the driver
should record the file-list expansion.  It is **not** a `_move_verify_journal` edit and **not** an ordering change, so it
is not the frozen-primitive scope-drift HALT.  If the executor instead finds the class cannot be kept depth-invariant
without editing `_move_verify_journal` or the copy/verify/journal ordering, THAT is scope drift → HALT.

**Backward-compat / no retro-migration (R-4).**  The class prefix changes only **forward** paths; already-annotated
two-level releases are NOT retro-migrated by R4a (R6b/R6d owns the whole-library re-derivation).  During R4a the library
is a mix of two-level (old) and three-level (new) trees; the `_work_top_dir` helper MUST handle both — for a
non-class-prefixed (legacy two-level) path it returns `parts[0]/parts[1]`, for a class-prefixed path it returns
`parts[1]/parts[2]`.  Discriminate by testing whether `parts[0]` is a known class name (the closed C-CLASS vocabulary is
the discriminator) — pin this dual-shape behaviour with a KAT.  State the forward-path-only posture in the
`_top_level_class` docstring.

**KATs (frozen).**  Unit: `test_top_level_class_classical`, `test_top_level_class_audiobook` (→ Spoken Word),
`test_top_level_class_soundtrack`, `test_top_level_class_compilation_nonclassical` (→ Compilations),
`test_top_level_class_popular`, `test_top_level_class_default_fallback` (→ Unsorted),
`test_build_dest_path_nests_composer_under_class` (classical top_dir unchanged beneath `Classical`),
`test_repath_reconstructs_class_from_tags` (empty-stub `build_dest_path` derives the class from
`RELEASETYPE`/`RELEASETYPE_SECONDARY` tags, not the live release), and `test_work_top_dir_depth_invariant` (the
`_work_top_dir` helper returns the correct work dir for BOTH a legacy two-level path and a class-prefixed three-level
path).  Integration (no internal-helper patching): a classical release → `Classical/<composer-first top_dir>/…` with
sidecars in the correct work dir; an audiobook release → `Spoken Word/…` with sidecars in the correct work dir.

**Defined-in:** S1 (`_tags.py`: `_top_level_class` + `build_dest_path` class-prefix insertion; the shared
`_work_top_dir`/`_work_dir_component` helper and its adoption across `_pipeline.py`/`_audit.py`/`_pipeline_io.py`).
**Consumed-by:** S2 (the classical subtree boundary); every `build_dest_path` caller (`run()`, `repath`, `regroup`,
`unify`); downstream R6b/R6d full-library repath.  Over-specify the class vocabulary per Category-A.

### C-INIT — within-classical initial directory component *(FROZEN at S2)*

The rule choosing the initial component *inside* the classical class (composer-first default;
performer-first recitals; compilation handling).  **Flavour: test-enforced** (KAT per branch) +
**prose-enforced** (the CE-anchored attribution rule in NOTES).  **Defined-in:** S2 (`_tags.py`).
**Consumed-by:** R6b/R6d re-derivation.  Nests strictly under C-CLASS — an apparent need to change the
*class* boundary from S2 is scope drift into C-CLASS: **HALT** (destructive — C-CLASS was mis-frozen).

### Consumed (frozen upstream — invalidation is a destructive-HALT)

- **C-PROV / C-MOVE + confirmation-provenance invariant** (repo `AGENTS.md`, NOTES): unchanged — R4a
  changes only the computed destination path; the copy/tag/verify/journal ordering and the single-site
  `_move_verify_journal` primitive are untouched.  `repath`/`regroup`/`unify` route the new class-prefixed
  moves through the frozen primitive.  If the executor finds they must edit `_move_verify_journal` or the
  ordering, that is scope drift: **HALT**.  **Flavour: compiler+test-enforced.**
- **C-L0 / C-L1 (leaf/intermediate numbering), C-S0 (aggregation spans media)** (ROADMAP consumed set):
  unchanged — the class prefix is above the work_dir; leaf/intermediate numbering and cross-medium
  aggregation are untouched.  **Flavour: test-enforced.**
- **The MB release-group type fields** (`MBReleaseGroup.primary_type`, `secondary_type_list`;
  `models.py:802,804`): consumed **unchanged** — R4a routes on them, adds no model field and no `Any`.  If
  the executor finds the signal must be added to the model or a `cast()` is needed, the model is
  under-typed: fix the model per house style, and note it as a discovery.  **Flavour: compiler-enforced.**

### Produced

- **C-CLASS** (S1), **C-INIT** (S2).  No other new contract.

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 1 | Introduce the top-level library class scheme in build_dest_path; route on MB release-group type; nest classical composer-first under it | done | 7666040 | C-CLASS |
| 2 | Refine the within-classical initial directory component (recitals, compilations, performer-led) | done | 2cef03e | C-INIT |

## Action-frame digest

### S1 inflection — 2026-07-21
Discovery/flex: C-CLASS interface frozen; `_top_level_class(tags: TrackTags) -> str` (no `release` param); 6-arm routing table; tag-derivable signal confirmed; depth-invariant `_work_top_dir` helper required at ~14 sites in `_pipeline.py`/`_audit.py`/`_pipeline_io.py` (additive, internal-continue).
Affected: C-CLASS (frozen), S1 expected-file list expanded to include `_pipeline.py`, `_audit.py`, `_pipeline_io.py` and their tests.
Deferred: no — depth reconciliation is in S1's scope, not deferred.
Texture: The `_work_top_dir` helper must handle dual-shape (legacy two-level + new three-level) during R4a; discriminate by testing `parts[0]` against the closed C-CLASS vocabulary.

## Discoveries & risks

- **R-1 (route on the release-type signal that already exists — do not invent one).**  `MBReleaseGroup.primary_type`
  + `secondary_type_list` exist and are already tag-persisted (`releasetype`/`releasetype_secondary`,
  `_tags.py:698,702`).  If the executor finds themselves adding a new model field or a new tag to carry
  the class, that is a signal the substrate was mis-read — prefer the existing signal.  A genuinely-missing
  signal is an **additive-reshard** signal (surface at the ◆), not a silent model widening.
- **R-2 (the class MUST be embedded-tag-derivable — the `repath` trap).**  `repath`/`regroup`/`unify` call
  `build_dest_path` with empty `MBRelease()`/`MBTrack()` stubs and no group context; a class computed from
  the live `MBRelease` (not from `tags`) will silently mis-classify the entire library on the next
  maintenance pass.  This is the S1 substrate correctness core — pin it with `test_repath_reconstructs_class_from_tags`.
  If `releasetype`/`releasetype_secondary` turn out **not** to survive `to_file_dict`, that is a discovery
  requiring either persisting the class signal or choosing an embedded one — surface it, do not compute
  from an unreachable source.
- **R-3 (the non-classical population is thin — build the frame, not five special cases).**  Most of the 15
  `non-classical-other` census dirs are operator-hand-moved; the genuine residue is ~5 (audiobook, kidz-bop
  ×2/education, hypnobirthing, amazon aggregate).  C-CLASS is a durable Act-III design frame; over-fitting
  it to five directories is defocus/gold-plating.  Over-specify the *vocabulary* (Category-A), but keep the
  non-classical top_dir shapes simple.
- **R-4 (no on-disk retro-migration in R4a).**  The forward class prefix changes paths for *new* ingests;
  already-annotated releases re-path under R6b/R6d, not now (NOTES/BACKLOG A-a: never disrupt an
  in-progress library with piecemeal renames).  If the executor reaches for a bulk `repath` of the existing
  library to apply the class scheme, that is scope drift into R6 — **internal-continue only for the forward
  path**; a bulk re-derivation is an **additive-reshard / R6 handoff** signal.
- **R-5 (S2 must not touch the class boundary).**  The within-classical refinement nests strictly under
  C-CLASS.  An apparent need to change the top-level class *from S2* (e.g. "recitals should be their own
  top-level class, not a within-classical shape") is a **destructive-HALT** signal that C-CLASS was
  mis-frozen — surface at the ◆, do not widen C-CLASS in place from the integrative session.
- **R-6 (defer to Picard / Classical Extras; document divergence).**  The routing follows Picard's
  release-type branching and CE's editorial stance; where the annotator diverges from CE, NOTES requires a
  documented rationale.  S1 should verify the Picard `%_primaryreleasetype%`/`%_secondaryreleasetype%`
  branching against current Picard docs/plugins at execution time (this PLAN states the model from
  `@architect`'s knowledge; confirm before freezing the vocabulary).

## Notes for executors

- **Tier routing.**  Both sessions are **Opus / `@architect`** — S1's class vocabulary + tag-derivable
  routing predicate is the substrate design (cost-of-wrong propagates to R6d); S2's within-classical
  refinement is a Classical-Extras editorial judgment freezing a contract.  Do not delegate either freeze
  to a mechanical pass.
- **Register: PEDAGOGY off** — thin mechanical docstrings per house style (Sphinx/PEP 257, 128-col).  The
  `_top_level_class` docstring states the routing table, the tag-derivable-source requirement, and the
  forward-path-only (no retro-migration) posture.
- **Invariants to preserve (do not regress):** C-PROV/C-MOVE copy/verify/journal ordering and the
  single-site `_move_verify_journal` primitive (only the destination path changes); C-L0/C-L1 numbering and
  C-S0 aggregation (the class prefix is above the work_dir); "path is a handle, not a manifest" (primary
  attribution only in the path); the CE editorial anchor (document divergences); no `Any`, no `cast()` (the
  release-type signal is already typed).
- **`match/case` convention.**  A `match/case` on the class or the within-classical branch needs a
  `case _: # pragma: no cover` arm per house style.
- **Sequencing:** R4a is the **first R4 shard** after the R3 code arc closed (R3d ◆).  It is on the Act II
  critical path to **J2** (naming-policy freeze), which gates R6.  On the S2 ◆, R4a closes and hands off to
  **R4b** (cross-medium fragmentation inventory — inventory-first, sharded later) and **R4c** (concerto
  soloist allowlist).  The parallel R5 operator drain and the now-eligible post-R3 structural-audit are
  independent of this sub-track.
- **Full gate before ◆ / commit:** `~/.local/bin/tox -m analyze` green (100% branch cov, mypy strict,
  pylint 10.00/10, pyupgrade clean).
- **Suggested `/plan-run` invocation:** `/plan-run halt-at-boundaries` — this is an unproven shard pattern
  (first Act-II shard; a new top-level path component is a wide substrate surface), and S1 carries the one
  load-bearing substrate decision (the C-CLASS vocabulary + tag-derivable routing).  A boundary halt lets
  the class-scheme freeze be reviewed before S2 consumes it and before the ◆ closes R4a.
