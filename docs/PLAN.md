<!-- juncture-tier: opus -->
<!-- sub-track: path-canonical-name-forms (post-v1 styleguide application, node A) — render STYLEGUIDE 3.1/NORM-2
     canonical entity name-forms in the compact path projection (rule 4.5), sourced from MB's own primary-flagged
     aliases (D-A7/D-A8 authority-deference posture).  CODE-ONLY: new ingests render canonical; the destructive
     library-wide repath rides R6d's one pass (D-A5 precedent).  This IS a /plan-run target: model + fetch + tags
     + maintenance-path changes verifiable by the src/tests gate with zero library access. -->

# PLAN — path-canonical-name-forms: STYLEGUIDE 3.1/NORM-2 canonical name-forms in the destination path

## Purpose (design intent)

*(Re-read at every ◆ boundary — anti-defocus anchor.)*

STYLEGUIDE rule 3.1 (one canonical form per entity) and NORM-2 (native Latin-script name; established Latin
reception form for non-Latin script) require that **compact projections render one canonical name-form per
entity, selected once, not per release**.  Rule 4.5 makes the destination path "the compact assembled
projection" — so the path's performers component (conductors, ensembles) and composer component are exactly
where 3.1/NORM-2 apply.  A code audit (D-A7) found that **no canonical-form machinery exists anywhere in
`src/`**: every path name-form renders verbatim from `MBArtist.name` (as-credited display name) or
`last_name(MBArtist.sort_name)`, so the path can render "Vienna Philharmonic" where NORM-2 demands "Wiener
Philharmoniker".  This sub-track closes that gap.

The D-A6 "3.1-vs-REND-1 conflict" is **dissolved** (D-A7): 3.1/NORM-2 govern the *compact* projection (the
path); REND-1/4.3 govern `ARTIST`/`ALBUMARTIST` (*preserved/full* surfaces, verbatim by design).  They apply
to different surfaces; `ARTIST` stays verbatim, the path renders canonical.  No preserved surface changes.

**MB-authority-deference posture (D-A8 — the register anchor).**  The canonical form is **MB's own
primary-flagged alias** (native/reception form per NORM-2), falling back to `MBArtist.name`.  The *only*
editorial act is **selecting among MB's own asserted forms** — never a local editorial name table, never a
form MB does not hold, never a new annotation convention or scholarly romanisation.  Accept MB as the source
of authority even where fallible; modify it only as defensibly and plainly as possible.  This posture is the
sub-track's neutrality guarantee (it scales across users and time; a private name authority does not).

**Sequencing (D-A7, D-A5 precedent).**  This shard is **code-only**: it changes how *new* ingests render the
path, verified by the src/tests gate.  The destructive library-wide repath (renaming existing directories to
canonical forms) is **deferred to R6d's one-pass re-derivation** under the J3 gate — this sub-track never runs
a destructive library operation.  A temporary as-credited/canonical inconsistency in the on-disk library
(old dirs as-credited, new dirs canonical) is accepted until R6d.

The three sessions, in landing order:

1. **N1 @architect — Alias substrate + canonical-form resolver.**  Add artist alias data (fetch include or a
   dedicated alias fetch — the inflection ruling; see N1 detail) and `MBArtist.alias_list`, and a
   `canonical_artist_form(artist) -> str` resolver (prefer primary-flagged native/reception alias per NORM-2,
   else `MBArtist.name`).  Freezes **C-CANON**.
2. **N2 — Render canonical name-forms in the destination path.**  Call the resolver at the path performers /
   composer assembly sites so the compact path projection carries canonical forms.  Consumes C-CANON.
3. **N3 ◆ — Align the maintenance repath + register anneal.**  Make the `_pipeline_maint.py` repath/regroup
   path render canonical too (so R6d's future one-pass repath produces canonical dirs, not as-credited);
   close the sub-track; anneal the planning register.

## Verify gate

Discovered from `pyproject.toml` (tox envs); do not assume `make`.  Both are **binding** — this is a code
sub-track.  (Confirm green at shard time before N1.)

- **VERIFY_TEST**: `~/.local/bin/tox -e test` (`pytest tests/`; **100% branch coverage enforced**, `fail_under = 100`).
- **VERIFY_TYPES**: `~/.local/bin/tox -e check_type` (`mypy src/ tests/`, strict).
- Full gate before declaring any row done: `~/.local/bin/tox -m analyze` (build + test + check_type + check_format
  + check_lint 10.00/10 + check_upgrade).  The `AGENTS.md` "never skip `tox -m analyze`" rule applies to every row.

## Session list

| # | Session | Cat | Tier | Consumes | Expected files |
|---|---------|-----|------|----------|----------------|
| 1 @architect | Add artist alias-list and canonical-form resolver | A | Opus | STYLEGUIDE 3.1/NORM-2/NORM-3, D-A8 posture | `src/music_annotator/models.py`, `src/music_annotator/_mb_api.py`, `src/music_annotator/_artists.py`, `tests/unit/test_models.py`, `tests/unit/test_mb_helpers.py`, `tests/unit/test_annotator.py` |
| 2 | Render canonical name-forms in the destination path | B | Sonnet | **C-CANON**, STYLEGUIDE 4.5 | `src/music_annotator/_tags.py`, `tests/unit/test_pipeline.py`, `tests/unit/test_annotator.py` |
| 3 ◆ | Align the maintenance repath to canonical forms + anneal | I | Sonnet | **C-CANON** | `src/music_annotator/_pipeline_maint.py`, `tests/unit/test_pipeline.py` |

`Cat`: **N1 is A (substrate)** — it adds the alias data surface + the resolver contract that N2/N3 and every
future canonical-rendering site consume; over-specify the resolver (carry the primary/locale selection even if
N2 uses only the primary-alias branch first).  **N2 is B** — it threads the resolver through the compact-path
render sites; internally self-contained once C-CANON exists.  **N3 is I (integrative)** — it gives the
contract its operator-visible/durable form (the maintenance repath is the surface R6d will drive), closes the
◆, and carries the register anneal.
`Tier`: **N1 is Opus + `@architect` inflection.**  The alias-attachment mechanism is a genuine design-error-cost
point (lever 3): musicbrainzngs is known to attach `aliases` inconsistently to nested artist entities on a
release fetch (AGENTS.md "musicbrainzngs implementation" caveat — verify against the raw dict, not the REST
JSON), so the substrate row may need a dedicated `fetch_artist_aliases` under the defensive-download pattern
rather than an include flag.  That choice, plus the C-CANON resolver shape, is the inflection judgment.  **N2,
N3 are Sonnet** — mechanical resolver-threading over a frozen substrate, strong inner loop (lever 5: 100%
branch coverage + strict mypy).  `juncture-tier: opus` — kept: C-CANON is a durable resolver contract every
future name-rendering site consumes, and the alias-source mechanism is a judgment tests alone cannot catch.

**Sizing (levers named).**  Default band ~150–400 LOC / 2–4 files.

- **N1 ≈ 120–200 LOC, 3–4 files** (alias model reuse + fetch wiring + resolver + tests).  Within band.
  **Irreducible unit (lever 2, floor):** the alias data source, the `MBArtist.alias_list` field, and the
  resolver are one contract — a resolver with no alias data is a no-op; alias data with no resolver is unused.
  Kept whole.  One-line-commit-title check: "Add artist alias-list and canonical-form resolver" — passes.
- **N2 ≈ 60–120 LOC, 2–3 files** (call the resolver at the path performers + composer sites + tests).  Under
  the band; a **separate session by the one-line-commit-title corollary** — "render canonical name-forms in
  the path" is a distinct render-path surface with no shared implementation with N1's resolver definition.
  Splitting N1's "define the resolver" from N2's "call the resolver" is legitimate at the contract-sharp
  C-CANON boundary (N1 freezes the interface N2 consumes).  Not fractured below the floor.
- **N3 ≈ 40–80 LOC, 2 files** (maintenance-path resolver call + tests + anneal).  Under the band; a **separate
  session by the one-line-commit-title corollary** — the maintenance/repath surface (`_pipeline_maint.py`) is
  a distinct code path from the primary ingest path N2 touches; merging into N2 yields an "and"-joined title
  (render in ingest AND align the repath).  It is already one irreducible unit (the repath render site + its
  coverage + the anneal).

## Session detail

### N1 @architect — Add artist alias-list and canonical-form resolver — freezes C-CANON

**Deliverable.**  `MBArtist` gains alias data and the pipeline gains a canonical-form resolver:
- `models.py`: add `alias_list: list[MBAlias] = Field(default_factory=list, alias="alias-list")` to `MBArtist`
  (`~265`), **reusing the existing `MBAlias` model** (`~689`, already carries `name`/`locale`/`type`/`primary`
  and `populate_by_name`).  Update the `MBArtist` class docstring's attribute list.
- `_mb_api.py`: obtain artist aliases.  **Inflection ruling (see Subtleties):** either add `"aliases"` to the
  artist includes on the release/recording fetch *if verified to attach to nested artist entities*, or add a
  dedicated `fetch_artist_aliases(mbid)` following the two-layer defensive-download pattern (`@_mb_retry` +
  `_mb_call`, 4xx-permanent / 5xx-transient) with a `_WORK_CACHE`-style per-MBID cache.
- `_artists.py`: add `canonical_artist_form(artist: MBArtist) -> str` — return the **primary-flagged alias**
  whose form matches NORM-2 (native alias where the entity is Latin-script; established Latin-reception alias
  where non-Latin), falling back to `artist.name` when no qualifying primary alias exists.  Plain, total,
  MB-sourced (D-A8): no local table, no authored form.

**KAT (the freeze witness for C-CANON).**  In `test_annotator.py`, over `canonical_artist_form`:
(a) an artist with a primary-flagged native-Latin alias ("Wiener Philharmoniker") whose `name` is the
anglicised form ("Vienna Philharmonic") resolves to the **alias**; (b) an artist with no alias resolves to
`name` (fallback proof); (c) an artist with only a non-primary alias resolves to `name` (primary-only proof).
Plus a `test_models.py` `MBArtist().alias_list == []` default test, and (in `test_mb_helpers.py`) a fetch test
proving aliases populate `MBArtist.alias_list` from the raw MB dict (verify against the actual `mb.get_*`
key names, per the AGENTS.md musicbrainzngs caveat — not the REST JSON alone).

**Subtleties.**
- **The alias-attachment inflection (the `@architect` judgment).**  musicbrainzngs may not attach `aliases` to
  artists nested inside `artist-credits`/relations on a release fetch even with the include — a known
  library-vs-REST-JSON gap (AGENTS.md).  **Verify by printing the raw `mb.get_release_by_id` dict** before
  committing to the include path; if aliases do not attach, freeze the dedicated-fetch mechanism instead.
  This is the design-error-cost point the Opus tier + inflection marker exist for; the wrong choice here is
  costly to revise after C-CANON is consumed.
- **NORM-2 selection is MB-sourced only (D-A8).**  "Native where Latin-script; established reception where
  non-Latin" is realised by *reading MB's own primary-alias flags and locales*, not by authoring a form.
  Where MB holds no primary alias, fall back to `name` plainly — do not synthesise.
- **Over-specify per Category-A.**  Carry the locale/primary selection logic in the resolver even though N2's
  first consumer may only need the primary-alias branch — a downstream full-projection consumer (playlists,
  as-credited-variant surfaces) will want it, and adding it later is costlier (compiler-contract rigidity).
- **100%-branch-coverage gate.**  The resolver's primary-alias branch, the no-primary branch, and the
  no-alias fallback branch each need an explicit test; the fetch/parse path needs both populated and empty
  alias-list cases.

**Deferrals.**  No path rendering (N2); no maintenance-path change (N3).

### N2 — Render canonical name-forms in the destination path

*(Lower-fidelity sketch — correct for a post-substrate row; crisply specified after C-CANON freezes at N1.)*

**Deliverable.**  Thread `canonical_artist_form` into the compact-path render sites so the destination path
carries canonical entity forms (STYLEGUIDE 4.5):
- **Performers component** (`_tags.py:1224–1227`): replace `[e.name for e in tags.cea_album_conductors_list]`
  / `cea_album_ensembles_list` with the canonical form.  The `ArtistEntry` construction sites (`_tags.py:433`,
  `_works.py:254`) are the natural place to carry the canonical form alongside `name`/`sort` — freeze at N1
  detail whether the resolver is called at `ArtistEntry` construction (once, carried) or at path assembly
  (per-render); prefer construction so all downstream path/tag readers see one resolved form.
- **Composer component** (`_tags.py:587`): the path composer is `last_name(sort_name)`.  Per D-A8, the
  MB sort-name surname is already an MB-asserted, stable, recognisable form — **N2 leaves it as-is unless N1's
  ruling extends the resolver to composer name-forms**; if the operator wants composer canonicalisation, it
  routes through the same primary-alias resolver, not a new mechanism.  (Default: performers only; composer
  unchanged — surface as an N2 discovery if the composer path is found to violate NORM-2.)

**KAT (behavioural witness).**  A `build_dest_path`-level test over a release whose ensemble has a primary
native-Latin alias asserts the path performers component carries the **alias** form, not the anglicised
`name`; a release whose entities have no aliases asserts the path is **unchanged** from the pre-N2 as-credited
form (no-regression proof).  Preserved surfaces (`ARTIST`/`ALBUMARTIST`) asserted **unchanged** (the D-A7
surface split — the path canonicalises, the preserved tags do not).

**Subtleties.**
- **Preserved surfaces must not change (D-A7).**  `ARTIST`/`ALBUMARTIST` and the CE verbatim tags stay
  as-credited — N2 touches only the compact-path assembly, never the preserved-tag render.  A test asserting
  `ARTIST` unchanged guards the surface split.
- **Layer-routing.**  Canonicalisation is a *rendering* concern (layer 4) over the one model — the resolved
  form is a projection, not a mutation of the model's credit data (P1).  Keep the as-credited credit intact in
  the model; render canonical only at the compact-path surface.
- **match/case coverage.**  Instrumenting the render sites must not mint unreachable arms; cover both the
  alias-present and alias-absent path outcomes.

**Deferrals.**  No maintenance-path change (N3); no destructive repath (R6d).

### N3 ◆ — Align the maintenance repath to canonical forms + register anneal

*(Lower-fidelity sketch — post-substrate integrative row.)*

**Deliverable.**  Make the `_pipeline_maint.py` repath/regroup path render canonical forms too, so R6d's future
one-pass repath produces canonical directories (not as-credited):
- `_canonical_composer_component` (`_pipeline_maint.py:721–743`) and any performer-component derivation in the
  repath path route through the same C-CANON resolver / carried canonical form as the primary ingest path
  (N2), so ingest and repath render **identically**.  (This is consistency, not a new destructive op — the
  repath render function is aligned; R6d decides when to *run* it.)

**KAT.**  A repath-path test asserts the maintenance component renders the canonical (alias) form for an
entity with a primary native-Latin alias, matching the N2 ingest render byte-for-byte (ingest/repath parity).

**Subtleties.**  Mirror N2's surface split exactly — the repath aligns the *compact path* only; no preserved
surface and no persisted tag changes in the maintenance path.  Purely a render-alignment change; **no
destructive library operation is performed by this sub-track** (R6d runs the repath under J3).

**◆ boundary (register anneal).**  Re-read Purpose.  Confirm all three sessions enacted, `tox -m analyze`
green, ledger complete.  **Planning-register anneal** (the integrative session is where the contract gets its
public form — the anneal is the same act):
- Durable files (`models.py`, `_mb_api.py`, `_artists.py`, `_tags.py`, `_pipeline_maint.py`
  docstrings/comments) carry **no plan coordinates** — no "N1/N2/N3", no "path-canonical-name-forms
  sub-track", no `/plan-run` vocabulary.  State the property/reason/invariant (e.g. "canonical entity
  name-form from MB's primary-flagged alias per NORM-2/C-CANON"), never the plan coordinate.
- Grep the durable files against the **anneal denylist** (Notes for executors); translate any leaked
  coordinate into standalone prose.
- Report to the styleguide roadmap: rule-3.1/NORM-2 canonical-path rendering is enacted; C-CANON frozen.
  **R6d coordination noted** — the repath render is aligned; R6d runs the destructive library-wide repath
  under J3 (this sub-track lands the render, not the repath).

## Cross-session contracts

### C-CANON — canonical entity name-form resolution *(field + resolver FROZEN at N1)*

**Alias data + resolver (frozen at N1).**  `MBArtist.alias_list: list[MBAlias]` (default `[]`) carries the
entity's MB aliases; `canonical_artist_form(artist) -> str` returns the entity's **canonical name-form** per
STYLEGUIDE 3.1/NORM-2 — the **primary-flagged MB alias** matching the native/reception rule (native alias
where Latin-script; established Latin-reception alias where non-Latin), falling back to `MBArtist.name` when no
qualifying primary alias exists.  **Authority-deference invariant (D-A8): the resolved form is always a form
MB itself asserts** — a primary alias or the display name — never a locally-authored form, editorial table, or
new scholarly romanisation.  The resolver is total (never raises; always returns a non-empty string given a
populated `MBArtist`).  Deterministic: the same artist resolves to the same form regardless of release
(3.1 "selected once, not per release").

**Alias source mechanism (frozen at N1 inflection).**  *To be frozen at N1* — either the `"aliases"` include
on the existing artist fetches (if verified to attach to nested artist entities) or a dedicated
`fetch_artist_aliases(mbid)` under the two-layer defensive-download pattern with a per-MBID cache.  The N1
`@architect` inflection rules this against the raw `mb.get_*` dict, not the REST JSON.

**Surface scope (frozen at N1/N2).**  C-CANON applies to the **compact path projection only** (4.5) —
performers component, and composer only if N1 extends the resolver there.  It **never** applies to preserved
surfaces (`ARTIST`/`ALBUMARTIST`, CE verbatim tags — REND-1/4.3): those stay as-credited (the D-A7 surface
split).

**Flavour:** compiler-enforced (the `MBArtist.alias_list` Pydantic field + the resolver signature; mypy strict)
**+ test-enforced** (the N1 resolver KATs: primary-alias-wins, no-primary fallback, no-alias fallback; the N2
behavioural KATs: canonical in path, preserved tags unchanged; the N3 ingest/repath parity KAT) **+
prose-enforced** (the D-A8 authority-deference invariant; the D-A7 surface split, cited to 3.1/NORM-2/4.5 and
REND-1/4.3).  **Defined-in:** N1 (field + resolver + source mechanism).  **Consumed-by:** N2 (compact path
render), N3 (maintenance repath render), any future full-projection/playlist canonical-form consumer, R6d (the
one-pass repath renders canonical via the aligned N3 surface).  Over-specified per Category-A: carries the
locale/primary selection even though N2's first consumer uses only the primary-alias branch.

### Consumed (frozen upstream — invalidation is out of scope for this sub-track)

- **STYLEGUIDE v1 3.1 / 3.2 / NORM-2 / NORM-3 / 4.5** — the authority: one canonical form per entity (native/
  reception per NORM-2; aliases are evidence per NORM-3); compact projections render canonical (3.2); the path
  is the compact assembled projection (4.5).  No ruling is re-opened.
- **REND-1 / REND-19 / 4.3** — `ARTIST`/`ALBUMARTIST` are preserved verbatim claims.  C-CANON does **not**
  touch them (the D-A7 surface split).  Validate-only.
- **C-RA-GRAMMAR / C-NOSOLO** (A-shards) — the composite-tag grammar and no-soloist-in-path rules; the path
  performers component is conductors-then-ensembles with soloists excluded — N2 canonicalises the forms
  *within* that frozen structure, never changes which positions the path carries.  Validate-only.
- **Defensive-download invariant** (repo `AGENTS.md`) — any dedicated `fetch_artist_aliases` follows the
  two-layer `@_mb_retry` + `_mb_call` pattern, distinguishing 4xx-permanent from 5xx-transient.
- **"Path is a handle, not a manifest" / uniform-ceiling-ragged-floor** (C-CLASS/C-INIT inputs to 4.5) — the
  canonicalisation changes name *forms*, not path *structure*; the handle stays a handle.

### Produced

- **C-CANON** — alias field + resolver at N1; compact-path render at N2; maintenance-repath alignment at N3.
  **Coordinates with R6d** (the destructive library-wide repath): the render is landed here; R6d runs the
  repath under J3.  Distinct from the sidecar-case-ids shard, which had no R6d coupling.

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 1 @architect | Add artist alias-list and canonical-form resolver | pending | | |
| 2 | Render canonical name-forms in the destination path | pending | | |
| 3 ◆ | Align the maintenance repath to canonical forms + anneal | pending | | |

## Action-frame digest

*(none yet)*

## Discoveries & risks

- **D-1 (N1 alias-attachment mechanism — the inflection judgment; OPEN until N1).**  musicbrainzngs may not
  attach `aliases` to nested artist entities on a release fetch even with the include (a known library-vs-REST
  gap, AGENTS.md).  N1 must verify against the raw `mb.get_*` dict and freeze either the include path or a
  dedicated `fetch_artist_aliases`.  If a dedicated fetch is needed, N1 grows toward the top of its band (the
  defensive-download wiring + cache).  *Additive-reshard* only if the dedicated fetch proves large enough to
  warrant its own row — surface at the N1 boundary; do not silently absorb.
- **D-2 (composer path name-form — scope decision at N1/N2).**  The path composer is `last_name(sort_name)`
  (an MB-asserted, stable surname).  Default: N2 leaves it unchanged (already MB-sourced and recognisable per
  D-A8).  If N2 finds the composer surname violates NORM-2 for some entity (e.g. non-Latin composer rendered
  in a non-reception form), that is a discovery — route it through the *same* primary-alias resolver, never a
  new mechanism.  *internal-continue* unless a real violation surfaces.
- **D-3 (R6d coupling — sequencing constraint, not a risk to this sub-track).**  This shard changes persisted
  paths for *new* ingests only; the destructive library-wide repath is R6d's under J3 (D-A7/D-A5).  The N3
  maintenance-repath alignment is the surface R6d drives — landing it here is what lets R6d re-path once, not
  piecemeal.  No destructive op in this sub-track.  *internal-continue.*
- **D-4 (temporary library inconsistency — accepted, D-A7).**  Until R6d, the on-disk library mixes
  as-credited (old dirs) and canonical (new dirs) forms.  Accepted by the operator; not a defect to remediate
  in-track.  Noted so `/plan-run` does not treat it as an in-track discovery.
- **D-5 (stale census/NOTES `cea_album_soloists_unified` refs — pre-existing, out of scope).**  Carried down
  from prior boundaries and both roadmaps' R6d caveat: `census-impl.md` / `NOTES.md` still describe a deleted
  field.  A doc-freshness item for R6d, **not** this sub-track's work.  Noted so `/plan-run` does not treat it
  as an in-track discovery.

## Notes for executors

- **Tier routing.**  N1 is **Opus + `@architect` inflection** (the alias-source-mechanism design judgment; the
  durable C-CANON resolver freeze).  N2, N3 are **Sonnet** (mechanical resolver-threading over the frozen
  substrate).  `juncture-tier: opus` — kept: C-CANON is durable and the alias mechanism is a judgment tests
  alone cannot catch.
- **Register: authority-deference, not authoring (D-A8).**  The canonical form is always a form MB itself
  asserts (a primary alias or the display name).  No local editorial name table, no synthesised form, no new
  scholarly romanisation, no new annotation convention.  If a row seems to *need* an authored form, that is a
  discovery (surface it), not a licence to author.
- **Surface split is load-bearing (D-A7).**  C-CANON touches the **compact path only**.  `ARTIST`/
  `ALBUMARTIST` and CE verbatim tags stay as-credited (REND-1/4.3).  Every render-site change must carry a
  test asserting the preserved surfaces are unchanged.
- **REGISTER rule (durable-file discipline).**  In source/tests, state the *property/reason/invariant* — never
  the plan coordinate.  "canonical entity name-form from MB's primary-flagged alias per NORM-2/C-CANON" is
  right; "the N2 path-canonicalisation" is not.  Plan vocabulary (N1/N2/N3, sub-track names, `/plan-run`)
  lives only in `PLAN.md` / `ROADMAP*.md` / the ledger / commit messages.  See also the repo `AGENTS.md`
  REGISTER block.
- **Anneal denylist (◆ gate greps durable files for these).**  Seeded from the `/plan-run` default, tuned for
  this project's vocabulary:
  - `\bN[1-9]\b` (this sub-track's plan session coordinates) **and** `\bS[1-9]\b` (prior sub-tracks') — **but**
    allow the STYLEGUIDE-rule-section forms (`\b[1-5]\.[0-9]\b` like "3.1", "4.5", "5.2" are register/rule
    cites, not plan coordinates — do **not** flag).
  - `sub-track`, `plan-run`, `plan-shard`, `halt-at-boundaries`, `run-to-boundary`
  - `C-CANON` **only outside docstrings that legitimately name the contract** — contract names in docstrings
    are the intended durable form (the C-TIER/C-CASE-PROV precedent); flag bare "N1 freeze"-style prose, not
    the contract name itself.
  - `juncture`, `inflection`, `action-frame`, `◆`
  - Do **not** add `alias`, `canonical`, `NORM-2`, `primary`, or register IDs (`NORM-`, `REND-`, `SEL-`) to
    the denylist — these are legitimate domain vocabulary this sub-track deliberately renders and cites.
- **Invariants to preserve:** the D-A7 surface split (preserved tags unchanged); C-RA-GRAMMAR / C-NOSOLO (the
  path carries conductors-then-ensembles, soloists excluded — canonicalise forms within that structure, never
  change which positions the path carries); the defensive-download pattern (any `fetch_artist_aliases` follows
  `@_mb_retry` + `_mb_call`); "path is a handle, not a manifest" (change name forms, not path structure); the
  confirmation-provenance and copy/verify invariants (untouched — this sub-track is not in the copy/verify
  network path).
- **Every row runs `~/.local/bin/tox -m analyze` before ledger-done** (build + test at 100% branch coverage +
  strict mypy + ruff + pylint 10.00/10 + pyupgrade).  Import order via `~/.local/bin/tox -m edit`, never
  hand-edited.
- **Suggested first `/plan-run` invocation:** `halt-at-boundaries` — the alias-source-mechanism (D-1) is the
  first unproven substrate judgment in this shard; stop after N1 for an operator check that the C-CANON freeze
  (especially the alias-attachment mechanism and the primary-alias selection rule) is right before N2 consumes
  it.  Once N1 confirms the pattern, `run-to-boundary` through the N3 ◆.
