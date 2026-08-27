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
  C-PROV chain (tag write → verify → journal) with a dedicated journal action.  **Adjudicated (S3, 2026-08-26; EPIST-9):**
  tag `MUSICBRAINZ_SECONDARY_ALBUMID` / TXXX `"MusicBrainz Secondary Album Id"`; payload release MBIDs only, `"; "`-joined
  scalar (REND-17 separator; the read plumbing is `str→str` and native multi-value would be truncated by `v[0]` read-back),
  append-only set-union; journal action `"cross-referenced"` with `release_id` = the *secondary* MBID being added;
  `_resolve_current_lib` treats it as an in-place re-registration (like `"enriched"`); audit primary-id equality checks
  exclude the action.
- **C-DEDUP** — frozen at S3 (2026-08-26; EPIST-10, operator ruling): physical copies of the same audio may be deleted,
  bounded by (1) `match=True` identity evidence — AcoustID cluster, byte identity a fortiori; `match=None` never deletes —
  and (2) per-group operator confirmation with the operator choosing the survivor.  Ordering invariant: the survivor's
  cross-reference write + verify + `"cross-referenced"` journal entry complete **before** any deletion executes (the
  reference exists durably before the bytes disappear).  Deletions are journaled per file with action `"deduplicated"`
  (source = deleted path, destination = surviving path, release_id = deleted copy's release MBID);
  `_resolve_current_lib` gains a pop-the-source arm for it (neither a move nor an in-place update).  The group prompt
  always offers keep-both (cross-reference only, non-fatal decline) and abort; prompts survive `--yes` (integrity
  prompts are not bulk consent); `--dry-run` reports, never prompts, never deletes.  Identity tolerance covers
  production-process variation (lead-in/out silence, minor post-mixdown gain).  This is a deliberate bounded exception
  to the completionist posture — rationale recorded in EPIST-10.

## Sessions

Ordering rationale: S1/S2 close the operational emergency (the journal store) and are adjudication-free — they land
first.  S3 is the interactive adjudication that gates all cross-reference work.  S4 restores a completable repath; S5
reconstructs past destructive choices from journal evidence; S6 is conditional on S5's census finding evidence gaps.
S7 is independent and can interleave.  S9 (added at S3: operator extended dedup to the general case) sweeps the whole
library for same-audio duplicates that never collide, reusing S4's group-resolution flow.  Each build session is one
conceptual unit, ~150–400 LOC, `tox -m analyze` green (100% branch coverage, mypy strict, pylint 10.00).

| ID | Type         | Deliverable (commit-title shape)                                                        | Deps   | Status |
|----|--------------|------------------------------------------------------------------------------------------|--------|--------|
| S1 | build        | JSONL journal store: O(1) durable appends, torn-tail recovery, atomic rewrite, migration (C-JRNL) | —      | todo   |
| S2 | build        | Hold journal in memory across maintenance runs; retire per-move full rewrites (C-JRNL)     | S1     | todo   |
| S3 | adjudication | STYLEGUIDE: cross-reference tag schema + repath collision interaction design (C-XREF)      | —      | todo   |
| S4 | build        | Plan-time collision completeness in repath: cross-ref flow + inconclusive prompt (C-FATAL) | S2, S3 | todo   |
| S5 | build        | Cross-reference reconstruction pass: journal census + confirmed secondary-ID tagging       | S3, S2 | todo   |
| S6 | build        | (conditional) MB-backed cross-reference enrichment for evidence-gap cases                  | S5     | todo   |
| S7 | build        | Tag-read cache for maintenance passes keyed on (path, size, mtime)                         | S2     | todo   |
| S9 | build        | Library-wide dedup command: AcoustID-cluster census + survivor-choice deletion (C-DEDUP)   | S4     | todo   |
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

### S3 — cross-reference schema adjudication (adjudicated 2026-08-26)

All four agenda items ruled; vocabulary frozen into C-XREF/C-DEDUP above and registered as EPIST-9/EPIST-10 in the
STYLEGUIDE.  Outcomes: (1) tag `MUSICBRAINZ_SECONDARY_ALBUMID`, release-MBID-only `"; "`-joined scalar; (2) journal
actions `"cross-referenced"` (in-place, release_id = secondary MBID) and `"deduplicated"` (pop-source);
(3) per-group prompts; `match=None` is suffix-or-abort only (no xref without identity evidence); prompts survive
`--yes`; `--dry-run` reports only; (4) **deletion ruled in** (supersedes the mover-stays-put default and the
flag-for-later option): survivor-choice per group under C-DEDUP, keep-both as the non-fatal decline arm.  The operator
additionally extended dedup from collision-surfaced cases to the general library-wide case → S9 added.

### S4 — plan-time collision completeness (C-FATAL corollary)

Files: `src/music_annotator/_pipeline_maint.py` (repath collision resolution gains the `match=True` and `match=None`
arms; regroup/unify audited for the same gap), `src/music_annotator/_tagger.py` (cross-reference tag write), tests.

The `match=True` arm is the **shared group-resolution flow** (S9 reuses it): per duplicate group, prompt with member
files, releases, and evidence method (sha256 vs acoustid); operator chooses survivor / keep-both / abort.  Survivor
choice determines the plan: survivor = occupant → mover deleted, move dropped; survivor = mover → occupant deleted
first, then the move executes through the normal C-PROV chain into the vacated path.  Keep-both → survivor xref'd,
move dropped, and later runs detect the existing secondary MBID and drop silently (idempotency, no re-prompt).
C-DEDUP ordering throughout: xref write + verify + journal on the survivor before any deletion.

KATs: same-audio occupant → survivor-choice flow, all three arms (each deletion preceded by the survivor's
`"cross-referenced"` entry; `"deduplicated"` entries carry deleted-path/surviving-path/deleted-release-id);
keep-both re-run → silent drop, no prompt; inconclusive occupant → prompt suffix-or-abort, both arms; a plan that
reaches `_move_verify_journal` contains no move whose destination is occupied by a non-vacated path (property test
over generated plans); execution-time C-NOCLOBBER refusal still raises `RuntimeError` (unchanged, now bug-indicating);
`_resolve_current_lib` handles both new actions (in-place re-registration; pop-source).

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

### S9 — library-wide dedup command (C-DEDUP general case)

Files: `src/music_annotator/_pipeline_maint.py` (new maintenance command), `src/music_annotator/__main__.py`, tests.
Offline census over the live library: group files by embedded `ACOUSTID_ID` cluster (via the tag-read cache), with
`AUDIO_HASH` equality as the byte-identity fast path; files lacking both are out of scope (inconclusive — C-DEDUP
never deletes without identity evidence).  Aggregate contiguous per-recording pairs up to medium-level groups before
prompting (the observed duplication shape is whole mediums).  Each group runs the S4 group-resolution flow
(survivor / keep-both / abort).  The prompt must surface the structural consequence when duplication scatters across
releases: deleting a compilation's member guts the compilation's directory — the release becomes partially virtual,
represented only by secondary MBIDs on files in other albums' directories.  Keep-both is always available; dry-run
reports the full census without prompting.  KATs: cluster grouping (cache-driven, no audio opens on cache hits);
medium-level aggregation; all three resolution arms with C-DEDUP ordering; scatter-consequence surfaced in prompt
text; files without acoustid/hash never enter a group.

### S8 — operator acceptance gate

Full `repath` on hades end-to-end.  Acceptance criteria:

- Run completes without abort; zero execution-time C-NOCLOBBER refusals.
- Move-phase throughput reflects the journal fix (per-move time hashing-bound, not journal-bound).
- Same-audio collisions resolved per S3 rulings (survivor-choice deletion, keep-both, or abort — operator's call per
  group); inconclusive collisions prompted, not fatal.
- Journal is valid JSONL afterward; `.json` backup intact; rebuild/audit commands read it cleanly.
- Reconstruction pass run once over the historical evidence; secondary references verified on known duplicated mediums
  (the Greensleeves pair as the canonical KAT case).
- Library-wide dedup run once; every surviving duplicate group is either operator-kept (cross-referenced) or collapsed
  to one physical copy carrying its secondary MBIDs; deleted lineages intact in the journal.

On acceptance: continue the repair turn per ROADMAP (leaf renumberings, year-label normalisations, consolidations), and
rewrite this PLAN at the boundary.

## Notes for executors

- **Register rule** (repo AGENTS.md): durable files state the property/invariant, never the plan coordinate.  Anneal
  denylist for this sub-track: `\bS[1-9]\b` (session ids), `repair-turn hardening`, `sub-track`, `plan-run`,
  `boundary rewrite`.  Contract names (C-JRNL, C-XREF, C-DEDUP, C-FATAL, C-NOCLOBBER, C-SEQ, C-PROV, C-MOVE, C-CASE,
  SEL-*, NORM-*, REND-*, EPIST-*) are legitimate durable vocabulary.
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
| S2 | In-memory journal threading; retire per-move full rewrites                      | done   | 235313d | in-memory threading pattern frozen for S4/S7 |
| S3 | STYLEGUIDE: cross-reference tag schema + collision interaction design           | done   |        | EPIST-9/EPIST-10 registered; C-XREF adjudicated; C-DEDUP minted; deletion in scope; S9 added |
| S4 | Plan-time collision completeness in repath                                      | done   | 496ba85 | scope grew at S3: shared group-resolution flow + deletion arm; extras: __init__.py, models.py (TrackTags.musicbrainz_secondary_albumid) |
| S5 | Cross-reference reconstruction pass (journal census)                            | done   | 147cd40 | extra: __init__.py (export); evidence-gap reporting included |
| S6 | (conditional) MB-backed cross-reference enrichment                              | cancelled |     | census found 59 journal-provable cross-references, zero evidence gaps; all files entered via music-annotator |
| S7 | Tag-read cache for maintenance passes                                           | done   | 05319dc |       |
| S9 | Library-wide dedup command (AcoustID-cluster census + survivor-choice deletion) | done   | 0eaca73 | extra: __init__.py (export); scatter-consequence warning included |
| S8 | Resume repair turn on hades; acceptance gate                                    | done   |        | repath complete; dedup-library run; reconstruct-xrefs: 59 xrefs written; rebuild --dry-run: 43478 entries clean; .array-backup deleted |

Frozen contracts: C-JRNL, C-FATAL, C-XREF (frozen at derivation 2026-08-25; vocabulary adjudicated 2026-08-26),
C-DEDUP (frozen 2026-08-26); C-NOCLOBBER, C-SEQ, C-GUARD, NORM-2-as-revised, SEL-23, REND-27 inherited unchanged from
the previous sub-track.  S4 froze: group-resolution flow (resolve_duplicate_group), write_secondary_albumid_flac/mp3,
_resolve_current_lib "cross-referenced"/"deduplicated" arms, TrackTags.musicbrainz_secondary_albumid field.

## Action-frame digest

(append non-trivial discoveries, contract flexes, and notable texture here as sessions run)

- Derivation (2026-08-25): operator ruled run-fatal integrity posture over per-file skip (C-FATAL); chose cross-reference
  tagging over suffix/dedup for same-audio collisions; journal census confirmed both destructive-choice shapes are fully
  reconstructible offline (SKIP → `skipped` entries with loser release_id; OVERWRITE → dual `tagged` entries per
  destination).  SQLite/LMDB/CBOR evaluated and rejected for the journal: SQLite loses pyfakefs compatibility and
  greppability for query capability this workload doesn't need; JSONL + in-memory copy achieves O(1) appends with
  stdlib-only, test-compatible mechanics.
- S3 adjudication (2026-08-26): all four agenda items ruled (EPIST-9/EPIST-10; C-XREF vocabulary; C-DEDUP).  Two posture
  shifts beyond the agenda: **deletion ruled in** — the derivation's "deletion is out" default was operator-overturned
  as a bounded completionist exception (rationale preserved verbatim-in-spirit in EPIST-10: secondary MBIDs suffice;
  "completion is practically forbidden by nature"); and **dedup generalised** from collision-surfaced cases to a
  library-wide census command (S9).  Substrate finding that shaped the tag encoding: FLAC Vorbis comments are natively
  multi-valued and mutagen always yields lists, but the entire read path flattens to `v[0]`
  (`_read_tags_flac`, MP3 `frame.text[0]`) — a native multi-value write would be silently truncated by `_verify_copy`
  round-trip, so the `"; "`-joined scalar is a safety ruling, not merely convenience.  Checked before minting: neither
  CE (in-repo census) nor Picard carries a multi-release idiom; nearest shape is Picard's `musicbrainz_originalalbumid`
  (different semantics); release-group id does not cover cross-RG duplication.
