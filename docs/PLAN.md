<!-- juncture-tier: opus -->
<!-- sub-track: naming-policy re-freeze — library-completion arc (docs/ROADMAP.md), J2-reopening
     freeze.  The top-level class scheme (Classical/Popular/Compilations/…) rendered by
     _top_level_class was refuted by operator judgment 2026-08-19 (ROADMAP Discoveries appendix): it
     derived the topmost, most topology-defining path component from MusicBrainz's entropic
     free-classification parameters (release-group primary/secondary types, is-classical predicate),
     which are crowd-set and unanchored — scrambling library topology.  This sub-track re-freezes the
     catalog naming policy under the operator's decided direction: a universal (prefix-less) top dir
     over scholarship-stable components; all editorial categorization relocates to the playlist lens
     (docs/ROADMAP-playlists.md).  The adjudication is complete (architect session 2026-08-19; Q1–Q3
     resolved); this shard is the implementation handoff.  Freezes C-UNIVERSAL (replaces the refuted
     C-CLASS; absorbs and generalises C-INIT) + the epistemic-criterion prose contract (NOTES). -->

# PLAN — naming-policy re-freeze: universal top dir (C-UNIVERSAL)

## Purpose (design intent)

*(Re-read at every ◆ boundary — anti-defocus anchor.)*

The refuted policy built the library's **topmost, most topology-defining** path component
(`Classical/`, `Popular/`, `Compilations/`, `Soundtracks/`, `Spoken Word/`, `Unsorted/`) from
MusicBrainz's **free-classification parameters** — release-group primary/secondary types and the
is-classical predicate — which are crowd-set, inconsistently applied, and unanchored.  The same
programme lands under different classes by MB editor whim, scrambling the library topology and
baking an editorial view into the *catalog* lens in violation of the arc's two-lens principle
(filesystem = catalog, playlists = reading room).

**The re-frozen policy.**  The catalog path is **prefix-less** — a universal top dir.  The
first component directly under `dest_root` is the scholarship-stable first-component shape
(composer-first / performer-led / compilation), which reads only release facts and MB
scholarship-convergent data (composer linkage, albumartist, album), never free-classification
parameters.  All editorial organization (the pop/classical split, genre views, curated sets) moves
to the playlist lens (`docs/ROADMAP-playlists.md`).

**The epistemic criterion (operator ruling 2026-08-19; pinned in NOTES this shard).**  Defer to MB
where variation is *scholarship-driven* and converges under editorial pressure — composer identity,
recording dates, catalogue facts.  Never let MB's *free-classification parameters* define library
topology: they are entropy, not signal.

**This sub-track delivers the C-UNIVERSAL re-freeze in code+tests+durable prose, code-only.**  The
destructive library-wide migration (every existing 3-level class-prefixed dir → prefix-less
2-level) rides R6d's one J3-gated pass — this shard changes only newly-computed paths and freezes
the policy; it does not run a destructive pass.

**Status note (2026-08-19).**  This shard supersedes the completed `unify`-plumbing fix (committed
`2161dae`, ledger closed).  It unblocks the halted J3/R6d line: after this freeze lands, the J3
preflight re-runs against the new policy (the 2026-08-14 evidence was stale by construction — every
one of the 9,009 `unify` destinations embedded a class dir).

**The structural facts that shape this shard (survey 2026-08-19).**

- **The refuted mechanism is three functions in `_tags.py`:** `_top_level_class` (`:228`, the
  C-CLASS routing table over free-classification params — **deleted**), `_classical_top_dir`
  (`:279`, the within-classical first-component rule, C-INIT — **generalised + renamed**, logic
  unchanged), and the class-prefix assembly in `build_dest_path` (`:1371`–`:1447`, the
  `class_dir = safe_name(_top_level_class(tags))` line + the `match class_dir:` block — **collapsed**
  to the prefix-less first-component computation).
- **C-INIT's three branches read only scholarship-stable data** (`releasetype_secondary` for the
  compilation gate; `albumartist`/`albumartistsort`/`album`; `CWP_COMPOSER_LASTNAMES`/
  `CEA_COMPOSER_LASTNAMES` for composer linkage).  None reads a free-classification param, so the
  epistemic criterion leaves them intact.  A pop album (no linked composer) routes naturally through
  the performer-led branch → `<albumartist> - <album>`.
- **`IS_CLASSICAL` is currently derived from the dying path layer** (`_tags.py:1005`:
  `tags.is_classical = "1" if _top_level_class(tags) == "Classical" else "0"`).  It must be rewired
  to derive from **compositional identity** (the CE-classical predicate: `cwp_work_top` non-empty
  AND `"Classical" in cwp_worktype_genres_top`) per REND-21/SEL-14 — tag layer ≠ path layer.
- **The legacy/class-prefixed path discriminator** (`_CLASS_VOCAB` at `_tags.py:225`, consumed by
  `_work_top_dir` `:359` and `_work_dir_component` `:344`, and imported in `_audit.py`,
  `_pipeline_io.py`, `_pipeline.py`) tests `parts[0] in _CLASS_VOCAB` to tell 3-level class-prefixed
  paths from 2-level legacy paths.  **It must stay for the transition** — the live library still
  holds 3-level class-prefixed dirs until R6d migrates them.  Removing the discriminator is a
  **post-R6d** cleanup, NOT this shard.  (After the freeze, newly-written paths are 2-level again;
  the discriminator's "legacy 2-level" arm is the new-path arm.)
- **The styleguide describes, does not define, this policy** (STYLEGUIDE 4.5; REND-22 explicitly:
  "an apparent conflict is a finding for that arc's boundary").  So the freeze needs only a **status
  correction** to 4.5 (drop "class directory;" from the path-component list) and REND-21/22/23 —
  NOT a reauthoring.  Deferred to a thin styleguide-sync follow-on (Deferrals); the code freeze does
  not block on it.

## Verify gate

Discovered from `pyproject.toml` (tox envs; do not assume `make`).  Binding — this is a code shard.

- **VERIFY_TEST**: `~/.local/bin/tox -e test` (`pytest tests/`; **100% branch coverage**, `fail_under = 100`).
- **VERIFY_TYPES**: `~/.local/bin/tox -e check_type` (`mypy src/ tests/`, strict).
- Full gate before ledger-done: `~/.local/bin/tox -m analyze` (build + test + check_type +
  check_format + check_lint 10.00/10 + check_upgrade).  Import order via `~/.local/bin/tox -m edit`,
  never hand-edited.

## Session list

| # | Session | Cat | Tier | Consumes | Expected files |
|---|---------|-----|------|----------|----------------|
| 1 ◆ | Re-freeze the catalog naming policy: delete the class prefix, generalise the first-component rule, decouple `IS_CLASSICAL` from the path | I | Sonnet | the refuted C-CLASS (delete), C-INIT (generalise+absorb), REND-21/SEL-14 (IS_CLASSICAL basis), the epistemic criterion | `src/music_annotator/_tags.py`, `tests/unit/test_annotator.py`, `tests/unit/test_pipeline.py`, `tests/integration/test_integration.py`, `docs/NOTES.md` |

`Cat`: **I (integrative)** — the path grammar is where the catalog lens's topology is defined; this
row re-freezes it under the scholarship-stable direction and pins the epistemic criterion in durable
prose.  Single-session sub-track, so the one row is the ◆ boundary.

`Tier`: **Sonnet.**  The design decisions are frozen (architect adjudication 2026-08-19, Q1–Q3
resolved); this row *enacts* them.  Mechanical over an existing structure (delete one function,
ungate+rename a second, collapse a `match` block, rewire one assignment), strong inner loop (100%
branch + strict mypy).  Lever 3/4 (design-error cost / correctness-crit) is discharged upstream: the
posture is decided; the risk that remains is coverage/round-trip regression, which the gate catches.
`juncture-tier: opus` — kept (arc default); no juncture fires in a one-row shard.

**Sizing (levers named).**  Default band ~150–400 LOC / 2–4 files.

- **S1 ≈ 120–250 LOC across 5 files** (net-negative in `_tags.py`: `_top_level_class` deleted, the
  `match class_dir:` block collapsed; the churn is in tests — the C-CLASS KAT class is deleted, the
  C-INIT KATs re-home under the universal rule, and every integration/pipeline test that expected a
  class-prefixed path drops the prefix).  **Irreducible unit (lever 2, floor):** the deletion, the
  generalisation, the `IS_CLASSICAL` rewire, and the test realignment are one conceptual unit
  ("re-freeze the catalog topology") — splitting them leaves the tree red (a deleted
  `_top_level_class` with callers, or a class-prefix `build_dest_path` with class-less tests).  Kept
  whole.  One-line commit-title passes.

## Session detail

### S1 ◆ — Re-freeze the catalog naming policy (C-UNIVERSAL)

**Deliverable.**  Enact the frozen re-freeze:

- **Delete `_top_level_class`** (`_tags.py:228`) and the `_CLASS_VOCAB`-based routing it drives *from
  the path layer*.  **Keep `_CLASS_VOCAB`** and the `_work_top_dir`/`_work_dir_component`
  discriminator — they are needed for the transition (live library still holds 3-level paths until
  R6d).  Update `_CLASS_VOCAB`'s docstring: it is now a *legacy-path-recognition* vocabulary (reads
  historical class-prefixed dirs), no longer a *live routing* vocabulary.
- **Generalise + rename `_classical_top_dir`** (`_tags.py:279`) to a universal first-component
  helper (suggested `_top_dir_component`; the executor picks a register-clean name — the point is it
  no longer says "classical"/"recital").  **Logic unchanged**: compilation → `<albumartist-last or
  "Various"> - <album>`; performer-led (no linked composer) → `<albumartist> - <album>`;
  single-composer → `None` (caller uses `<composer> - <performers>`).  Update the docstring to state
  the branches are universal (a pop album is the performer-led branch), and that all three read
  scholarship-stable data (release facts + composer-convergent MB data), never free-classification
  params.
- **Collapse the class-prefix assembly in `build_dest_path`** (`_tags.py:1371`–`:1447`).  Remove
  `class_dir = safe_name(_top_level_class(tags))` and the `match class_dir:` block.  The first
  component becomes: call the generalised first-component helper; when it returns `None`
  (single-composer) use `safe_name(f"{composer} - {performers}")`.  Every `dest_root / class_dir /
  top_dir / …` path build drops the `class_dir` segment → `dest_root / top_dir / …`.  The
  Soundtracks/Unsorted/Spoken-Word/Popular/Compilations arms of the old `match` fold away: their
  distinctions were the editorial class split (now playlist-lens); their *shape* (`<artist> -
  <album>` or `<album>`) is already the performer-led/compilation branch of the generalised helper.
  **Confirm no non-classical shape is silently lost** — the old Soundtracks arm used bare `<album>`;
  verify the generalised helper's performer-led branch (which uses `<albumartist> - <album>`, or
  bare `<album>` when albumartist is empty) covers it, or add an explicit branch.  This is the one
  place the collapse could drop behaviour — the executor must diff the old `match` arms against the
  generalised helper's outputs and either confirm equivalence or preserve the arm.
- **Rewire `IS_CLASSICAL`** (`_tags.py:1005`).  Replace `_top_level_class(tags) == "Classical"` with
  the CE-classical predicate directly: `tags.is_classical = "1" if (tags.cwp_work_top and
  "Classical" in tags.cwp_worktype_genres_top) else "0"` (REND-21/SEL-14 — classification derives
  from compositional identity, never the code path).  Tag layer ≠ path layer: the flag survives as a
  work-type tag and a future playlist input; it no longer defines topology.
- **Pin the epistemic criterion in `docs/NOTES.md`** as a prose contract (alongside the two-lens,
  "path is a handle", layer-routing, "journal detects, tag adjudicates" contracts): defer to MB
  where variation is scholarship-driven and converges (composer identity, dates, catalogue facts);
  never let MB's free-classification parameters (release-group types, is-classical predicates)
  define library topology.  Name it as the basis for the universal-top-dir catalog shape and the
  playlist-lens relocation of editorial views.

**KAT (the row's behavioural witnesses).**

(a) **prefix-less path witnesses** — `build_dest_path` for a single-composer classical release
returns `dest_root / "<Composer> - <Performers>" / "<Work> [YYYY]" / "<nn> - <title>"` with **no**
class component (was `dest_root / "Classical" / …`).  A pop album returns `dest_root / "<Artist> -
<Album>" / …`.  A compilation returns `dest_root / "<Various or last> - <Album>" / …`.  Replaces
every existing test that asserted a `"Classical"`/`"Popular"`/etc. first component.
(b) **first-component-rule witnesses** — the generalised helper: compilation gate →
`<albumartist-last> - <album>`; empty-composer → `<albumartist> - <album>` (and bare `<album>` when
albumartist empty); linked-composer → `None`.  Re-homes the C-INIT KATs (`test_annotator.py:4652+`)
under the universal name; **deletes the C-CLASS KAT class** (`test_annotator.py:4384+`) — the routed
vocabulary no longer exists.
(c) **`IS_CLASSICAL`-from-work-type witnesses** — `IS_CLASSICAL == "1"` iff `cwp_work_top` non-empty
AND `"Classical" in cwp_worktype_genres_top`, **independent of any path component** (the REND-21 KATs
at `test_pipeline.py:4327+` re-key off the predicate, not `_top_level_class`).  Add a witness that a
classical work whose path is prefix-less still gets `IS_CLASSICAL == "1"` (proves the decouple).
(d) **discriminator-still-works witness** — `_work_top_dir` / `_work_dir_component` still correctly
read a *legacy* 3-level class-prefixed path (transition safety) AND a new 2-level prefix-less path.
Both arms covered (the class-prefixed arm is now legacy-only, still live until R6d).

**Subtleties.**
- **Coverage: the deleted routing.**  Deleting `_top_level_class` removes 6 match arms' worth of
  branches; deleting its KAT class removes their tests in lockstep — net coverage-neutral, but the
  executor must confirm no *other* caller of `_top_level_class` survives (grep: `models.py:1385`
  comment reference is docstring-only; the live callers are `_tags.py:1005` and `:1374`, both
  rewritten here).
- **The collapse-equivalence check is the one real risk.**  The old `match class_dir:` had
  class-specific shapes (Soundtracks = bare `<album>`).  The generalised helper must reproduce every
  shape or the collapse silently re-paths a population.  Diff the arms; confirm or preserve.
- **No R6d run, no migration machinery.**  This shard changes newly-computed paths only.  The
  destructive migration of existing class-prefixed dirs rides R6d (D-A5 precedent); `repath`/`unify`
  re-derive the prefix-less shape on demand from embedded tags.
- **Register discipline.**  NOTES prose and docstrings state the *property/invariant* (scholarship-
  stable topology; free-classification params never define topology; tag layer ≠ path layer) — never
  a plan coordinate (no "C-CLASS-refutation", no "J2", no "R6d", no "S1").  Contract names
  (C-UNIVERSAL, C-INIT, REND-21) are legitimate durable vocabulary.

**Deferrals.**
- **Styleguide sync** (thin follow-on, not this shard): STYLEGUIDE 4.5 drop "class directory;" from
  the path-component list; REND-22 status → "C-CLASS refuted; superseded by C-UNIVERSAL (prefix-less
  universal top dir)"; REND-23 status → "C-INIT absorbed into C-UNIVERSAL, generalised"; REND-21
  gloss → note the flag now derives from the predicate directly (the "must derive from
  classification, never the code path" caveat is now satisfied).  Deferred because the styleguide
  *describes* the policy (REND-22) and the code freeze does not block on the prose sync.
- **Discriminator removal** (post-R6d): once R6d migrates every 3-level class-prefixed dir,
  `_CLASS_VOCAB` and the two-arm discriminator in `_work_top_dir`/`_work_dir_component` collapse to
  the single 2-level form.  Sequenced after the destructive pass; a reopen trigger, not this shard.
- **J3 preflight re-run** (post-freeze, separate): the go/no-go evidence re-runs against the new
  policy; not this shard's scope.

**◆ boundary (register anneal).**  Re-read Purpose.  Confirm the row enacted, `tox -m analyze`
green, ledger complete.  **Planning-register anneal:**
- Durable files (`_tags.py`, the three test files, `NOTES.md`) carry **no plan coordinates** — state
  the property/invariant (scholarship-stable topology; tag layer ≠ path layer), never
  "C-CLASS-refutation"/"S1"/"J2"/"R6d".
- Grep durable files against the anneal denylist (Notes for executors); translate any leaked
  coordinate to standalone prose.
- Report to the roadmap: C-CLASS refuted-and-deleted; **C-UNIVERSAL frozen** (prefix-less universal
  top dir; generalised first-component rule absorbing C-INIT); `IS_CLASSICAL` decoupled to the
  work-type predicate; epistemic criterion pinned in NOTES.  J2's taxonomy half re-freezes here; J3
  preflight re-run unblocks.

## Cross-session contracts

### Produced (frozen this sub-track)

- **C-UNIVERSAL** — the re-frozen catalog naming policy.  The catalog path is prefix-less (no
  top-level class component); the first component under `dest_root` is the scholarship-stable
  first-component shape (compilation → `<albumartist-last or "Various"> - <album>`; performer-led →
  `<albumartist> - <album>`; single-composer → `<composer> - <performers>`), reading only release
  facts and composer-convergent MB data, never free-classification parameters.  Replaces the refuted
  **C-CLASS**; absorbs and generalises **C-INIT**.
- **The epistemic-criterion prose contract** (pinned in NOTES this shard) — defer to MB where
  variation is scholarship-driven and converges; never let MB free-classification parameters define
  library topology.

### Consumed (frozen upstream — validate-only)

- **REND-21 / SEL-14** — `IS_CLASSICAL` derives from compositional identity (work-type predicate),
  not the code path.  This shard satisfies the caveat REND-21 flagged.
- **The two-lens principle + "path is a handle, not a manifest"** (NOTES) — the catalog lens is
  uniform and fact-anchored; editorial views live in the playlist lens (`docs/ROADMAP-playlists.md`).
- **C-PROV / C-MOVE + confirmation-provenance** — untouched; this shard changes path computation
  only, not the move/verify/journal chain.
- **C-CANON** — canonical name-forms in the performers component; untouched (the `<composer> -
  <performers>` shape still renders canonical forms).

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 1 ◆ | Re-freeze the catalog naming policy: delete the class prefix, generalise the first-component rule, decouple `IS_CLASSICAL` | pending | — | C-UNIVERSAL + epistemic-criterion prose contract |

## Action-frame digest

*(none yet)*

## Discoveries & risks

- **D-1 (the refutation — this shard's reason).**  The top-level class scheme derived the topmost
  path component from MB free-classification parameters; operator refutation 2026-08-19.  Resolution:
  delete the class layer; freeze C-UNIVERSAL (prefix-less universal top dir).  *internal-continue.*
- **D-2 (collapse-equivalence risk — the one real risk).**  The old `match class_dir:` block had
  class-specific first-component shapes (Soundtracks = bare `<album>`); the generalised helper must
  reproduce each or the collapse silently re-paths a population.  Executor must diff the arms and
  confirm/preserve.  KAT (a)/(b) witness the shapes.  *internal-continue.*
- **D-3 (transition-safety — keep the discriminator).**  The live library holds 3-level
  class-prefixed dirs until R6d migrates them, so `_CLASS_VOCAB` + the `_work_top_dir` discriminator
  must stay this shard (removal is post-R6d).  KAT (d) witnesses both arms.  *internal-continue.*
- **D-4 (`IS_CLASSICAL` wiring dies with the class layer).**  The flag currently reads
  `_top_level_class(tags) == "Classical"`; deleting that function requires the rewire to the
  work-type predicate (REND-21/SEL-14).  Not optional — the current wiring is unavailable post-delete.
  KAT (c) witnesses.  *internal-continue.*
- **D-5 (styleguide describes, does not define — sync deferred).**  STYLEGUIDE 4.5/REND-22 point at
  this policy by reference (REND-22 anticipated the conflict).  The prose sync is a thin follow-on,
  not a code-freeze blocker.  Deferred.  *internal-continue.*

## Notes for executors

- **Tier routing.**  S1 is **Sonnet** (the design is frozen upstream; this enacts it).  `juncture-tier:
  opus` kept (arc default); no juncture fires in a one-row shard.
- **Delete, generalise, collapse, rewire — in that order.**  (1) delete `_top_level_class`; (2)
  generalise+rename `_classical_top_dir`; (3) collapse the `match class_dir:` block in
  `build_dest_path` (with the equivalence diff, D-2); (4) rewire `IS_CLASSICAL` (D-4).  Keep
  `_CLASS_VOCAB`/discriminator (D-3).
- **The equivalence diff is mandatory (D-2).**  Before collapsing, enumerate the old `match` arms'
  first-component outputs and confirm the generalised helper reproduces each, or preserve the arm.
- **REGISTER rule (durable-file discipline).**  In source/tests/NOTES, state the *property/invariant*
  — scholarship-stable topology; MB free-classification params never define topology; tag layer ≠
  path layer — never a plan coordinate.  Plan vocabulary (S1, J2, J3, R6d, C-CLASS-refutation,
  sub-track, `/plan-run`) lives only in `PLAN.md`/`ROADMAP*.md`/ledger/commit messages.
- **Anneal denylist (◆ gate greps durable files for these).**
  - `\bS[1-9]\b` (plan session coordinates) — **but** allow STYLEGUIDE rule-section forms
    (`\b[1-5]\.[0-9]\b` like "4.5", "3.1" are register cites — do **not** flag).
  - `\bR6[a-e]\b`, `\bR[0-9]\b`, `\bJ[1-3]\b` (roadmap node + juncture coordinates) — flag in durable
    source/tests; legitimate only in PLAN/ROADMAP/ledger/commit messages.
  - `sub-track`, `plan-run`, `plan-shard`, `halt-at-boundaries`, `run-to-boundary`, `juncture`,
    `inflection`, `action-frame`, `◆`, `C-CLASS-refutation`, `naming-policy re-freeze` (as a coordinate).
  - Do **not** flag: `C-UNIVERSAL`, `C-INIT`, `C-CLASS` (as a superseded-contract *name* in a
    docstring status note), `REND-21`/`REND-22`/`REND-23`, `SEL-14`, `IS_CLASSICAL`, `_CLASS_VOCAB`,
    `build_dest_path`, `cwp_work_top`, `cwp_worktype_genres_top`, `MUSICBRAINZ_*` — legitimate
    domain/API/contract vocabulary this shard renders.
- **Invariants to preserve:** C-UNIVERSAL (prefix-less scholarship-stable topology); the epistemic
  criterion (MB free-classification params never define topology); tag layer ≠ path layer
  (`IS_CLASSICAL` from work-type); the two-lens principle; C-PROV/C-MOVE provenance (untouched);
  transition-safety (discriminator kept until R6d).
- **Every row runs `~/.local/bin/tox -m analyze` before ledger-done.**  Import order via
  `~/.local/bin/tox -m edit`, never hand-edited.
- **Suggested first `/plan-run` invocation:** `run-to-boundary` — a single-row shard with the design
  frozen upstream; run the row through its ◆ in one pass.  Watch item: the D-2 collapse-equivalence
  diff is the one place to slow down before committing.
