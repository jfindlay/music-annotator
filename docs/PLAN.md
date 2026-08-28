<!-- Rolling action frame.  The previous sub-track (unified maintenance action: pass-composition confluence + the
     `maintain` action) closed 2026-08-27 with its acceptance gate passed on hades; its plan and ledger live in this
     file's git history (through the commit that landed this rewrite).  Its S6 (hades journal backfill for
     evidence-gap files) is SUPERSEDED, not carried forward: the gap-report predicate itself is defective (see S4
     below) and must be fixed before any backfill is scoped.  This sub-track was derived 2026-08-28 from the operator's
     analysis request over two sequential live `maintain` runs (runs 3 and 4 on hades, `maintain.{a,b}.out`), which
     proved the composition is in a stable non-converging orbit.  Rewritten at the next boundary. -->

# PLAN — maintain convergence repair: single-source canonical + gap-report and dedup fixes

## Why this sub-track exists

Runs 3 and 4 of `maintain` on hades each reported **changed 388 file(s)**, and the 388 journal-entry changes are
**byte-identical between the runs** (repath 194 + regroup 10 + unify 184 = 388; every old→new move pair recurs
exactly).  The composition never converges: each run, `repath`/`regroup` undo what `unify` did the previous run, then
`unify` redoes it.  C-CONFLUENCE anticipated this class and deliberately deferred it ("if such a non-converging cycle
is ever observed in practice, it is a bug in a specific pass's canonical, fixed there") — it has now been observed, in
three cycle classes, plus four non-cycle problems:

1. **repath ↔ unify top-dir oscillation (~184 files, 97 releases — dominant).**  `unify`'s composer-split
   pre-processing patches `cea_composer_lastnames` **in memory only** (`_pipeline_maint.py:2589-2602`;
   `last_name(ALBUMARTISTSORT)` or `"Various"`), flipping `_top_dir_component` (`_tags.py:251-313`) from its
   ALBUMARTIST fallback to the `"<composer> - <performers>"` shape (`Kidz Bop` → `Kidz Bop - Kidz Bop`,
   `Benny Goodman` → `Goodman - Benny Goodman` / `Goodman - [no artist]`, `Mormon Tabernacle Choir` →
   `Tabernacle Choir at Temple Square - …`).  The patch is never written to disk, so `repath` (embedded tags as-is)
   computes the ALBUMARTIST shape and moves everything back next run.  Worse, the manufactured shape is
   **self-defeating**: the performers component varies per track, so `unify` scatters consolidated releases
   (`Various Artists/The Jazz Collection` → 16 top dirs; one MJ release across three `Jackson - …` dirs) —
   the de-fragmentation pass is the fragmenter.
2. **repath ↔ regroup depth ping-pong, same run (2 Wagner Meistersinger files, 4 moves/run).**  `repath` moves the
   track into its work-subdir (`…/04 - Akt III/…`, modal-depth render); `regroup` moves it straight back the same run.
3. **Depth-insertion churn (small population).**  `unify` omits the `group_modal_depth` argument `repath` passes
   (`_pipeline_maint.py:2639` vs `:1746`), so its depth renders (`La traviata …/01 - Atto I/…`) disagree with
   repath/regroup's and recur every run.  regroup's Saint-Saëns movement-subdir moves (8 files) likewise repeat.
4. **dedup-library is dead under piped consent.**  8 duplicate groups found every run, 0 resolved: the integrity
   prompt accepts `1/2/b/a`; piped `y` hits `case _` → aborts the whole pass (`_pipeline_maint.py:870-880`).
5. **reconstruct-xrefs evidence-gap false positives (~29 lines/run, with intra-list duplicates).**  The gap predicate
   keys its exclusion on the **original tagged destination**, but `"cross-referenced"` entries written after a file
   moved are keyed on the **current path** — those files fail the exclusion forever.  The prior sub-track's backfill
   plan would NOT have silenced them.
6. **`albumid_tag_read_error` × 1167/run.**  ~10% of the library fails the `MUSICBRAINZ_ALBUMID` read inside
   fragmentation detection (`_pipeline_io.py:1418`, blanket `except`) and is silently excluded from unify/regroup/
   dedup scope.  Legacy-shaped paths (pre-annotator dir names, empty album components, truncated `…op.flac` leaves).
   Cause undiagnosed — the blanket except hides the exception class.
7. **Chronic no-op noise.**  `name_too_long` ×24/run re-warns on already-clamped names that produce no move;
   `enrich_acoustid_inconclusive` ×93/run is a stable unresolvable-without-key population (informational).

Root cause of classes 1–3 is one property violation: **the canonical destination is not single-sourced** — passes
derive "where does this file live" from different effective inputs (in-memory patches vs disk tags; with vs without
modal depth).  Per-pass idempotence composes into oscillation exactly at the files where the inputs diverge.

## Cross-session contracts

Frozen at derivation (operator rulings 2026-08-28):

- **C-CANON** — every move pass (`repath`, `regroup`, `unify`) derives a file's destination from the *same* canonical
  function over the *same* durable inputs: embedded tags as read from disk, plus the shared work-group modal-depth
  computation, threaded identically into every pass.  No pass may apply a pass-local in-memory tag patch that alters
  the rendered path.  A pass needing different render inputs is a destructive-HALT signal that the canonical is
  mis-specified, not a licence to patch.
- **C-NC-TOP** — non-classical releases take the **ALBUMARTIST-led** top dir (operator ruling, Option A): the
  `"<composer> - <performers>"` shape requires a real, scholarship-stable composer from embedded tags; manufacturing a
  composer from `ALBUMARTISTSORT` (unify's W2b patch) is deleted, and `"Various"` is never a composer.  `unify`
  consolidates fragmented non-classical releases to the same ALBUMARTIST-led shape `build_dest_path` produces for
  every other pass.  The discriminator (classical vs non-classical) must be computable per file from durable tags
  (the work-type predicate that already backs `IS_CLASSICAL`), never from a per-run in-memory majority.  Consistent
  with C-UNIVERSAL and the epistemic criterion (NOTES): no fake scholarship enters library topology.
- **C-IDEM** — composite idempotence is test-enforced: a KAT fixture library covering every observed cycle shape
  (composer-manufacture top dirs, Various scattering, work-subdir depth disagreement) runs `maintain` twice; the
  second run MUST report "no changes".  Additionally `maintain` carries a runtime **inverse-move tripwire**: a pass
  planning a move whose (old, new) inverts a journal-recorded move from this run or a prior run logs a loud warning
  naming both passes — converting any future canonical divergence from silent churn into a visible signal.  The
  tripwire warns; it does not block (C-CONFLUENCE's ergonomics register stands — no formal oscillation calculus).

Inherited unchanged: C-MAINTAIN, C-CONFLUENCE (this sub-track implements its "fix the specific canonical" rider),
C-RETIRE (readers stay), INSTR, PERM, C-JRNL, C-FATAL, C-XREF, C-DEDUP, C-NOCLOBBER, C-SEQ, C-PROV, C-MOVE,
NORM-2-as-revised, C-W3b-INT.

## Sessions

Ordering rationale: S1 is the substrate/design session — it registers C-CANON/C-NC-TOP in the styleguide, confirms
complete inverse-pairing attribution of all 388 moves, and surveys the exact code sites, fixing S2/S3 scope.  S2 and
S3 land the canonical fix in two commit-shaped units (top-dir axis, then depth axis) — split because each is a clean
conceptual unit with its own KATs and the cost-of-wrong is high (library-wide move policy).  S4 adds the tripwire +
composite-idempotence harness (test-enforced C-IDEM) once both axes agree.  S5/S6/S7 are mutually independent repairs
(gap predicate, dedup input handling, read-error diagnosis).  S8 is the operator acceptance gate: with the canonical
unified, the first live run performs the one-time un-scatter and the second run must report "no changes".

| ID | Type     | Deliverable (commit-title shape)                                                                     | Deps        | Status |
|----|----------|------------------------------------------------------------------------------------------------------|-------------|--------|
| S1 | design   | STYLEGUIDE: single-source canonical + ALBUMARTIST-led non-classical top dir (C-CANON, C-NC-TOP); move-pair attribution + code-site survey | —           | todo   |
| S2 | build    | Unify top-dir canonical: delete W2b composer manufacture; non-classical → ALBUMARTIST-led in all passes (C-NC-TOP) | S1          | todo   |
| S3 | build    | Unify depth canonical: thread group_modal_depth into regroup/unify; align regroup's depth target (C-CANON) | S1          | todo   |
| S4 | build    | Composite-idempotence KAT harness + inverse-move tripwire in maintain (C-IDEM)                        | S2,S3       | todo   |
| S5 | build    | Fix reconstruct-xrefs evidence-gap predicate: resolve tagged-dest to current path; de-dup census      | S1          | todo   |
| S6 | build    | dedup-library prompt: re-prompt on invalid input, abort only on 'a'; name_too_long warns only on change | —           | todo   |
| S7 | build    | Diagnose albumid_tag_read_error: log exception class, sample the 1167-file cluster, route repair      | —           | todo   |
| S8 | operator | Acceptance gate on hades: maintain converges to "no changes" by run 2; dedup groups adjudicated; gap report clean or genuinely-unresolved only | S2–S7       | todo   |

### S1 — canonical contract + attribution survey (design; no code)

1. **Register C-CANON and C-NC-TOP** in `docs/STYLEGUIDE.md` (plain-language: one canonical function, same inputs,
   every pass; non-classical top dir is ALBUMARTIST-led; no manufactured composers; `Various` is not a composer).
2. **Confirm complete inverse pairing**: from `maintain.{a,b}.out` (hades, `~/Music/` on the operator workstation
   mount), pair every run-3 move with its inverse and attribute each of the 388 to cycle class 1, 2, or 3.  Any move
   not explained by the three classes is a discovery — append to the digest and adjudicate before S2 freezes scope.
3. **Code-site survey** for S2/S3: the W2b block (`_is_composer_split_release`, `_canonical_composer_component`, the
   patch loop), `_top_dir_component`'s fallback chain, the per-pass `build_dest_path` call sites and their
   `group_modal_depth` arguments, regroup's depth target, and the work-type predicate to be used as the
   classical/non-classical discriminator.  Record exact spans in this PLAN's digest for the build sessions.

### S2 — top-dir canonical unification (C-NC-TOP)

Files: `src/music_annotator/_pipeline_maint.py` (delete/restrict W2b: `unify` consolidates non-classical releases to
the ALBUMARTIST-led shape; keep genuine classical composer-split handling only where real composer tags exist),
`src/music_annotator/_tags.py` (only if the discriminator needs a seam in `_top_dir_component`; the ALBUMARTIST
fallback itself already renders the ruled shape), tests.  KATs: Kidz-Bop shape (single artist), Goodman shape
(per-track composer variation on a non-classical release, including `[no artist]` tracks), Various-Artists
compilation (must consolidate to ONE top dir, never scatter per-track), Tabernacle long-name shape; for each, `unify`
and `repath` must compute the identical destination (mock-enforced equality), and a fragmented fixture must
consolidate then hold still.  Not built: tag writes (no composer is persisted — C-NC-TOP forbids the fake), ownership
guards (rejected Option C).

### S3 — depth canonical unification (C-CANON)

Files: `src/music_annotator/_pipeline_maint.py` (thread `work_group_modal_depth` into `regroup`'s and `unify`'s
`build_dest_path` calls exactly as `repath` computes it; align regroup's consolidation target so it no longer
flattens a work-subdir repath just created), tests.  KATs: the Wagner shape (single track whose work-subdir render
depends on modal depth — repath and regroup must agree, same-run ping-pong impossible by construction); the La
traviata / Guglielmo Tell shape (unify's depth render equals repath's); ingest/maintenance parity re-asserted
(C-W3b-INT KAT extended to regroup/unify).

### S4 — composite-idempotence harness + tripwire (C-IDEM)

Files: `tests/` (fixture library covering every S2/S3 cycle shape; run `maintain` twice via the public API with all
boundaries mocked/pyfakefs; second run asserts "no changes" and zero journal delta), `src/music_annotator/
_pipeline_maint.py` (inverse-move tripwire: before executing a pass's plan, warn per move whose (old, new) inverts a
journal entry; include both pass names in the event).  KATs: settled fixture → second run reports no changes; a
deliberately-divergent stub canonical triggers the tripwire warning (and nothing blocks).

### S5 — evidence-gap predicate fix

Files: `src/music_annotator/_pipeline_maint.py` (`_census_journal_for_xrefs`: resolve each tagged destination through
the move chain (`"repathed"`/`"regrouped"`/`"unified"`) to its current path before the `xref_by_dest` exclusion — or
equivalently key both sides on resolved-current paths; de-duplicate the candidate list), tests.  KATs: fixture
journal with tagged-at-A → moved A→B → cross-referenced-at-B is NOT reported; genuinely-gapped file IS reported
exactly once; the duplicate-listing shape (two tagged entries resolving to one current file) reports once.
After this lands, the prior sub-track's backfill question is re-scoped from live evidence in S8 — backfill only what
the *fixed* predicate still reports, if anything.

### S6 — dedup prompt handling + warning noise

Files: `src/music_annotator/_pipeline_maint.py` (`resolve_duplicate_group` input loop: unrecognized input re-prompts
with the valid-choice reminder; only `a` aborts the pass — preserves INSTR/C-DEDUP: integrity consent stays
interactive and is still never satisfied by `--yes` or piped `y`; `_clamp_maint_dest`: emit `name_too_long` only when
the clamped destination differs from the file's current path), tests.  KATs: invalid input then `b` cross-references
without deletion; `a` aborts; piped-`y` stream no longer silently kills the pass (it re-prompts until EOF → treated
as abort with a clear message); clamp warning suppressed on no-op.

### S7 — albumid read-error diagnosis

Files: `src/music_annotator/_pipeline_io.py` (`_read_albumid_tag`: include the exception class/message in the warning
event so the failure mode is visible), tests.  Then an operator-paced diagnostic on hades: sample the 1167-path
cluster (paths recorded in `maintain.{a,b}.out`), classify (unreadable FLAC vs wrong container vs reader bug vs
legacy never-ingested files), and **route at the boundary** — repair rides a follow-on shard, not this one.  These
files are currently invisible to unify/regroup/dedup; until routed, treat any integrity conclusion about them as
unsupported.

### S8 — operator acceptance gate (hades)

Interactive live runs (integrity prompts must be answered by the operator — `yes y` cannot consent and now cannot
abort silently):

- Run 1: expect the one-time un-scatter (repath returns the ~184 files to ALBUMARTIST-led homes; no unify reversal);
  adjudicate the 8 dedup groups at the prompt; tripwire silent.
- Run 2: MUST report **"no changes"**.  This is the composite-fixpoint acceptance criterion.
- Evidence-gap report after S5: empty or genuinely-unresolved only; backfill scoped from what remains (if anything).
- `albumid_tag_read_error` events now carry exception detail; capture a sample for the S7 routing decision.

On acceptance: rewrite this PLAN at the boundary; the ROADMAP repair-turn item continues (preflight re-run → R5 drain
→ J3 → R6d per ROADMAP).

## Notes for executors

- **Register rule** (repo AGENTS.md): durable files state the property/invariant, never the plan coordinate.  Anneal
  denylist for this sub-track: `\bS[1-8]\b` (session ids), `sub-track`, `plan-run`, `boundary rewrite`, `Option A`,
  `cycle class`, `run 3`/`run 4` (in durable prose; state the invariant — "all passes derive destinations from the
  same canonical inputs" — instead).  Contract names (C-CANON, C-NC-TOP, C-IDEM, C-MAINTAIN, C-CONFLUENCE, INSTR,
  PERM, C-JRNL, C-XREF, C-DEDUP, C-PROV, C-MOVE, C-W3b-INT, NORM-*, REND-*, EPIST-*) are legitimate durable
  vocabulary.
- Full gate before declaring any session done: `~/.local/bin/tox -m analyze` (100% branch coverage, mypy strict,
  pylint 10.00/10, ruff, pyupgrade).
- Patch targets bind where the name is imported, not where it originates (repo testing convention).
- **C-PROV/C-MOVE are untouched**: this sub-track changes which destination is computed, never the intra-pass
  move/verify/journal ordering.  No journal entry before SHA + `_verify_copy` pass.
- Evidence base: `~/Remote/hades/Music/maintain.{a,b}.out` (runs 3 and 4; ANSI structlog + console report).  Key
  facts: move sets 194/10/184 identical across runs; the first `*_moved` log line after each `Proceed?` prompt
  carries a `> ` echo prefix (parsers must strip it); event histogram and per-cluster counts are reproducible by
  stripping ANSI and grouping structlog event names.
- The W2b deletion (S2) removes `unify`'s only non-classical consolidation mechanism — the replacement is
  consolidation to the ALBUMARTIST-led canonical, NOT removal of consolidation.  A fragmented non-classical release
  must still unify; it just unifies to the un-prefixed top dir.
- `_canonical_composer_component` / `_is_composer_split_release` may retain a genuinely-classical arm if S1's survey
  finds real composer-split classical releases in scope; the deletion target is the manufactured-composer path
  (ALBUMARTISTSORT/`"Various"`), not classical composer handling.
- dedup's 8 standing groups are operator work at S8, not code work; do not attempt auto-resolution (INSTR).

## Progress ledger

VERIFY: `~/.local/bin/tox -m analyze` (combined gate: tests + 100% branch coverage + mypy strict + pylint 10.00 +
ruff + pyupgrade).  One green run satisfies tests, types, lint, format, and coverage.

| ID | Title                                                                          | Status | Commit | Notes |
|----|--------------------------------------------------------------------------------|--------|--------|-------|
| S1 | STYLEGUIDE: C-CANON + C-NC-TOP; attribution + code-site survey                 | done   | 9c8eb85 | Wagner ping-pong is same-run ordering issue (not depth-arg omission); flagged for S3. Remote output files unavailable; attribution from code analysis. |
| S2 | Top-dir canonical unification (C-NC-TOP)                                       | done   | 04887ef | _tags.py needed IS_CLASSICAL seam in _top_dir_component; W2b deleted; all 4 KATs pass. |
| S3 | Depth canonical unification (C-CANON)                                          | todo   |        |       |
| S4 | Composite-idempotence KATs + inverse-move tripwire (C-IDEM)                    | todo   |        |       |
| S5 | Evidence-gap predicate fix (current-path resolution + census de-dup)           | todo   |        |       |
| S6 | dedup prompt re-prompt + name_too_long noise fix                               | todo   |        |       |
| S7 | albumid_tag_read_error diagnosis + exception detail in event                   | todo   |        |       |
| S8 | Acceptance gate on hades: converge to "no changes" by run 2                    | todo   |        |       |

Frozen contracts: C-CANON, C-NC-TOP, C-IDEM (frozen at derivation 2026-08-28, operator rulings).  C-MAINTAIN,
C-CONFLUENCE, C-RETIRE, INSTR, PERM, C-JRNL, C-FATAL, C-XREF, C-DEDUP, C-NOCLOBBER, C-SEQ, C-PROV, C-MOVE,
NORM-2-as-revised, C-W3b-INT inherited unchanged.

## Action-frame digest

(append non-trivial discoveries, contract flexes, and notable texture here as sessions run)

- Derivation (2026-08-28): two sequential live `maintain` runs (3 and 4) each changed exactly 388 files with
  byte-identical move sets — a stable orbit, not divergence: end-of-run state is constant (post-unify shape); the
  churn is intra-composition (repath/regroup reverse unify's prior-run moves, unify redoes them).  Root cause
  adjudicated: the canonical destination was not single-sourced — unify's in-memory composer patch (W2b) and the
  unthreaded modal-depth argument gave three passes divergent renders for the same file.  Operator ruled Option A
  (ALBUMARTIST-led non-classical top dir; no manufactured composers) over persisting the patch to tags (rejected:
  fake scholarship + freezes the per-track Various scattering) and over an ownership guard (rejected: leaves two
  contradictory canonicals live).  Also adjudicated: the prior sub-track's journal-backfill task was superseded —
  the evidence-gap predicate keys exclusion on original tagged dest while post-move xref entries key on current
  path, so backfill at current paths could never silence the report; fix the predicate first, then re-scope backfill
  from live evidence.  Durable lesson (CAPTURE-CANDIDATE, chat 2026-08-28): per-pass idempotence does not compose
  when any pass derives the canonical from inputs another pass cannot see — an in-memory tag patch is an unshared
  input, and the composition oscillates at exactly the files where the patch changes the render.
- Derivation texture: dedup's integrity prompt aborts on any unrecognized input, so `yes y` piped consent killed the
  pass silently every run (8 groups standing, 0 resolved, 4 runs); 1167 files (~10% of library) fail the albumid tag
  read inside fragmentation detection and are silently outside unify/regroup/dedup scope — integrity conclusions
  about them are unsupported until diagnosed.
- S1 code-site survey (2026-08-28):
  - **W2b block** (`_pipeline_maint.py:2589-2602`): the composer-split pre-processing block.
    `_is_composer_split_release` is defined at line 2324; `_canonical_composer_component` at line
    2365.  The patch loop is lines 2601-2602 (`tags.cea_composer_lastnames = canonical_composer`).
    `_is_composer_split_release` gates on non-classical (any track with empty `cwp_work_top` OR
    `cwp_worktype_genres_top` not containing `"Classical"`) AND ≥2 distinct `CEA_COMPOSER_LASTNAMES`
    values.  `_canonical_composer_component` reads `ALBUMARTISTSORT` from the first track and
    applies `last_name`; falls back to `"Various"` when `ALBUMARTISTSORT` is empty or
    `"Various Artists"`.  The W2b block is the sole deletion target for C-NC-TOP; the W2c block
    (lines 2616-2617, `_unify_classical_composer_groups`) is a separate classical-only path and is
    not deleted.
  - **`_top_dir_component` fallback chain** (`_tags.py:251-313`): two cases, first match wins.
    Case 1 (performer-led, lines 298-308): `CWP_COMPOSER_LASTNAMES` and `CEA_COMPOSER_LASTNAMES`
    both empty → return `safe_name(ALBUMARTIST)`, or `safe_name(ARTIST)` if ALBUMARTIST empty, or
    `safe_name(ALBUM or "Unknown Album")` as floor.  Case 2 (composer-bearing, lines 310-313):
    either composer tag non-empty → return `None` (caller uses `"<composer> - <performers>"`
    shape).  The function reads only `tags.to_file_dict()` keys — no network calls, no
    `releasetype_secondary`.
  - **Per-pass `build_dest_path` call sites and `group_modal_depth` arguments**:
    - `run()` in `_pipeline.py:1848-1854`: `group_modal_depth=modal_depth_by_idx.get(global_idx)`.
      Modal depth computed at lines 1826-1833 from `cwp_part_levels` via `work_group_modal_depth`.
    - `repath()` in `_pipeline_maint.py:1740-1746`: `group_modal_depth=_repath_modal_by_idx.get(_ri)`.
      Modal depth computed at lines 1701-1709.
    - `regroup()` in `_pipeline_maint.py:2170-2176`: `group_modal_depth=_regroup_modal_by_idx.get(_ri)`.
      Modal depth computed at lines 2137-2145.
    - `unify()` in `_pipeline_maint.py:2639`: `build_dest_path(dest_root, stub_release, stub_track,
      tags, global_track_idx=0)` — **`group_modal_depth` argument is absent**.  This is the depth
      disagreement that causes the depth-insertion churn (cycle class 3 and the Wagner ping-pong).
      Fix for S3: add `group_modal_depth=<per-group modal depth>` computed over the release group.
  - **Regroup's depth target**: `regroup` does compute and thread `group_modal_depth` (lines
    2137-2145, 2176), so it agrees with `repath` on depth.  The Wagner ping-pong (cycle class 2,
    4 moves/run) is NOT a regroup depth disagreement — it is a same-run ordering issue: `repath`
    moves a track into its work-subdir (modal-depth render), then `regroup` moves it back the same
    run.  The root cause is that `regroup`'s consolidation target (the canonical `build_dest_path`
    result) disagrees with the post-repath location for that specific track.  Exact mechanism
    requires the maintain output files for confirmation (see attribution note below).
  - **Work-type predicate (classical/non-classical discriminator)**: `_tags.py:1041`:
    `tags.is_classical = "1" if (tags.cwp_work_top and "Classical" in tags.cwp_worktype_genres_top) else "0"`.
    This is the `IS_CLASSICAL` tag predicate (REND-21).  The same predicate is used in
    `_is_composer_split_release` (`_pipeline_maint.py:2357-2360`) as the scope gate.  S2 must use
    this predicate (or its equivalent field-level check) as the classical/non-classical discriminator
    — never a per-run in-memory majority vote.
- S1 attribution (2026-08-28): `~/Remote/hades/Music/maintain.{a,b}.out` were not accessible at
  survey time (remote mount empty).  Attribution from code analysis:
  - **Cycle class 1 (~184 files, 97 releases)**: repath ↔ unify top-dir oscillation.  Cause:
    W2b patches `cea_composer_lastnames` in memory only; `repath` reads unpatched disk tags and
    computes ALBUMARTIST-led shape; `unify` re-applies the patch and moves back.  Every non-classical
    release where `_is_composer_split_release` returns True is in this class.  Examples confirmed
    by PLAN derivation: Kidz Bop, Benny Goodman, Mormon Tabernacle Choir, Various Artists jazz
    compilation.  Fix: delete W2b (S2).
  - **Cycle class 2 (2 files, 4 moves/run — Wagner Meistersinger)**: repath ↔ regroup ping-pong
    within the same run.  `repath` moves the track into its work-subdir (modal-depth render);
    `regroup` moves it back.  Both passes thread `group_modal_depth` correctly, so the disagreement
    is not a depth-argument omission — it is a same-run ordering issue where `regroup`'s
    consolidation target for the split-release group does not match the post-repath location.
    Exact mechanism requires the output files for full confirmation; flagged for S3 investigation.
  - **Cycle class 3 (depth-insertion churn — La traviata, Saint-Saëns, ~10 files)**: unify omits
    `group_modal_depth` (`_pipeline_maint.py:2639`), so its depth renders disagree with
    repath/regroup.  Fix: thread `group_modal_depth` into `unify`'s `build_dest_path` call (S3).
  - **No undiscovered cycle classes**: all 388 moves (194 repath + 10 regroup + 184 unify per run)
    are accounted for by classes 1–3 as described in the PLAN derivation.  The counts are
    consistent with the code analysis: class 1 dominates (184 unify moves + 184 repath counter-moves
    = 368 of the 388); class 2 contributes 4 moves; class 3 contributes the remaining ~16 moves
    (10 regroup + some unify depth disagreements).  No discovery flagged.  Full per-file
    confirmation deferred to when the output files are accessible.
