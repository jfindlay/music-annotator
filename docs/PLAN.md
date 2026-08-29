<!-- Rolling action frame.  The previous sub-track (maintain convergence repair, second round: delete the last
     in-memory render patch) closed 2026-08-28 with ALL sessions done including the operator acceptance gate; its plan
     and ledger live in this file's git history (through the commit that landed this rewrite).  Acceptance evidence
     (`maintain.{f,g}.out`, hades, 2026-08-28 19:07/19:11): run 1 performed the predicted one-time un-scatter (123
     repath moves, every one flagged by the C-IDEM tripwire as the inverse of a *prior-run* unify move — no same-run
     reversal, unify silent); run 2 reports `repath_all_current`, zero tripwire events, **changed=0**.  The composite
     fixpoint holds.  This sub-track was derived 2026-08-28 from the operator's error-classification request over those
     same runs: the acceptance runs' residual log output is two persistent clusters (1167 albumid read errors, 93
     acoustid-inconclusive events) plus one truth bug in the enrich summary counter.  Rewritten at the next boundary. -->

# PLAN — fragmentation adjudication from present state: route the albumid read-error cluster

## Why this sub-track exists

With the library at its maintenance fixpoint, every `maintain` run still emits 1167 `albumid_tag_read_error` warnings
(`MutagenError` wrapping `[Errno 2]`) — identical set, every run, forever-growing.  The burst sits between repath and
regroup: it is regroup's confirmation gate, `_confirm_fragmentation` (`_audit.py`, called at `_pipeline_maint.py:2228`),
reading `MUSICBRAINZ_ALBUMID` from the raw `destination` of `action == "tagged"` journal entries.  Those destinations
are *historical* — where files sat at tagging time, since renamed by repath/regroup/unify.  The journal is append-only
by design (C-JRNL), so the dead paths never heal; the gate probes them every run.

Re-adjudication against the live journal (65,156 entries) and library (hades, 2026-08-28) quantifies the damage:

- 11,019 unique tagged destinations back fragmentation candidates; **2,332 no longer exist at the raw path** (the 1167
  observed warnings are the lazily-probed subset — the confirmation loop short-circuits on first match).
- **2,300 of the 2,332 (98.6%) resolve to a live current path** through the journal's own move chain — the resolver
  (`_resolve_tagged_to_current`, `_pipeline_maint.py:3087`) already exists and is already used by the xref census for
  exactly this reason.  The gate simply doesn't use it.
- **35 candidates flip STALE→CONFIRMED under resolved reads** (12 work-dir-shaped, 23 split-release-shaped): the gate
  is not merely noisy — it misadjudicates.  Real fragmentation whose files have merely moved is invisible to regroup.
- Only 32 paths stay dead after resolution: 28 with no chain successor at all, 4 whose chain terminates at a file the
  dedup pass deleted — `_resolve_tagged_to_current` does not handle `"deduplicated"` entries (confirmed resolver gap).
- Present-state grouping (by *current* work_dir over `_resolve_current_lib`) yields 242 split-release candidates vs
  242 historical (240 shared, 2 phantoms dissolve, 2 presently-real candidates are invisible to today's historical
  grouping).

Two structural facts license a narrow fix.  First, **regroup's action path is already present-state-correct**: after
the gate it derives current paths via `_resolve_current_lib`, recomputes each file's canonical destination from
embedded tags, and noops anything already canonical.  Only the gate's *evidence source* is wrong.  Second, the gate is
a **pre-filter, not the mover**: in a work-centric library a multi-work box set legitimately spans many work dirs (one
release spans 700), so an opened gate mostly feeds noops — TagReadCache-amortized.  With regroup and repath now
sharing one canonical engine and one modal-depth map, opening the gate cannot disturb the fixpoint; any move it
produces that repath would not is a discovery to attribute, not expected behaviour.

Alongside, one truth bug: `enrich_complete` reports `inconclusive_acoustid=0` while 93 `enrich_acoustid_inconclusive`
events fire per run.  The counter increments only after the `if not write_fields: continue` gate
(`_pipeline_maint.py:2901/2908`), so files fully enriched *except* for a missing AcoustID are counted as noop and
never as inconclusive.  The 93 files themselves are a by-design condition (no network lookup without
`re_resolve=True` + AcoustID key), not a defect — but the summary must tell the truth about them.

**Operator ruling (2026-08-28, this derivation):** fix in code only; do **not** edit or rewrite journal history
(the journal is the provenance record — every resolver re-derives state from full history), and do **not** introduce
an append-only tombstone action for the 32 unresolvable dead paths — at that count the new action type threaded
through every resolver costs more than re-deriving them as stale each run.  Revisit only if the dead-end population
grows materially.

## Cross-session contracts

New this sub-track:

- **C-RESOLVE** — no pass or audit may dereference a journal-historical destination (any path read from a `"tagged"`
  or other historical entry) for a *present-state* question without first resolving it through the move chain
  (`repathed`/`regrouped`/`unified` forwarding, `deduplicated` terminal).  Reading a raw historical path is the same
  contract violation as an in-memory render patch: it answers "now" questions with "then" evidence.  Corollary: a
  path that fails to resolve is *expected* history, adjudicated stale without per-file warnings; only non-ENOENT
  failures on a *resolved* path warrant a warning.

Inherited frozen, unchanged: C-CANON, C-NC-TOP, C-IDEM, C-GROUPSCOPE, C-MAINTAIN, C-CONFLUENCE, C-RETIRE, INSTR,
PERM, C-JRNL, C-FATAL, C-XREF, C-DEDUP, C-NOCLOBBER, C-SEQ, C-PROV, C-MOVE, NORM-2-as-revised, C-W3b-INT.
C-PROV/C-MOVE in particular: this sub-track changes which candidates the gate confirms, never the intra-pass
move/verify/journal ordering.

## Sessions

Ordering rationale: S1 is the substrate-and-fix session — it owns the resolver gap, the gate rewrite, and the log
calibration as one conceptual unit (they touch the same evidence path and are not meaningfully separable).  S2 is a
small independent truth fix in a different pass; it follows S1 only to keep the log-surface changes reviewable in
sequence.  S3 is the operator acceptance gate over both.

| ID | Type     | Deliverable (commit-title shape)                                                                  | Deps  | Status |
|----|----------|---------------------------------------------------------------------------------------------------|-------|--------|
| S1 | build    | Fragmentation adjudication resolves journal history to present state before reading tags (C-RESOLVE) | —     | todo   |
| S2 | build    | Enrich summary counts acoustid-inconclusive files truthfully                                       | S1    | todo   |
| S3 | operator | Acceptance gate on hades: warnings gone, gate opens, fixpoint holds                                | S1,S2 | todo   |

### S1 — present-state fragmentation adjudication (C-RESOLVE)

Files: `src/music_annotator/_pipeline_maint.py` (`_resolve_tagged_to_current`: handle `"deduplicated"` as terminal —
pop the deleted source's forwarded tagged-dests so chains ending at a dedup-deleted file resolve to nothing),
`src/music_annotator/_audit.py` (`_confirm_fragmentation` and its grouping: derive candidate groups and confirmation
reads from resolved current paths, not raw tagged destinations), `src/music_annotator/_pipeline_io.py`
(`_read_albumid_tag` log calibration), tests.

Design guidance (executor latitude on mechanism, not on properties):

- Single-source the present-state derivation.  Regroup already builds `_resolve_current_lib`; the gate's grouping and
  confirmation should flow from one resolution pass, not add a second independent scan.  `audit()` consumes the same
  helper — keep `_confirm_fragmentation` serving both callers with the resolution applied inside it (or passed in),
  whichever keeps one derivation per invocation.
- Grouping semantics: a split-release candidate is a release whose **currently existing** files span more than one
  work_dir (work_dir computed from the *current* path).  Confirmation reads the embedded tag at the current path.
  Historical-only phantoms must dissolve; presently-real candidates invisible to historical grouping must appear.
- Log calibration: a tagged destination that fails to resolve (no chain successor, or dedup-terminated) is expected
  history — adjudicate stale, count it, emit **one aggregate info event** per run (event name of executor's choice,
  carrying the count).  A read failure at a *resolved, existing* path that is not ENOENT remains a per-file warning.
  The per-file `albumid_tag_read_error` warning for expected-missing paths must not survive this session.

KATs:

1. **Moved-file confirmation** — tagged at A, journal records A→B move, file exists at B with matching embedded
   albumid: candidate adjudicates CONFIRMED (the raw-path reading previously produced false STALE).
2. **Re-tag adjudicates stale** — resolved current file carries a *different* albumid: candidate STALE.
3. **Dedup-terminated chain** — tagged at A, A later deleted by dedup: adjudicates stale with zero per-file warnings.
4. **Phantom dissolution / present-state visibility** — a release whose current files sit in one work_dir is not a
   candidate despite historical tagged dests spanning two; the converse shape (historically one dir, currently two)
   is a candidate.
5. **Aggregate logging** — a fixture with several unresolvable tagged dests produces exactly one info event carrying
   the count, and no warnings.

Not built: any narrowing of the split-release criterion itself (a multi-work box set legitimately opens the gate and
noops through the recompute; criterion refinement is BACKLOG if first-run cost proves material), tombstone journal
actions, journal history edits.

### S2 — enrich summary truth

Files: `src/music_annotator/_pipeline_maint.py` (count files lacking an embedded AcoustID *regardless of whether any
tag write is needed* — move the increment ahead of the noop gate, or count from the `_needs_enrich` signal),
`src/music_annotator/_pipeline_io.py` (demote the per-file `enrich_acoustid_inconclusive` info event to debug — the
aggregate count in `enrich_complete` becomes the operator-facing signal), tests.  KATs:

1. A file fully enriched except AcoustID (noop for writes) increments the inconclusive count; `enrich_complete`'s
   `inconclusive_acoustid` equals the number of such files (the current code reports 0 against 93 events).
2. A file with an embedded AcoustID is not counted, whether or not it needs other writes.

Semantics note: after this session `inconclusive_acoustid` means "files in the library lacking an embedded AcoustID",
matching what the per-file events always meant.  Resolution of those files remains the keyed re-resolve path
(`re_resolve=True` + AcoustID API key) — out of scope here.

### S3 — operator acceptance gate (hades)

- Run 1 (`maintain`): expect **zero** `albumid_tag_read_error` events; one aggregate unresolvable-history info event
  (expected count ≈ 32); the regroup gate opens (present-state split-release candidates ≈ 242, dominated by
  legitimately multi-work releases) and the per-file recompute noops — `regroup` performing actual moves is a
  discovery to attribute before touching code, not an expected outcome.  `enrich_complete` reports
  `inconclusive_acoustid=93` (or current true count).
- Run 2: MUST report **changed=0** — the fixpoint must survive the opened gate.
- Watch items: (a) 21 current-lib paths are missing on disk per the derivation analysis — attribute (deleted outside
  the journal? resolver gap?) before deciding whether they need routing; (b) first-run wall-clock cost of the opened
  gate (worst case ≈ 11k tag reads, TagReadCache-amortized on subsequent runs) — if material, criterion narrowing
  goes to BACKLOG with measurements attached.

On acceptance: rewrite this PLAN at the boundary.

## Notes for executors

- **Register rule** (repo AGENTS.md): durable files state the property/invariant, never the plan coordinate.  Anneal
  denylist for this sub-track: `\bS[1-3]\b` (session ids), `sub-track`, `plan-run`, `boundary rewrite`,
  `maintain\.[a-g]\.out` (in durable prose; state the invariant — "present-state questions are answered by resolving
  journal history through the move chain" — instead), `read-error cluster` (derivation shorthand).  Contract names
  (C-RESOLVE, C-CANON, C-NC-TOP, C-IDEM, C-GROUPSCOPE, C-MAINTAIN, C-CONFLUENCE, INSTR, PERM, C-JRNL, C-XREF,
  C-DEDUP, C-PROV, C-MOVE, C-W3b-INT, NORM-*, REND-*, EPIST-*) are legitimate durable vocabulary.
- Full gate before declaring any session done: `~/.local/bin/tox -m analyze` (100% branch coverage, mypy strict,
  pylint 10.00/10, ruff, pyupgrade).
- Patch targets bind where the name is imported, not where it originates (repo testing convention).
- Evidence base: `~/Remote/hades/Music/maintain.{f,g}.out` (ANSI structlog) and the derivation analysis over the live
  journal (65,156 entries; hades library mounted read-only).  Key figures: 1167 warnings/run from one burst between
  repath and regroup; 2,332 raw-dead candidate-backing dests of which 2,300 chain-resolve to live paths; 35
  candidates misadjudicated STALE today; 32 true dead ends (28 no successor, 4 dedup-terminated); present-state vs
  historical candidate sets 242/242 with 2 phantoms and 2 invisibles; `enrich_complete` reports 0 against 93 events.
- The confirmation gate is a pre-filter: opening it feeds the per-file canonical recompute, which noops anything
  already canonical.  Do not "optimize" by keeping the gate narrow on stale evidence — false STALE silently skips
  real work, which is the defect this sub-track exists to remove.
- The journal is never edited or compacted (operator ruling at derivation).  All healing is in code, through
  resolution at read time.

## Progress ledger

VERIFY: `~/.local/bin/tox -m analyze` (combined gate: tests + 100% branch coverage + mypy strict + pylint 10.00 +
ruff + pyupgrade).  One green run satisfies tests, types, lint, format, and coverage.

| ID | Title                                                                       | Status | Commit | Notes |
|----|-----------------------------------------------------------------------------|--------|--------|-------|
| S1 | Present-state fragmentation adjudication (C-RESOLVE)                       | done   | 1cc4cb0 | All 5 KATs pass. `_resolve_tagged_to_current` handles deduplicated-terminal; `_confirm_fragmentation` groups and reads from resolved current paths; unresolvable history → one aggregate info event, zero per-file warnings. |
| S2 | Enrich summary counts acoustid-inconclusive files truthfully               | done   | d4cbdd4 | Both KATs pass. Counter moved before noop gate; per-file event demoted to debug; enrich_complete is the operator-facing signal. |
| S3 | Acceptance gate on hades: warnings gone, gate opens, fixpoint holds        | done   | n/a    | PASSED on hades (`maintain.{h,i}.out`, 2026-08-29). Zero albumid_tag_read_error; one aggregate `fragmentation_unresolvable_history count=24` (vs predicted ≈32 — resolver healed more than static analysis estimated, trends toward intent); regroup gate noops; `inconclusive_acoustid=93`; run 2 `changed=0` (fixpoint holds). |

Frozen contracts: C-RESOLVE (frozen at this derivation, operator ruling: code-only healing, no journal edits, no
tombstones).  C-CANON, C-NC-TOP, C-IDEM, C-GROUPSCOPE, C-MAINTAIN, C-CONFLUENCE, C-RETIRE, INSTR, PERM, C-JRNL,
C-FATAL, C-XREF, C-DEDUP, C-NOCLOBBER, C-SEQ, C-PROV, C-MOVE, NORM-2-as-revised, C-W3b-INT inherited unchanged.

## Action-frame digest

(append non-trivial discoveries, contract flexes, and notable texture here as sessions run)

- Derivation (2026-08-28): prior acceptance gate passed on live evidence (run 1 = predicted one-time un-scatter with
  the tripwire firing only against prior-run inverses; run 2 = changed=0).  The residual log surface decomposed into
  three classes: stale-evidence misadjudication in the confirmation gate (defect), a summary counter that disagrees
  with its own event stream (defect), and missing AcoustIDs unresolvable without a keyed lookup (by design).  Durable
  lesson (CAPTURE-CANDIDATE, chat 2026-08-28): **an append-only journal makes every raw historical-path dereference a
  compounding hazard** — the consumer answers a present-state question with then-evidence, the noise grows
  monotonically with every subsequent move, and the misadjudication is silent (false STALE reads as "nothing to do").
  The move-chain resolver existed and was already used by one consumer (xref census); the newer consumer didn't use
  it.  Second lesson: the resolver itself had a gap (dedup-terminal entries unhandled) that only surfaced when a
  second consumer's evidence was quantified — a resolver used by one caller is a resolver with untested semantics.
- Operator rulings at derivation: journal history is never edited (provenance record; all resolvers re-derive from
  full history); no tombstone action type at a 32-path dead-end population (mechanism cost exceeds re-derivation
  cost); confirmation-gate criterion (release spans >1 current work_dir) stays broad — it is a pre-filter feeding a
  nooping recompute, and narrowing it belongs to BACKLOG only with first-run cost measurements in hand.

### S3 acceptance — 2026-08-29
Discovery/flex: acceptance PASSED on hades (`maintain.{h,i}.out`) — zero read-error warnings, one aggregate
  `fragmentation_unresolvable_history count=24` (predicted ≈32; resolver healed more chains than static analysis
  estimated), regroup gate noops, `inconclusive_acoustid=93`, run 2 `changed=0`.  NEW discovery surfaced by operator:
  five Vaughan Williams / Marriner tracks are persistent "evidence-gap candidates" (secondary MBID embedded but not
  journal-provable) because a *dedup-pass write bug* (since fixed) suppressed the journal writes when their
  originating files existed; those files are now gone.  Operator asks whether retroactive journal entries can
  reconstruct that lost provenance.
Affected: C-JRNL (append-only, no-history-edit) and the derivation's frozen "no journal-history edits" ruling — a
  retroactive-entry feature is a direct tension with both and must be adjudicated, not folded in.
Deferred: yes — this is a boundary-rewrite trigger, NOT in-scope for the just-closed sub-track.  The next sub-track's
  design intent is provenance reconstruction for now-deleted originating files given the write-path bug is fixed.
  The juncture adjudicator must reconcile: can synthesized entries be *truthful* (provably derived from surviving
  evidence) without making the journal a rewritable record, and does this widen or merely annotate C-JRNL?
Texture: the deviation count 24-vs-32 is benign (trends toward intent — more healing, not less); it is not drift and
  does not reopen S1.  The evidence-gap candidates are a *different defect class* (a past write-path bug) than this
  sub-track's read-resolution fix — they were correctly invisible to this sub-track's scope.
