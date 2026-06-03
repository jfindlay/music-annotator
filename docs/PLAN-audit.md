# PLAN-audit.md — Structural-coherence audit (investigation-only)

**Type:** pre-shard, **investigation-only**.  This plan produces *findings and restructuring
proposals*; it does **not** ship restructurings.  Each axis emits a findings report; implementation
is decided at a HALT after the user reviews the proposals, and only then graduates into its own
sharded action plan (`PLAN-audit-action.md` or per-axis action sub-tracks).

**Origin:** graduated from `docs/BACKLOG.md` "Codebase-audit items" + "Destructive maintenance
commands" (the five concrete point-items), expanded by the user (2026-06-03) with three
structural-coherence axes: app-code organization, test-code organization, and CLI command taxonomy,
each judged against *coherent / logical / intuitive / relentlessly-minimal at every level of
structure*.

**Why investigation-first (the load-bearing sequencing decision).**  The five backlog point-items
(WorkGroup object, `__init__` surface, repath confirmation, duplicated move-loop, command
consistency) are each a **symptom** of one of the three structural facts below.  Shipping a point-fix
before the structural decision risks building something a restructuring later undoes — e.g. adding a
`WorkGroup` dataclass to `run()` before deciding whether `run()` is split, or adding a `--yes` flag
to `repath` before deciding whether the maintenance commands share one confirmation helper.  So the
audit reasons over structure first; the point-items are re-derived as *outputs* of the structural
findings, not as independent tasks.

---

## Structural facts (established 2026-06-03 by two read-only surveys; re-verify at reopen)

These are the inputs the investigation reasons over.  They are facts, not yet judgments.

- **Two mega-modules carry 43% of source.**  `_pipeline.py` (2,674 lines) + `_pipeline_io.py`
  (2,189) = 4,863 lines; next is `models.py` (1,576).
- **`run()` is ~704 lines** (`_pipeline.py:750–1454`) — by far the largest function; its work-group
  loop contains ~7 sequential within-group passes over the same `group_idxs`, each with its own
  accumulator (point-item 1's substrate).
- **`_pipeline.py` hosts five logically independent operations** (`run`, `repath`, `regroup`,
  `unify`, `enrich`) that share no code with each other — co-located by convention, not coupling.
- **`_pipeline_io.py` spans two abstraction levels** — low-level I/O primitives (`_sha256_file`,
  `_read_tags_flac`) *and* four high-level library operations (`audit`, `rebuild_journal`,
  `diff_journal`, `enrich_origin_time`).
- **The move/verify/journal/cleanup block is triplicated** (~60 lines × 3) across `repath`,
  `regroup`, `unify`, including the cross-fs EXDEV fallback and empty-dir cleanup (point-item 4).
  The block embeds the **journal-provenance invariant** (AGENTS.md): the `"…"` journal entry is
  appended only *after* `_verify_copy` returns.  Any extracted primitive MUST preserve that ordering.
- **`test_pipeline.py` is 10,583 lines** — the largest file in the repo, covering four source modules
  (`_pipeline.py`, `_pipeline_io.py`, `_tags.py`, `_tagger.py`).
- **Four core factory helpers (`_w`, `_rec`, `_rel`, `_trk`) are duplicated verbatim** between
  `test_annotator.py` and `test_pipeline.py`; no `conftest.py` / shared fixture module exists.
- **`audit` is secretly four subcommands** — its `case "audit":` arm dispatches to `audit` /
  `enrich` / `enrich_origin_time` / `diff_journal` via an `if/elif` chain; the three modes
  (`--enrich`, `--origin-time`, `--diff`) are not an argparse mutually-exclusive group.
- **`repath` is the only mass-mutating command with neither a confirmation prompt nor `-y/--yes`**
  (point-items 3+5), despite an epilog warning it "MASS-RELOCATES the entire library."
- **Apparent mis-homings:** `parse_disc_title`/`parse_disc_toc` (in `_pipeline_io.py`, used by
  `_discover.py`); `_write_sidecars`/`_write_freedb_yaml` (in `_pipeline.py`, are sidecar I/O);
  `_tags_from_file_dict` (in `_pipeline.py`, reads tags back from files).

---

## Sessions

The investigation is sharded into one prelude + three axis sessions + one synthesis session.  Each
axis session is read-only and emits a **findings + proposals** report (no code).  The synthesis
session is the HALT where the user reviews proposals and decides what graduates to action.

### A0 — Style-audit substrate (prelude)
Run `/style-audit` (code + doc + test) against `src/` and `tests/`.  Its per-file findings —
especially the **structuring-principle** findings (when to split functions/classes/modules) from
`STYLE-CODE.md` and the test-organization findings from `STYLE-TEST.md` — become factual input to the
axis sessions.  The structural audit then reasons over a clean conformance baseline rather than
re-discovering mechanical violations.
- **Output:** the merged `/style-audit` per-file report, filed as the substrate the axis sessions cite.
- **Note:** `/style-audit` children are partly STUBs; treat missing children as "no findings on that
  axis" and record the gap rather than blocking.

### A1 — App-code organization axis
Judge package/module/class/function structure against *coherent / logical / intuitive / minimal*.
Anchor on the structural facts above.  Questions the report must answer:
- **Module boundaries.**  Should `_pipeline.py` split (maintenance ops out of the ingest module)?
  Should `_pipeline_io.py` split along its abstraction-level seam (raw I/O vs. high-level library
  ops)?  Candidate homes: a `_pipeline_maint.py` (point-item 4's hinted split) and/or an `_audit.py`.
- **`run()` decomposition.**  Is the ~704-line `run()` and its 7-pass work-group loop best served by
  a `WorkGroup`/`ReleaseContext` aggregation object (point-item 1), by extracting the loop body into
  named passes, or left alone?  Note the passes are *sequential within-group* (each consumes the
  prior's accumulators), not independent scans — the aggregation object must respect that ordering.
- **The shared move/verify/journal primitive** (point-item 4): propose the extraction shape that
  preserves the journal-provenance invariant (entry appended only after `_verify_copy` succeeds) and
  the cross-fs EXDEV fallback.  Name the primitive and its signature.
- **`__init__.py` surface** (point-item 2): rule on the `__all__`-plus-`_reexports`-tuple pattern —
  is the test-patch-driven private-helper re-export coherent, or should the test-patch strategy
  change (e.g. patch at the binding module) so the public surface shrinks?
- **Mis-homed helpers:** rule on each (`parse_disc_*`, `_write_sidecars`/`_write_freedb_yaml`,
  `_tags_from_file_dict`, `TrackTags.to_file_dict`'s business logic in `models.py`).
- **Output:** findings + ranked restructuring proposals, each with a cost/blast-radius estimate and
  an explicit "worse-at" tradeoff line.

### A2 — Test-code organization axis
Judge test structure and its **correspondence** to the app code.
- **Cross-cutting test modules.**  `test_pipeline.py` (10,583), `test_main.py` (8,447), and
  `test_annotator.py` (3,592) each span four source modules.  Propose whether (and how) test modules
  should track source modules 1:1, and how any A1 module split forces a matching test split.
- **Factory-helper duplication.**  Rule on the verbatim `_w`/`_rec`/`_rel`/`_trk` duplication and the
  absence of a `conftest.py` — propose a shared fixture home if warranted, weighed against the
  project's typed-factory convention (helpers return real model instances, not raw dicts).
- **Coverage-shape coherence.**  100% branch coverage is enforced; check that the test→source
  mapping keeps each branch's test legible (a branch tested far from its module is a smell).
- **Output:** findings + proposals, explicitly coupled to A1's module-split proposals (a test
  reorganization that contradicts the code reorganization is a defect).

### A3 — CLI command-taxonomy axis
Judge the subcommand set against *intuitive / well-organized / minimal / complete*.
- **`audit`-as-four-commands.**  Decide whether `--enrich` / `--origin-time` / `--diff` should be
  their own subcommands, a mutually-exclusive group, or stay folded — and what "audit" should mean
  as a verb.
- **Confirmation/flag consistency** (point-items 3+5).  Propose the parity fix for `repath`
  (prompt + `-y/--yes`) AND answer the broader question: should all mutating maintenance commands
  (`prune`, `repath`, `regroup`, `unify`, `audit --enrich`, `audit --origin-time`, `rebuild --write`)
  share **one** confirmation helper + dry-run convention rather than each re-implementing it?  Note
  the `--dry-run` default inversion on `rebuild` (defaults to True) as a consistency datum.
- **Flag-home consistency:** `--acoustid-key` and `--no-cache` are defined in two places / missing
  where expected.  Rule on a single common-args home.
- **Completeness/minimality:** does the taxonomy cover the needed functionality with no redundant or
  missing verbs?  Map each subcommand to its `_pipeline`/`_discover` entry point.
- **Output:** findings + a proposed canonical taxonomy (subcommand list + flag matrix), with the
  point-item 3/5 fix called out as the minimal-viable subset shippable independently.

### A4 — Synthesis + HALT (the action boundary)
Merge A0–A3 into one prioritized findings register.  For each proposal: blast radius, dependency on
other proposals (A1 module splits gate A2 test splits gate nothing; CLI taxonomy is mostly
independent), and a recommended ship/defer call.  **HALT for user review.**  Approved proposals
graduate into a sharded action plan; rejected ones return to `BACKLOG.md` with the rationale.
- **Output:** the merged register + a recommended action-plan shard outline.  No code.

---

## Cross-session contracts

*(none frozen yet — this is investigation-only; contracts are proposed at A4 and frozen only when an
action plan is sharded.)*  Two **inviolable invariants** any future action plan inherits from
AGENTS.md / NOTES.md and must not regress:

- **C-PROV (journal-provenance chain).**  A `"copied"`/`"repathed"`/`"regrouped"`/`"unified"` journal
  entry is appended only after the file passes SHA-256 + `_verify_copy`.  Any extracted
  move/verify/journal primitive preserves this ordering exactly.
- **C-DL (defensive-download posture).**  Untouched by this audit (no network code in scope), but any
  CLI/flag change touching fetch paths must preserve the `@_mb_retry` + `_mb_call` two-layer pattern.

---

## Progress ledger

| Session | State | Output artifact |
|---------|-------|-----------------|
| A0 style-audit substrate | **done** | findings inline below (§Findings register) |
| A1 app-code axis          | **done** | §A1 below |
| A2 test-code axis         | **done** | §A2 below |
| A3 CLI-taxonomy axis      | **done** | §A3 below |
| A4 synthesis + HALT       | **done — awaiting user review** | §A4 below |

## Point-item → axis mapping (provenance from BACKLOG.md)

| BACKLOG point-item | Subsumed by | Shippable independently? |
|--------------------|-------------|--------------------------|
| 1. WorkGroup/ReleaseContext object | A1 | only after A1 rules on `run()` decomposition |
| 2. `__init__.py` API surface       | A1 | yes (low blast radius) |
| 3. repath confirmation gap         | A3 | yes — minimal-viable subset |
| 4. move/verify/journal duplication | A1 | yes, but C-PROV-gated |
| 5. destructive-cmd consistency     | A3 | yes (superset of #3) |

---

# Findings register (investigation output, 2026-06-03)

## A0 — Style-audit substrate (conformance baseline)

Mechanical conformance against `STYLE-CODE.md` / `STYLE-TEST.md` is **strong**; the few real
findings are small and feed the axis proposals.

**Source (clean unless noted):** zero >128-char lines; no banned rST type fields; complete public
docstring coverage.  Real findings:
- **`_pipeline.py:1402`** — `case _:` arm in `run()`'s `match ext:` is missing `# pragma: no cover`
  (every analogous block in the file has it).  *One-line fix.*
- Nine private **nested** helpers lack docstrings (closures / `@_mb_retry` `_wrapper`).  Minor.

**Tests:** docstring coverage near-complete; no `cast()`.  Real findings:
- **`Any` in `test_mb_helpers.py`** (lines 356/721/1490/1806 + import at :11) — 4 helper returns use
  `Any`; project bans it.  Fix: `MagicMock` (TYPE_CHECKING) or the `JSON` alias.
- **`unittest.mock.patch` used directly** for `sys.argv` (`test_main.py`, ~70 sites) and `os.environ`
  (`test_mb_helpers.py`) instead of `mocker`/`monkeypatch` — style-rule violation.
- **~56 lines >128 chars** in tests, ~48 from the `# pylint: disable=unused-argument` signature
  pattern (the unreferenced `fs` pyfakefs param).
- **No `conftest.py` anywhere**; `_w/_rec/_rel/_trk` duplicated verbatim across `test_annotator.py`
  + `test_pipeline.py`; `_MINIMAL_FLAC/_MINIMAL_MP3` replicated ×4 with acknowledged
  `# pylint: disable=duplicate-code`.

**Rubric-gap finding (load-bearing for A1/A2):** `STYLE-CODE.md`'s **class / module / package**
structuring sections and `STYLE-TEST.md`'s **structuring-principles** section are **TODO stubs**.
The function-size rubric (60–90 lines) is real and applied below; module/package judgments are made
from first principles because the style files lend no rubric for them.  *(Capture-worthy: the audit
that most needs the module/package rubric is the one that exposed the rubric is absent — see A4
captures.)*

---

## A1 — App-code organization

### Findings (confirmed against current code)

- **F-A1.1 — `_pipeline.py` (2,674 L) concentrates 5 oversized entry points.**  `run` (~702),
  `unify` (~287), `repath` (~286), `regroup` (~265), `enrich` (~174) — none share code with each
  other; co-located by convention, not coupling.
- **F-A1.2 — `_pipeline_io.py` (2,189 L) interleaves three abstraction strata.**  Low-level I/O
  primitives (`_sha256_file`, `_read_tags_flac/mp3`, `_verify_copy`, ~15 `_read_*_tag` readers,
  `write_transaction_log`/`read_journal`); mid-level domain logic (collision/corroboration,
  `parse_disc_*`, fragmentation detection); and high-level entry points (`audit`,
  `enrich_origin_time`, `rebuild_journal`, `diff_journal`, `detect_fragmented_releases`).
- **F-A1.3 — Triple duplication, not double.**  (a) the **move/verify/journal/cleanup** block (~60 L)
  is verbatim across `repath`/`regroup`/`unify`, *including* the EXDEV cross-fs fallback and empty-dir
  cleanup; (b) the **`current_lib` journal-lineage walk** (~30 L) is verbatim across
  `repath`/`regroup`/`enrich`.  Both embed **C-PROV** (journal entry only after `_verify_copy`).
- **F-A1.4 — `run()`'s work-group loop runs ~7 sequential within-group passes** over `group_idxs`
  (movement numbers, intermediate-sibling index, composer unification, recording-date-work,
  first-release normalisation, soloist union).  Each consumes the prior's accumulators — *sequential*,
  not independent scans (gates the point-item-1 design).
- **F-A1.5 — Mis-homed helpers.**  `parse_disc_title`/`parse_disc_toc` (in `_pipeline_io.py`, used by
  `_discover.py`); `_write_sidecars`/`_write_freedb_yaml` (in `_pipeline.py`, are sidecar I/O);
  `_tags_from_file_dict` (in `_pipeline.py`, reads tags back — twins `_read_tags_*` in `_pipeline_io`).
- **F-A1.6 — `__init__.py` (378 L) carries `__all__` (52 names, incl. 4 private helpers) plus a
  `_reexports` tuple of ~70 private names** kept alive only to suppress unused-import lint — a
  test-patch-driven surface (`music_annotator._sha256_file`, …).
- **F-A1.7 — `models.py` (1,576 L)** holds 40+ models spanning three concerns (MB API response types,
  CE-internal types, journal/transaction types); `TrackTags.to_file_dict` is the one model carrying
  non-trivial business logic.

### Proposals (ranked; each names a worse-at)

- **P-A1.a — Extract the shared move/verify/journal primitive** (point-item 4).  Signature sketch:
  `_move_verify_journal(plan_pairs, *, journal_path, action, dest_root, now) -> int`, with a sibling
  `_resolve_current_lib(journal) -> dict[Path, str]` for F-A1.3(b).  **Removes ~180 L of triplication.**
  *Worse-at:* a shared primitive couples three commands to one signature; a future per-command
  variation (e.g. regroup needs `release_id` in the entry) must be threaded as a parameter, slightly
  widening the interface.  **C-PROV-gated** — the extraction must keep entry-append strictly after
  `_verify_copy`.  *Blast: `_pipeline.py` + tests; medium; the single highest-leverage refactor.*
- **P-A1.b — Split `_pipeline.py` along the run/maintenance seam.**  Move `repath`/`regroup`/`unify`/
  `enrich` (+ the P-A1.a primitive) into `_pipeline_maint.py`; `_pipeline.py` keeps `run()` and ingest
  helpers.  *Worse-at:* `__init__.py` re-exports and test patch-targets must update; a reader now
  traverses two files to see all library mutations.  *Blast: `_pipeline.py`, `_pipeline_maint.py`,
  `__init__.py`, test imports; medium-high.  Depends on P-A1.a (extract first, then move the seam).*
- **P-A1.c — Split `_pipeline_io.py` along its abstraction seam.**  Carve the high-level read
  operations (`audit`, `diff_journal`, `detect_fragmented_releases`, the `_audit_*` helpers) into an
  `_audit.py`; keep raw I/O primitives + journal r/w in `_pipeline_io.py`.  *Worse-at:* the audit ops
  still depend on the I/O primitives, so the split adds an import edge without removing coupling;
  risks a thin module if drawn too narrowly.  *Blast: 2 source files + tests; medium.*
- **P-A1.d — `run()` decomposition** (point-item 1).  Extract the work-group unification loop into a
  named `_apply_workgroup_unification(tags_map, all_media_pairs, release)` (~200 L out of `run`), and
  the copy/tag/verify/journal loop into a named pass.  **Prefer named-pass extraction over a
  `WorkGroup`/`ReleaseContext` dataclass** — the passes are sequential-accumulator, so an object buys
  little encapsulation while adding a stateful type threaded through every pass.  *Worse-at:* extracted
  passes need a wide parameter list (the same data the object would have carried); the seam is chosen
  for length, not a crisp single responsibility.  *Blast: `_pipeline.py` + tests; medium.*  **Rules the
  point-item-1 question: lift into an object = NOT YET; extract named passes = YES.**
- **P-A1.e — Re-home the F-A1.5 strays.**  `parse_disc_*` → a `_disc.py` (or `_discover.py`);
  `_write_sidecars`/`_write_freedb_yaml`/`_tags_from_file_dict` → `_pipeline_io.py`.  *Worse-at:* churn
  in import sites + test patch-targets for low functional gain; cosmetic.  *Blast: small; low priority.*
- **P-A1.f — `__init__.py` surface** (point-item 2).  Two options: **(i)** shrink the public surface
  by moving test patch-targets to patch-at-binding-module (`music_annotator._pipeline.apply_tags_flac`
  is already the documented convention — extend it and drop the `_reexports` tuple); **(ii)** keep the
  pattern but document it as deliberate.  *Worse-at (i):* a one-time sweep of every test patch string;
  any external caller relying on `music_annotator._X` breaks (none known — it's a private surface).
  *Blast: `__init__.py` + test patch strings; low-risk, mechanical.*
- **P-A1.g — `models.py` split (optional).**  Carve MB-API models / CE-internal models / journal
  models into `models/` submodules or `_models_mb.py` + `_models_cea.py`.  *Worse-at:* 1,576 L is
  large but cohesive (all Pydantic, low branching); splitting buys navigation but adds import edges
  and risks ordering pitfalls (`MBAttribute` must precede its referents — an existing constraint).
  *Blast: medium; LOW priority — defer unless A4 says otherwise.*

---

## A2 — Test-code organization (coupled to A1)

### Findings

- **F-A2.1 — Three test modules each span ~4 source modules.**  `test_pipeline.py` (10,583 L — largest
  file in the repo) covers `_pipeline`/`_pipeline_io`/`_tags`/`_tagger`; `test_main.py` (8,447 L)
  covers `__main__` + overflow re-tests of `_pipeline_io`/`_discover`; `test_annotator.py` (3,592 L)
  covers `_artists`/`_works`/`_tags`/`_mb_api`.  The implied `tests/unit` ↔ package 1:1 mirror is not
  maintained.
- **F-A2.2 — No `conftest.py`; verbatim factory duplication** (`_w/_rec/_rel/_trk`) — `STYLE-TEST.md`'s
  hoisting rule is explicit and violated.  `_MINIMAL_FLAC/_MINIMAL_MP3` replicated ×4 with an
  *acknowledged* `# pylint: disable=duplicate-code` — a documented self-containment choice in tension
  with the style rule.  The tension must be resolved one way (hoist) or the other (keep + document).
- **F-A2.3 — `@pytest.mark.parametrize` used only 7× across 31k L**; several in-body `for` loops are
  parametrize candidates.  Mild, systematic.
- **F-A2.4 — `STYLE-TEST.md` structuring-principles section is a TODO stub** — no rubric to cite for
  "when to split a test module"; the split judgment is by analogy to A1.

### Proposals

- **P-A2.a — Test reorg TRACKS A1 (dependency, not independent).**  If P-A1.b lands (maint split),
  add `test_pipeline_maint.py`; if P-A1.c lands (`_audit.py`), add `test_audit.py`.  Migrate the
  `repath`/`regroup`/`unify`/`enrich` tests out of `test_main.py`/`test_pipeline.py` to match.
  **A test reorg that contradicts the code reorg is a defect** — sequence test splits *after* the
  corresponding code split in the action plan.  *Worse-at:* large mechanical churn; coverage must stay
  100% branch through the move.  *Blast: high (test line-count), low-risk if done as pure moves.*
- **P-A2.b — Introduce `tests/conftest.py` for the shared factories** (`_w/_rec/_rel/_trk`,
  `_MINIMAL_FLAC/_MINIMAL_MP3`).  *Worse-at:* trades module self-containment (a deliberate, documented
  choice) for DRY; a reader of one test file must now look in conftest for the factory.  **This is a
  genuine values conflict — needs a user ruling** (hoist vs. keep-and-document).  *Blast: small.*
- **P-A2.c — Mechanical test-conformance fixes** (from A0): replace `Any`→`MagicMock`/`JSON`;
  `unittest.mock.patch`→`mocker`/`monkeypatch`; the >128-char `# pylint: disable` lines; parametrize
  the obvious loops.  *Worse-at:* none material; pure conformance.  *Blast: small, independent.*

---

## A3 — CLI command taxonomy

Full report retained in session; the actionable distillate:

### Findings
- **F-A3.1 — `audit` is four commands under one verb** (`__main__.py:749-772`): bare `audit` (read),
  `--diff` (read), `--enrich` (MUTATES tags+journal), `--origin-time` (MUTATES sidecars).  **A read verb
  hosting mutating modes** is the central coherence smell; the modes aren't an argparse
  `mutually_exclusive_group`, and `--dry-run`/`--re-resolve` silently no-op in the wrong modes.
- **F-A3.2 — `repath` is the only mass-mutating command with no prompt and no `-y/--yes`**
  (point-items 3+5); `prune`/`regroup`/`unify` all have both.
- **F-A3.3 — Flag inconsistencies:** `--acoustid-key` defined twice (`_add_common_args` + `audit`);
  `--no-cache` absent from `enrich` (latent); `rebuild --dry-run` defaults **True** (inverted vs. all
  others — a deliberate safety choice).
- **F-A3.4 — 8× duplicated `try/except (KeyboardInterrupt, Exception)` dispatch wrapper**
  (`__main__.py:669-795`); `prune`'s continue-on-error variation is invisible in the structure.
- **F-A3.5 — Doc drift:** module + `main()` docstrings list 7 subcommands; `regroup` is missing.

### Proposals (ranked)
- **P-A3.a (MINIMAL-VIABLE) — `repath` parity:** `yes` param + prompt mirroring `regroup`, + `-y/--yes`
  flag.  *Worse-at:* existing `repath` automation now blocks on stdin without `-y`.  *Blast: 2 files,
  ~30 L.  Ships alone.*
- **P-A3.b — `audit` mode mutual-exclusion** (interim if P-A3.d deferred): declare an
  `mutually_exclusive_group`.  *Worse-at:* can't express "`--re-resolve` requires `--enrich`".  *Blast:
  1 file.*
- **P-A3.c — `_add_acoustid_arg` helper** to de-dup `--acoustid-key`.  *Worse-at:* one more indirection.
  *Blast: 1 file.*
- **P-A3.d — Split `audit` at the read/mutate boundary:** promote `enrich` and `diff` (and an
  origin-time/`migrate-provenance`) to top-level verbs; bare `audit` stays read-only.  *Worse-at:*
  breaks `audit --enrich` muscle memory/scripts; verb count 8→10.  *Blast: `__main__.py` + tests;
  no `_pipeline` change.*
- **P-A3.e — Single dispatch helper** to remove the 8× try/except.  *Worse-at:* must preserve `prune`'s
  continue-on-error and per-command log keys.  *Blast: 1 file.*
- **P-A3.f — `rebuild --write`→`--apply`, keep inverted dry-run default + document.**  *Worse-at:*
  `rebuild` stays inconsistent with the uniform `--dry-run` convention (safety wins).  *Blast: 2 files.*
- **P-A3.g — Merge `regroup`+`unify`→`consolidate --strategy` (DEFER).**  *Worse-at:* loses
  single-strategy control; high blast.  **Depends on P-A1.a/b.**
- **P-A3.h — fix the docstring subcommand drift** (add `regroup`).  Trivial.

---

# A4 — Synthesis, sequencing, and HALT

## Merged register: three independent ship-lanes

The proposals cluster into **three lanes that share almost no files** — they can be sharded as
parallel action sub-tracks, with one hard ordering inside the code lane.

**Lane 1 — Quick conformance + safety (independent, low-risk, ship first).**
`A0` mechanical fixes (`_pipeline.py:1402` pragma; test `Any`/`mock`/line-length — P-A2.c) +
**P-A3.a (repath parity — the point-item-3/5 minimal subset)** + P-A3.b + P-A3.c + P-A3.h.
*No module moves; no C-PROV exposure.  This is the highest value-per-risk and retires 2 of the 5
backlog point-items immediately.*

**Lane 2 — App-code structure (ordered chain; C-PROV-gated).**
`P-A1.a` (extract move/verify/journal + `_resolve_current_lib`) **→** `P-A1.b` (maint split) **→**
`P-A1.c` (`_audit.py` split) **→** `P-A2.a` (test reorg tracking the splits).  Then independently:
`P-A1.d` (run() named-pass extraction; **rules point-item-1 = extract, don't lift an object**) and
`P-A1.f` (`__init__` surface; point-item-2).  *P-A1.a is the lane's keystone and the single
highest-leverage change in the whole audit (~180 L de-duplicated, C-PROV consolidated to one site).*

**Lane 3 — CLI taxonomy (mostly independent of Lane 2).**
`P-A3.d` (split `audit`) + `P-A3.e` (dispatch helper) + `P-A3.f` (`rebuild --apply`).  P-A3.e's
dispatch-table touches the same `__main__.py` as Lane 1; sequence Lane 1 first.  P-A3.g
(`consolidate`) **defers until Lane 2's P-A1.a lands.**

## Dependency graph (action plan)

```
Lane 1 (conformance+safety) ──── independent, ship first
Lane 2:  P-A1.a ─→ P-A1.b ─→ P-A1.c ─→ P-A2.a
              └─→ (P-A1.d, P-A1.f independent of the chain)
Lane 3:  P-A3.d, P-A3.e, P-A3.f  (after Lane 1 on __main__.py)
              └─ P-A3.g  ── waits on Lane 2 P-A1.a
```

## Two decisions that need a user ruling (cannot be defaulted)

1. **Module-split appetite (Lane 2 depth).**  Do we want the structural splits (P-A1.b maint module,
   P-A1.c `_audit.py`) — which improve navigability at the cost of import churn + a multi-file mutation
   surface — or stop at the in-file extraction (P-A1.a + P-A1.d) and leave the mega-modules whole?
2. **Test self-containment vs. DRY (P-A2.b).**  The codebase made a *documented* choice
   (`# pylint: disable=duplicate-code`) to keep test modules self-contained over hoisting shared
   factories.  `STYLE-TEST.md` says hoist.  These conflict; the user owns this values call.

## Recommended action-plan shard outline (for the post-HALT sharded plan)

- **PLAN-audit-action.md, Track Q (Lane 1):** Q1 conformance sweep; Q2 repath parity + audit MEG +
  acoustid helper + docstring drift.  *(2 commit-shaped sessions; retires point-items 3, 5.)*
- **Track S (Lane 2):** S1 `P-A1.a` primitive (C-PROV freeze); S2 `P-A1.b` maint split; S3 `P-A1.c`
  audit split; S4 `P-A2.a` test reorg; S5 `P-A1.d` run() passes (point-item 1); S6 `P-A1.f` `__init__`
  (point-item 2).  *(C-PROV is the frozen contract; S1 is the juncture.)*
- **Track C (Lane 3):** C1 `P-A3.d` audit split; C2 `P-A3.e` dispatch helper + `P-A3.f` rebuild.
  *(P-A3.g held in BACKLOG until S1 lands.)*

**P-A1.g (models split) and P-A3.g (consolidate) return to BACKLOG** as deferred-pending items unless
the user pulls them in.

## HALT — awaiting user review

No code has been written.  The two values decisions above and the lane/track sequencing need user
sign-off before any action plan is sharded.
