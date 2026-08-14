<!-- juncture-tier: opus -->
<!-- sub-track: R6d J3-preflight (dry-run evidence harness) — library-completion arc
     (docs/ROADMAP.md), Act III-a.  R6a/R6b/R6c each built code-only library-wide maintenance
     machinery (depth-clamp repath; catalogue-colon repatch; AcoustID repatch) and deferred the
     destructive run to R6d's one J3-gated pass.  R6d itself is double-gated: J3 (a go/no-go on the
     destructive-scale full-library repath) AND R5 exit (Original/ drained, operator-paced).  This
     sub-track builds the J3 *evidence* — a consolidated DRY-RUN harness that runs every deferred
     pass against the live library without mutating it and produces the three J3 evidence categories
     (dry-run change-set, journal capacity, Reference/ retention support).  CODE + STANDALONE-SCRIPT:
     the src/tests gate proves the plan-return machinery on fixtures; the S4 harness run against the
     mounted library produces the J3 report artifact.  This is a /plan-run target for S1–S3 (the
     typed dry-run-plan return + the harness + the CLI wiring, verifiable by the src/tests gate); S4
     is the operator-gated live scan whose gating role is producing the J3 evidence, not passing the
     gate.  NOT the destructive pass itself — R6d's destructive one-pass rides J3 firing + R5 exit,
     neither delivered here. -->

# PLAN — R6d J3-preflight: consolidated dry-run evidence harness

## Purpose (design intent)

*(Re-read at every ◆ boundary — anti-defocus anchor.)*

R6d is the arc's terminal node: the destructive, library-wide "one-pass re-derivation" that re-paths
every file under the frozen heuristics and runs the three deferred tag-content repatches (depth,
catalogue-colon, AcoustID) so the library is made "more like itself" exactly once.  It is gated by
**J3** — a go/no-go juncture that must weigh three evidence categories before any destructive operation
touches the live library:

1. **Dry-run evidence** — *what would change.*  A precise, structured change-set: how many files each
   deferred pass would move or re-tag, and to what.  Today this evidence is unobtainable in structured
   form: every deferred pass (`repath`, `regroup`, `unify`, `repatch_catalogue_colon`,
   `repatch_acoustid_tags`) supports `dry_run` but emits only per-file **structlog events** and returns
   `None`.  A go/no-go on a destructive-scale operation cannot rest on parsed log lines.
2. **Journal capacity** — *can the transaction journal absorb the write burst.*  The journal
   (`music_annotator_journal.json`) is rewritten in full on every append (`write_transaction_log`,
   `_pipeline_io.py:1192`); a library-wide repatch appends thousands of entries.  There is **no**
   size/capacity measurement helper today.
3. **`Reference/` retention** — *the non-destructiveness safety net decision.*  `Reference/` is the
   pre-annotation library snapshot retained as a non-destructiveness check (NOTES).  J3 decides whether
   to keep it through the destructive pass.  There is **no** `Reference/` code — it is a human decision
   the harness must *support with evidence* (disk footprint, coverage), not automate.

**This sub-track builds the J3 evidence, not the destructive pass.**  It delivers a consolidated
dry-run harness that runs every deferred pass in `dry_run` mode against the live library, composes a
structured change-set, measures journal capacity, and surfaces `Reference/` retention evidence — all
without mutating a single file.  R6d's destructive one-pass rides J3 *firing* (this evidence) plus R5
*exit* (Original/ drained, operator-paced) — neither delivered here.  Sequencing matches R6a/R6b/R6c:
code + fixtures now, live destructive run deferred to the J3-gated R6d pass (D-A5 precedent).

**The structural facts that shape this shard (survey 2026-08-13).**

- **Every deferred pass already materializes its full plan before the `dry_run` gate, then only logs
  it.**  Confirmed at `repath` (`_pipeline_maint.py:634` — `plan_pairs` is fully built, including
  collision resolution, before the gate), and the same shape holds for `regroup` (`:847`), `unify`
  (`:1222`), `repatch_catalogue_colon` (`:1576`), `repatch_acoustid_tags` (`:1743`), and `enrich`
  (`:1363`).  So the structured plan the harness needs **already exists in memory** at the gate — the
  only change is to *return* it instead of discarding it after logging.
- **No composite runner exists.**  Each pass is an independent CLI subcommand in `__main__.py`
  (`repath`/`regroup`/`unify`/`enrich`/`repatch-acoustid`); there is no `preflight` or
  `maintenance-run` subcommand.
- **`repatch_catalogue_colon` has no CLI subcommand at all** — it is only callable in-process
  (`_pipeline_maint.py:1446`).  A genuine gap the harness closes.
- **`repatch_acoustid_tags` has an asymmetric signature** — it takes `journal: Path` as its first
  positional arg (all other passes derive the journal path from `dest_root` internally) and already
  returns `list[TransactionEntry]` (`[]` in dry_run).  The harness must special-case its call, and its
  existing return is the closest precedent for C-PREFLIGHT.
- **The standalone-scan precedent is `scripts/scan_*.py`** — read-only, `_check_root`-gated
  (distinguishes scan-not-run/unmounted from no-findings), outside the tox `src/`+`tests/` gate.
- **`Reference/` has zero code** — a pure J3 human decision backed by the NOTES description.

**Interface posture (resolved at this PLAN derivation — the S1 inflection judgment):**

1. **Structured dry-run evidence comes from a typed plan the passes *return*, not from parsing
   structlog (option A).**  Each pass's `dry_run` branch is widened to return a typed **`DryRunPlan`**
   (the already-materialized plan) instead of returning `None` after logging; the harness composes the
   returned plans.  Chosen over structlog-capture (option B) because J3 evidence backs a
   destructive-scale go/no-go — it must be typed, testable, and covered by the src/tests gate (100%
   branch + strict mypy), not brittle log-parsing living outside the gate.  **Tradeoff:** worse on blast
   radius and on the passes' public-contract surface — five passes' `dry_run` return type widens from
   `None`, and their tests grow a plan-return assertion (vs option B's zero `src/` change).  Accepted
   because the passes already build the plan (the widening is mechanical, not a re-architecture), and
   the evidence durability is load-bearing for a one-shot destructive decision.  **Reopen trigger:** if
   widening a pass's return proves to fracture an irreducible internal invariant (e.g. a pass that
   cannot expose its plan without leaking a half-built state), surface as a discovery — do not fall back
   to log-parsing silently; an A-lite (return-plan for the tag-content passes only) is the documented
   fallback shape.

**The four sessions, in landing order:**

1. **S1 @architect — the dry-run-plan return contract (substrate).**  Freeze **C-PREFLIGHT**: the typed
   `DryRunPlan` return shape, and widen every deferred pass's `dry_run` branch to return it.  The plan
   captures each pass's already-materialized change-set (moves for repath/regroup/unify; per-file
   tag-content deltas for the repatch passes).  Per-pass KAT witnesses that dry_run returns the plan and
   still writes nothing.  No harness, no CLI, no live run.
2. **S2 — the consolidated preflight harness.**  A new standalone `scripts/preflight_r6d.py`
   (`scan_*.py` precedent) that composes the five passes' `DryRunPlan`s into one structured change-set,
   measures journal capacity (`len(entries)` + on-disk size), surfaces `Reference/` retention evidence,
   and is `_check_root`-gated.  Consumes C-PREFLIGHT.  Read-only; no live destructive op.
3. **S3 — CLI wiring (integration).**  Add the missing `repatch-catalogue-colon` subcommand (survey
   gap) and a `preflight` composite subcommand that runs the harness over `dest_root`; integration KAT.
   Consumes C-PREFLIGHT.
4. **S4 ◆ — run the harness + produce the J3 evidence report + anneal (integrative).**  Run the harness
   against the operator-mounted library; produce the J3 evidence artifact (`docs/census-r6d-preflight.md`
   / `.json`) — the three evidence categories, distinguishing scan-not-run from no-findings; a
   no-regression parity KAT; close the sub-track; anneal the planning register.

## Verify gate

Discovered from `pyproject.toml` (tox envs; `[tool.tox.env.*]` + the `analyze` label); do not assume
`make`.  Both **binding** — S1–S3 are a code sub-track.

- **VERIFY_TEST**: `~/.local/bin/tox -e test` (`pytest tests/`; **100% branch coverage enforced**,
  `fail_under = 100`).
- **VERIFY_TYPES**: `~/.local/bin/tox -e check_type` (`mypy src/ tests/`, strict).
- Full gate before any row is declared done: `~/.local/bin/tox -m analyze` (build + test + check_type +
  check_format + check_lint 10.00/10 + check_upgrade).  The AGENTS.md "never skip `tox -m analyze`" rule
  applies to every row.  Import order via `~/.local/bin/tox -m edit`, never hand-edited.
- **S4 harness run is not gate-covered:** `scripts/preflight_r6d.py` lives outside `src/`+`tests/`
  (like `scan_nonuniform_depth.py` / `scan_catalogue_colon.py` / `scan_acoustid_tags.py`); it runs
  clean under `venv/bin/python -m py_compile` and best-effort `venv/bin/mypy scripts/` but is not
  `tox`-enforced.  Its gating role is producing the fresh J3 evidence report the S4 ◆ review consumes,
  not passing the gate.  (S2 builds the harness *logic* it can gate-test the composable parts of; the
  standalone script wrapper and the live run are the ungated surface.)

## Session list

| # | Session | Cat | Tier | Consumes | Expected files |
|---|---------|-----|------|----------|----------------|
| 1 @architect | Return a typed dry-run plan from every deferred maintenance pass | A | Opus | C-PROV / C-MOVE (move/verify/journal provenance), the dry_run structlog convention | `src/music_annotator/models.py`, `src/music_annotator/_pipeline_maint.py`, `tests/unit/test_pipeline_maint.py` |
| 2 | Compose the deferred-pass plans into a consolidated dry-run preflight report | B | Sonnet | **C-PREFLIGHT** | `scripts/preflight_r6d.py`, `src/music_annotator/_pipeline_maint.py`, `tests/unit/test_pipeline_maint.py` |
| 3 | Wire the catalogue-colon repatch and preflight composite CLI subcommands | I | Sonnet | **C-PREFLIGHT** | `src/music_annotator/__main__.py`, `tests/unit/test_main.py` |
| 4 ◆ | Run the preflight harness for J3 evidence + census + anneal | I | Sonnet | **C-PREFLIGHT**, `scripts/preflight_r6d.py` | `scripts/preflight_r6d.py`, `docs/census-r6d-preflight.md`, `tests/unit/test_pipeline_maint.py` |

`Cat`: **S1 is A (substrate)** — freezes **C-PREFLIGHT**, the dry-run-plan return shape every later
session and the eventual R6d destructive-run consume; over-specify (carry the tag-content-delta plan
fields even though only the harness/report consumes them, and expose a plan-summary shape a future
destructive-run confirmation prompt can reuse).  **S2 is B** — the composition + capacity/`Reference/`
evidence mechanics over the frozen plan shape, modelled on the read-only `scan_*.py` scripts.  **S3 is
I** — the CLI is where the machinery gets its operator-visible public form (the missing subcommand +
the composite); small but integrative.  **S4 is I (integrative)** — the live harness run + the J3
evidence report give the contract its operator-visible/durable form (the report is what J3
adjudicates), close the ◆, carry the anneal.

`Tier`: **S1 is Opus + `@architect` inflection.**  The dry-run-plan return shape is the evidence seam
for a *destructive-scale* go/no-go, and widening five passes' public return type is a design-error-cost
decision that tests alone cannot adjudicate — lever 3 (design-error cost: a wrong plan shape either
under-captures the change-set J3 needs or leaks half-built pass state) and lever 4
(correctness-criticality: this evidence gates a one-shot destructive library-wide operation).  **S2,
S3, S4 are Sonnet** — mechanical over the frozen plan shape with a strong inner loop (lever 5: 100%
branch coverage + strict mypy) and direct precedents (`scan_*.py` for the standalone read-only harness;
the existing `repatch-acoustid` subcommand for the CLI wiring).  `juncture-tier: opus` — kept (arc
default).

**Sizing (levers named).**  Default band ~150–400 LOC / 2–4 files.

- **S1 ≈ 180–320 LOC, 3 files** (the `DryRunPlan` model + the six `dry_run`-branch widenings +
  per-pass plan-return KATs).  Within band.  **Irreducible unit (lever 2, floor):** the plan shape is
  not *proven* until every deferred pass returns it — a shape that fits `repath`'s move-plan but not
  the repatch passes' tag-content delta is a mis-freeze.  Splitting the shape-freeze from the five-pass
  widening (the rejected alternative) would leave the contract unwitnessed by its own consumers.  Kept
  whole.  **Lever 3/4:** high cost-of-wrong / correctness-crit is *why* S1 is Opus+inflection, not why
  it fractures.  One-line title passes.
- **S2 ≈ 150–280 LOC, 2–3 files** (the composition logic + journal-capacity measurement +
  `Reference/` evidence + `_check_root` gate; the gate-testable composition helpers land in
  `_pipeline_maint.py` so they are covered, the thin standalone wrapper in `scripts/`).  Within band.
  **Separate session by the one-line-commit-title corollary** — "compose the plans into a report" is
  distinct from "change what each pass returns" (S1); split at the contract-sharp C-PREFLIGHT boundary.
  **Lever 1:** two read-only precedents (`scan_catalogue_colon.py` composition + `_check_root`) — not
  greenfield.
- **S3 ≈ 80–150 LOC, 2 files** (the `repatch-catalogue-colon` subcommand + the `preflight` composite
  subcommand + `test_main.py` CLI tests).  Under/within band.  **Separate by the corollary** — CLI
  surface is distinct from harness logic; the survey flagged the missing `repatch-catalogue-colon`
  subcommand as its own gap.  Not fractured below the floor (the two subcommands are one CLI-surface
  unit).  Cat I because the CLI is the public form.
- **S4 ≈ 60–120 LOC + harness run, 2–3 files** (the live run + the J3 report artifact + a
  no-regression parity KAT + anneal).  Under band; **separate by the corollary** — the
  run/report/anneal is one integrative unit; merging into S2/S3 yields an "and"-joined title.  Not
  fractured below the floor (the run produces the population the report tabulates).

## Session detail

### S1 @architect — Return a typed dry-run plan from every deferred maintenance pass — freezes C-PREFLIGHT

**Deliverable.**  The typed dry-run-plan return shape and its propagation, no new harness/CLI/run:
- **`DryRunPlan` model (`models.py`).**  A Pydantic model capturing a single pass's dry-run change-set:
  the pass name, a list of per-file entries (each with the current path and — depending on pass kind —
  the planned new path *or* the planned tag-content delta), and a per-pass summary count.  Modelled on
  `repatch_acoustid_tags`'s existing `list[TransactionEntry]` dry-run return; carries both the
  move-plan shape (repath/regroup/unify) and the tag-content-delta shape (the repatch/enrich passes) so
  one type serves all five (over-specify per Category-A).
- **Widen every deferred pass's `dry_run` branch to return the plan.**  In each of `repath` (`:634`),
  `regroup` (`:847`), `unify` (`:1222`), `repatch_catalogue_colon` (`:1576`), and `enrich` (`:1363`),
  the `dry_run` branch already builds and logs the full plan — change it to build a `DryRunPlan` from
  the already-materialized `plan_pairs` (or per-file corrected-tag set) and **return** it; keep the
  existing structlog events (the log is still useful; the return is additive).  `repatch_acoustid_tags`
  (`:1743`) already returns `list[TransactionEntry]` — adapt it to the `DryRunPlan` shape (or wrap its
  existing return) so all five are uniform.  The **non-dry-run return stays `None`** for the
  move/regroup/unify/repatch passes (they mutate; the dry_run branch is the only plan-returning path) —
  i.e. the return type becomes `DryRunPlan | None`.
- Docstrings state the property (dry_run returns the structured change-set; the plan is the same one
  the pass would enact) citing the provenance-chain invariant, never the plan coordinate.

**KAT (the freeze witness for C-PREFLIGHT).**  In `test_pipeline_maint.py`, per pass:
(a) **plan-return witness** — a `dry_run=True` call returns a `DryRunPlan` whose per-file entries match
the fixture's expected change-set (right count, right paths/deltas);
(b) **no-write witness preserved** — the existing "no file moved / no journal entry appended" assertion
still holds alongside the new return (dry_run stays non-mutating);
(c) **empty-plan witness** — a fixture with nothing to change returns an empty `DryRunPlan` (count 0),
*distinct* from a `None`/error (so the harness can tell "ran, found nothing" from "did not run");
(d) **shape-uniformity witness** — a move-pass plan and a tag-content-pass plan both validate against
the one `DryRunPlan` type (the over-specification is exercised).

**Subtleties.**
- **The plan already exists at the gate — this is a return-widen, not a re-plan.**  Confirmed at
  `repath:634`; the same holds for all five.  Do **not** re-derive the plan; capture what is already in
  `plan_pairs` / the per-file corrected-tag set.
- **Provenance chain untouched.**  The non-dry-run paths (`_move_verify_journal` /
  re-tag→`_verify_copy`→journal) are not touched; only the dry_run branch grows a return.  The
  confirmation-provenance invariant is unchanged.
- **`repatch_acoustid_tags`'s asymmetry.**  It takes `journal: Path` positionally and already returns a
  list — uniformize to `DryRunPlan` without breaking its existing non-dry-run return contract (its
  callers in `__main__.py:957` expect the current shape; adapt the type or keep a compatible surface).
- **`enrich` is included** even though it is not strictly an R6d "repatch" — it is the provenance-chain
  model and its dry-run change-set is legitimate J3 evidence (backfill scope).  Include it for
  uniformity; the reopen trigger is if its inclusion bloats the plan shape.
- **100%-branch-coverage gate.**  The `dry_run` / non-dry_run branch split in each pass now has a
  return on one arm — both arms need explicit tests; any `match/case` gets `case _: # pragma: no cover`
  if exhaustive.

**Deferrals.**  No harness (S2); no CLI (S3); no live run/report (S4); no destructive repatch (R6d).

### S2 — Compose the deferred-pass plans into a consolidated dry-run preflight report

*(Lower-fidelity sketch — correct for a post-substrate row; crisply specified after C-PREFLIGHT freezes at S1.)*

**Deliverable.**  The consolidated read-only harness:
- **Composition helper (gate-covered, in `_pipeline_maint.py`).**  A function that runs each deferred
  pass with `dry_run=True` over a `dest_root`, collects the returned `DryRunPlan`s, and assembles a
  consolidated report object (total files touched per pass; overlap detection where a file appears in
  more than one pass's plan — a load-bearing J3 signal for one-pass ordering).  This is the composable,
  testable core.
- **Journal-capacity measurement.**  Measure `len(journal.entries)` and the on-disk journal file size;
  project the post-repatch entry-count delta from the composed plans (each planned repatch appends one
  entry).  No helper exists today — add one here.
- **`Reference/` retention evidence.**  Surface the evidence a human J3 decision needs — the
  `Reference/` directory's presence and disk footprint (read-only `os.path` inspection); do **not**
  automate the retention decision.
- **Standalone wrapper (`scripts/preflight_r6d.py`).**  A thin `scan_*.py`-style CLI wrapper that
  `_check_root`-gates the library root (scan-not-run vs no-findings), calls the composition helper, and
  prints/serializes the report.  The wrapper is the ungated surface; the helper it calls is gate-tested.

**KAT (behavioural witness).**  Over a fixture library with known depth/catalogue-colon/AcoustID-legacy
files: the composition helper returns a report whose per-pass counts match the fixtures; the
journal-capacity measurement returns the right entry count + a nonzero size; an overlap fixture (a file
both depth-repathed and AcoustID-repatched) is flagged; an empty fixture reports no-findings (distinct
from scan-not-run); `Reference/` evidence reflects a fixture `Reference/` dir.

**Subtleties.**
- **Read-only, like the scan scripts.**  The harness runs every pass in `dry_run=True`; it must never
  reach a mutating branch.  A test asserts no journal entry / no move across the whole composition.
- **Overlap is J3-load-bearing.**  A file in multiple passes' plans means R6d must order the passes
  (tag-content before repath, so the path re-renders the corrected tags) — surface it, don't hide it.
- **`_check_root` from the scan-script precedent** — distinguish unmounted/empty root (scan-not-run,
  never "clean") from no-findings (the R4b D-1 / R6a D-3 / R6b D-3 / R6c D-3 hazard).

**Deferrals.**  No CLI subcommands (S3); no live run/report artifact (S4); no destructive run (R6d).

### S3 — Wire the catalogue-colon repatch and preflight composite CLI subcommands

*(Lower-fidelity sketch — post-substrate integrative row.)*

**Deliverable.**  Close the CLI-surface gaps:
- **`repatch-catalogue-colon` subcommand (`__main__.py`).**  `repatch_catalogue_colon` exists in
  `_pipeline_maint.py` but has no CLI entry (survey gap) — add the subcommand mirroring the existing
  `repatch-acoustid` dispatch (`__main__.py:957`), `dry_run`-aware.
- **`preflight` composite subcommand.**  A subcommand that runs the S2 harness over `dest_root` and
  emits the consolidated report — the operator's one-command J3-evidence entry point.
- Both follow the existing subcommand-dispatch pattern; no new pass logic.

**KAT.**  A CLI test invoking `repatch-catalogue-colon --dry-run` dispatches to the pass with the right
args and writes nothing; a `preflight` invocation runs the harness and emits the report; arg parsing /
help surfaces are covered.

**Subtleties.**  No `src/` pass logic change beyond dispatch wiring.  Cover the `--dry-run` /
non-dry-run arg branches for the new subcommands (branch coverage).

**Deferrals.**  No live run/report artifact (S4); no destructive run (R6d).

### S4 ◆ — Run the preflight harness for J3 evidence + census + anneal

*(Lower-fidelity sketch — post-substrate integrative row.)*

**Deliverable.**  Produce the J3 evidence:
- **Run the harness against the operator-mounted library** and produce the J3 evidence artifact
  (`docs/census-r6d-preflight.md` + `.json`): the three J3 categories — (1) the consolidated dry-run
  change-set (per-pass counts + the overlap map), (2) journal capacity (current entries + size + the
  projected post-repatch delta), (3) `Reference/` retention evidence (presence + footprint).
  **Distinguish scan-not-run** (unmounted/empty root → never report clean) **from no-findings**; if
  unmounted at execution, record the census as *not run*, not clean.
- A signature the harness mis-handles (e.g. a pass whose live change-set contradicts its fixture-proven
  shape, or an overlap the composed plan orders wrongly) is the reopen trigger — surface as a
  discovery; do not silently absorb.

**KAT.**  A no-regression parity test asserting the S1 plan-return + S2 composition behaviour still
holds against a representative fixture (the integrative session's behavioural pin).

**Subtleties.**  No `src/` change in S4 unless a harness helper is promoted (it should not be — keep the
standalone wrapper in `scripts/` per the `scan_fragmentation.py` precedent).  Purely a
run-validation + J3-report + anneal row; **no destructive library operation** (R6d runs the passes for
real under J3, after this evidence and R5 exit).

**◆ boundary (register anneal).**  Re-read Purpose.  Confirm all four sessions enacted, `tox -m analyze`
green, ledger complete.  **Planning-register anneal:**
- Durable files (`models.py`, `_pipeline_maint.py`, `__main__.py`, `preflight_r6d.py`
  docstrings/comments) carry **no plan coordinates** — no "S1/S2/S3/S4", no "R6d", no "J3-preflight
  sub-track", no `/plan-run` vocabulary.  State the property/reason/invariant (e.g. "dry_run returns
  the structured change-set the pass would enact"), never the plan coordinate.
- Grep the durable files against the **anneal denylist** (Notes for executors); translate any leaked
  coordinate into standalone prose.
- Report to the library-completion roadmap: the J3 dry-run evidence harness is enacted; C-PREFLIGHT
  frozen; the J3 evidence artifact is produced.  **R6d coordination noted** — J3 can now weigh the
  dry-run change-set + journal capacity + `Reference/` evidence; R6d's destructive one-pass still
  awaits J3 firing *and* R5 exit (Original/ drained, operator-paced).  Neither is delivered here.

## Cross-session contracts

### C-PREFLIGHT — the typed dry-run-plan return shape *(RESOLVED at inflection design 2026-08-13; frozen at S1)*

**Return shape (frozen at S1).**  Every deferred offline maintenance pass, when called with
`dry_run=True`, **returns** a typed **`DryRunPlan`** capturing the change-set it would enact instead of
returning `None`/`[]` after logging.  The plan carries: the pass identity, a list of per-file entries
(current path + planned new path for the move passes; current path + planned tag-content delta for the
tag-content passes), and a summary count.  An empty plan (count 0) is a *ran-found-nothing* result,
structurally distinct from a not-run/error.  **Invariant:** a `dry_run=True` call is non-mutating — it
returns the plan and writes nothing (no file move, no journal append); the plan is the same change-set
the non-dry-run call would enact.

**Survey reconciliation (2026-08-13 — corrects two draft framings against the code):**

- **"Plan already materialized at the gate" holds only for the three *move* passes.**  `repath`
  (`:634`), `regroup` (`:847`), `unify` (`:1222`) build a full `plan_pairs` list — including collision
  resolution — *before* a single `dry_run` gate; the widening reads `plan_pairs`.  Move-plan tuple
  shape confirmed: `(current_path, new_dest, acoustid, length[, release_id])` (`repath` omits
  `release_id`; `regroup`/`unify` carry it).  The three *tag-content* passes — `enrich` (`:1363`),
  `repatch_catalogue_colon` (`:1576`), `repatch_acoustid_tags` (`:1743`) — gate `dry_run` **inside a
  per-file loop** (`continue`); there is **no** pre-gate aggregate.  Each iteration has the fully
  computed per-file datum at the gate (`enrich`: `write_fields.keys()`; `repatch_catalogue_colon`:
  `corrected_parts` + `new_groupheading`; `repatch_acoustid_tags`: `fingerprint` present +
  `will_re_resolve`).  So the widening for these three is **accumulate a `DryRunEntry` per loop
  iteration into a plan list, and `return` the accumulated `DryRunPlan` at function end** — *not* a
  pre-gate `plan_pairs` capture.  This is reconcilable and mechanical (the entry content is fully
  materialized before each `continue`; no half-built state leaks) — **the D-1 reopen trigger does not
  fire.**  The executor instruction "capture what is in `plan_pairs`" is corrected to: *capture what is
  materialized at each pass's gate — `plan_pairs` for the move passes, the accumulated per-file
  corrected-tag set for the tag-content passes.*
- **`repatch_acoustid_tags` does NOT "already return the dry-run plan."**  Its `appended:
  list[TransactionEntry]` is populated only on the **mutating** path (`:1823`); its dry-run arm
  (`:1743`) logs and `continue`s, so its dry-run return is **unconditionally `[]`** regardless of how
  many files would migrate.  The `list[TransactionEntry]` return is therefore a *result of mutation*,
  not a *plan* — it is not the C-PREFLIGHT precedent the draft named it.  Its non-dry-run
  `list[TransactionEntry]` return, however, **is** a real contract: the `__main__.py:957` caller and
  `test_dry_run_writes_nothing_returns_empty` depend on it.  Resolution below (item 2, acoustid arm).

**Resolved interface (frozen at S1).**  The concrete `DryRunPlan` fields, the per-pass return-type
change, and the composition contract, split by which session mutates each.

1. **`DryRunPlan` / `DryRunEntry` models (`models.py`).**  Two Pydantic `BaseModel`s, defaults per repo
   convention (`""` / `[]`), no `Any`, `model_config = {"populate_by_name": True}` if any alias is
   introduced (none needed — these are internal, not MB-API):
   - **`DryRunEntry`** — one type spanning both plan kinds (over-specified per Category-A):
     - `current_path: str = ""` — the file's current on-disk path (move passes: the *old* path;
       tag-content passes: the in-place path).
     - `planned_path: str = ""` — the planned new path (move passes populate; tag-content passes leave
       `""`, since they write in place).
     - `tag_delta: dict[str, str] = {}` — the planned per-tag change-set keyed by tag name
       (tag-content passes populate: `enrich` → the `write_fields` map; `repatch_catalogue_colon` →
       the corrected `CWP_PART_i` labels + rebuilt `CWP_GROUPHEADING`; `repatch_acoustid_tags` → the
       migrated `ACOUSTID_FINGERPRINT` [+ `ACOUSTID_ID` when re-resolved]).  Move passes leave `{}`.
       (`dict[str, str]`, not `Any` — every value is a rendered tag string.)
     - The move-vs-tag-content distinction is **structural, not a discriminator field**: a move entry
       has `planned_path != ""` and `tag_delta == {}`; a tag-content entry has `planned_path == ""`
       and `tag_delta != {}`.  The KAT (d) shape-uniformity witness exercises both against the one type.
   - **`DryRunPlan`** —
     - `pass_name: str = ""` — the pass identity (e.g. `"repath"`, `"enrich"`,
       `"repatch_acoustid_tags"`; the pass's own name, a durable property, not a plan coordinate).
     - `entries: list[DryRunEntry] = []` — the per-file change-set.
     - `count: int = 0` — **stored, not derived** (an explicit field, so an empty plan serializes
       `count=0` and a not-run/error state — a `None` return — is structurally distinct from
       `DryRunPlan(count=0)`).  S1 sets `count = len(entries)` at construction; do not make it a
       computed property (the harness/report reads it as a plain field, and a stored count survives
       JSON round-trip for the S4 `.json` artifact).
2. **Per-pass `dry_run`-branch return.**
   - **Move passes** — `repath`, `regroup`, `unify`: return type widens `None` → `DryRunPlan | None`
     (the `DryRunPlan` built from `plan_pairs` on the `dry_run` arm; `None` on the mutating arm).
     Build the plan just before the existing `if dry_run:` gate's `return`, from the
     already-collision-resolved `plan_pairs`.
   - **Tag-content loop passes** — `enrich`, `repatch_catalogue_colon`: return type widens
     `None` → `DryRunPlan | None`.  Initialise an accumulator list before the loop; on the `dry_run`
     arm, append a `DryRunEntry` (with `tag_delta`) instead of only `continue`; after the loop, when
     `dry_run` is set, `return DryRunPlan(...)`.  The mutating arm still returns `None`.
   - **`repatch_acoustid_tags` (the asymmetric pass)** — return type becomes
     **`DryRunPlan | list[TransactionEntry]`**: the `dry_run` arm returns a `DryRunPlan` (accumulated
     across the loop, as above); the **non-dry-run arm keeps its existing `list[TransactionEntry]`
     return unchanged** (the `appended` list — the `__main__.py:957` caller contract preserved
     exactly).  Rationale: the harness (S2) calls every pass with `dry_run=True`, so it always receives
     a `DryRunPlan`; the CLI caller always takes the mutating path, so it always receives the list.
     **Uniformity is on the dry-run arm** (all six passes return a `DryRunPlan` under `dry_run=True`) —
     which is exactly the surface the harness composes over — *not* total-signature uniformity.  Chosen
     over a `DryRunPlan | None` rewrite (would break the mutating-path list contract) and over a
     `DryRunPlan | list` union on *all six* (needlessly widens the five `None`-returning mutating arms).
     **Tradeoff:** this pass's signature is non-uniform with the other five (two return-type shapes
     vs one), so the harness must not assume a single return type across passes — but it never does
     (it only ever reads the dry-run arm).  **KAT (c) update:** `test_dry_run_writes_nothing_returns_empty`
     currently asserts `result == []`; S1 changes it to assert an **empty `DryRunPlan` (`count == 0`,
     `entries == []`)**, per the empty-plan-≠-not-run witness — the `[]` return is retired on the
     dry-run arm.
   - The keyword-only `dry_run` params and the `journal: Path` positional-first asymmetry of
     `repatch_acoustid_tags` are unchanged — only return types widen.
3. **Composition contract.**  A helper (`_pipeline_maint.py`) that runs the five/six passes with
   `dry_run=True`, collects the `DryRunPlan`s, and assembles a consolidated report (per-pass totals +
   the cross-pass overlap map — files appearing in >1 plan, keyed on `current_path`).  Landed at S2.
4. **Journal-capacity measurement.**  `len(journal.entries)` + on-disk journal size + the projected
   post-repatch entry-count delta from the composed plans (each tag-content `DryRunEntry` projects one
   appended journal entry; each move `DryRunEntry` projects one).  New; no helper exists.  Landed at S2.
5. **`Reference/` evidence surface.**  Read-only presence + footprint of the `Reference/` snapshot dir;
   evidence only, never an automated retention decision.  Landed at S2.
6. **CLI surface.**  `repatch-catalogue-colon` subcommand (survey gap — the pass exists at
   `_pipeline_maint.py:1446` but has no CLI entry; only `repatch-acoustid` is wired at
   `__main__.py:955`) + a `preflight` composite subcommand over `dest_root`.  Landed at S3.

**S1 lands items 1–2** (the `DryRunPlan` + `DryRunEntry` models + the six per-pass return-widenings,
`test_pipeline_maint.py` KATs); **items 3–5 are S2's** composition/capacity/`Reference/` mechanics;
**item 6 is S3's** CLI wiring; the live run + report is **S4's**.

**Flavour:** compiler-enforced (the `DryRunPlan` type + the widened return signatures; mypy strict) +
test-enforced (the S1 plan-return/no-write/empty-plan/shape-uniformity KATs; the S2 composition/
overlap/capacity KATs; the S3 CLI-dispatch KATs; the S4 parity KAT) + prose-enforced (the dry_run-is-
non-mutating invariant; the scan-not-run-vs-no-findings distinction, cited to the `scan_*.py`
precedent).  **Defined-in:** S1.  **Consumed-by:** S2 (composition), S3 (CLI), S4 (the live run +
report), **J3** (weighs the evidence), and the eventual R6d destructive one-pass (the plan-summary
shape a destructive-run confirmation prompt reuses).  Over-specified per Category-A: one `DryRunPlan`
type spans both move and tag-content plan kinds, and a plan-summary shape is exposed for the future
destructive-run consumer though only the harness consumes it now.

### Consumed (frozen upstream — invalidation is out of scope for this sub-track)

- **C-PROV / C-MOVE + confirmation-provenance** — move/verify/journal provenance.  The passes'
  *mutating* paths are untouched; only the `dry_run` branch grows a return.  The
  re-tag→`_verify_copy`→journal ordering and the confirmation-provenance chain are preserved exactly.
  Validate-only.
- **The `dry_run` structlog convention** — every pass already logs a per-file dry-run event; S1 keeps
  the log and adds the structured return (additive, not a replacement).  Validate-only.
- **The `scan_*.py` read-only standalone precedent** — `_check_root` gating (scan-not-run vs
  no-findings), root-not-mounted safety, outside the tox gate.  The harness follows it.  Validate-only.
- **C-ACID / C-CAT-INT / C-W3b-INT** — the three deferred passes' own frozen contracts.  The preflight
  runs them in dry_run; it does not re-open their re-derivation logic.  Validate-only.
- **"Path is a handle, not a manifest"** — the preflight reports what the passes *would* do; it changes
  no path structure and no `build_dest_path`.

### Produced

- **C-PREFLIGHT** — the typed dry-run-plan return shape at S1; the composition + capacity/`Reference/`
  evidence at S2; the CLI at S3; the live J3 report at S4.  **Coordinates with J3 and R6d:** the
  evidence is the J3 go/no-go input; the plan shape is the substrate the R6d destructive one-pass
  reuses (each pass runs for real under J3, ordered by the S2 overlap map).

## Progress ledger

| # | Session | Status | Commit | Froze |
|---|---------|--------|--------|-------|
| 1 @architect | Return a typed dry-run plan from every deferred maintenance pass | done | ac9ddb7 | C-PREFLIGHT (DryRunPlan/DryRunEntry models + six per-pass return widenings) |
| 2 | Compose the deferred-pass plans into a consolidated dry-run preflight report | done | d80cd38 | |
| 3 | Wire the catalogue-colon repatch and preflight composite CLI subcommands | done | 9233438 | (extra: __init__.py — plainly part of the unit, exposing compose_preflight_report in __all__) |
| 4 ◆ | Run the preflight harness for J3 evidence + census + anneal | pending | | |

## Action-frame digest

### S1 — 2026-08-13
Discovery/flex: Inflection design confirmed two draft-framing corrections: tag-content passes accumulate DryRunEntry per loop iteration (not pre-gate plan_pairs); repatch_acoustid_tags dry-run arm was returning [] unconditionally (not a plan), resolved by DryRunPlan | list[TransactionEntry] asymmetric return.
Affected: C-PREFLIGHT
Deferred: no
Texture: D-1 reopen trigger evaluated and does not fire; Option A (typed return) stands; KATs witness all four witnesses for all six passes; 1816 tests green, 100% branch coverage.

## Discoveries & risks

- **D-1 (S1 evidence-seam judgment — the inflection).**  Structured dry-run evidence comes from a typed
  `DryRunPlan` the passes *return* (option A), not from parsing structlog (option B).  Resolution to
  freeze at S1: the passes already materialize the plan before the dry_run gate (`repath:634` et al.),
  so the return-widen is mechanical; typed + gate-covered evidence is required to back a
  destructive-scale J3 go/no-go.  **Reopen trigger:** if widening a pass's return fractures an
  irreducible internal invariant (a pass that cannot expose its plan without leaking half-built state),
  surface as a discovery — the documented fallback is A-lite (return-plan for the tag-content passes
  only), *not* a silent drop to log-parsing.  **Reopen-trigger evaluated at inflection design
  (2026-08-13): does NOT fire.**  The move passes expose a pre-gate `plan_pairs`; the three
  tag-content passes gate `dry_run` inside a per-file loop but the per-file datum is fully computed
  before each `continue`, so the widening accumulates a `DryRunEntry` per iteration and returns the
  plan at function end — no half-built state leaks, no invariant fractures.  Option A stands; C-PREFLIGHT
  is resolved.  See the C-PREFLIGHT "Survey reconciliation" block for the two corrected draft framings
  (the loop-gate accumulation shape; `repatch_acoustid_tags`'s dry-run return being `[]`, not a plan).
  *internal-continue* → C-PREFLIGHT resolved, ready for S1 freeze.
- **D-2 (`repatch_catalogue_colon` has no CLI subcommand — a real gap, resolved by S3).**  It is only
  callable in-process (`_pipeline_maint.py:1446`); the CLI has `repatch-acoustid` but no
  `repatch-catalogue-colon`.  S3 adds it alongside the `preflight` composite.  Not a risk; a scope item.
  *internal-continue.*
- **D-3 (host-path silent-no-op hazard — carried from R6a D-3 / R6b D-3 / R6c D-3 / R4b D-1).**  The
  harness's `ROOT` is machine-specific (`~/Remote/hades/Music/Done`, the `scan_*.py` pattern).  S2/S4
  **must** distinguish scan-not-run (unmounted/empty root → never "clean") from no-findings via
  `_check_root`.  Operator mounts the library before the S4 run; if unmounted at execution, the J3
  report is recorded *not run*, not clean.  *internal-continue* (S2/S4 handle it structurally).
- **D-4 (cross-pass overlap ordering — a J3-planning input, surfaced by S2).**  A file in more than one
  pass's dry-run plan (e.g. depth-repathed *and* AcoustID-repatched) means the R6d destructive pass
  must order the passes (tag-content before repath, so the path re-renders corrected tags).  The S2
  overlap map surfaces this; the *ordering decision* belongs to R6d's PLAN derivation, not this
  sub-track.  Noted so R6d planning consumes the overlap evidence.  *internal-continue.*
- **D-5 (R6d + J3 + R5 coupling — sequencing constraint, not a risk).**  This sub-track builds J3
  *evidence*; R6d's destructive one-pass rides J3 *firing* (this evidence) plus R5 *exit* (Original/
  drained, operator-paced).  Neither the J3 verdict nor the destructive run is in scope here.  The
  harness makes J3 *decidable*, not *decided*.  *internal-continue.*
- **D-6 (journal write-amplification — an evidence datum, not an in-track defect).**  The journal is
  rewritten in full on every append (`write_transaction_log`, `_pipeline_io.py:1192`); a library-wide
  repatch appends thousands of entries, each rewriting the whole file.  S2 *measures* this (the J3
  capacity category); whether it warrants a streaming-append rewrite is an R6d/J3 decision, not an
  in-track remedy.  Noted so `/plan-run` does not treat the measurement as a discovered defect to fix.
  *internal-continue.*

## Notes for executors

- **Tier routing.**  S1 is **Opus + `@architect` inflection** (C-PREFLIGHT — the dry-run-plan evidence
  seam for a destructive-scale J3 go/no-go; widening five passes' public return type; correctness-crit).
  S2, S3, S4 are **Sonnet** (mechanical over the frozen plan shape, modelled on the read-only
  `scan_*.py` scripts and the existing `repatch-acoustid` subcommand).  `juncture-tier: opus` — kept.
- **This sub-track builds J3 evidence, never runs the destructive pass.**  Every pass runs in
  `dry_run=True`.  A mutating branch reached anywhere in the harness is a violation.  R6d runs the
  passes for real, later, under J3 + R5 exit.
- **Return-widen, don't re-plan.**  The plan already exists at each pass's dry_run gate (`repath:634`,
  and the same for the other four/five).  Capture what is in `plan_pairs` / the per-file corrected-tag
  set; do not re-derive.
- **dry_run stays non-mutating.**  Keep the existing structlog events; the `DryRunPlan` return is
  additive.  Every widened pass keeps its "no move / no journal entry" test alongside the new
  plan-return assertion.
- **Empty plan ≠ not-run.**  An empty `DryRunPlan` (count 0) is *ran-found-nothing*; a not-run
  (unmounted root) is a distinct state.  The harness must never report unmounted as clean (`_check_root`
  from the `scan_*.py` precedent).
- **Model the harness on `scan_*.py`, not fresh.**  `scan_catalogue_colon.py` / `scan_acoustid_tags.py`
  are read-only, `_check_root`-gated, outside the tox gate — reuse that shape.  Keep the gate-testable
  composition logic in `_pipeline_maint.py`; keep only the thin wrapper in `scripts/`.
- **`repatch_acoustid_tags` is asymmetric.**  It takes `journal: Path` positionally and already returns
  a list — uniformize to `DryRunPlan` without breaking its `__main__.py:957` caller contract.
- **REGISTER rule (durable-file discipline).**  In source/tests, state the *property/reason/invariant*
  — never the plan coordinate.  "dry_run returns the structured change-set the pass would enact" is
  right; "the S1 plan-return freeze" is not.  Plan vocabulary (S1/S2/S3/S4, R6d, J3-preflight,
  sub-track names, `/plan-run`) lives only in `PLAN.md` / `ROADMAP*.md` / the ledger / commit messages.
  See the repo `AGENTS.md` "Register rule" block.
- **Anneal denylist (◆ gate greps durable files for these).**  Seeded from the `/plan-run` default,
  tuned for this project's vocabulary:
  - `\bS[1-9]\b` (this sub-track's plan session coordinates) — **but** allow STYLEGUIDE-rule-section
    forms (`\b[1-5]\.[0-9]\b` like "4.5", "3.1" are register/rule cites, not plan coordinates — do
    **not** flag).
  - `\bR6[a-e]\b`, `\bR[0-9]\b`, `\bJ[1-3]\b` (roadmap node + juncture coordinates) — flag in durable
    source/tests; legitimate only in PLAN/ROADMAP/ledger/commit messages.
  - `sub-track`, `plan-run`, `plan-shard`, `halt-at-boundaries`, `run-to-boundary`, `preflight`
    **only as a plan coordinate** — the `preflight` CLI subcommand name and `preflight_r6d.py` are
    legitimate durable vocabulary (flag "the R6d preflight sub-track" prose, not the command/file name).
  - `C-PREFLIGHT` **only outside docstrings that legitimately name the contract** — contract names in
    docstrings are the intended durable form; flag bare "S1 freeze"-style prose, not the contract name.
  - `juncture`, `inflection`, `action-frame`, `◆`
  - Do **not** add `dry_run`, `DryRunPlan`, `repath`, `regroup`, `unify`, `repatch`, `enrich`,
    `journal`, `Reference`, `Original`, `Done`, `preflight_r6d`, `scan_*` — these are legitimate
    domain/API vocabulary this sub-track deliberately renders and cites.
- **Invariants to preserve:** the dry_run-is-non-mutating invariant + the empty-plan-≠-not-run
  distinction (C-PREFLIGHT); the `enrich` / copy-tag-verify confirmation-provenance chain (the mutating
  paths are untouched); the three deferred passes' own frozen contracts (C-ACID / C-CAT-INT /
  C-W3b-INT — the preflight runs them in dry_run, never re-opens their logic); "path is a handle, not a
  manifest" (the preflight reports, changes nothing).
- **Every row runs `~/.local/bin/tox -m analyze` before ledger-done** (build + test at 100% branch
  coverage + strict mypy + ruff + pylint 10.00/10 + pyupgrade).  Import order via
  `~/.local/bin/tox -m edit`, never hand-edited.
- **Suggested first `/plan-run` invocation:** `halt-at-boundaries` — the C-PREFLIGHT dry-run-plan
  return shape is the first unproven substrate judgment in this shard (it widens five passes' public
  return type to back a destructive-scale J3 evidence decision); stop after S1 for an operator check
  that the plan shape captures the J3 evidence J3 actually needs (especially the tag-content-delta shape
  and the empty-vs-not-run distinction) before S2 composes it.  Once S1 confirms, `run-to-boundary`
  through the S4 ◆.
