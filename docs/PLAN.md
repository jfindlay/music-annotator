<!-- juncture-tier: opus -->
<!-- sub-track: A-shards (post-v1 styleguide application) — the four tag/path-grammar code changes that STYLEGUIDE v1
     (layer 4) mandates against the enacted code.  Lives under ROADMAP R6d (library-completion arc) as an R6-planning
     input; lands AHEAD of R6d so the one-pass re-derivation runs already-corrected code.  This IS a /plan-run target
     (unlike the interactive V1b PLAN it replaces): the four shards are mechanical code+test changes verifiable by the
     src/tests gate with zero library access. -->

# PLAN — A-shards: post-v1 styleguide application (four tag/path-grammar changes)

## Purpose (design intent)

*(Re-read at every ◆ boundary — anti-defocus anchor.)*

STYLEGUIDE v1 froze the editorial rendering rules (layer 4) that the enacted code predates.  Four rulings diverge from
what the code does; each is a persisted-tag or path-grammar change that R6d's one-pass re-derivation will bake into the
whole library, so they must land *before* R6d re-derives.  This sub-track makes the code match the frozen styleguide —
no new editorial decisions are taken here; every change cites a v1 ruling as its authority.  The unifying principle is
**one attribution model, many projections** (STYLEGUIDE P1): every composite tag and the path component must be a
declared grammar over the one model, in the model's billing order, under a name that means what CE means by it.

The four changes, in landing order:

1. **S1 — Concerto-gate deletion (SEL-11 overturned).**  The soloist is never in the path, however principal
   (STYLEGUIDE 4.5, SEL-11).  Delete the concerto-soloist path injection *and its now-dead plumbing* (the `run()`
   soloist-union pass, the `cea_album_soloists_unified` field, the `_pipeline_maint` reader).  Contract-sharp: freezes
   **C-NOSOLO** ("no soloist ever enters the path component").
2. **S2 — REND-14 reorder + naming realignment.**  Reorder the recording-artist composite to billing order
   (soloists → conductors → ensembles; STYLEGUIDE 4.2), and resolve the standing-rule-2 naming-drift hazard: the enacted
   composite carries *assembled* semantics under `CEA_RECORDING_ARTIST`, a CE variable whose CE meaning is the
   *verbatim* recording credit.  Freezes **C-RA-GRAMMAR**.
3. **S3 — chorusmaster-into-CONDUCTOR.**  `CONDUCTOR` also carries the annotated chorusmaster credit
   ("Name (choirmaster)"; STYLEGUIDE 4.4, SEL-3 credit-routing half, REND-3 vocabulary).  The separate `CHORUSMASTER`
   tag is retained; this is additive routing, not a move.
4. **S4 ◆ — IS_CLASSICAL conditionalisation (REND-21).**  Make `IS_CLASSICAL` reflect the actual `_top_level_class`
   result instead of the hardcoded `"1"`.  Currently latent (only classical releases reach `build_track_tags`); make it
   correct now so R6d can never surface it.

## Verify gate

Discovered from `pyproject.toml` (tox envs); do not assume `make`.  Both are **binding** — this is a code sub-track.

- **VERIFY_TEST**: `~/.local/bin/tox -e test` (`pytest tests/`; **100% branch coverage enforced**, `fail_under = 100`).
- **VERIFY_TYPES**: `~/.local/bin/tox -e check_type` (`mypy src/ tests/`, strict).
- Full gate before declaring any row done: `~/.local/bin/tox -m analyze` (build + test + check_type + check_format +
  check_lint 10.00/10 + check_upgrade).  The `AGENTS.md` "never skip `tox -m analyze`" rule applies to every row.

## Session list

| # | Session | Cat | Tier | Consumes | Expected files |
|---|---------|-----|------|----------|----------------|
| 1 | Delete the concerto-soloist path injection and its dead plumbing (SEL-11) | A | Opus | STYLEGUIDE 4.5/SEL-11, C-S0 | `src/music_annotator/_tags.py`, `src/music_annotator/_pipeline.py`, `src/music_annotator/models.py`, `src/music_annotator/_pipeline_maint.py`, `tests/unit/test_annotator.py`, `tests/integration/test_integration.py` |
| 2 | Reorder CEA recording-artist to billing order and realign composite naming (REND-14) | B | Opus | **C-NOSOLO**, STYLEGUIDE 4.2/4.4, standing rule 2 | `src/music_annotator/_tags.py`, `src/music_annotator/_tagger.py`, `src/music_annotator/models.py`, `tests/unit/test_pipeline.py` |
| 3 | Route the chorusmaster credit into CONDUCTOR (REND-3/SEL-3) | B | Sonnet | **C-RA-GRAMMAR**, STYLEGUIDE 4.4/SEL-3 | `src/music_annotator/_tags.py`, `tests/unit/test_pipeline.py` |
| 4 ◆ | Conditionalise IS_CLASSICAL on top-level class (REND-21) | A | Sonnet | STYLEGUIDE 4.7/REND-21, C-CLASS | `src/music_annotator/_tags.py`, `src/music_annotator/models.py`, `tests/unit/test_pipeline.py`, `tests/unit/test_models.py` |

`Cat`: **S1 is A (substrate)** — it *removes* a path-grammar surface and freezes C-NOSOLO, the invariant every later
path change and R6d assume; treat as substrate, over-specify the "no soloist anywhere" property.  **S2 is B** — it
reshapes an existing composite (order + name).  **S3 is B** — additive credit routing into an existing tag.  **S4 is A**
— it conditionalises a persisted flag on the C-CLASS substrate (a class-scheme consumer), the sub-track's clean closer.
`Tier`: **S1 and S2 are Opus** — S1's deletion has real blast radius across four source files plus the C-S0 aggregation
pass, and getting "dead" wrong leaves orphan machinery under the 100%-coverage gate; S2 carries the standing-rule-2
naming decision, a same-name-different-semantics judgment a test cannot fully catch.  **S3 and S4 are Sonnet** —
mechanical, single-surface, one clear ruling each; the strong inner loop (lever 5) covers them.  The ◆ on S4 closes the
sub-track and reports the corrected grammar as an R6-planning input to R6d (ROADMAP).

**Sizing (levers named).**  Default band ~150–400 LOC / 2–4 files.
- **S1 ≈ 150–250 LOC, up to 6 files** (mostly deletions + test adjustments).  **Lever 2 (the floor) forbids splitting**:
  deleting the `_tags.py` injection strands the `run()` soloist-union pass and the `cea_album_soloists_unified` field
  with no consumer — the 100%-branch-coverage gate (lever 5) then *fails* on the now-unreachable union pass, so the
  deletion is one irreducible unit.  It exceeds the file-count band by design (the plumbing spans four source files);
  splitting at any interior point leaves the tree red.  One-line-commit-title check: passes.
- **S2 ≈ 60–100 LOC, 3–4 files.**  Reorder + naming realignment are bundled because NOTES/STYLEGUIDE treat them as one
  queue item ("REND-14 reorder + naming realignment") and they are one conceptual unit — you cannot realign the tag's
  name without settling the order it now renders in.  **Lever 2** keeps them whole; **lever 3 (cost of a design error)**
  is why it is Opus, not why it splits.
- **S3 ≈ 30–50 LOC, 2 files;  S4 ≈ 30–50 LOC, 2–4 files.**  Each is well under the band but is kept a **separate
  session by the one-line-commit-title corollary**: chorusmaster routing and IS_CLASSICAL conditionalisation are
  unrelated tag semantics with no shared contract; merging them yields an "and"-joined title (the tell of two sessions).
  Not fractured below the floor — each is already one irreducible unit.

## Session detail

### S1 — Delete the concerto-soloist path injection and its dead plumbing — freezes C-NOSOLO

**Deliverable.**  The concerto-soloist path injection and every piece of machinery that exists only to feed it are
removed; the path performers component is always `conductors → ensembles` (billing order over its occupied positions,
STYLEGUIDE 4.5) with no soloist branch.  Concretely:
- `_tags.py`: delete the injection block (`_tags.py:1174–1190`) and scrub the two docstring references to it
  (`build_dest_path` docstring ~1085–1091; the `_classical_top_dir` docstring tail at ~298 "including any
  concerto-soloist injection").
- `_pipeline.py`: delete the `run()` soloist-union pass (`~1069–1101`) that computes `cea_album_soloists_unified` — its
  sole consumer was the injection.
- `models.py`: remove the `cea_album_soloists_unified` field (`~1404`) and its `to_file_dict` exclusion entry (`~1512`).
- `_pipeline_maint.py`: update the `regroup` docstring (`~839–840`) that describes the unified-soloist path component;
  confirm no live read of the field remains (it is path-only, so removing the field forces the compiler/mypy to surface
  any residual reader — that is the intended safety net).
- Tests: retire `TestBuildDestPathConcertoSoloist` (`test_annotator.py:1838+`) and adjust the concerto integration
  assertions (`test_integration.py:1823/1854`) so the path no longer expects a soloist segment.

**KAT (the freeze witness for C-NOSOLO).**  A build_dest_path case over a Concerto work whose recording carries a
named soloist (e.g. Mutter) asserts the soloist name is **absent** from every path component — the inverse of the
retired KAT.  A second KAT over a multi-disc concerto with different soloists per disc asserts all movements still land
under the *same* top directory (the union pass's original purpose) purely from the conductor/ensemble component, proving
the deletion did not regress cross-medium grouping.

**Subtleties.**
- **C-S0 interaction (over-specify here — Category-A discipline):** the deleted union pass ran over `group_idxs`
  spanning all media (the C-S0 aggregation contract).  Removing it must not touch the *composer* cross-medium pass or
  `recording_date_work` pass that share that loop — only the soloist accumulation.  Freeze in C-NOSOLO that C-S0
  aggregation is unchanged.
- The field is excluded from `to_file_dict`, so no on-disk tag changes — but the *path* changes for every concerto
  already ingested; that is exactly what R6d will re-derive.
- mypy strict is the deletion's ally: remove the field and let the type checker enumerate every residual reference.

**Deferrals.**  No reorder (S2), no chorusmaster (S3), no IS_CLASSICAL (S4).  The `_works.py` `"Concerto"` worktype
mapping stays — it is still used for genre/worktype rendering; only the *soloist path injection* keyed on it goes.

### S2 — Reorder CEA recording-artist to billing order and realign composite naming (REND-14)

**Deliverable.**  Two coupled changes to the recording-artist composite:
1. **Reorder** (`_tags.py:749–757`): assemble `cea_recording_artist` (and `cea_recording_artists_sort`) in billing
   order **soloists → conductors → ensembles** (STYLEGUIDE 4.2), replacing the enacted soloists → ensembles →
   conductors.  The `rec_artist_phrase` verbatim fallback is retained (ratified).
2. **Naming realignment** (standing rule 2): the enacted composite carries *assembled* semantics under the CEA variable
   name whose CE meaning is the *verbatim* recording credit.  Resolve per the frozen ruling — **the realignment
   decision itself is the S2 juncture judgment** (see below): either rename the assembled composite to a
   non-colliding own-namespace name (and keep a verbatim-semantics tag under the CE-meaning name), or document the
   divergence explicitly in code + register.  Whichever the juncture rules, `_tagger.py`'s `_MP3_TXXX_MAP` /
   `_MP3_STD_KEYS` and `models.py`'s field + `to_file_dict` mapping move together with it.

**KAT (freeze witness for C-RA-GRAMMAR).**  A build_track_tags case with a soloist, a conductor, and an ensemble
asserts `CEA_RECORDING_ARTIST` (or its realigned name) renders exactly `soloist; conductor; ensemble` — and, if a
verbatim-semantics tag is introduced, a paired assertion that it renders the raw MB credit phrase.

**Subtleties.**
- **This is the standing-rule-2 hazard the juncture exists for.**  The realignment is a same-name-different-semantics
  judgment a test cannot fully adjudicate; the Opus juncture (or operator) must rule the target name/register before
  the row is implementable.  *This subsection over-specifies the interface only after that ruling; until then C-RA-
  GRAMMAR is "to be frozen at S2".*
- REND-15 (path order conductors-first) is **not** touched — the path already conforms to billing order over its
  occupied positions (conductors → ensembles; soloists excluded by C-NOSOLO).  S2 changes only the *tag* composite.
- Any tag-name change is a persisted-tag migration R6d will apply library-wide — that is why it lands pre-R6d.

**Deferrals.**  No chorusmaster (S3); no IS_CLASSICAL (S4).

### S3 — Route the chorusmaster credit into CONDUCTOR (REND-3/SEL-3)

**Deliverable.**  `CONDUCTOR` additionally carries the annotated chorusmaster credit in CE annotation form
"Name (choirmaster)" (STYLEGUIDE 4.4; REND-3 vocabulary; SEL-3 credit-routing half).  `_tags.py:891`/`714`/`895`: when
`cea.chorusmasters` is non-empty, append each as `"<name> (choirmaster)"` to `conductor_name` (after the conductors,
`"; "`-joined).  The standalone `CHORUSMASTER` tag (`_tags.py:895`) is **retained** — this is additive routing, not a
move (SEL-3's *position* ruling is untouched; only credit routing changes).

**KAT.**  A build_track_tags case with a conductor and a chorusmaster asserts `CONDUCTOR` renders
`Conductor Name; Chorusmaster Name (choirmaster)` and `CHORUSMASTER` still renders the bare chorusmaster name.

**Subtleties.**  Match the exact annotation string the CE census records (`"choirmaster"`, `census-ce.md:138`), not
`"chorusmaster"`.  The empty-chorusmasters branch must leave `CONDUCTOR` exactly as before (coverage: both arms tested).

**Deferrals.**  No IS_CLASSICAL (S4).

### S4 ◆ — Conditionalise IS_CLASSICAL on top-level class (REND-21)

**Deliverable.**  `IS_CLASSICAL` reflects the actual class instead of the hardcoded `"1"`.  `_tags.py:906`: set
`is_classical` from the `_top_level_class` result (`"1"` when the class is `Classical`, else `"0"`) rather than the
literal.  `models.py:1358`: keep the `"1"` default (CE convention for the classical-only fields default) but document
that `build_track_tags` now sets it explicitly.  STYLEGUIDE 4.7 / REND-21 authority.

**KAT.**  A build_track_tags case for a classical release asserts `IS_CLASSICAL == "1"`; a case that forces a
non-classical `_top_level_class` asserts `IS_CLASSICAL == "0"` (the branch REND-21 flagged as the latent bug).

**Subtleties.**
- Currently latent: `build_track_tags` is only reached for classical releases (non-classical uses the minimal-tags
  path).  The fix makes the flag *correct if that ever changes* and lets R6d re-derive without carrying a known-wrong
  flag.  Both branches must be covered even though one is presently unreached in the live pipeline — construct the
  non-classical case directly in the unit test.
- Confirm `_top_level_class` is importable/available at the `build_track_tags` call site without a layer-routing
  violation (tier/class routing is provenance-adjacent; keep the class *derivation* where C-CLASS put it).

**◆ boundary.**  Re-read Purpose.  Confirm all four rulings are enacted, `tox -m analyze` green, ledger complete.
Report to R6d planning (ROADMAP): the tag/path grammar now matches STYLEGUIDE v1; R6d re-derives already-corrected code.

## Cross-session contracts

### C-NOSOLO — no soloist enters the path component *(to be frozen at S1)*

The soloist is never a path component, however principal (STYLEGUIDE 4.5, SEL-11 overturned).  Also freezes the
negative: **no machinery computes a cross-medium soloist union for the path** (`cea_album_soloists_unified` and the
`run()` union pass are deleted), and **C-S0 aggregation is otherwise unchanged** (the composer and recording-date
cross-medium passes over `group_idxs` survive intact).  **Flavour: test-enforced** (the S1 KATs: soloist absent from
every path component; multi-disc concerto still groups under one top dir) **+ compiler-enforced** (mypy strict surfaces
any residual reader of the deleted field).  **Defined-in:** S1.  **Consumed-by:** S2 (relies on soloists being
path-absent so REND-15 needs no change), R6d re-derivation.  Over-specified per Category-A: carries the C-S0-unchanged
assertion even though no session immediately re-checks it.

### C-RA-GRAMMAR — recording-artist composite: order + name/semantics *(frozen at S2)*

**Order (REND-14).**  The assembled recording-artist composite renders performer principals in billing order
**soloists → conductors → ensembles** (STYLEGUIDE 4.2), replacing the enacted soloists → ensembles → conductors.  This
governs `cea_recording_artist` and `cea_recording_artists` (identical content) and their sort sibling
`cea_recording_artists_sort` (same three role classes in the same order, sort names).  The `rec_artist_phrase` /
`rec_artist_sort` verbatim MB-credit **fallback is retained** for the empty-composite case (ratified, REND-14).

**Name/register ruling (standing-rule-2 juncture judgment — Option "keep, no rename, no new tag").**  The assembled
composite stays under its CE name **`CEA_RECORDING_ARTIST`** (with `CEA_RECORDING_ARTISTS` / `CEA_RECORDING_ARTISTS_SORT`);
**no rename, no newly-introduced verbatim tag.**  Rationale, cited to the standing rules:
- The premise of the queued naming-drift note (STYLEGUIDE:657–660 — "assembled semantics under a name whose CE meaning
  is the verbatim recording credit") is imprecise against the CE variable register.  census-ce.md:655 defines
  `_cea_recording_artist` = "*Artist credited with the recording*" and census-ce.md:436–438 shows CE **itself assembles**
  it (a performer composite, not a verbatim credit).  CE's *verbatim* recording-credit variable is a **different** name,
  `_cea_MB_artists` = "Original track artists before any replacement/merge" (census-ce.md:657).
- Both CE meanings are already correctly realised in the enacted code: the verbatim credit lives under **`CEA_MB_ARTISTS`**
  (`_tags.py:759–760` `cea_mb_artists = rec_artist_phrase`; census-impl.md:298–299 states the two are distinct — composite
  vs. raw credit), and `ARTIST` also renders the MB credit verbatim (REND-1, STYLEGUIDE 4.3).  No verbatim tag needs to be
  introduced; `CEA_MB_ARTISTS` and `ARTIST` already carry it.
- Therefore keeping the assembled composite under `CEA_RECORDING_ARTIST` **conforms to standing rule 1** (the CE name keeps
  its established assembled meaning) and produces **no fragmentation** under standing rule 2.  The only genuine divergence
  from CE is the assembly *order* (REND-14) — an already-registered CE divergence (STYLEGUIDE:648–649) — which S2's reorder
  step discharges.  The queued "naming-drift remediation" is thereby **resolved as: no name change required**; verbatim
  semantics already live under `CEA_MB_ARTISTS`/`ARTIST`.

**Enacted deltas for S2 (interface, not implementation).**
- `_tags.py:749–757`: reorder the assembly to `all_soloists + cea.conductors + cea.ensembles` for both
  `cea_recording_artist`/`cea_recording_artists` and `cea_recording_artists_sort`; keep the `or rec_artist_phrase` /
  `or rec_artist_sort` fallback.  Add a one-line comment recording that `CEA_RECORDING_ARTIST` is CE's *assembled*
  composite and the verbatim credit is `CEA_MB_ARTISTS`/`ARTIST` (discharges the standing-rule-2 comment obligation).
- `_tagger.py` (`_MP3_TXXX_MAP`/`_MP3_STD_KEYS`) and `models.py` (field + `to_file_dict` mapping): **unchanged** — no
  tag-name change means no descriptor-map or field move.  (The PLAN's "move together with it" clause is satisfied vacuously
  because the ruling introduces no name change.)

**KAT (freeze witness).**  A `build_track_tags` case carrying one soloist, one conductor, and one ensemble asserts
`CEA_RECORDING_ARTIST` renders exactly `<soloist>; <conductor>; <ensemble>` (billing order) — and a **paired assertion**
that `CEA_MB_ARTISTS` renders the raw MB credit phrase (`rec_artist_phrase`), witnessing that the verbatim credit is
carried by the distinct existing tag.  A fallback KAT: with all three role classes empty, `CEA_RECORDING_ARTIST` renders
the `rec_artist_phrase` fallback.  Tests land in `tests/unit/test_pipeline.py` (no existing test asserts on these tags —
this is a genuinely new witness, authored, not rewired).

**Flavour:** test-enforced (the three KAT assertions above) **+ prose-enforced** (this name/register ruling, cited to
standing rules 1/2 and the CE variable register).  **Defined-in:** S2.  **Consumed-by:** S3 (chorusmaster routes into
`CONDUCTOR`, a *different* tag — must not disturb `CEA_RECORDING_ARTIST`), R6d (persisted-tag re-derivation — because the
name is unchanged, R6d re-derives the composite content only; **no library-wide tag rename migration is incurred**).

### Consumed (frozen upstream — invalidation is out of scope for this sub-track)

- **STYLEGUIDE v1 layer 4** (REND-1/2/3/14/15/21/24, 4.1–4.7) and **layer 1 SEL-11** — the rulings this sub-track
  enacts; frozen at V1b-S6.  Every row cites one; none re-opens one.
- **C-CLASS / C-INIT** (R4a; J2-ratified final) — S4 consumes `_top_level_class`; S1 consumes `_classical_top_dir`
  (scrubbing only its concerto-injection docstring tail).  Validate-only; no redefinition.
- **C-S0** (aggregation spans media) — S1 must preserve it while deleting the soloist union (see C-NOSOLO).
- **The confirmation-provenance and defensive-download invariants** (repo `AGENTS.md`) — untouched; none of these
  shards enters the copy/tag/verify loop's provenance chain.

### Produced

- **C-NOSOLO** at S1; **C-RA-GRAMMAR** at S2.  The corrected tag/path grammar is the sub-track's output to R6d planning.

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 1 | Delete the concerto-soloist path injection and its dead plumbing (SEL-11) | done | 6eaedaa | C-NOSOLO ✓ (extra: tests/unit/test_pipeline.py — retired test for deleted union pass) |
| 2 | Reorder CEA recording-artist to billing order and realign composite naming (REND-14) | done | 4d90566 | C-RA-GRAMMAR ✓ (extra: docs/STYLEGUIDE.md — CE-divergence register note updated to resolved) |
| 3 | Route the chorusmaster credit into CONDUCTOR (REND-3/SEL-3) | done | b553f65 | — |
| 4 ◆ | Conditionalise IS_CLASSICAL on top-level class (REND-21) | done | e0b9f54 | — |

## Action-frame digest

### S2 inflection — 2026-07-31
Discovery/flex: C-RA-GRAMMAR naming-realignment juncture resolved: keep assembled composite under CEA_RECORDING_ARTIST (no rename, no new verbatim tag); the standing-rule-2 premise was imprecise — CE's verbatim credit is already CEA_MB_ARTISTS.
Affected: C-RA-GRAMMAR (now fully specified; "to be frozen at S2" placeholder replaced)
Deferred: no — D-A2 resolved; STYLEGUIDE CE-divergence register note updated to "resolved: no rename" by S2 implementer (done).
Texture: No library-wide tag rename migration incurred at R6d — the survivor avoids the persisted-tag rename cost entirely.

### S4 ◆ boundary — 2026-07-31
Discovery/flex: Boundary juncture returned still-on-intent; all four STYLEGUIDE v1 rulings enacted and KAT-witnessed; sub-track closed on intent.
Affected: none — no contract drift.
Deferred: yes — census-impl.md and NOTES.md still reference the deleted cea_album_soloists_unified field (stale descriptive docs, out of sub-track write scope). Refresh before R6d consumes the census so R6d planning does not read a superseded concerto path rule.
Texture: Sub-track output to R6d: tag/path grammar now matches STYLEGUIDE v1; R6d re-derives against already-corrected code.

## Discoveries & risks

- **D-A1 (S1 blast radius — dead-code completeness).**  The concerto feature spans four source files; the risk is
  leaving orphan machinery that the 100%-branch-coverage gate then rejects.  Mitigation: delete the `models.py` field
  first and let mypy strict enumerate every reader (compiler-enforced completeness).  If a *live* (non-injection) reader
  of `cea_album_soloists_unified` surfaces in `_pipeline_maint`, that is an **additive-reshard** signal (the field was
  more load-bearing than the survey showed) — surface it, do not silently retain the field.
- **D-A2 (S2 naming-realignment — resolved: keep, no rename).**  The standing-rule-2 hazard (assembled semantics under
  CE's verbatim-credit variable name) resolved at the S2 inflection juncture (2026-07-31): the premise was imprecise —
  CE's `_cea_recording_artist` means the *assembled* composite (census-ce.md:655), not the verbatim credit; the verbatim
  credit is `_cea_MB_artists` → `CEA_MB_ARTISTS`, already correctly realised.  **Ruling: keep assembled composite under
  `CEA_RECORDING_ARTIST`, no rename, no new verbatim tag.**  No library-wide tag rename migration at R6d.  C-RA-GRAMMAR
  frozen.  STYLEGUIDE CE-divergence register note (lines 657–660) to be updated to "resolved: no rename" by S2 implementer.
- **D-A3 (sequencing vs. R6d — internal-continue).**  These four land ahead of R6d (J3/R5-gated) so the library
  re-derives once against corrected code (ROADMAP R6d fold-in, 2026-07-30).  They are logically independent of R6d's
  destructive repath — R6d re-derives with whatever the code then does.  No dependency inversion; safe to land now.
- **D-A4 (path changes without tag changes — S1).**  Deleting the injection changes concerto *paths* but no on-disk
  *tags* (the field was path-only, `to_file_dict`-excluded).  Already-ingested concertos will re-path under R6d; this is
  expected, not a regression.

## Notes for executors

- **Tier routing.**  S1, S2 are **Opus** (blast radius; the standing-rule-2 judgment).  S3, S4 are **Sonnet**
  (mechanical single-surface changes; the strong inner loop covers them).  `juncture-tier: opus` — kept at the arc
  default because two of four shards touch persisted-tag semantics R6d bakes library-wide (cost-of-wrong high despite
  the strong inner loop), and S2's naming decision is a judgment tests cannot fully catch.
- **Register: application, not authoring.**  No new editorial decisions.  Every change cites a frozen STYLEGUIDE v1
  ruling as its authority; if a row seems to *need* a new ruling, that is a discovery (surface it), not a licence to
  decide.
- **Invariants to preserve:** C-S0 aggregation (S1 deletes only the soloist union, not the composer/date passes);
  C-CASE append-only (register edits are cross-referencing adjudications); the confirmation-provenance and defensive-
  download invariants (untouched — none of these shards is in the copy/tag/verify loop); STYLEGUIDE self-containment
  (rulings cite principles, never working docs).
- **Every row runs `~/.local/bin/tox -m analyze` before ledger-done** (build + test at 100% branch coverage + strict
  mypy + ruff + pylint 10.00/10 + pyupgrade).  Import order via `~/.local/bin/tox -m edit`, never hand-edited.
- **Suggested first `/plan-run` invocation:** `halt-at-boundaries` — this is the first A-shard (code) sub-track derived
  from the styleguide arc; the shard pattern for "enact a frozen ruling" is unproven here, so stop at the S1 boundary
  (◆ is only S4, but halt-at-boundaries also stops after the first row of an unproven pattern) for an operator check
  before continuing S2–S4.  Once S1 confirms the pattern, `run-to-boundary` through the S4 ◆.
