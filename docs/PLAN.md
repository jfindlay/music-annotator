<!-- Rolling action frame.  The previous sub-track (repair-turn hardening: journal store, collision completeness,
     duplicate cross-referencing) closed 2026-08-27 with its S8 acceptance gate passed on hades; its plan and ledger
     live in this file's git history (through commit 1b791ab).  This sub-track was derived 2026-08-27 from an operator
     request to unify all idempotent library-maintenance passes behind a single `maintain` action, combined with a
     design ruling that the maintenance passes be treated as a composition of morphisms whose confluence (ordering,
     dry-run fidelity, idempotence across applications, absence of oscillating cycles) is analysed and contracted
     before the umbrella is built.  Rewritten at the next boundary. -->

# PLAN — unified maintenance action: pass-composition confluence + `maintain` umbrella

## Why this sub-track exists

Every recurring library-maintenance capability now exists as a standalone subcommand (`repath`, `regroup`, `unify`,
`enrich`, `origin-time`, `reconstruct-xrefs`, `dedup-library`), plus two one-shot migration subcommands
(`repatch-catalogue-colon`, `repatch-acoustid`) whose library-wide purpose was completed by the S8 hades run, plus a
read-only `preflight` action that dry-runs six of the passes and emits a consolidated report.  The operator wants one
action — `music-annotator maintain <dest>` — that runs the whole recurring maintenance set, with `--dry-run` standing
in for (and superseding) `preflight`.

The request is not a wiring job.  The passes are **morphisms on library state**, and composing them exposes four
confluence properties that must be adjudicated before the umbrella can make honest guarantees:

1. **Non-commutativity.** Order is load-bearing and already documented in-code
   (`_pipeline_maint.py:1956`): tag-content rewrites must precede path-moves because the destination path is *computed
   from* the tags.  The pass set has a required topological order; naive iteration corrupts.
2. **Dry-run fidelity gap (the dangerous one).** `preflight` runs every pass against the *same* unmutated current
   state (`compose_preflight_report`, `_pipeline_maint.py:3504-3509` — six independent `dry_run=True` calls, no state
   threading).  A *live* run feeds pass `k+1` the output of pass `k`.  So for any pass downstream of a mutating pass,
   the dry-run plan is computed against a state that will never exist at that pass's live execution.  The dry-run
   report is truthful only for the first mutating pass and for genuinely independent passes.  A `--dry-run` that
   claims to preview a live `maintain` run is lying unless it either threads a simulated state or explicitly declares
   its guarantee and flags the cascade points.
3. **Decision-divergence.** The two integrity passes (`reconstruct-xrefs`, `dedup-library`) fork the downstream graph
   on operator choice (survivor / keep-both / abort).  Dry-run cannot know the branch taken.
4. **Idempotence across applications + oscillation.** Per-pass single-application idempotence is documented, but
   *composite* idempotence — `f(f(L)) == f(L)` — is not established and does not follow from per-pass idempotence once
   the passes are ordered and state-threaded.  Two hazards: (a) a legitimate multi-application fixpoint (`enrich` adds
   an acoustid this run → `dedup-library` can cluster it only on the *next* run), which is acceptable if declared and
   reported; and (b) an **irreducible inter-pass oscillation** (A→B→A) that never converges — the silently
   counterproductive case.  C-SEQ swap-cycle detection (`_pipeline_maint.py:974,981`) covers intra-pass swap cycles
   only, not inter-pass oscillation.

The operator ruling frames the goal precisely: look for covariances or intersections among the sub-actions that could
lead to contradictory or counterproductive results — silently destructive or incorrect transforms, or irreducible
action cycles across multiple `maintain` applications — and provide basic preparation for the edge cases where
commutativity does not flow with optimal ease.

## Design principle in play (durable vocabulary)

**INSTR (instrument-the-editorial-decision).** A design principle of music-annotator: the tool instruments and
automates human editorial decision points to optimise human efficiency, error-rate, and ergonomics.  An
operator-driven maintenance pass is as legitimate a mode as matching a release to an MBID.  This is why `maintain` is
deliberately **interactive** in its live mode — the C-XREF / C-DEDUP integrity prompts fire inline and are not
suppressed by `--yes` (integrity prompts are not bulk consent), consistent with the passes' existing contracts.

## Cross-session contracts

Frozen at derivation (operator rulings 2026-08-27).  The governing lens is **user ergonomics and archival fidelity,
not a formal composition calculus** (operator ruling): the passes compose informally; `maintain` is judged by whether
the operator's editorial time is well spent and no data is silently harmed, not by whether it reaches a provable
fixpoint in one application.

- **C-MAINTAIN** — the `maintain` action runs the recurring maintenance set (`enrich`, `origin-time`, `repath`,
  `regroup`, `unify`, `reconstruct-xrefs`, `dedup-library`) as a single composition over one dest_root.  The journal
  is read once at the top and threaded in memory through all passes (C-JRNL / the in-memory pattern from the prior
  sub-track); no pass re-reads the journal.  Live mode is interactive: move-confirmation prompts (repath/regroup/
  unify) are suppressible by `-y/--yes`; integrity prompts (reconstruct-xrefs, dedup-library) are **never** suppressed
  by `--yes` (INSTR + C-XREF/C-DEDUP).  `--dry-run` renders every pass report-only — including the two integrity
  passes, which degrade to census-only — and emits the consolidated report that supersedes `preflight`.
- **C-CONFLUENCE** — the pass-composition contract, kept deliberately at the ergonomics/fidelity register (no
  simulated-state theory).  Three plain rulings:
  (a) **Pass order.**  Passes run in a fixed sequence: content-before-path (`enrich`, `origin-time` before the move
      passes), then the move passes, then the integrity passes last (they delete/prompt and nothing downstream may
      depend on their operator-divergent outcome).  The order exists for correctness (a moved path is computed from
      tags, so tags must be corrected first); it is not claimed to be a unique or provably-optimal order.
  (b) **Dry-run is a preview, not a rehearsal.**  `maintain --dry-run` runs each pass against the *current* library
      state (the existing `preflight` behaviour — cheap, reuses existing machinery), so a pass downstream of a
      mutating pass may plan differently in a live run.  This is **accepted and stated to the operator**, not
      engineered away.  The report labels files that appear in more than one pass's plan (the existing cross-pass
      overlap map) as the places where a live run may diverge from the preview.  Deliberately *not* built:
      simulated-state threading that would make dry-run a faithful rehearsal — the token/LoC cost is not worth it when
      the operator can simply re-run (operator ruling).
  (c) **Convergence is "a run that changed nothing."**  `maintain` does not auto-loop and does not compute a formal
      fixpoint.  Its final line states whether the run enacted changes; the operator re-runs `maintain` until a run
      reports zero changes, which is the practical convergence signal.  Some legitimate cases need a second run (e.g.
      `enrich` adds an acoustid this run, so `dedup-library` can cluster it only next run) — this is normal and the
      operator is told, in plain language, that re-running is expected, not a defect.
  No formal oscillation detector is built.  The one concrete oscillation risk — `repath`'s policy-canonical
  disagreeing with `regroup`/`unify`'s consolidation-canonical on a file's home — is handled by the fixed pass order
  and by the operator noticing a run that never reaches zero changes; if such a non-converging cycle is ever observed
  in practice, it is a bug in a specific pass's canonical, fixed there, not a class of behaviour `maintain` polices
  abstractly.
- **C-RETIRE (this instance only)** — no general policy.  The two completed migration commands
  (`repatch-catalogue-colon`, `repatch-acoustid`) are removed this sub-track; their journal action-verbs and every
  reader that interprets them are retained forever (durable content in `docs/NOTES.md` § "Journal action-verbs are
  append-only vocabulary").  **Forward stance (PERM, operator ruling 2026-08-27):** the project prefers *fewer or no*
  temporary library refactors.  A newly-discovered maintenance need is first evaluated as a candidate for the
  permanent action basis — a new durable action, or a refinement/extension of an existing one — and only falls back
  to a throwaway singleton when it genuinely cannot be expressed as durable grammar.  The maturing maintenance grammar
  is meant to *absorb* future needs, not spawn disposable passes.

Inherited unchanged: C-JRNL, C-FATAL, C-XREF, C-DEDUP, C-NOCLOBBER, C-SEQ, C-PROV, C-MOVE, NORM-2-as-revised.

## Sessions

Ordering rationale: S1 confirms the pass order and registers C-CONFLUENCE (already ruled at derivation — a short
write-up, not an open adjudication).  S2 builds the umbrella against that order.  S3 folds the surviving `preflight`
report logic into `maintain --dry-run` and deletes `preflight`.  S4 removes the two completed singletons (commands
only; journal readers retained).  S5 is the operator acceptance gate on hades.  Each build session is one conceptual
unit, `tox -m analyze` green (100% branch coverage, mypy strict, pylint 10.00).

| ID | Type   | Deliverable (commit-title shape)                                                                    | Deps       | Status |
|----|--------|-----------------------------------------------------------------------------------------------------|------------|--------|
| S1 | design | STYLEGUIDE: maintenance-pass order + dry-run-is-a-preview + convergence-is-a-quiet-run (C-CONFLUENCE) | —          | todo   |
| S2 | build  | `maintain` umbrella action: single-composition run, in-memory journal threading, interactive live mode (C-MAINTAIN) | S1         | todo   |
| S3 | build  | Fold preflight report into `maintain --dry-run`; remove `preflight` subcommand + compose_preflight_report | S2         | todo   |
| S4 | build  | Retire completed singletons: remove repatch-catalogue-colon / repatch-acoustid commands; retain journal readers (C-RETIRE) | S2         | todo   |
| S5 | operator | Acceptance gate: `maintain` and `maintain --dry-run` end-to-end on hades                            | S2,S3,S4   | todo   |

### S1 — pass order + ergonomic contract (C-CONFLUENCE)

No code.  Short write-up registering C-CONFLUENCE (above) in `docs/STYLEGUIDE.md`.  The rulings are already made at
derivation; S1 only confirms the pass order against the code and records the plain-language ergonomic contract.  It
does **not** design simulated-state machinery, a formal convergence predicate, or an oscillation detector — those were
ruled out (operator: ergonomics and archival fidelity over computational rigor).

1. **Confirm the pass order** against the actual passes: content-before-path (`enrich`, `origin-time` → move passes),
   move passes (`reconstruct-xrefs` before `dedup-library` so survivors carry known secondaries and dedup does not
   re-prompt; `repath`/`regroup`/`unify` mutual order confirmed against how each computes its canonical), integrity
   passes last.  Record the order and the one-line reason per edge.
2. **State the dry-run contract in plain language:** `maintain --dry-run` previews each pass against current state and
   flags cross-pass-overlap files as "may change after earlier passes run"; it is a preview, not a rehearsal; re-run
   `maintain` if a pass's real input shifts.  No shadow-state build.
3. **State the convergence contract in plain language:** `maintain` reports whether it changed anything; a run that
   changes nothing is "done"; some cases legitimately need a second run and the operator is told so.  No auto-loop, no
   formal fixpoint.

Output: C-CONFLUENCE registered.  S2's scope is fixed and small (option B; no simulated state).

### S2 — `maintain` umbrella (C-MAINTAIN)

Files: `src/music_annotator/_pipeline_maint.py` (new `maintain(dest_root, *, dry_run, yes)` orchestrator + a uniform
pass-adapter seam absorbing the non-uniform signatures — `repatch_acoustid_tags`'s journal-first positional and the
varied return types `DryRunPlan | None`, `list[str]`, `int`, `None`; note the two repatch passes are gone by S4, so
the surviving seam covers `enrich`/`origin-time`/`repath`/`regroup`/`unify`/`reconstruct-xrefs`/`dedup-library`),
`src/music_annotator/__main__.py` (new `maintain` subcommand), `src/music_annotator/__init__.py` (export), tests.  The
journal is read once and threaded in memory (prior sub-track's in-memory pattern); passes execute in the C-CONFLUENCE
order.  A "changed anything?" flag is accumulated across passes for the final convergence line — a plain boolean/count,
not a fixpoint computation.  KATs: composition runs all recurring passes in frozen order (mock-enforced call order);
journal read exactly once (mock-enforced); `-y` suppresses move prompts but NOT integrity prompts (both asserted);
`--dry-run` renders all passes report-only including the two integrity passes (no prompt, no mutation asserted); the
final line reports "changed N" vs "no changes"; a no-change run over a settled fixture reports "no changes" (the
practical convergence signal).  Not built: shadow-state dry-run, oscillation detector (KAT for the
candidate cycle).

### S3 — fold preflight into `maintain --dry-run`; remove preflight

Files: `src/music_annotator/_pipeline_maint.py` (migrate the report-assembly worth keeping — cross-pass overlap map,
journal-capacity projection, Reference/ evidence — into the `maintain` dry-run arm, now extended to the three passes
preflight never covered; delete `compose_preflight_report` and its private helpers once nothing references them),
`src/music_annotator/__main__.py` (delete the `preflight` subcommand + its `_run_preflight` closure),
`src/music_annotator/__init__.py` (drop the export), `src/music_annotator/models.py` (retire `PreflightReport` /
`PreflightPassSummary` / `PreflightOverlapEntry` if unused after migration, or rename to the maintain-report shape),
tests (delete/port the preflight test module).  KATs: `maintain --dry-run` emits overlap map + journal capacity +
reference evidence covering all recurring passes; the `--json PATH` serialisation preflight offered is preserved on
`maintain --dry-run`; no dangling references to removed symbols (import-graph clean).

### S4 — retire completed singletons (C-RETIRE)

Predicate confirmed empirically (operator 2026-08-27): both passes ran library-wide on hades, each twice, second run a
no-op — composite fixpoint reached, purpose complete.

Files: `src/music_annotator/_pipeline_maint.py` (delete `repatch_catalogue_colon` and `repatch_acoustid_tags` pass
functions + their private log helpers), `src/music_annotator/__main__.py` (delete both subcommands + docstring lines
1065-1067), `src/music_annotator/__init__.py` (drop exports at lines 141-142, 223-224; and `is_catalogue_colon_corrupt`
at 168/195 — see below), `src/music_annotator/models.py` (drop the `repatch_*` mentions in the DryRunPlan/pass-summary
docstrings at 1888/1922/1944), tests (delete the corresponding test modules/cases).

**`_works.py` split — verified 2026-08-27:** `rederive_part_label` is the *forward* label rule used by ingest, not
repair scaffolding — it **stays** (confirm the ingest reference holds before touching it).  `is_catalogue_colon_corrupt`
is referenced only by the repair pass and by `rederive_part_label`'s neighbourhood — it goes with the command *if* the
grep after deletion shows no other referent.

**Retention invariant (C-RETIRE) — residual journal-read surface.**  The journal is append-only history: every
`"repatched"` / `"acoustid-repatched"` entry the two commands ever wrote stays in the journal forever, and multiple
readers walk *every* entry.  Deleting the *writers* is safe only if all *readers* still interpret those verbs.  Three
residual sites, all **preservation, no new logic**:

1. **Resolver arm** — `_resolve_current_lib` (`_pipeline_maint.py:540`): the members `"repatched"` /
   `"acoustid-repatched"` in the in-place-update set MUST remain verbatim, with the docstring at `:514`.  These entries
   are source==destination in-place updates; dropping them from the set would silently no-op today but removes the
   documented intent and is a latent trap if the arm logic changes.
2. **Audit/diff/rebuild readers** — `_audit.py` filters on action verbs via allow-lists (`{"tagged", "enriched"}` at
   `:147/193/251/340`) and `action != "tagged"` skips (`:490/544`).  These already exclude the repatch verbs by
   omission, so they need no change — but S4 must *confirm* none of them is a deny-by-default that would misclassify a
   retired verb.  Verify, don't assume.
3. **Model field** — `models.py:1832` `action: str` is a bare string, so historical entries deserialize regardless of
   which verbs the current code emits.  **C-RETIRE trap:** if `action` is ever tightened to a `Literal[...]` union
   (a plausible future hardening), the retired verbs `"repatched"` / `"acoustid-repatched"` (and every other
   historical verb) MUST stay in the union or `model_validate` rejects the hades journal on read.  Record this on the
   field docstring in S4 so the constraint travels with the code.

Only the two pass functions (the writers) are deleted; all three read sites stay.

KATs: resolver still replays both retained action-verbs against a fixture journal containing them; `read_journal` +
`_resolve_current_lib` + `audit` + `rebuild_journal` all round-trip a fixture journal that includes `"repatched"` and
`"acoustid-repatched"` entries with no error and correct resolution; no source or test references the removed commands;
`maintain` does not invoke the removed passes (they are complete, not folded in).

### S5 — operator acceptance gate

Full `maintain` and `maintain --dry-run` on hades.  Acceptance criteria:

- `maintain --dry-run` emits the consolidated report (overlap map, journal capacity, reference evidence) across all
  recurring passes; matches the shape the old `preflight` produced plus the three newly-covered passes.
- `maintain` (live) runs the passes in C-CONFLUENCE order; move prompts honour `-y`; integrity prompts fire regardless.
- The final line reports whether the run changed anything.  Re-running `maintain` until it reports "no changes" reaches
  a settled library in a small number of runs; the operator experience is legible (a run that still changes things says
  so, and re-running is understood as normal, not a defect).
- `preflight`, `repatch-catalogue-colon`, `repatch-acoustid` subcommands are gone; `rebuild`/`audit` still read the
  journal cleanly; the resolver still replays the two retained singleton action-verbs.

On acceptance: continue the repair turn / library-completion arc per ROADMAP, and rewrite this PLAN at the boundary.

## Notes for executors

- **Register rule** (repo AGENTS.md): durable files state the property/invariant, never the plan coordinate.  Anneal
  denylist for this sub-track: `\bS[1-5]\b` (session ids), `unified maintenance action`, `sub-track`, `plan-run`,
  `boundary rewrite`, `umbrella action` (in durable prose; use "the `maintain` action").  Contract names (C-MAINTAIN,
  C-CONFLUENCE, C-RETIRE, INSTR, PERM, C-JRNL, C-FATAL, C-XREF, C-DEDUP, C-NOCLOBBER, C-SEQ, C-PROV, C-MOVE, NORM-*,
  REND-*, EPIST-*) are legitimate durable vocabulary.
- Full gate before declaring any session done: `~/.local/bin/tox -m analyze` (100% branch coverage, mypy strict,
  pylint 10.00/10, ruff, pyupgrade).
- Patch targets bind where the name is imported, not where it originates (repo testing convention).
- C-PROV/C-MOVE provenance chain is inviolable: no journal entry before SHA + `_verify_copy` pass.  `maintain` changes
  the *composition* of passes, never the intra-pass verification ordering.
- The confluence evidence base: `compose_preflight_report`'s overlap map (`_pipeline_maint.py:3540-3554`) already
  identifies cross-pass file intersections — the raw material for the S1 pass-order confirmation and the dry-run
  overlap labels.  The dry-run-is-a-preview behaviour is exactly today's six-independent-dry-run-calls structure
  (`compose_preflight_report`, `:3504-3509`) — reused, not replaced.
- S3 deletion is a coverage cliff: migrate the report logic and its tests into `maintain --dry-run` in the *same*
  commit as the deletion so coverage never dips and no report evidence is lost.
- S4 deletion must NOT touch the journal replay arms.  The exact line is `_pipeline_maint.py:540` (the
  `_resolve_current_lib` action set); `"repatched"` / `"acoustid-repatched"` stay as members.  Delete only the two
  writer functions.  S4 predicate is operator-confirmed (both passes run twice on hades, second run a no-op).

## Progress ledger

VERIFY: `~/.local/bin/tox -m analyze` (combined gate: tests + 100% branch coverage + mypy strict + pylint 10.00 + ruff
+ pyupgrade).  One green run satisfies tests, types, lint, format, and coverage.

| ID | Title                                                                     | Status | Commit | Notes |
|----|---------------------------------------------------------------------------|--------|--------|-------|
| S1 | STYLEGUIDE: maintenance-pass order + dry-run/convergence ergonomics (C-CONFLUENCE) | done   | 9e030e7 | C-CONFLUENCE registered in STYLEGUIDE.md; pass order confirmed against code |
| S2 | `maintain` umbrella action (C-MAINTAIN)                                   | todo   |        |       |
| S3 | Fold preflight report into `maintain --dry-run`; remove preflight         | todo   |        |       |
| S4 | Retire completed singletons; retain journal readers (C-RETIRE)            | todo   |        |       |
| S5 | Acceptance gate on hades                                                  | todo   |        |       |

Frozen contracts: C-MAINTAIN, C-CONFLUENCE, C-RETIRE (frozen at derivation 2026-08-27; C-CONFLUENCE registered — not
adjudicated — at S1); INSTR and PERM (design principles, durable).  C-JRNL, C-FATAL, C-XREF, C-DEDUP, C-NOCLOBBER,
C-SEQ, C-PROV, C-MOVE, NORM-2-as-revised inherited unchanged from the prior sub-track.

## Action-frame digest

(append non-trivial discoveries, contract flexes, and notable texture here as sessions run)

- Derivation (2026-08-27): operator surfaced, then deliberately bounded, the pass-composition question.  The passes
  *do* compose non-trivially (order matters; a dry-run can mispredict a live run; some sequences need a second run),
  but the operator ruled the governing lens is **user ergonomics and archival fidelity, not a formal state/composition
  calculus** — a `maintain` that is imperfect but cost the project ~nothing extra beats a provably-confluent one that
  cost 10M tokens and 15k LoC, even if the operator must run it a few times or think between `--dry-run` and live.
  Rulings: (1) `maintain` is deliberately interactive (INSTR — the tool instruments editorial decisions; an
  operator-run maintenance pass is as legitimate as MBID matching); all recurring passes, including the two integrity
  passes, are unified, accepting that the integrity passes' idempotence kernel is predicated on operator discretion.
  (2) **Dry-run is a preview, not a rehearsal** — reuse today's per-pass-against-current-state behaviour
  (`compose_preflight_report`), label cross-pass-overlap files as "may change," and do NOT build simulated-state
  threading.  (3) **Convergence is a quiet run** — `maintain` reports "changed N" vs "no changes"; the operator
  re-runs to settle; no auto-loop, no formal fixpoint, no oscillation detector.  (4) `preflight` folds into
  `maintain --dry-run` and is removed.  (5) The two completed migration singletons are removed (commands only; journal
  readers retained — the durable content is captured in NOTES § "Journal action-verbs are append-only vocabulary").
  (6) **No general C-RETIRE policy — PERM instead:** the project prefers fewer/no temporary refactors; future
  maintenance needs are evaluated first as candidates for the permanent action basis (new durable action, or refine an
  existing one), the maintenance grammar absorbing needs rather than spawning disposable passes.
- S4 predicate confirmed (2026-08-27): operator ran `repatch-catalogue-colon` and `repatch-acoustid` library-wide on
  hades, each twice, second run a no-op → both at fixpoint, safe to remove.  Removal is clean: the only durable
  retention is `_resolve_current_lib`'s replay of the `"repatched"` / `"acoustid-repatched"` action-verbs
  (`_pipeline_maint.py:540`) — the reader stays, the writers go.  `rederive_part_label` stays (forward ingest rule);
  `is_catalogue_colon_corrupt` goes with the command.
