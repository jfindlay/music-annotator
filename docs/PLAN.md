<!-- Rolling action frame.  The previous sub-track (preflight-evidence corrective fixes) closed 2026-08-25 with its
     acceptance gate passed and the repair turn unblocked; its plan and ledger live in this file's git history.  This
     sub-track was derived 2026-08-25 from the first live repair-turn repath run on hades, which surfaced the journal-store
     O(N²) per-move cost, an in-place-rewrite corruption window, and an unhandled same-audio collision outcome that aborted
     the run 12 minutes into its move phase.  Rewritten at the next boundary. -->

# PLAN — repair-turn hardening: journal store, collision completeness, duplicate cross-referencing

## Why this sub-track exists

The first live `repath` on hades (2026-08-25) ran 78 minutes and aborted mid-move-phase.  Root-caused defects and one
capability gap, evidence in run logs and `~/Remote/hades/Music/Done/music_annotator_journal.json` (44 MB, 49k entries):

- **Journal O(N²)** — `write_transaction_log` re-reads, re-validates (49k × `model_validate`), and fully rewrites the whole
  array journal for **every single move** (~2.7 s/file, confirmed from log timestamps; called per-file from
  `_execute_single_move`).  It also rewrites in place (truncate-then-write, no temp+rename), so a crash or full disk
  mid-write destroys the entire journal.  The re-read defends nothing: there is no lock, so it provides zero concurrency
  safety, and the tool is a single-process CLI.
- **Collision-outcome incompleteness** — repath's plan handles only `match=False` (suffix; `_pipeline_maint.py:1183`).
  `match=True` (same audio, different bytes — duplicate rips, e.g. the Greensleeves pair present in two Marriner/ASMF
  releases) and `match=None` (inconclusive) fall through unhandled, reach execution, and die on C-NOCLOBBER — correctly
  refusing the clobber, but discarding the remaining run.  Operator ruling: run-fatal stays (maximal integrity bias); the
  obligation moves to the plan, which must never emit an unwinnable move.
- **Duplicate releases are invisible in tags** — during ingest the operator destructively chose one release when whole
  mediums were duplicated (SKIP/OVERWRITE collision policy).  Goal: reference *both* releases from tags — primary release
  drives path + annotation; secondary releases are recorded as ID-only cross-references.  The journal holds complete
  offline evidence for both destructive paths: SKIP wrote `action="skipped"` entries carrying the losing release_id;
  OVERWRITE left both `tagged` entries at the same destination with different release_ids (chronological-last = primary).
- **Re-run cost** — resume-for-correctness already works (per-move journal flush + `_resolve_current_lib` chain makes
  re-runs idempotent), but every re-run pays ~an hour of tag re-reads over the full library before the first move.
  Operator ruling: bring the tag-read cache into scope.

## Cross-session contracts

Frozen at derivation (operator rulings 2026-08-25):

- **C-JRNL** — journal storage: append-only JSON Lines, one entry per line.  Per-entry durable flush (write + flush +
  fsync) before the next move — C-PROV ordering and timing are unchanged.  Reads tolerate exactly one torn final line
  (warn and ignore); any other malformation is corruption and a hard error, never a silent reset.  Full rewrites
  (rebuild/compaction/migration) are atomic: temp file + `os.replace`.  A one-time migration converts the existing array
  journal; the original `.json` is preserved as a read-only backup the tool never deletes.  Maintenance passes hold the
  journal in memory for the run and never re-read it between moves.
- **C-FATAL** — data-integrity errors are always run-fatal (reaffirms C-NOCLOBBER exactly as frozen; no per-file skip
  flex).  Corollary obligation, **plan-time completeness**: every collision outcome (`True`/`False`/`None`) is resolved at
  plan time — cross-reference + plan-drop, suffix, or operator prompt — so an execution-time C-NOCLOBBER refusal indicates
  a defect in plan construction, not a data condition to tolerate.
- **C-XREF** — cross-references are ID-only, append-only, and inert: secondary release references never drive path
  computation, annotation content, or medium selection; the primary release (single-valued `MUSICBRAINZ_ALBUMID`) remains
  the sole annotation source.  Cross-reference tag mutations require operator confirmation and pass through the full
  C-PROV chain (tag write → verify → journal) with a dedicated journal action.  Exact tag vocabulary and journal action
  name are adjudicated in the schema session and appended to the STYLEGUIDE case register (C-CASE append-only discipline).

## Sessions

Ordering rationale: S1/S2 close the operational emergency (the journal store) and are adjudication-free — they land
first.  S3 is the interactive adjudication that gates all cross-reference work.  S4 restores a completable repath; S5
reconstructs past destructive choices from journal evidence; S6 is conditional on S5's census finding evidence gaps.
S7 is independent and can interleave.  Each build session is one conceptual unit, ~150–400 LOC, `tox -m analyze` green
(100% branch coverage, mypy strict, pylint 10.00).

| ID | Type         | Deliverable (commit-title shape)                                                        | Deps   | Status |
|----|--------------|------------------------------------------------------------------------------------------|--------|--------|
| S1 | build        | JSONL journal store: O(1) durable appends, torn-tail recovery, atomic rewrite, migration (C-JRNL) | —      | todo   |
| S2 | build        | Hold journal in memory across maintenance runs; retire per-move full rewrites (C-JRNL)     | S1     | todo   |
| S3 | adjudication | STYLEGUIDE: cross-reference tag schema + repath collision interaction design (C-XREF)      | —      | todo   |
| S4 | build        | Plan-time collision completeness in repath: cross-ref flow + inconclusive prompt (C-FATAL) | S2, S3 | todo   |
| S5 | build        | Cross-reference reconstruction pass: journal census + confirmed secondary-ID tagging       | S3, S2 | todo   |
| S6 | build        | (conditional) MB-backed cross-reference enrichment for evidence-gap cases                  | S5     | todo   |
| S7 | build        | Tag-read cache for maintenance passes keyed on (path, size, mtime)                         | S2     | todo   |
| S8 | operator     | Resume the repair turn: full repath on hades end-to-end; acceptance gate                   | all    | todo   |

### S1 — JSONL journal store (C-JRNL)

Files: `src/music_annotator/_pipeline_io.py` (new append primitive; `read_journal` gains JSONL + torn-tail handling and
the one-time array→JSONL migration; the full-rewrite path used by rebuild becomes atomic temp+`os.replace`), tests.
KATs: append→read round-trip; torn final line tolerated exactly once and logged; non-tail malformation is a hard error;
migration preserves entry order and count against a fixture array journal and leaves the `.json` backup untouched;
rewrite is atomic (temp visible only post-replace).  JSONL is pyfakefs-compatible (pure-Python I/O) — no test-harness
change needed.

### S2 — in-memory journal threading

Files: `src/music_annotator/_pipeline_maint.py` (`_move_verify_journal` and callers accept/mutate an in-memory
`TransactionLog` + append handle instead of re-reading per move), `src/music_annotator/_pipeline.py` (ingest call site),
tests (fixture churn: journal fixtures move to JSONL).  KATs: a multi-move run performs zero journal re-reads (mock-
enforced); per-move append lands before the next move begins (C-PROV ordering preserved); crash simulation between moves
leaves a complete, readable journal.  Expected effect: per-move journal cost drops from ~2.7 s to ~1 ms; the move phase
becomes hashing-bound.

### S3 — cross-reference schema adjudication (interactive; operator present; architect/dialectic register)

Deliverable: STYLEGUIDE case-register updates + the frozen vocabulary S4–S6 consume.  Agenda, with evidence to bring:

1. **Tag vocabulary** — secondary-reference tag name(s) and payload: release MBID only vs + release-track MBID vs
   + medium/position.  Check Classical Extras / Picard conventions for an existing multi-release idiom before minting one.
   FLAC multi-value vs delimited scalar; the MP3 TXXX mapping.
2. **Journal action** — name for a cross-reference tag mutation (extends the `TransactionEntry` action set; audit passes
   must classify it; `_resolve_current_lib` must treat it as an in-place re-registration like "enriched").
3. **Prompt UX** — repath `match=True`: confirm cross-reference per group or per file; `match=None`: suffix-or-abort
   prompt wording; semantics under `--yes` (operator lean: prompts still shown — integrity prompts are not bulk-consent)
   and `--dry-run` (report, never prompt).
4. **Redundant-copy disposition** — in the repath `match=True` case both physical copies survive (occupant at the
   canonical path, mover stays put).  Adjudicate the mover's disposition: stays in place cross-referenced, or flagged
   into a future dedup adjudication pass.  Deletion is out unless the operator rules otherwise (C-NOCLOBBER rationale).

### S4 — plan-time collision completeness (C-FATAL corollary)

Files: `src/music_annotator/_pipeline_maint.py` (repath collision resolution gains the `match=True` and `match=None`
arms per S3; regroup/unify audited for the same gap), `src/music_annotator/_tagger.py` (cross-reference tag write),
tests.  KATs: same-audio occupant → operator-confirmed cross-reference + move dropped from plan + journal action
recorded; inconclusive occupant → prompt suffix-or-abort, both arms; a plan that reaches `_move_verify_journal` contains
no move whose destination is occupied by a non-vacated path (property test over generated plans); execution-time
C-NOCLOBBER refusal still raises `RuntimeError` (unchanged, now bug-indicating).

### S5 — cross-reference reconstruction pass

Files: `src/music_annotator/_pipeline_maint.py` (new maintenance command), `src/music_annotator/__main__.py`, tests.
Census the journal for both destructive-choice shapes: (a) `skipped` entries joined to the surviving `tagged` entry at
the same destination (SKIP policy); (b) multiple `tagged` entries at one destination with distinct release_ids
(OVERWRITE policy, chronological-last = primary).  Present grouped findings; on operator confirmation, write secondary
references per C-XREF and journal each mutation.  Offline; dry-run supported.  The census also reports evidence-gap
candidates (duplicates suspected but not journal-provable) as input to the S6 conditional.

### S6 — MB-backed enrichment (conditional)

Gated on S5's census reporting evidence gaps (duplicates resolved outside the pipeline, e.g. never-ingested second
source dirs).  Per affected recording, `recording → releases` MB lookups under the standard `@_mb_retry` + `_mb_call`
posture; propose cross-references for operator confirmation.  Skip this session entirely if the census shows no gaps.

### S7 — tag-read cache

Files: `src/music_annotator/_pipeline_maint.py` (Pass 1 of repath and the preflight/audit walks consult a cache keyed on
`(path, size_bytes, mtime_ns)`), cache persistence sidecar under dest_root, tests.  KATs: hit returns identical tags
without opening the audio file (mock-enforced); any key component change invalidates; cache corruption or absence
degrades to a full read (never an error); moves via `_move_verify_journal` re-key the entry.  Expected effect: re-run
planning drops from ~an hour to minutes on an unchanged library.

### S8 — operator acceptance gate

Full `repath` on hades end-to-end.  Acceptance criteria:

- Run completes without abort; zero execution-time C-NOCLOBBER refusals.
- Move-phase throughput reflects the journal fix (per-move time hashing-bound, not journal-bound).
- Same-audio collisions resolved as cross-references per S3 rulings; inconclusive collisions prompted, not fatal.
- Journal is valid JSONL afterward; `.json` backup intact; rebuild/audit commands read it cleanly.
- Reconstruction pass run once over the historical evidence; secondary references verified on known duplicated mediums
  (the Greensleeves pair as the canonical KAT case).

On acceptance: continue the repair turn per ROADMAP (leaf renumberings, year-label normalisations, consolidations), and
rewrite this PLAN at the boundary.

## Notes for executors

- **Register rule** (repo AGENTS.md): durable files state the property/invariant, never the plan coordinate.  Anneal
  denylist for this sub-track: `\bS[1-8]\b` (session ids), `repair-turn hardening`, `sub-track`, `plan-run`,
  `boundary rewrite`.  Contract names (C-JRNL, C-XREF, C-FATAL, C-NOCLOBBER, C-SEQ, C-PROV, C-MOVE, C-CASE, SEL-*,
  NORM-*, REND-*) are legitimate durable vocabulary.
- Full gate before declaring any session done: `~/.local/bin/tox -m analyze` (100% branch coverage, mypy strict,
  pylint 10.00/10, ruff, pyupgrade).
- Patch targets bind where the name is imported, not where it originates (repo testing convention).
- The C-PROV/C-MOVE provenance chain is inviolable: no journal entry before SHA + `_verify_copy` pass.  C-JRNL changes
  the storage of the journal, never the ordering of verification relative to the append.
- The 2026-08-25 failed-run evidence: hades run logs (`repathed_moved`/`journal_written` timestamps, the C-NOCLOBBER
  traceback) and the 44 MB array journal on the dev mount at `~/Remote/hades/Music/Done/` (hades paths:
  `/home/findlay/Music/`).  The Greensleeves collision pair is the canonical same-audio-different-bytes fixture shape.
- Test-fixture churn in S2 is expected and mechanical (array-journal fixtures → JSONL); do not let it balloon the
  session — if it exceeds the commit window, split fixture migration into its own follow-up commit within the session.

## Progress ledger

VERIFY: `~/.local/bin/tox -m analyze` (combined gate: tests + 100% branch coverage + mypy strict + pylint 10.00 + ruff +
pyupgrade).  One green run satisfies tests, types, lint, format, and coverage.

| ID | Title                                                                          | Status | Commit | Notes |
|----|--------------------------------------------------------------------------------|--------|--------|-------|
| S1 | JSONL journal store: appends, torn-tail recovery, atomic rewrite, migration     | done   | 60189da | C-JRNL append primitive + read_journal JSONL frozen |
| S2 | In-memory journal threading; retire per-move full rewrites                      | todo   |        |       |
| S3 | STYLEGUIDE: cross-reference tag schema + collision interaction design           | todo   |        |       |
| S4 | Plan-time collision completeness in repath                                      | todo   |        |       |
| S5 | Cross-reference reconstruction pass (journal census)                            | todo   |        |       |
| S6 | (conditional) MB-backed cross-reference enrichment                              | todo   |        |       |
| S7 | Tag-read cache for maintenance passes                                           | todo   |        |       |
| S8 | Resume repair turn on hades; acceptance gate                                    | todo   |        |       |

Frozen contracts: C-JRNL, C-FATAL, C-XREF (all frozen at derivation, 2026-08-25); C-NOCLOBBER, C-SEQ, C-GUARD,
NORM-2-as-revised, SEL-23, REND-27 inherited unchanged from the previous sub-track.

## Action-frame digest

(append non-trivial discoveries, contract flexes, and notable texture here as sessions run)

- Derivation (2026-08-25): operator ruled run-fatal integrity posture over per-file skip (C-FATAL); chose cross-reference
  tagging over suffix/dedup for same-audio collisions; journal census confirmed both destructive-choice shapes are fully
  reconstructible offline (SKIP → `skipped` entries with loser release_id; OVERWRITE → dual `tagged` entries per
  destination).  SQLite/LMDB/CBOR evaluated and rejected for the journal: SQLite loses pyfakefs compatibility and
  greppability for query capability this workload doesn't need; JSONL + in-memory copy achieves O(1) appends with
  stdlib-only, test-compatible mechanics.
