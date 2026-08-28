<!-- Rolling action frame.  The previous sub-track (maintain convergence repair: single-source canonical + gap-report
     and dedup fixes) closed 2026-08-28 with all sessions done; its plan and ledger live in this file's git history
     (through the commit that landed this rewrite).  Its S8 acceptance ("maintain converges to no changes by run 2")
     is REOPENED, not carried forward as passed: two post-acceptance live runs on hades (`maintain.{d,e}.out`,
     2026-08-28 15:20/15:24) are byte-identical modulo timestamps and each reports **changed=246** — a residual
     stable orbit of 123 repath moves exactly inverted by 123 unify moves, with the C-IDEM tripwire firing
     `inverse_move_detected` ×246/run precisely as designed.  This sub-track was derived 2026-08-28 from the
     operator's analysis request over those runs.  Rewritten at the next boundary. -->

# PLAN — maintain convergence repair, second round: delete the last in-memory render patch

## Why this sub-track exists

The prior sub-track's repair deleted the non-classical composer manufacture and threaded modal depth — and the two
spots it deliberately spared are exactly where the residual oscillation lives.  Evidence (`maintain.{d,e}.out`,
hades): each run `repath` moves 123 files and `unify` moves the **same 123 files back** (the unify plan is the exact
inverse of the repath plan, 123/123, verified by normalized-log diff); the two runs' outputs are byte-identical
modulo timestamps, so the library is in a stable orbit with `changed=246` forever.  Two divergence classes:

1. **Classical composer-chain patch (112 of 123 moves — dominant).**  `_unify_classical_composer_groups`
   (`_pipeline_maint.py:2455`, called only from `unify` at `:2659`) patches `cea_composer_lastnames` **and**
   `cwp_composer_lastnames` **in memory only** before `build_dest_path`, propagating the fullest ("; "-richest)
   chain across each work group.  `repath` renders the raw embedded per-file chains.  The patch is never written to
   disk, so the two passes disagree forever: repath moves `Mozart; Süßmayr - …` files to `Mozart - …` (37 moves,
   the K.626 completer shape), unify moves them back.  This is a direct violation of frozen C-CANON ("no pass may
   apply a pass-local in-memory tag patch that alters the rendered path") that survived the prior deletion because
   the survey classified it as genuine classical handling rather than manufacture.  Two aggravating defects,
   confirmed in code:
   - **The claimed scope gate does not exist.**  The call-site comment (`:2656-2658`) asserts a classical gate
     "enforced inside" the function; the body has none — it groups by `cwp_workid_top or musicbrainz_workid` and
     skips only empty IDs.  Jazz and musical releases with MB work IDs are chain-patched (Goodman
     `Waller; Kander` → `Goodman; Sampson; Kander`; the MJ-musical moves), and patching chains onto files whose
     embedded composer tags are **empty** flips their top dir from ALBUMARTIST-led to composer-led — a C-NC-TOP
     violation through the back door (`Michael Jackson/…` → `Bahler - The Andraé Crouch Choir/…`).
   - **The chooser clobbers real scholarship.**  All concertos of the 16 Konzerte release share one top-work MBID,
     so fullest-chain/first-appearance rewrites `Vivaldi; Bach` → `Bach; Marcello` (18 moves) — flattening
     genuinely different arrangement sources.  Persisting this to tags would bake fake data into the library.
2. **Modal-depth group-membership asymmetry (~11 moves).**  `repath` (and `regroup`, per the prior repair) compute
   `group_modal_depth` over **library-wide** `cwp_workid_top` groups (`:1784-1799`); `unify` computes it over
   **the fragmented release's files only** (`:2684-2689`).  Same durable inputs, different denominators → repath
   collapses a work subdir, unify re-inserts it, every run (the Saint-Saëns op. 78 / La traviata / Guglielmo Tell /
   Walküre Akt-subdir moves — same-composer pairs differing only at the work-subdir level).

Root cause is the same single property the prior sub-track named: the canonical destination is still not
single-sourced.  A group-scope canonicalization is shared only when **both the function and the group membership**
are identical across passes — computing the same statistic over different denominators diverges exactly like an
in-memory patch.

**Operator ruling (2026-08-28, this derivation):** delete the composer-chain patch outright (over persist-to-tags
and over symmetrizing the patch into other passes).  After deletion, genuinely divergent embedded chains render
distinct top dirs (K.626 movements split across `Mozart - …` and `Mozart; Süßmayr - …`) — this is accepted as the
truthful render of current tags.  Chain repair, if wanted, belongs to **re-annotation** (which writes real
per-track MB data through the full verify chain), never to maintenance-time render patching.  Tracked in BACKLOG,
out of scope here.

## Cross-session contracts

Inherited frozen, now enforced in full: **C-CANON** (this sub-track deletes the last surviving pass-local in-memory
render patch and extends "threaded identically into every pass" to the modal-depth **membership**, not just the
argument), **C-NC-TOP** (the discriminator becomes per-file-durable again once chain patching is gone), **C-IDEM**
(tripwire stays warn-only; the composite harness gains the two observed residual shapes).

New this sub-track:

- **C-GROUPSCOPE** — any group-scope statistic that feeds `build_dest_path` (work-group modal depth, release-scope
  ensemble expansion) must be computed by one shared helper over one pass-invariant membership definition
  (library-wide scan, keyed identically), and threaded into every pass.  A pass computing the same statistic over a
  pass-local membership is the same contract violation as an in-memory tag patch.

Inherited unchanged: C-MAINTAIN, C-CONFLUENCE, C-RETIRE, INSTR, PERM, C-JRNL, C-FATAL, C-XREF, C-DEDUP,
C-NOCLOBBER, C-SEQ, C-PROV, C-MOVE, NORM-2-as-revised, C-W3b-INT.

## Sessions

Ordering rationale: S1 and S2 are the two canonical-divergence deletions, split because each is a clean conceptual
unit with its own KATs and the cost-of-wrong is library-wide move policy (small commits, dense green gates).  They
touch the same region of `_pipeline_maint.py`, so they run serially (S1 first — it deletes code S2 would otherwise
have to reason around).  S3 extends the composite-idempotence harness with exactly the two shapes the existing
harness demonstrably missed — it must follow both fixes because the harness asserts the composed fixpoint.  S4 is
the reopened operator acceptance gate.

| ID | Type     | Deliverable (commit-title shape)                                                                      | Deps  | Status |
|----|----------|-------------------------------------------------------------------------------------------------------|-------|--------|
| S1 | build    | Delete unify's classical composer-chain render patch; render raw embedded chains (C-CANON)            | —     | todo   |
| S2 | build    | Shared library-wide modal-depth computation threaded into all passes (C-CANON, C-GROUPSCOPE)          | S1    | todo   |
| S3 | build    | Composite-idempotence KATs: chain-patch and depth-membership cycle shapes (C-IDEM)                    | S1,S2 | todo   |
| S4 | operator | Reopened acceptance gate on hades: one-time un-scatter, then "no changes" by run 2                    | S1–S3 | todo   |

### S1 — delete the composer-chain render patch (C-CANON)

Files: `src/music_annotator/_pipeline_maint.py` (delete `_unify_classical_composer_groups` and its call site with
the stale scope-gate comment block; delete the module-docstring index entry), tests (delete/rewrite tests that
assert the patched render; add KATs below).  KATs:

1. **Completer shape** — fragmented fixture where movements of one work carry `Mozart` and `Mozart; Süßmayr`
   embedded chains: `unify` and `repath` compute **identical** destinations from raw tags (mock-enforced equality);
   after consolidation the library holds still (the two chains render two top dirs, and that is the asserted
   fixpoint — not a regression).
2. **Mixed-arrangement shape** — a release whose sub-works carry genuinely different chains (`Vivaldi; Bach` vs
   `Bach; Marcello`) sharing one top-work MBID: no pass ever rewrites either chain's render.
3. **Work-ID'd non-classical shape** — a jazz release whose tracks carry `musicbrainz_workid` and empty composer
   tags: top dir stays ALBUMARTIST-led through repath and unify (the back-door C-NC-TOP flip is impossible once
   the patch is gone).

Not built: any replacement chain-unification logic (the operator ruling routes chain repair to re-annotation);
tripwire changes.

### S2 — shared modal-depth computation (C-CANON, C-GROUPSCOPE)

Files: `src/music_annotator/_pipeline_maint.py` (extract the library-wide `cwp_workid_top` → modal-depth
computation that `repath` performs into one shared helper; `maintain` computes the map once over the full library
scan and threads it into `repath`, `regroup`, and `unify`; standalone per-pass invocations compute it themselves
via the same helper — one function, one membership definition), tests.  KATs:

1. **Membership-divergence shape** — library fixture holding two recordings of the same top work with different
   `cwp_part_levels` distributions, one of them a fragmented release: `unify`'s depth render equals `repath`'s
   (mock-enforced equality); previously unify's release-local modal computed a different depth.
2. **Same-run inverse-free** — repath then unify over the fixture produces zero `inverse_move_detected` events.
3. Ingest/maintenance parity re-asserted (C-W3b-INT KAT extended to the shared helper).

### S3 — composite-idempotence harness extension (C-IDEM)

Files: `tests/` (extend the existing twice-run fixture library with both residual cycle shapes: the completer
chain shape including the empty-composer non-classical flip, and the depth-membership shape; second `maintain` run
asserts "no changes" and zero journal delta).  The prior harness passed while both shapes churned live — the
fixture set must now include the pathology documented in this sub-track's own derivation evidence (same durable
lesson as the resolver-cycle miss recorded in the prior ledger).

### S4 — reopened operator acceptance gate (hades)

- Run 1: expect the one-time un-scatter — repath moves the ~123 files to their raw-embedded-chain and
  shared-modal-depth homes; unify performs **no reversal**; tripwire silent.
- Run 2: MUST report **"no changes"**.  This is the composite-fixpoint acceptance criterion, reopened from the
  prior sub-track.
- Watch item: any residual moves in run 2 are a discovery — attribute before touching code.  Candidate residual
  class flagged at derivation: ~1167 files fail the albumid read inside fragmentation detection and are invisible
  to unify's release-scope SEL-23/modal groupings while visible to repath's library-wide ones; no such churn is
  present in the current evidence (all 123 moves attributed), but the asymmetric visibility survives until the
  read-error cluster is routed (prior sub-track's diagnosis session landed exception detail in the event; sampling
  remains operator work here).

On acceptance: rewrite this PLAN at the boundary.

## Notes for executors

- **Register rule** (repo AGENTS.md): durable files state the property/invariant, never the plan coordinate.
  Anneal denylist for this sub-track: `\bS[1-4]\b` (session ids), `sub-track`, `plan-run`, `boundary rewrite`,
  `cycle class`, `\bW2[bc]\b` (work-item ids from the prior sub-track; the deleted function's docstring carries
  one — it goes with the deletion; no new durable prose may use them), `run [de]\b`, `maintain\.[de]\.out` (in
  durable prose; state the invariant — "all passes derive destinations from the same canonical inputs over the
  same group membership" — instead).  Contract names (C-CANON, C-NC-TOP, C-IDEM, C-GROUPSCOPE, C-MAINTAIN,
  C-CONFLUENCE, INSTR, PERM, C-JRNL, C-XREF, C-DEDUP, C-PROV, C-MOVE, C-W3b-INT, NORM-*, REND-*, EPIST-*) are
  legitimate durable vocabulary.
- Full gate before declaring any session done: `~/.local/bin/tox -m analyze` (100% branch coverage, mypy strict,
  pylint 10.00/10, ruff, pyupgrade).
- Patch targets bind where the name is imported, not where it originates (repo testing convention).
- **C-PROV/C-MOVE are untouched**: this sub-track changes which destination is computed, never the intra-pass
  move/verify/journal ordering.  No journal entry before SHA + `_verify_copy` pass.
- Evidence base: `~/Remote/hades/Music/maintain.{d,e}.out` (ANSI structlog; strip ANSI + timestamps to compare —
  the two files are then byte-identical).  Key facts: 123 repath moves, 123 unify moves (exact inverses, verified
  pairwise); `inverse_move_detected` ×246 with `current_pass=repath prior_pass=unified`; composer-chain component
  differs in 112 pairs, work-subdir depth in the remainder; `regroup_nothing_to_regroup` (regroup is converged);
  `changed=246` in both runs.
- The deletion in S1 removes `unify`'s only classical chain handling — there is **no replacement**.  A release
  whose movements carry divergent embedded chains legitimately renders multiple top dirs; consolidation of that
  shape is re-annotation's job (BACKLOG), not maintenance's.
- S2's helper must be the single source for modal depth in ingest parity too — check `run()`'s computation
  (`_pipeline.py:1826-1833`) against the helper and unify only if behaviour is identical; if ingest's membership
  is per-release by construction (it only sees one release), document that at the helper, don't force it.

## Progress ledger

VERIFY: `~/.local/bin/tox -m analyze` (combined gate: tests + 100% branch coverage + mypy strict + pylint 10.00 +
ruff + pyupgrade).  One green run satisfies tests, types, lint, format, and coverage.

| ID | Title                                                                          | Status | Commit | Notes |
|----|--------------------------------------------------------------------------------|--------|--------|-------|
| S1 | Delete unify's composer-chain render patch (C-CANON)                          | done   | 6eb7669 | `_unify_classical_composer_groups` deleted; 3 KATs pass (completer, mixed-arrangement, non-classical ALBUMARTIST-led). |
| S2 | Shared library-wide modal-depth computation (C-CANON, C-GROUPSCOPE)           | done   | aba7285 | `compute_library_modal_depth` helper extracted; maintain threads map into all passes; 3 KATs pass. |
| S3 | Composite-idempotence KATs for both residual shapes (C-IDEM)                  | done   | 55d1220 | 3 KATs added: completer chain, empty-composer non-classical flip, depth-membership; second run asserts "no changes". |
| S4 | Reopened acceptance gate on hades: "no changes" by run 2                      | todo   | —      | |

Frozen contracts: C-CANON, C-NC-TOP, C-IDEM (frozen 2026-08-28, prior derivation); C-GROUPSCOPE (frozen at this
derivation, operator ruling).  C-MAINTAIN, C-CONFLUENCE, C-RETIRE, INSTR, PERM, C-JRNL, C-FATAL, C-XREF, C-DEDUP,
C-NOCLOBBER, C-SEQ, C-PROV, C-MOVE, NORM-2-as-revised, C-W3b-INT inherited unchanged.

## Action-frame digest

(append non-trivial discoveries, contract flexes, and notable texture here as sessions run)

- Derivation (2026-08-28): two post-acceptance live runs are byte-identical modulo timestamps with `changed=246` —
  the residual orbit lives exactly in the two mechanisms the prior repair spared: the classical chain patch
  (survey-classified as "genuine classical handling, not deleted") and unify's release-local modal-depth
  membership.  Durable lesson (CAPTURE-CANDIDATE, chat 2026-08-28): **group-scope canonicalization is shared only
  when both the function and the group membership are identical across passes** — the prior repair aligned the
  function argument (`group_modal_depth` threaded everywhere) but not the membership (library-wide vs
  release-local), and the same statistic over different denominators diverges exactly like an in-memory patch.
  Second lesson: a comment asserting a scope gate is not a scope gate — the claimed classical gate on the chain
  patch never existed in the function body, and the `musicbrainz_workid` fallback silently widened the patch to
  jazz and musicals, including a shape flip (empty-composer files rendered composer-led) that re-violated C-NC-TOP
  after its own repair session had landed.
- Operator rulings at derivation: delete the chain patch (over persist-to-tags — rejected here not on the fake-
  scholarship ground alone but because the chooser provably clobbers real distinctions: one top-work MBID spanning
  arrangement sources with conflicting chains — and over symmetrizing the patch into other passes, which preserves
  two render sources and fragile membership identity).  Divergent embedded chains rendering multiple top dirs is
  accepted as truthful; repair routes through re-annotation (BACKLOG).

### Boundary (S1–S3) — 2026-08-28
Discovery/flex: none — all three sessions delivered exactly as specified; anneal found W2b/W2c plan coordinate labels in test files (fixed before boundary fork).
Affected: none
Deferred: no — the live-fixpoint claim (S4 acceptance) is genuinely deferred to the operator gate; the boundary fork confirmed code/tests track intent; the albumid read-error watch item (~1167 files) is anticipated in the PLAN and does not retroactively invalidate the boundary.
Texture: boundary fork noted that deleting the chain patch outright means releases with divergent embedded chains now legitimately render multiple top dirs (accepted-as-truthful per operator ruling); S4 should read any such split as expected, not as a new anomaly.
