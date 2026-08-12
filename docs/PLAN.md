<!-- juncture-tier: opus -->
<!-- sub-track: R6a (hierarchy-depth normalisation) — library-completion arc (docs/ROADMAP.md), Act III-a.  Render the
     frozen uniform-ceiling/ragged-floor depth rule (STYLEGUIDE 4.5; NOTES "two durable rules"; C-W3b, graduated from
     provisional at J2) in build_dest_path so a work-group's over-resolved branches clamp to the group's modal depth.
     CODE-ONLY: new ingests render clamped; the destructive library-wide repath rides R6d's one J3-gated pass (D-A5/D-A7
     precedent).  This IS a /plan-run target: build_dest_path + caller-threading + tests, verifiable by the src/tests
     gate; the fresh scan_nonuniform_depth.py run is the substrate-session gating step (operator mounts the library). -->

# PLAN — R6a: uniform-ceiling / ragged-floor hierarchy-depth normalisation in the destination path

## Purpose (design intent)

*(Re-read at every ◆ boundary — anti-defocus anchor.)*

STYLEGUIDE 4.5 and the NOTES "two durable rules" fix the path-depth policy: **uniform ceiling, ragged floor** —
render each leaf at `min(its own tree depth, the work-group's modal tree depth)`.  Over-resolved branches **clamp
down** (removing structure the path does not need — faithful); shallow branches are **never padded up** (inventing
structure that is not there — unfaithful).  J2 (2026-07-30) graduated **C-W3b** from provisional: the rule, the
two-sub-shape routing, and the corner pins (modal ties → shallower; PL=0 orphans excluded) are frozen.  J2
explicitly left the **`build_dest_path` interface mechanics (the `depth_clamp` posture) and the tag-data-sufficiency
question** to "R6a PLAN derivation" — this shard.

A code audit (survey 2026-08-12) confirmed **no depth-clamp machinery exists**: `build_dest_path` (`_tags.py:1089`)
reads `CWP_PART_LEVELS` per-track (`:1333`) and emits one intermediate directory per level when `part_levels >= 2`
(`:1387–1417`), with **no group context and no clamp**.  Depth is set upstream in `build_cwp_tags`
(`cwp.part_levels = len(work_hierarchy) - 1`, `_tags.py:498`).  So a work-group whose modal depth is 2 but whose
Handel "Water Music" movement III carries sub-parts IIIa/IIIb renders that one movement at PL=3 — the exact "ragged
depth" the rule targets (Shapes C/D of the census).

**Scope is narrow and the census small (Shapes C/D = 3 groups at L2-design time).**  This is not a large migration;
it is a permanent-policy interface decision over a small population (BACKLOG:279–281: "changes the path output for
~3% of the library and becomes the permanent policy for all future `run()`").

**Interface posture (resolved at this PLAN derivation — the S1 inflection judgments):**

1. **Direct parameter, not `model_extra`.**  `build_dest_path` receives the work-group's modal depth as an explicit
   typed parameter; callers (`run`/`repath`/`regroup`) compute it from the group and pass it.  Chosen over the
   BACKLOG:315 `cwp_render_levels`-as-`model_extra` sketch because `model_extra` access is loosely typed (tension
   with the repo's strict-mypy / no-`Any` rule) and couples the depth decision into the tag-model lifecycle; a
   direct parameter keeps the data flow explicit and unit-testable, and matches the BACKLOG:302 "callers pass the
   group context" clause.  **Tradeoff:** every caller must now assemble the work-group and compute the modal depth
   (more caller-side wiring) — worse on caller simplicity than a self-contained `model_extra` read, accepted as the
   price of typed, explicit depth flow.
2. **Default already-modal (clamp on by default), not default-`None`.**  Since new-ingest rendering *is* the
   deliverable, the clamp is **on** for `run()` immediately.  BACKLOG:300's "default `None` until repath completes"
   was written pre-J2 when repath and ingest were coupled; with the D-A5 code-only split, new ingests render clamped
   now and the existing library re-derives when `repath` runs (R6d's one J3-gated pass).  **Matches the
   canonical-name-forms precedent** (new ingests render canonical now; repath rides R6d).  **Tradeoff:** existing
   `Done/` dirs are temporarily non-conformant for the Shapes-C/D groups until R6d — the accepted D-A4-style
   inconsistency — worse on immediate library uniformity than an in-shard repath, accepted to keep this shard off
   the J3 gate and inside the fast src/tests inner loop.

**Sequencing (D-A5/D-A7 precedent).**  Code-only: `build_dest_path` renders clamped depth for new ingests, verified
by the src/tests gate.  The destructive library-wide depth-repath is the existing offline `repath` engine
(`_pipeline_maint.py:405` — recomputes every path from `build_dest_path`, no network), run once under R6d's J3
gate.  This shard lands the render; R6d runs the repath.  No destructive library operation in R6a.

The three sessions, in landing order:

1. **S1 @architect — Modal-depth substrate + clamped `build_dest_path` interface.**  Add a work-group modal-depth
   helper (frozen corner pins: modal ties → shallower; PL=0 orphans excluded) and the group-modal-depth parameter
   on `build_dest_path`, clamping `part_levels` to `min(own, modal)` at the depth branch.  Freezes **C-W3b-INT**.
2. **S2 — Thread the modal depth through the render callers.**  Compute the work-group modal depth in `run`,
   `repath`, `regroup` and pass it to `build_dest_path` so ingest and maintenance render identically.  Consumes
   C-W3b-INT.
3. **S3 ◆ — Fresh scan gate + census refresh + register anneal.**  Re-run `scan_nonuniform_depth.py` against the
   complete library (the gating step; distinguish scan-not-run from no-findings), validate the six-shape taxonomy
   against the fresh scan (a new mishandled shape = J2 reopen trigger), refresh the stale 36-group census; close
   the sub-track; anneal the planning register.

## Verify gate

Discovered from `pyproject.toml` (tox envs); do not assume `make`.  Both **binding** — this is a code sub-track.
(Confirm green at shard time before S1.)

- **VERIFY_TEST**: `~/.local/bin/tox -e test` (`pytest tests/`; **100% branch coverage enforced**, `fail_under = 100`).
- **VERIFY_TYPES**: `~/.local/bin/tox -e check_type` (`mypy src/ tests/`, strict).
- Full gate before any row is declared done: `~/.local/bin/tox -m analyze` (build + test + check_type + check_format
  + check_lint 10.00/10 + check_upgrade).  The AGENTS.md "never skip `tox -m analyze`" rule applies to every row.
- **S3 scan step is not gate-covered:** `scripts/scan_nonuniform_depth.py` lives outside `src/`+`tests/` (like
  `scan_fragmentation.py` / `census_original.py`); it runs clean under `venv/bin/python -m py_compile` and
  best-effort `venv/bin/mypy scripts/` but is not `tox`-enforced.  Its gating role is producing a fresh scan the S3
  ◆ review consumes, not passing the gate.

## Session list

| # | Session | Cat | Tier | Consumes | Expected files |
|---|---------|-----|------|----------|----------------|
| 1 @architect | Add work-group modal-depth clamp to build_dest_path | A | Opus | C-W3b (rule + corner pins), STYLEGUIDE 4.5, NOTES two-rules | `src/music_annotator/_tags.py`, `src/music_annotator/_works.py`, `tests/unit/test_annotator.py` |
| 2 | Thread work-group modal depth through the render callers | B | Sonnet | **C-W3b-INT** | `src/music_annotator/_pipeline.py`, `src/music_annotator/_pipeline_maint.py`, `tests/unit/test_pipeline.py`, `tests/unit/test_pipeline_maint.py` |
| 3 ◆ | Refresh the depth census + anneal | I | Sonnet | **C-W3b-INT**, `scan_nonuniform_depth.py` | `scripts/scan_nonuniform_depth.py`, `docs/BACKLOG.md`, `tests/unit/test_annotator.py` |

`Cat`: **S1 is A (substrate)** — freezes **C-W3b-INT**, the modal-depth helper + `build_dest_path` clamp interface
that S2's caller-threading and every future clamped render consume; over-specify (carry the group-context parameter
and the corner-pin logic even though S2 is the first consumer).  **S2 is B** — mechanical threading of the frozen
interface through the three callers; self-contained once C-W3b-INT exists.  **S3 is I (integrative)** — the
fresh-scan gate + taxonomy validation + census refresh give the contract its operator-visible/durable form (the
fresh scan is what R6d's repath will re-derive against), close the ◆, carry the anneal.

`Tier`: **S1 is Opus + `@architect` inflection.**  BACKLOG:279 names it "an architectural boundary decision …
permanent policy for all future `run()`"; the interface posture (resolved above) and the **tag-data-sufficiency
question** — can `CWP_PART_LEVELS` + the group modal depth distinguish faithful-over-resolution (clamp) from a
data-gap shallowness (preserve) *without* a MB network call — is the S1 judgment tests alone cannot catch (lever 3:
design-error cost).  **S2, S3 are Sonnet** — mechanical over a frozen interface with a strong inner loop (lever 5:
100% branch coverage + strict mypy).  `juncture-tier: opus` — kept (arc default; C-W3b-INT is durable permanent path
policy).

**Sizing (levers named).**  Default band ~150–400 LOC / 2–4 files.

- **S1 ≈ 150–250 LOC, 3 files** (modal-depth helper + corner-pin logic + `build_dest_path` parameter + clamp +
  tests).  Within band.  **Irreducible unit (lever 2, floor):** the modal-depth computation, the corner pins, and
  the clamp interface are one contract — a modal helper with no interface to consume it is dead code; the interface
  with no modal source is a no-op.  Kept whole.  **Lever 3 (design-error cost ↑):** high cost-of-wrong is *why* S1
  is Opus+inflection, not why it fractures.  One-line title: "Add work-group modal-depth clamp to build_dest_path"
  — passes.
- **S2 ≈ 80–150 LOC, 4 files** (compute modal depth in `run`/`repath`/`regroup` + pass it + tests).  **Separate
  session by the one-line-commit-title corollary** — "thread the modal depth through the callers" is distinct from
  "define the clamp"; split legitimately at the contract-sharp C-W3b-INT boundary (S1 freezes the interface S2
  consumes).  **Lever 1 (ambient complexity):** the three-caller threading (each caller assembles the work-group)
  is the real work; not fractured below the floor.
- **S3 ≈ 40–100 LOC + scan run, 3 files** (scan-run gate + taxonomy-validation note + census refresh + a
  no-regression parity test + anneal).  Under band; **separate by the corollary** — the fresh-scan/census/anneal is
  one integrative unit; merging into S2 yields an "and"-joined title.  Not fractured below the floor (the scan
  *validates* the taxonomy the anneal reports).

## Session detail

### S1 @architect — Add work-group modal-depth clamp to build_dest_path — freezes C-W3b-INT

**Deliverable.**  `build_dest_path` clamps per-track depth to the work-group modal depth:
- `_works.py` (or `_tags.py` near `build_cwp_tags`): add `work_group_modal_depth(part_levels_list: list[int]) ->
  int` — the modal `CWP_PART_LEVELS` over a work-group's tracks, with the **frozen corner pins**: on a modal tie,
  choose the **shallower** depth; **exclude PL=0 orphans** (Shape E) from the modal computation.  Total, pure, no
  I/O.
- `_tags.py`: `build_dest_path` gains a typed parameter carrying the work-group modal depth (name at implementer
  discretion, e.g. `group_modal_depth: int | None = None`).  At the depth branch (`:1333`, `:1387`), the effective
  level count becomes `min(part_levels, group_modal_depth)` when the parameter is supplied.  **Posture: default
  already-modal** for `run()` — but the *parameter default* is `None` = "no group context, render own depth" (so a
  caller that genuinely has no group, e.g. a single-file diagnostic, still works); the clamp engages whenever a
  caller passes the modal depth, which S2 makes `run()`/`repath()`/`regroup()` always do.  (This reconciles "default
  already-modal" behaviour with a safe parameter default: the *pipeline* default is modal because the callers always
  pass it; the *function* tolerates absence.)
- Update the `build_dest_path` and helper docstrings to state the property (uniform-ceiling/ragged-floor clamp to
  the work-group modal depth), never the plan coordinate.

**KAT (the freeze witness for C-W3b-INT).**  In `test_annotator.py`, over `build_dest_path` + the modal helper:
(a) **clamp-down** — a work-group with modal depth 2 and one PL=3 over-resolved movement (Handel Water Music
IIIa/IIIb shape) renders that movement's path at **2 levels** (the over-resolution removed), not 3;
(b) **ragged-floor preserved** — a work-group with modal depth 2 and one genuinely-shallow PL=1 node (Shape A
overture-among-acts) renders the shallow node **unchanged at 1 level** (never padded up);
(c) **modal-tie → shallower** — a group split evenly (e.g. {2,2,3,3}) resolves the tie to the **shallower** modal;
(d) **PL=0 orphan excluded** — a group with a PL=0 orphan (Shape E) computes the modal over the non-orphan tracks;
(e) **no-group / parameter-absent** — `build_dest_path(..., group_modal_depth=None)` renders own depth (backward
compatibility / no-regression proof).

**Subtleties.**
- **The tag-data-sufficiency inflection (the `@architect` judgment).**  J2 left open whether `CWP_PART_LEVELS` + the
  group modal depth can distinguish **faithful over-resolution** (clamp) from a **data-quality-gap shallowness**
  (preserve + surface upstream) *without a MB network call*.  Ruling to make and freeze at S1: the clamp is **purely
  a down-projection** — it only ever *reduces* a leaf's depth to the modal, and **never pads up**, so the data-gap
  case (a node shallower than modal) is **automatically left untouched** by a `min()` clamp.  Therefore the
  min-clamp needs **no** gap-vs-faithful discrimination and **no** network call: the asymmetry of the rule (clamp
  down only) makes the distinction moot for *rendering*.  (The upstream data-gap *surfacing* — flagging the missing
  `part-of` link — is a separate MB-upstream concern, Shape E, explicitly out of scope here.)  Confirm this
  reasoning against the census shapes before freezing; if a shape is found where min-clamp mis-renders, that is the
  reopen trigger J2 named.
- **Over-specify per Category-A.**  Carry the corner-pin logic (tie → shallower, PL=0 exclusion) and the group
  parameter now even though S2 is the first consumer and the census population is 3 groups — a future
  full-projection/audit consumer or a new shape will want them, and adding them later is costlier (compiler-contract
  rigidity).
- **100%-branch-coverage gate.**  The clamp branch, the parameter-absent branch, the modal-tie branch, and the
  PL=0-exclusion branch each need an explicit test; any `match/case` over shape needs a `case _: # pragma: no cover`
  arm if the union is exhaustive.
- **"Path is a handle, not a manifest."**  The clamp changes *depth* (how many nested dirs), never *which* entities
  the path carries — the handle stays a handle.

**Deferrals.**  No caller threading (S2); no fresh scan / census refresh (S3); no destructive repath (R6d).

### S2 — Thread work-group modal depth through the render callers

*(Lower-fidelity sketch — correct for a post-substrate row; crisply specified after C-W3b-INT freezes at S1.)*

**Deliverable.**  Compute the work-group modal depth at each `build_dest_path` caller and pass it, so ingest and
maintenance render identically:
- `_pipeline.py` `run()` (the work-group loop): assemble each work-group's `CWP_PART_LEVELS` set, compute the modal
  via the S1 helper, pass it to `build_dest_path`.
- `_pipeline_maint.py` `repath()` (`:405`) and `regroup()` (`:620`): same, from the embedded-tag work-group each
  builds offline.  This is the site R6d's one-pass repath drives — after S2, `repath` re-derives existing paths at
  clamped depth.
- Freeze at S1 detail whether the modal depth is computed once per work-group and shared (preferred — one
  computation per group) or per-track (redundant); prefer per-group.

**KAT (behavioural witness).**  A `run()`-level (or `repath()`-level) test over a work-group with a Shape-C/D
over-resolved movement asserts the emitted path clamps to the modal depth; a uniform-depth group asserts the path
is **unchanged** from pre-S2 (no-regression).  A `repath`-vs-`run` parity assertion (same group → byte-identical
path) guards ingest/maintenance identity.

**Subtleties.**
- **Ingest/maintenance parity is the point.**  `run` and `repath`/`regroup` must compute the modal depth the same
  way or the library repath (R6d) would diverge from new ingests.  A parity test guards it.
- **Group assembly off embedded tags.**  `repath`/`regroup` have no `MBRelease` (they call `build_dest_path` with
  empty `MBRelease()`); the work-group must be assembled from `CWP_WORKID_TOP` on the embedded tags exactly as
  `scan_nonuniform_depth.py` groups.  Reuse that grouping definition; do not mint a second one.
- **match/case coverage.**  Cover both clamp-engaged and clamp-noop caller outcomes.

**Deferrals.**  No fresh scan / census (S3); no destructive repath (R6d).

### S3 ◆ — Refresh the depth census + register anneal

*(Lower-fidelity sketch — post-substrate integrative row.)*

**Deliverable.**  Validate the frozen taxonomy against a fresh scan and refresh the stale census:
- Re-run `scripts/scan_nonuniform_depth.py` against the **complete library** (operator mounts it — confirmed
  2026-08-12).  **Distinguish scan-not-run (unmounted/empty root → never report clean) from no-findings** (the R4b
  D-1 hazard); if the library is unmounted at execution, S3 records the scan as *not run* and the ◆ review notes the
  census refresh as pending, rather than asserting the taxonomy holds.
- **Validate the six-shape taxonomy** (A/B/C/D/E/F, BACKLOG:340–347) against the fresh scan.  A new shape the
  uniform-ceiling min-clamp mishandles is the **J2 reopen trigger** (surface as a discovery; do not silently
  absorb).  If the taxonomy holds, refresh the "36-group / 3.6%" figures in `docs/BACKLOG.md` to the current
  library.
- Optionally have the scanner emit a small machine-readable artifact (JSON) if that eases the R6d prerequisite;
  implementer judgment, not a freeze.

**KAT.**  A no-regression parity test asserting the S1/S2 clamp behaviour still holds against a representative
Shape-C/D fixture (belt-and-suspenders over the S1/S2 KATs; the integrative session's behavioural pin).

**Subtleties.**  No `src/` change in S3 unless a scanner helper is promoted (it should not be — keep the scanner
standalone per the `scan_fragmentation.py`/`census_original.py` precedent).  Purely a render-validation + census +
anneal row; **no destructive library operation** (R6d runs the repath under J3).

**◆ boundary (register anneal).**  Re-read Purpose.  Confirm all three sessions enacted, `tox -m analyze` green,
ledger complete.  **Planning-register anneal** (the integrative session is where the contract gets its public form —
the anneal is the same act):
- Durable files (`_tags.py`, `_works.py`, `_pipeline.py`, `_pipeline_maint.py`, `scan_nonuniform_depth.py`
  docstrings/comments) carry **no plan coordinates** — no "S1/S2/S3", no "R6a", no "path-depth-normalisation
  sub-track", no `/plan-run` vocabulary.  State the property/reason/invariant (e.g. "clamp leaf depth to the
  work-group modal depth per the uniform-ceiling/ragged-floor rule, STYLEGUIDE 4.5 / C-W3b"), never the plan
  coordinate.
- Grep the durable files against the **anneal denylist** (Notes for executors); translate any leaked coordinate into
  standalone prose.
- Report to the library-completion roadmap: the depth-clamp render is enacted; C-W3b-INT frozen.  **R6d coordination
  noted** — the clamp render is aligned in `repath`/`regroup`; R6d runs the destructive library-wide depth-repath
  under J3 (this sub-track lands the render, not the repath), as one part of its paths-only one-pass (see the
  ROADMAP R6d tag-content-scope caveat).

## Cross-session contracts

### C-W3b-INT — the build_dest_path depth-clamp interface *(FROZEN at S1)*

**Modal-depth helper + clamp interface (frozen at S1).**  `work_group_modal_depth(part_levels_list) -> int` returns
the work-group's modal `CWP_PART_LEVELS` with the frozen corner pins (modal tie → shallower; PL=0 orphans excluded).
`build_dest_path` gains a typed group-modal-depth parameter; the effective per-leaf level count is `min(own
part_levels, group_modal_depth)` when supplied, else own depth (parameter default `None` = own depth).  **Rule
invariant (C-W3b, J2-frozen): clamp down only, never pad up** — the `min()` makes this structural, so no
gap-vs-faithful discrimination and **no MB network call** is needed at render (the tag-data-sufficiency question,
resolved: the rule's asymmetry moots it for rendering; upstream data-gap surfacing is a separate MB-upstream concern,
out of scope).  The helper is total (never raises; returns a non-negative int).  Deterministic: the same work-group
resolves to the same modal depth regardless of release.

**Posture (frozen at S1).**  *Default already-modal* at the pipeline level (callers always pass the modal depth, so
new ingests clamp immediately) reconciled with a *safe function default* (`None` → own depth, so a group-less caller
still renders).  Chosen carrier: **direct typed parameter**, not `cwp_render_levels` `model_extra`
(strict-mypy/no-`Any` house rule; explicit data flow).  The existing library re-derives via the offline `repath`
engine (`_pipeline_maint.py:405`) — R6d's one J3-gated pass; temporary Shapes-C/D non-conformance until then
(D-A4-style, accepted).

**Flavour:** compiler-enforced (the `build_dest_path` parameter + the helper signature; mypy strict) **+
test-enforced** (the S1 KATs: clamp-down, ragged-floor-preserved, modal-tie, PL=0-exclusion, parameter-absent; the
S2 parity/no-regression KATs) **+ prose-enforced** (the uniform-ceiling/ragged-floor rule and clamp-down-only
invariant, cited to STYLEGUIDE 4.5 / NOTES two-rules / C-W3b).  **Defined-in:** S1.  **Consumed-by:** S2 (caller
threading), S3 (validation), R6d (the one-pass `repath` renders clamped via the S2-aligned callers), any future
audit/full-projection depth consumer.  Over-specified per Category-A: carries the corner pins and group parameter
though S2 is the first consumer over a 3-group population.

### Consumed (frozen upstream — invalidation is out of scope for this sub-track)

- **C-W3b (J2-graduated)** — the uniform-ceiling/ragged-floor *rule* + two-sub-shape routing + corner pins.  R6a
  freezes its *interface* (C-W3b-INT); it does **not** re-open the rule.  A shape the min-clamp mishandles is a
  finding for the arc boundary (J2 reopen trigger), not an in-arc rule change.
- **C-CLASS / C-INIT (J2-ratified)** — the top-level class scheme and within-classical first component.  The clamp
  changes depth *below* `work_dir`, never the class/top_dir structure.  Validate-only.
- **C-L0 / C-L1** — leaf/intermediate numbering.  The clamp changes *how many* intermediate dirs render, never their
  numbering grammar.  Validate-only.
- **C-PROV / C-MOVE** — move/verify/journal provenance.  The `repath`/`regroup` threading (S2) preserves the journal
  provenance chain unchanged (the clamp only changes the computed destination, not the copy/verify/journal
  ordering).  Validate-only.
- **"Path is a handle, not a manifest"** — the clamp changes name *depth*, not path *identity*; the handle stays a
  handle.

### Produced

- **C-W3b-INT** — the depth-clamp interface at S1; caller threading at S2; census validation at S3.  **Coordinates
  with R6d** (the destructive library-wide depth-repath): the render is landed here; R6d runs the `repath` one-pass
  under J3.  Distinct from the canonical-name-forms shard's R6d coupling only in the surface it aligns (depth vs.
  name-form) — both ride the same R6d `repath` pass.

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 1 @architect | Add work-group modal-depth clamp to build_dest_path | done | 4fe4025 | C-W3b-INT (work_group_modal_depth helper + build_dest_path group_modal_depth param; corner pins: tie→shallower, PL=0 excluded; all-orphan→0; min() clamp down-only) |
| 2 | Thread work-group modal depth through the render callers | pending | | |
| 3 ◆ | Refresh the depth census + anneal | pending | | |

## Action-frame digest

### S1 — 2026-08-12
Discovery/flex: Inflection design-confident; D-1 tag-data-sufficiency dissolved (min() asymmetry moots gap-vs-faithful discrimination; no MB call needed). Shape F (2-track even-split tie) flagged as aggressive-but-rule-licensed; pre-classified "acceptable" in BACKLOG. All-orphan edge pinned (returns 0).
Affected: C-W3b-INT (frozen as designed; no contract flex)
Deferred: no — all three advisory notes (Shape F, determinism phrasing, all-orphan edge) resolved in S1 implementation. S2 must reuse scanner grouping and include repath-vs-run parity KAT.
Texture: design-confident verdict; self-continued. Extra file __init__.py not needed (work_group_modal_depth is internal, not added to __all__).

## Discoveries & risks

- **D-1 (S1 tag-data-sufficiency — the inflection judgment; provisionally resolved at PLAN derivation).**  J2 left
  open whether the clamp can distinguish faithful-over-resolution from a data-gap without a MB call.  Resolution to
  confirm-and-freeze at S1: the rule is **clamp-down-only**, so a `min()` to the modal depth **automatically** leaves
  data-gap-shallow nodes untouched and needs no discrimination and no network call.  *internal-continue* unless a
  census shape is found where min-clamp mis-renders — that is a **destructive-HALT / J2-reopen** signal (the rule,
  not just the interface, would be wrong).
- **D-2 (fresh-scan population — additive-reshard signal).**  The 36-group / 3.6% census is stale by construction
  (BACKLOG:337).  If the fresh S3 scan surfaces a **new shape** the uniform-ceiling rule mishandles, or a much
  larger/varied population, that is J2's named reopen trigger — surface it; do **not** absorb it in-track.
  *additive-reshard* (a new-shape handling row) or *destructive-HALT* (rule wrong), decided live at the S3 scan.
- **D-3 (host-path silent-no-op hazard — carried from R4b D-1 / scan_nonuniform_depth ROOT).**  The scanner's `ROOT`
  is machine-specific (`scan_nonuniform_depth.py:25`, `~/Remote/hades/Music/Done`).  S3 **must** distinguish
  scan-not-run (unmounted/empty root → never "clean") from no-findings.  Operator mounts the library before
  `/plan-run` (confirmed 2026-08-12); if unmounted at execution, the census refresh is recorded pending, not
  asserted.  *internal-continue* (S3 handles it structurally).
- **D-4 (R6d coupling — sequencing constraint, not a risk).**  This shard changes computed paths for *new* ingests
  only; the destructive library-wide depth-repath is R6d's one J3-gated `repath` pass (D-A5/D-A7).  The S2
  `repath`/`regroup` threading is the surface R6d drives.  No destructive op in this sub-track.  *internal-continue.*
- **D-5 (R6d is paths-only — carried up to ROADMAP R6d node 2026-08-12).**  `repath`/`regroup`/`unify` re-derive
  paths only, offline from embedded tags — they do **not** regenerate tag *content* from MB.  R6d's "one-pass
  re-derivation" scope (paths-only vs. also tag-content) is an R6d-planning decision, folded into the ROADMAP R6d
  node.  Noted so `/plan-run` does not treat it as an in-track R6a discovery.  *internal-continue.*
- **D-6 (temporary library inconsistency — accepted, D-A4-style).**  Until R6d's repath, the on-disk library mixes
  over-resolved (old Shapes-C/D dirs) and clamped (new ingests) depth.  Accepted by the operator (posture 1); not a
  defect to remediate in-track.  Noted so `/plan-run` does not treat it as an in-track discovery.

## Notes for executors

- **Tier routing.**  S1 is **Opus + `@architect` inflection** (the C-W3b-INT interface + tag-data-sufficiency design
  judgment; permanent path-depth policy).  S2, S3 are **Sonnet** (mechanical threading + scan/census/anneal over the
  frozen interface).  `juncture-tier: opus` — kept.
- **Register: render the rule, don't re-open it.**  C-W3b (the rule) is J2-frozen; R6a freezes only its interface.
  If a row seems to *need* a rule change (a new shape mis-clamps), that is a **discovery / J2-reopen** (surface it),
  not a licence to re-adjudicate the rule in-track.
- **Clamp-down-only is load-bearing.**  The `min()` to the modal depth **never pads up**.  Every render-site change
  must carry a test asserting a genuinely-shallow (ragged-floor) node is left unchanged — never padded.
- **Ingest/maintenance parity is load-bearing.**  `run` and `repath`/`regroup` must compute the work-group modal
  depth identically (reuse the `scan_nonuniform_depth.py` `CWP_WORKID_TOP` grouping; do not mint a second grouping).
  A parity test guards it — R6d's repath must render byte-identically to new ingests.
- **REGISTER rule (durable-file discipline).**  In source/tests, state the *property/reason/invariant* — never the
  plan coordinate.  "clamp leaf depth to the work-group modal depth per the uniform-ceiling/ragged-floor rule
  (STYLEGUIDE 4.5 / C-W3b)" is right; "the S1 depth-clamp" is not.  Plan vocabulary (S1/S2/S3, R6a, sub-track names,
  `/plan-run`) lives only in `PLAN.md` / `ROADMAP*.md` / the ledger / commit messages.  See the repo `AGENTS.md`
  "Register rule" block.
- **Anneal denylist (◆ gate greps durable files for these).**  Seeded from the `/plan-run` default, tuned for this
  project's vocabulary:
  - `\bS[1-9]\b` (this sub-track's plan session coordinates) **and** `\bN[1-9]\b` (prior sub-tracks') — **but** allow
    the STYLEGUIDE-rule-section forms (`\b[1-5]\.[0-9]\b` like "4.5", "3.1" are register/rule cites, not plan
    coordinates — do **not** flag).
  - `\bR6[a-e]\b`, `\bR[0-9]\b` (roadmap node coordinates) — flag in durable source/tests; legitimate only in
    PLAN/ROADMAP/ledger/commit messages.
  - `sub-track`, `plan-run`, `plan-shard`, `halt-at-boundaries`, `run-to-boundary`
  - `C-W3b-INT` **only outside docstrings that legitimately name the contract** — contract names in docstrings are
    the intended durable form; flag bare "S1 freeze"-style prose, not the contract name itself.
  - `juncture`, `inflection`, `action-frame`, `◆`
  - Do **not** add `depth`, `modal`, `part_levels`, `CWP_PART_LEVELS`, `clamp`, `uniform-ceiling`, `ragged-floor`,
    `C-W3b`, or `W3b` to the denylist — these are legitimate domain/rule vocabulary this sub-track deliberately
    renders and cites.  (`C-W3b` names the frozen rule; `W3b` appears in NOTES/BACKLOG as durable rule vocabulary.)
- **Invariants to preserve:** the clamp-down-only rule (C-W3b); ingest/maintenance parity; C-CLASS/C-INIT (class and
  top_dir structure unchanged — clamp acts below `work_dir`); C-L0/C-L1 (numbering grammar unchanged); the
  C-PROV/C-MOVE provenance and confirmation-provenance/copy-verify invariants (untouched — R6a is not in the
  copy/verify network path; `repath` threading only changes the computed destination, not the move/journal ordering);
  "path is a handle, not a manifest" (change depth, not identity).
- **Every row runs `~/.local/bin/tox -m analyze` before ledger-done** (build + test at 100% branch coverage + strict
  mypy + ruff + pylint 10.00/10 + pyupgrade).  Import order via `~/.local/bin/tox -m edit`, never hand-edited.
- **Suggested first `/plan-run` invocation:** `halt-at-boundaries` — the C-W3b-INT interface (posture + carrier + the
  D-1 tag-data-sufficiency ruling) is the first unproven substrate judgment in this shard; stop after S1 for an
  operator check that the freeze (especially the clamp-down-only / no-network reasoning and the corner pins) is right
  before S2 consumes it.  Once S1 confirms, `run-to-boundary` through the S3 ◆.
