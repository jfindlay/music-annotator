<!-- Rolling action frame.  The previous sub-track (fragmentation adjudication from present state: route the albumid
     read-error cluster / C-RESOLVE) closed 2026-08-29 with ALL sessions done including the operator acceptance gate;
     its plan and ledger live in this file's git history (through commit bb0610f).  Acceptance evidence
     (`maintain.{h,i}.out`, hades, 2026-08-29): run 1 = zero read-error warnings, one aggregate unresolvable-history
     info event (count=24, vs predicted ≈32 — the resolver healed more chains than static analysis estimated, trending
     toward intent), regroup gate opens and noops, `inconclusive_acoustid=93`; run 2 = changed=0 (composite fixpoint
     holds).  This sub-track was derived 2026-08-29 from a discovery surfaced by the operator during that acceptance:
     five Vaughan Williams / Sir Neville Marriner tracks are persistent "evidence-gap candidates" — the surviving file
     carries an embedded MUSICBRAINZ_SECONDARY_ALBUMID but the journal has no "cross-referenced" entry proving it,
     because a dedup-pass write bug (since fixed) suppressed the journal writes when the originating duplicate files
     existed; those originating files are now gone.  The operator's 2026-08-29 ruling licenses a one-time truthful
     journal amendment (see below); the prior "no journal edits" ruling is narrowed, not abolished.  Rewritten at the
     next boundary. -->

# PLAN — one-time truthful amendment: journal the cross-references a fixed write-bug suppressed

## Why this sub-track exists

Five surviving library files (Vaughan Williams / Sir Neville Marriner tracks, hades, 2026-08-29) each carry an embedded
`MUSICBRAINZ_SECONDARY_ALBUMID` — a secondary release MBID proving the file absorbed a duplicate — yet the journal
holds **no `"cross-referenced"` entry** for that MBID at that file.  Every acceptance run therefore reports them as
persistent "evidence-gap candidates": the file's own tag asserts a cross-reference that the journal cannot corroborate.

The cause is a *past defect*, not the present code path.  A dedup-pass write bug (since fixed) suppressed the
`"cross-referenced"` journal writes at the moment the originating duplicate files existed.  The tag write to the
survivor landed; the journal append did not.  The originating files have since been deleted, so the census machinery
can no longer re-derive the secondary MBID from a sibling `"tagged"`/`"skipped"`/overwrite shape — the *only* surviving
witness to the cross-reference is the survivor file's own embedded tag.

The detection machinery already exists and already isolates exactly these files:

- `_census_journal_for_xrefs(journal)` (`_pipeline_maint.py:3090`) returns `(groups, evidence_gap_dests)`.  An
  evidence-gap dest is a destination with exactly one unique `"tagged"` release_id, no `"skipped"` sibling, no
  existing `"cross-referenced"` entry at the resolved current path — the journal alone cannot prove a secondary.
- `reconstruct_cross_references(...)` (`_pipeline_maint.py:3210`) already resolves each gap dest to its current path,
  reads the live file, and — when a non-empty embedded `MUSICBRAINZ_SECONDARY_ALBUMID` is present — appends it to
  `gap_paths` (lines 3306-3324).  Today these are **reported to the operator and returned, but never written to the
  journal**.  That report-only step is the whole gap.

The write helper also already exists.  `_write_xref_and_journal(survivor_path, secondary_mbid, ...)`
(`_pipeline_maint.py:596`) writes the secondary MBID into the tag (append-only set-union, a **verified no-op when the
value is already present**), reads it back to verify, then appends the `"cross-referenced"` journal entry — the full
C-PROV chain, in that order.  For the evidence-gap case the secondary is already embedded, so the tag write is a
verified no-op and the journal append is the truthful correction.

**The key insight that makes this simple and truthful:** data and taxonomical integrity are *already intact* — the
secondary MBID is in the file.  Reconstruction fabricates nothing; it writes the `"cross-referenced"` journal entry
that the surviving file's own embedded evidence proves should exist.  The journal is being made to agree with a fact
the library already durably records.

**Governing operator ruling (2026-08-29, this derivation):** "This is a one-time journal amendment.  The error
originated from bad music-annotator code; keep the solution simple.  The journal is useful history, but not inviolable
… The journal is a log of music-annotator more than an integrity check of the library itself.  music-annotator should
preserve data and taxonomical integrity, and once that's affirmed, the utility of the journal is secondary."  This
**narrows** — does not abolish — the prior frozen ruling ("fix in code only; do not edit or rewrite journal history").
The narrowing is adjudicated precisely in C-AMEND below.

## Cross-session contracts

New this sub-track:

- **C-AMEND** — a journal *amendment* is truthful and permitted; a journal *rewrite* is forbidden.  The distinguishing
  invariant, in property terms:

  > A journal write is a permitted **amendment** iff it (a) only **appends** entries (never edits, reorders, or deletes
  > an existing entry), (b) records an action the *current, correct* code would have journalled, and (c) is provably
  > derived from evidence that **already durably exists in the library** (here: the survivor file's own embedded
  > `MUSICBRAINZ_SECONDARY_ALBUMID`), such that the entry asserts nothing the library does not already record.  A write
  > that fails any of (a)–(c) — editing or deleting a real recorded action, reordering history, or asserting a
  > cross-reference no surviving evidence corroborates — is a forbidden **rewrite** and is out of scope forever.

  C-AMEND does **not** widen the journal to a rewritable record.  It licenses one narrow correction: writing the
  `"cross-referenced"` entry a *fixed* write-bug suppressed, sourced from surviving embedded evidence, still strictly
  append-only.  This is why it coexists with C-JRNL rather than contradicting it: the amendment is an APPEND (the
  journal grows; no entry is ever mutated or removed), so C-JRNL's append-only invariant is preserved in the letter.
  C-AMEND is what *licenses* an append whose subject is a past action rather than a present one — and bounds that
  license to the truthful, evidence-backed, bug-correcting case.

  Relation to the prior frozen ruling: the just-closed sub-track froze "do not edit or rewrite journal history (the
  journal is the provenance record — every resolver re-derives state from full history)."  C-AMEND narrows it: the
  journal must never be edited to FALSIFY or REWRITE a real recorded action, but appending the entry a bug PREVENTED —
  reconstructing what the correct code would have journalled, from evidence already embedded in the surviving file — is
  a truthful amendment, not a rewrite.  Why the operator ruling licenses this: the journal is a *log of
  music-annotator* subordinate to the library's data and taxonomical integrity; that integrity is already affirmed by
  the embedded tag, and the amendment merely makes the log agree with a fact the library already records.  The prior
  ruling's re-derivation guarantee is preserved: after the amendment, every resolver that walks full history now finds
  the `"cross-referenced"` entry and re-derives the *same* present state — including idempotent exclusion of the file
  from future gap reports.

Inherited frozen, unchanged: C-RESOLVE, C-CANON, C-NC-TOP, C-IDEM, C-GROUPSCOPE, C-MAINTAIN, C-CONFLUENCE, C-RETIRE,
INSTR, PERM, C-JRNL, C-FATAL, C-XREF, C-DEDUP, C-NOCLOBBER, C-SEQ, C-PROV, C-MOVE, NORM-2-as-revised, C-W3b-INT.
C-PROV/C-MOVE in particular: if the amendment path performs any tag write at all (even a set-union no-op), it must
not append the journal entry before that write's read-back verification passes — the intra-pass write→verify→journal
ordering is unchanged.  C-XREF in particular: the amendment's idempotency exclusion must compare on the same
current-path basis as the `"cross-referenced"` record (the census already resolves through the move chain before
checking `xref_by_dest`; the amendment inherits that).

## Sessions

Ordering rationale: the detection machinery, the write+verify helper, and the idempotency exclusion all already exist;
the missing wiring is one small step (journal the evidence-gap files instead of only reporting them).  This is a single
build session (S1).  Operator acceptance is folded into the operator's own verification run on hades rather than a
separate planned session — the acceptance is a single `reconstruct-xrefs` (or `maintain`) invocation with a trivially
checkable predicate (the five candidates gain journal-provable entries; a re-run is a noop), and the operator holds the
live library.  S1's KATs fully specify the acceptance predicate so the operator run needs no further design.

| ID | Type  | Deliverable (commit-title shape)                                                                       | Deps | Status |
|----|-------|--------------------------------------------------------------------------------------------------------|------|--------|
| S1 | build | Journal the cross-reference a fixed write-bug suppressed for evidence-gap survivors (C-AMEND)          | —    | todo   |

### S1 — one-time truthful amendment of suppressed cross-references (C-AMEND)

Files: `src/music_annotator/_pipeline_maint.py` (`reconstruct_cross_references`: for each evidence-gap file whose live
tag carries a non-empty `MUSICBRAINZ_SECONDARY_ALBUMID` with no journal-provable `"cross-referenced"` entry, write the
amending journal entry sourced from the embedded value — reuse `_write_xref_and_journal`, which for the already-embedded
case performs a verified set-union no-op tag write then appends the entry; gate the amendment behind the same
integrity-prompt discipline as the existing xref writes, and keep it within the same `dry_run` reporting path), tests.

Design guidance (executor latitude on mechanism, not on properties):

- **Reuse, do not rebuild.**  `_write_xref_and_journal` already performs write(set-union, no-op-if-present) → read-back
  verify → append `"cross-referenced"`.  For an evidence-gap file the secondary MBID is already embedded, so the tag
  write is a verified no-op and only the journal append is new.  Prefer routing the amendment through this existing
  helper so the C-PROV chain and the read-back verification are shared with the primary xref path.  Do not introduce a
  new action type, a tombstone, or a separate write path — the operator asked for simplicity and rejected new-action-type
  machinery in spirit.
- **Source the entry from the embedded tag, nothing else.**  The `secondary_mbid` for the amendment is the value read
  from the survivor's live `MUSICBRAINZ_SECONDARY_ALBUMID` (the same read that `gap_paths` already performs at
  `_pipeline_maint.py:3322`).  A file whose embedded secondary is empty is not amendable and stays a plain report line —
  never invent a value.  If a single file's tag carries multiple secondary MBIDs (set-union of "; "-joined values),
  each distinct embedded value gets its own truthful entry.
- **Idempotency is inherited, verify it holds.**  Once a `"cross-referenced"` entry exists at the resolved current
  path, `_census_journal_for_xrefs` excludes the file from `evidence_gap_dests` (`_pipeline_maint.py:3200-3202`,
  `current_dest in xref_by_dest`).  The amendment must therefore be a strict noop on re-run: no new entry, the gap list
  empties and stays empty.  Confirm the exclusion compares on the same current-path basis the amendment writes on.
- **Prompt discipline.**  The amendment is an integrity mutation; surface it under the same operator-confirmation
  discipline as the existing xref writes (the prompt survives `--yes`).  `--dry-run` reports the amendable files and
  writes nothing.  Whether the amendable-gap files are folded into the existing "secondary MBIDs to write" prompt block
  or shown as a distinct "amend suppressed cross-references" block is executor latitude, provided the operator sees the
  count and can decline.

KATs:

1. **Suppressed entry reconstructed** — a survivor whose live tag carries a non-empty `MUSICBRAINZ_SECONDARY_ALBUMID`
   and has no journal-provable `"cross-referenced"` entry at its resolved current path gains exactly one truthful
   `"cross-referenced"` entry whose `release_id` equals the embedded secondary MBID and whose
   `source == destination == current path`.  The entry is an APPEND; no existing entry is edited or removed.
2. **Idempotent noop on re-run** — running the amendment a second time over the just-amended journal produces zero new
   entries; the evidence-gap list for that file is empty.
3. **Already-provable file untouched** — a survivor whose secondary MBID is already recorded by a `"cross-referenced"`
   entry (at the resolved current path) receives no new entry and is not reported as amendable.
4. **Empty embedded evidence is not amendable** — a survivor with no embedded `MUSICBRAINZ_SECONDARY_ALBUMID` (or an
   empty/whitespace value) receives no amending entry; it may still appear as a plain report line but the journal is
   not mutated on its behalf (C-AMEND clause (c): no entry without surviving evidence).
5. **C-PROV/C-MOVE preserved** — if the amendment path performs any tag write (including the set-union no-op), the
   `"cross-referenced"` journal entry is appended only after the read-back verification passes; a verification failure
   raises and appends no entry.

Not built: any new journal action type, tombstones, or in-place edit/delete of existing entries (forbidden by C-AMEND
clause (a) and by C-JRNL); any amendment sourced from anything other than surviving embedded evidence (forbidden by
C-AMEND clause (c)); network re-resolution of the secondary MBID (out of scope — the evidence is already local);
flattening or compacting the journal against the current codebase (the operator named this as a *possible* fallback for
a full-library rebuild, explicitly not the chosen simple path here).

## Notes for executors

- **Register rule** (repo AGENTS.md): durable files state the property/invariant, never the plan coordinate.  Anneal
  denylist for this sub-track: `\bS1\b` (session id), `sub-track`, `plan-run`, `boundary rewrite`,
  `juncture`/`inflection`/`action-frame` (plan-run command vocabulary), `maintain\.[a-i]\.out` (evidence filenames —
  in durable prose state the invariant instead: "a `\"cross-referenced\"` journal entry is appended for a survivor whose
  embedded secondary MBID has no journal-provable record, sourced from that embedded value and strictly append-only"),
  `evidence-gap candidate` and `write-bug` (derivation shorthand — durable prose says "a survivor carrying an embedded
  secondary MBID with no corroborating `\"cross-referenced\"` entry").  Contract names (C-AMEND, C-RESOLVE, C-JRNL,
  C-XREF, C-DEDUP, C-PROV, C-MOVE, C-CANON, C-NC-TOP, C-IDEM, C-GROUPSCOPE, C-MAINTAIN, C-CONFLUENCE, INSTR, PERM,
  C-W3b-INT, NORM-*, REND-*, EPIST-*) are legitimate durable vocabulary.
- Full gate before declaring the session done: `~/.local/bin/tox -m analyze` (100% branch coverage, mypy strict,
  pylint 10.00/10, ruff, pyupgrade).
- Patch targets bind where the name is imported, not where it originates (repo testing convention).
- The amendment is an APPEND, not an in-place edit: the journal only ever grows.  C-JRNL is preserved in the letter;
  C-AMEND is what licenses an append whose subject is a past (bug-suppressed) action, and bounds it to the truthful,
  evidence-backed case.  Never edit, reorder, or delete an existing entry — that is the forbidden rewrite.
- The secondary MBID is already embedded in the survivor.  Data and taxonomical integrity are already intact; the
  amendment makes the log agree with a fact the library already records.  Never synthesize a secondary MBID from any
  source other than the file's own embedded tag.
- Evidence base: the operator's acceptance runs on hades (2026-08-29) surfaced five Vaughan Williams / Marriner
  survivors as persistent evidence-gap candidates; the detection (`_census_journal_for_xrefs` → `evidence_gap_dests`)
  and the current-path resolution + embedded-tag read (`reconstruct_cross_references`, the `gap_paths` loop) already
  isolate exactly these files.  The suppressing defect (a dedup-pass write bug) is already fixed; this session corrects
  only the historical gap it left.

## Progress ledger

VERIFY: `~/.local/bin/tox -m analyze` (combined gate: tests + 100% branch coverage + mypy strict + pylint 10.00 +
ruff + pyupgrade).  One green run satisfies tests, types, lint, format, and coverage.

| ID | Title                                                                          | Status | Commit | Notes |
|----|--------------------------------------------------------------------------------|--------|--------|-------|
| S1 | Journal the cross-reference a fixed write-bug suppressed for evidence-gap survivors (C-AMEND) | done   | be68c38 | All 5 KATs pass (suppressed-entry reconstructed, idempotent noop, already-provable untouched, empty-evidence not amendable, C-PROV/C-MOVE preserved) plus multi-MBID / declined / dry-run / MP3 coverage. Amendment appends via existing `_write_xref_and_journal`; strictly append-only, sourced solely from embedded secondary MBID. Awaiting operator acceptance run on hades (`reconstruct-xrefs`/`maintain`): five candidates gain journal-provable entries, gap list empties, re-run is a noop. |

Frozen contracts: C-AMEND (frozen at this derivation, operator ruling 2026-08-29: a one-time truthful amendment —
append-only, sourced from surviving embedded evidence, recording what the current correct code would have journalled —
is permitted and narrows the prior "no journal edits" ruling; a rewrite that edits, reorders, deletes, or falsifies a
real recorded action remains forbidden).  C-RESOLVE (frozen prior derivation: code-only healing of present-state
reads, no tombstones).  C-CANON, C-NC-TOP, C-IDEM, C-GROUPSCOPE, C-MAINTAIN, C-CONFLUENCE, C-RETIRE, INSTR, PERM,
C-JRNL, C-FATAL, C-XREF, C-DEDUP, C-NOCLOBBER, C-SEQ, C-PROV, C-MOVE, NORM-2-as-revised, C-W3b-INT inherited unchanged.

## Action-frame digest

(append non-trivial discoveries, contract flexes, and notable texture here as sessions run)

- Derivation (2026-08-28, prior sub-track): prior acceptance gate passed on live evidence (run 1 = predicted one-time
  un-scatter with the tripwire firing only against prior-run inverses; run 2 = changed=0).  The residual log surface
  decomposed into three classes: stale-evidence misadjudication in the confirmation gate (defect), a summary counter
  that disagrees with its own event stream (defect), and missing AcoustIDs unresolvable without a keyed lookup (by
  design).  Durable lesson (CAPTURE-CANDIDATE, chat 2026-08-28): **an append-only journal makes every raw
  historical-path dereference a compounding hazard** — the consumer answers a present-state question with then-evidence,
  the noise grows monotonically with every subsequent move, and the misadjudication is silent (false STALE reads as
  "nothing to do").  The move-chain resolver existed and was already used by one consumer (xref census); the newer
  consumer didn't use it.  Second lesson: the resolver itself had a gap (dedup-terminal entries unhandled) that only
  surfaced when a second consumer's evidence was quantified — a resolver used by one caller is a resolver with untested
  semantics.
- Operator rulings at the prior derivation: journal history is never edited (provenance record; all resolvers
  re-derive from full history); no tombstone action type at a 32-path dead-end population (mechanism cost exceeds
  re-derivation cost); confirmation-gate criterion (release spans >1 current work_dir) stays broad — it is a pre-filter
  feeding a nooping recompute, and narrowing it belongs to BACKLOG only with first-run cost measurements in hand.

### S3 acceptance — 2026-08-29 (prior sub-track)
Discovery/flex: acceptance PASSED on hades (`maintain.{h,i}.out`) — zero read-error warnings, one aggregate
  `fragmentation_unresolvable_history count=24` (predicted ≈32; resolver healed more chains than static analysis
  estimated), regroup gate noops, `inconclusive_acoustid=93`, run 2 `changed=0`.  NEW discovery surfaced by operator:
  five Vaughan Williams / Marriner tracks are persistent "evidence-gap candidates" (secondary MBID embedded but not
  journal-provable) because a *dedup-pass write bug* (since fixed) suppressed the journal writes when their originating
  files existed; those files are now gone.  Operator asks whether retroactive journal entries can reconstruct that lost
  provenance.
Affected: C-JRNL (append-only, no-history-edit) and the prior derivation's frozen "no journal-history edits" ruling —
  a retroactive-entry feature is a direct tension with both and must be adjudicated, not folded in.
Deferred: yes — this was a boundary-rewrite trigger, NOT in-scope for the just-closed sub-track.
Texture: the deviation count 24-vs-32 is benign (trends toward intent — more healing, not less); it is not drift and
  did not reopen the fragmentation fix.  The evidence-gap candidates are a *different defect class* (a past write-path
  bug) than the prior sub-track's read-resolution fix — they were correctly invisible to that scope.

### Derivation — 2026-08-29 (this sub-track)
Adjudication of the relaxation: the prior frozen "no journal edits" ruling is **narrowed, not abolished**.  The
  operator's 2026-08-29 ruling establishes that the journal is a *log of music-annotator* subordinate to the library's
  data and taxonomical integrity; once that integrity is affirmed (here: the secondary MBID is already embedded in the
  survivor), making the log agree with a fact the library already records is a truthful amendment.  Named crisply as
  **C-AMEND**: an APPEND recording what the current correct code would have journalled, sourced from surviving embedded
  evidence, is permitted; any edit/reorder/delete/falsification of a real recorded action remains a forbidden rewrite.
  C-AMEND coexists with C-JRNL because the amendment is strictly append-only (the journal grows; no entry is mutated);
  it *licenses* an append whose subject is a past bug-suppressed action and bounds that license to the evidence-backed
  case.  The prior ruling's re-derivation guarantee is preserved — after the amendment, every full-history resolver
  finds the entry and re-derives the same present state, including idempotent exclusion from future gap reports.
Reconciliation confidence: HIGH.  Confirmed by direct code read — the detection (`_census_journal_for_xrefs` →
  `evidence_gap_dests`, `_pipeline_maint.py:3090-3207`), the current-path resolution + embedded-tag read
  (`reconstruct_cross_references` `gap_paths` loop, `:3306-3324`), and the write+verify+journal helper
  (`_write_xref_and_journal`, `:596-660`) all already exist; the only missing wiring is journalling the gap files
  instead of only reporting them, and the idempotency exclusion (`:3200-3202`) already guarantees a noop re-run.  The
  amendment sources the entry solely from the survivor's own embedded tag, so it fabricates no provenance.  Load-bearing
  assumption (named per judgment discipline): this design assumes every one of the five candidates carries a non-empty
  embedded secondary MBID — the operator's discovery states they do, and the `gap_paths` predicate already filters on
  exactly `existing_secondary_raw.strip()` (`:3323`), so a file with empty embedded evidence would simply not be
  amended (KAT-4).  If a candidate's embedded value were empty, it is correctly not amendable rather than a defect.
  Tradeoff named: reusing `_write_xref_and_journal` performs a set-union tag write (a verified no-op when the value is
  already present) rather than a journal-only append; this is marginally more work than a pure append but keeps a single
  C-PROV write path and its read-back verification shared with the primary xref case — chosen for simplicity and one
  verification surface over a second journal-only code path.

### S1 — 2026-08-29 (this sub-track)
Discovery/flex: none — the design held exactly as adjudicated.  The amendment reused `_write_xref_and_journal`
  unchanged; the tag write is a verified set-union no-op for the already-embedded case and only the `"cross-referenced"`
  append is new.  All 5 KATs plus multi-MBID / declined-prompt / dry-run / MP3 coverage pass; full gate green (2040
  tests, 100% branch, mypy/pylint/ruff/pyupgrade clean).
Affected: C-AMEND (implemented as specified; append-only, sourced solely from embedded evidence, idempotent via the
  existing census exclusion at the resolved current path).  No inherited contract flexed.
Deferred: yes — operator acceptance on hades is the remaining gate (folded into the operator's own `reconstruct-xrefs`
  or `maintain` run by design, not a separate planned session).  Acceptance predicate: the five survivors gain
  journal-provable entries, the evidence-gap report empties, and a re-run appends nothing.  On acceptance, rewrite this
  PLAN at the boundary.
Texture: implementation session was interrupted mid-run; recovery was by re-deriving state from disk (git status +
  full gate + KAT presence grep) rather than trusting a returned summary — the "state lives on disk" invariant carried
  the recovery cleanly, the tree was green and complete on inspection.
